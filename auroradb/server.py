"""Local SQL workbench HTTP transport; the engine remains the only data layer."""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .ai import AIError, AIRateLimitError, AIService, MAX_PROMPT_CHARS, MAX_SQL_CHARS
from .sql import parse
from .semantic import SemanticExecutor
from .optimizer_demo import run_demo

MAX_REQUEST_BYTES = 512 * 1024
MAX_JSON_DEPTH = 64
REQUEST_FIELDS = {
    '/api/query': {'sql'}, '/api/compare': {'sql'}, '/api/integrity': set(),
    '/api/ai': {'action', 'prompt', 'sql'},
    '/api/research/analyze': set(), '/api/research/optimizer': {'mode'},
    '/api/research/buffer': {'policy', 'sample_size'},
    '/api/research/optimize': {'sql', 'candidate_sql', 'use_ai'},
    '/api/research/semantic': {'sql', 'column', 'prompt', 'operation', 'batch_size',
                               'max_rows', 'max_calls', 'budget_seconds', 'use_cache'},
    '/api/research/semantic/cache': set(),
    '/api/research/join-demo': set(),
}


def request_object(raw: bytes) -> dict:
    """Reject ambiguous/nonstandard JSON before any database operation."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('JSON 不允许重复字段。')
            value[key] = item
        return value

    def constant(_):
        raise ValueError('JSON 不允许 NaN 或 Infinity。')

    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('请求必须是 JSON 对象。')
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError('JSON 嵌套层级过深。')
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    # Also reject finite-token overflow (1e9999) and escaped lone surrogates.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return value


def resource_directory() -> Path:
    """Resolve bundled assets both in source and in a PyInstaller executable."""
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "auroradb"
    return Path(__file__).resolve().parent


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, database, port: int = 8765, ai_service=None):
        self.database = database
        self.database_lock = threading.RLock()
        self.ai = ai_service if ai_service is not None else AIService.from_env()
        self.ai_gate = threading.BoundedSemaphore(2)
        self.demo_gate = threading.BoundedSemaphore(1)
        self.semantic = SemanticExecutor(self.ai)
        self.token = secrets.token_urlsafe(32)
        self.assets = resource_directory() / "web"
        if not (self.assets / "index.html").is_file():
            raise FileNotFoundError(f"找不到工作台资源：{self.assets}")
        super().__init__(("127.0.0.1", port), WorkbenchHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # SQL and request bodies never enter the HTTP access log.
        return

    def _respond(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json(self, status: int, value):
        self._respond(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

    def _valid_host(self) -> bool:
        return self.headers.get("Host", "") in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }

    def _validate_request(self, write: bool = False) -> bool:
        # Restrict Host to prevent DNS rebinding, and refuse cross-origin requests.
        if not self._valid_host():
            self.close_connection = True
            self._json(403, {"error": "只允许通过本机工作台地址访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers['Host']}":
            self.close_connection = True
            self._json(403, {"error": "拒绝跨来源请求。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.close_connection = True
            self._json(403, {"error": "拒绝跨站点请求。"})
            return False
        if write and not secrets.compare_digest(self.headers.get("X-Aurora-Token", "").encode("utf-8"), self.server.token.encode("ascii")):
            self.close_connection = True
            self._json(403, {"error": "工作台会话已失效，请刷新页面。"})
            return False
        return True

    def do_GET(self):
        if not self._validate_request():
            return
        path = self._request_path()
        if path is None:
            return
        if path == "/api/session":
            self._json(200, {"token": self.server.token})
            return
        if path == "/api/ai/status":
            self._json(200, self.server.ai.status())
            return
        if path == "/api/research/status":
            try:
                with self.server.database_lock:
                    value={'optimizer':self.server.database.research_status(),
                           'buffer':self.server.database.pager.cache_policy_status(),
                           'semantic':self.server.semantic.status(),'ai':self.server.ai.status()}
                self._json(200,value)
            except Exception as exc: self._json(500,{'error':str(exc)})
            return
        if path in {"/api/schema", "/api/stats"}:
            try:
                with self.server.database_lock:
                    value = self.server.database.schema() if path == "/api/schema" else self.server.database.stats()
                self._json(200, {"tables": value} if path == "/api/schema" else value)
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
        }
        if path not in assets:
            self._json(404, {"error": "资源不存在。"})
            return
        filename, content_type = assets[path]
        try:
            self._respond(200, (self.server.assets / filename).read_bytes(), content_type)
        except OSError:
            self._json(404, {"error": "工作台资源不存在。"})

    def do_POST(self):
        if not self._validate_request(write=True):
            return
        path = self._request_path()
        if path is None:
            return
        research_routes={"/api/research/analyze","/api/research/optimizer","/api/research/buffer",
                         "/api/research/optimize","/api/research/semantic","/api/research/semantic/cache",
                         "/api/research/join-demo"}
        if path not in {"/api/query", "/api/integrity", "/api/compare", "/api/ai"}|research_routes:
            self.close_connection = True
            self._json(404, {"error": "接口不存在。"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._json(400, {"error": "不支持分块请求体。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._json(413 if length > MAX_REQUEST_BYTES else 411, {"error": "SQL 请求必须包含有效长度，且不能超过 512 KiB。"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.close_connection = True
            self._json(415, {"error": "请求格式必须为 application/json。"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求体不完整。")
            payload = request_object(raw)
            if set(payload) - REQUEST_FIELDS[path]:
                raise ValueError('接口包含未知参数。')
            sql = payload.get("sql")
            if path in {"/api/query", "/api/compare"} and (not isinstance(sql, str) or not sql.strip()):
                raise ValueError("请输入 SQL 语句。")
        except (ValueError, UnicodeError, RecursionError) as exc:
            self._json(400, {"error": f"无效请求：{exc}"})
            return
        try:
            if path in research_routes:
                self._research(path,payload)
                return
            if path == "/api/ai":
                self._assist(payload)
                return
            # One engine connection also means one explicit transaction session.
            with self.server.database_lock:
                if path == "/api/query":
                    value = {"results": self.server.database.execute(sql)}
                elif path == "/api/integrity":
                    value = self.server.database.integrity_check()
                else:
                    value = self.server.database.compare_query(sql)
            self._json(200, value)
        except Exception as exc:
            self._json(400, {"error": str(exc)})

    def _request_path(self):
        try:
            return urlsplit(self.path).path
        except ValueError:
            self.close_connection = True
            self._json(400, {'error': '无效请求地址。'})
            return None

    def _research(self,path,payload):
        """Explicit experiments. Remote inference never holds the database lock."""
        if path == '/api/research/join-demo':
            if not self.server.demo_gate.acquire(blocking=False):
                self._json(429, {'error': 'PPT 用例正在运行，请稍后重试。'})
                return
            try:
                self._json(200, run_demo())
            except Exception as exc:
                self._json(400, {'error': str(exc)})
            finally:
                self.server.demo_gate.release()
            return
        network=path in {'/api/research/optimize','/api/research/semantic'}
        if network and not self.server.ai_gate.acquire(blocking=False):
            self._json(429,{'error':'AI 或语义实验正在处理中，请稍后重试。'})
            return
        try:
            db=self.server.database
            if path=='/api/research/semantic':
                allowed={'sql','column','prompt','operation','batch_size','max_rows','max_calls','budget_seconds','use_cache'}
                if set(payload)-allowed: raise ValueError('语义实验含未知参数。')
                value=self.server.semantic.run(db,**payload)
            elif path=='/api/research/semantic/cache':
                value=self.server.semantic.clear()
            elif path=='/api/research/optimize':
                sql=payload.get('sql')
                candidate=payload.get('candidate_sql')
                use_ai=payload.get('use_ai',False)
                if (not isinstance(sql,str) or not sql.strip() or len(sql)>MAX_SQL_CHARS
                        or type(use_ai) is not bool
                        or candidate is not None and (not isinstance(candidate,str) or len(candidate)>MAX_SQL_CHARS)):
                    raise ValueError('请提供一个 SELECT 与有效的候选 SQL、AI 开关。')
                candidate=candidate if candidate and candidate.strip() else None
                if use_ai and candidate: raise ValueError('手动候选与 AI 候选请选择一种。')
                started=time.perf_counter()
                model_ms=0
                model_calls=0
                advice=None
                if use_ai:
                    with self.server.database_lock:
                        if db.active:
                            raise ValueError('请先提交或回滚活动事务，再运行可信优化实验。')
                        # Reject unsupported comparisons before spending inference.
                        statement=db._diagnostic_select(sql)
                        if statement.get('limit') is not None or statement.get('offset'):
                            raise ValueError('可信优化实验暂不支持 LIMIT/OFFSET。')
                        # School API receives schema/plan, never result rows here.
                        preview=db.preview_query(sql)
                        schema=[{'name':t['name'],'row_count':t['row_count'],
                                 'columns':[{k:c[k] for k in ('name','type','primary','unique','not_null') if k in c}
                                            for c in t['columns']]} for t in db.schema()]
                    model_start=time.perf_counter()
                    advice=self.server.ai.assist('optimize',sql=sql,context={'schema':schema,'plan':preview['plan']})
                    model_ms=(time.perf_counter()-model_start)*1000
                    model_calls=int(self.server.ai.mode=='remote')
                    candidate=advice.get('sql') or None
                    if candidate is None: raise AIError('模型没有提供可比较的 SELECT 候选。')
                with self.server.database_lock: value=db.optimize_query(sql,candidate_sql=candidate)
                timings=value.setdefault('timings',{})
                timings['optimizer_ms']=timings.get('total_ms',0)
                timings['model_ms']=round(model_ms,3)
                timings['total_ms']=round((time.perf_counter()-started)*1000,3)
                value['model_calls']=model_calls
                value['model_mode']=self.server.ai.mode if use_ai else 'local-rules'
                if advice: value['model_explanation']=advice['explanation']
            else:
                with self.server.database_lock:
                    if path=='/api/research/analyze': value=db.analyze_statistics()
                    elif path=='/api/research/optimizer': value=db.set_optimizer_mode(payload.get('mode'))
                    elif path=='/api/research/buffer':
                        value=db.set_buffer_policy(payload.get('policy'),payload.get('sample_size',8))
                    else: raise ValueError('未知研究接口。')
            self._json(200,value)
        except AIRateLimitError as exc:
            self._json(429,{'error':str(exc),'retry_after_seconds':exc.retry_after_seconds})
        except AIError as exc:
            self._json(502 if self.server.ai.mode=='remote' else 400,{'error':str(exc)})
        except Exception as exc: self._json(400,{'error':str(exc)})
        finally:
            if network: self.server.ai_gate.release()

    def _suggestion_validation(self, sql):
        """Only compile a SELECT; a failed suggestion must not roll back user work."""
        if not isinstance(sql, str) or not sql.strip():
            return {"valid":False, "read_only":False, "kind":"none", "error":"没有待执行 SQL。"}
        kind = 'unknown'
        try:
            statements = parse(sql)
            kind = statements[0]['kind'] if len(statements) == 1 else 'script'
            value = self.server.database.preview_query(sql)
            value['scope'] = '语法、名称绑定和计划校验；未执行查询。'
            return value
        except Exception as exc:
            return {"valid":False, "read_only":False, "kind":kind, "error":str(exc)}

    def _assist(self, payload):
        action = payload.get('action')
        prompt, sql = payload.get('prompt', ''), payload.get('sql', '')
        if action not in {'generate','explain','optimize'} or not isinstance(prompt,str) or not isinstance(sql,str):
            self._json(400, {'error':'请选择生成、解释或优化，并提供文本输入。'})
            return
        if len(prompt)>MAX_PROMPT_CHARS or len(sql)>MAX_SQL_CHARS:
            self._json(400, {'error':f'AI 问题不能超过 {MAX_PROMPT_CHARS} 字符，SQL 不能超过 {MAX_SQL_CHARS} 字符。'})
            return
        if not self.server.ai_gate.acquire(blocking=False):
            self._json(429, {'error':'AI 请求正在处理中，请稍后重试。'})
            return
        try:
            with self.server.database_lock:
                db = self.server.database
                schema = [{'name':table['name'], 'row_count':table['row_count'],
                           'columns':[{key:column[key] for key in ('name','type','primary','unique','not_null')
                                       if key in column} for column in table['columns']],
                           'indexes':[{key:index[key] for key in ('name','column','unique') if key in index}
                                      for index in table['indexes']]} for table in db.schema()]
                validation = self._suggestion_validation(sql) if sql.strip() else None
                context = {'schema':schema, 'plan':validation.get('plan') if validation else None}
            # The network request never holds the database lock and receives no rows.
            answer = self.server.ai.assist(action, prompt=prompt, sql=sql, context=context)
            with self.server.database_lock:
                answer['validation'] = self._suggestion_validation(answer.get('sql', ''))
            answer['context_policy'] = '发送问题、SQL、表结构和编译计划；不发送查询结果行。'
            self._json(200, answer)
        except AIRateLimitError as exc:
            self._json(429,{'error':str(exc),'retry_after_seconds':exc.retry_after_seconds})
        except AIError as exc:
            self._json(502 if self.server.ai.status()['mode']=='remote' else 400, {'error':str(exc)})
        finally:
            self.server.ai_gate.release()

    def do_OPTIONS(self):
        self.close_connection = True
        self._json(403, {"error": "不允许跨来源访问。"})


def serve(database, port: int = 8765, open_browser: bool = True, ai_service=None):
    """Serve until Ctrl+C. The caller owns the Database lifetime."""
    import webbrowser

    with WorkbenchServer(database, port, ai_service=ai_service) as server:
        print(f"AuroraDB SQL 工作台：{server.url}",flush=True)
        print("按 Ctrl+C 停止服务。所有浏览器标签页共享一个事务会话。",flush=True)
        if open_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\n工作台已停止。")
