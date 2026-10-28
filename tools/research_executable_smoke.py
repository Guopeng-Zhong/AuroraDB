"""Exercise the shipped research features through a separate EXE and HTTP.

The harness uses only a disposable database and an explicit local mock config.
It never imports AuroraDB or contacts an external model. Run after the final
dist/AuroraDB.exe build has been frozen; the evidence identifies that binary.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "AuroraDB.exe"
EVIDENCE = ROOT / "docs" / "research_executable_smoke.json"


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise AssertionError("The local workbench must not redirect smoke requests")


class ExeWorkbench:
    def __init__(self, folder, config):
        self.folder, self.config = folder, config
        self.process = None
        self.reader = None
        self.lines = queue.Queue()
        self.logs = []
        self.base = None
        self.token = None
        # Ignore ambient HTTP proxy and AI configuration. Every request is to
        # the EXE's actual ephemeral loopback port; mock cannot call a model.
        self.opener = build_opener(ProxyHandler({}), NoRedirects())
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.upper().startswith("AURORA_AI_")}
        self.environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")

    def launch(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [str(EXE), "--db", str(self.folder), "--demo", "--web", "--port", "0",
             "--no-browser", "--ai-config", str(self.config)],
            cwd=tempfile.gettempdir(), env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags, start_new_session=os.name != "nt")

        def read_output():
            for line in self.process.stdout:
                self.logs.append(line.rstrip())
                self.lines.put(line)

        self.reader = threading.Thread(target=read_output, daemon=True)
        self.reader.start()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.reader.join(timeout=1)
                raise AssertionError(f"EXE exited during startup: {self.logs[-15:]}")
            try:
                line = self.lines.get(timeout=.2)
            except queue.Empty:
                continue
            found = re.search(r"http://127\.0\.0\.1:(\d+)/?", line)
            if found:
                port = int(found.group(1))
                assert 1 <= port <= 65535, f"Invalid actual server port: {port}"
                self.base = f"http://127.0.0.1:{port}"
                status, value = self.request("GET", "/api/session")
                assert status == 200 and isinstance(value.get("token"), str), value
                self.token = value["token"]
                return {"port": port, "bind": "127.0.0.1", "port_requested": 0,
                        "demo": True, "browser_opened": False, "model_mode": "mock"}
        raise AssertionError(f"EXE did not announce a port within 40 seconds: {self.logs[-15:]}")

    def request(self, method, route, payload=None, *, token=True, origin=None, timeout=12):
        assert self.base and self.base.startswith("http://127.0.0.1:"), "Loopback URL required"
        headers = {"Accept": "application/json", "Connection": "close"}
        if token and self.token:
            headers["X-Aurora-Token"] = self.token
        if origin is not None:
            headers["Origin"] = origin
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.base + route, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(4 * 1024 * 1024 + 1)
            assert len(raw) <= 4 * 1024 * 1024, "Unexpectedly large smoke response"
            status = response.status
            if route in {"/", "/app.js", "/styles.css"}:
                return status, raw.decode("utf-8")
            return status, json.loads(raw.decode("utf-8"))

    def get(self, route):
        status, value = self.request("GET", route)
        assert status == 200, (route, status, value)
        return value

    def post(self, route, payload, *, expected=200):
        status, value = self.request("POST", route, payload)
        assert status == expected, (route, status, value)
        return value

    def query(self, sql):
        return self.post("/api/query", {"sql": sql})["results"]

    def stop(self):
        if not self.process:
            return
        # Only terminate this harness's owned process tree. PyInstaller one-file
        # mode has a bootloader parent and a DB-owning child on Windows.
        if self.process.poll() is None:
            if os.name == "nt":
                stopped = subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    capture_output=True, timeout=15)
                assert stopped.returncode == 0, stopped.stderr.decode("utf-8", errors="replace")
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                self.process.kill()
            else:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        if self.reader:
            self.reader.join(timeout=3)
            assert not self.reader.is_alive(), "Owned EXE child did not close its output pipe"
        if self.process.stdout:
            self.process.stdout.close()


def main():
    checks = []
    evidence = {
        "format_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"platform": platform.platform(), "driver_python": sys.version.split()[0]},
        "passed": False, "checks": checks,
        "scope": "Final packaged EXE, actual loopback HTTP, disposable seeded DB, explicit local keyword mock only.",
        "limitations": ["No campus or external model endpoint contacted.",
                        "HTTP checks do not substitute for real browser layout QA.",
                        "Performance recommendations are observed, never forced on the tiny demo data."]}
    started = time.perf_counter()

    def check(name, action):
        beginning = time.perf_counter()
        try:
            details = action()
        except Exception as error:
            checks.append({"name": name, "passed": False,
                           "elapsed_ms": round((time.perf_counter() - beginning) * 1000, 3),
                           "error": f"{type(error).__name__}: {error}"})
            raise
        checks.append({"name": name, "passed": True,
                       "elapsed_ms": round((time.perf_counter() - beginning) * 1000, 3),
                       "details": details or {}})

    try:
        assert EXE.is_file(), f"Final EXE is missing: {EXE}"
        digest = hashlib.sha256(EXE.read_bytes()).hexdigest()
        evidence.update(exe_sha256=digest, exe_bytes=EXE.stat().st_size)
        with tempfile.TemporaryDirectory(prefix="AuroraDB research exe ") as temporary:
            folder = Path(temporary) / "database"
            config = Path(temporary) / "ai.local.json"
            config.write_text(json.dumps({"mode": "mock"}), encoding="utf-8")
            server = ExeWorkbench(folder, config)
            try:
                check("packaged EXE startup with ephemeral loopback port and local mock", server.launch)

                def assets():
                    status, markup = server.request("GET", "/")
                    assert status == 200 and "tool-tab-research" in markup and "研究实验" in markup
                    status, javascript = server.request("GET", "/app.js")
                    assert status == 200 and "/api/research/semantic" in javascript and "recommendation.replace" in javascript
                    status, stylesheet = server.request("GET", "/styles.css")
                    assert status == 200 and "color-scheme:light" in stylesheet and "research-section" in stylesheet
                    return {"html_characters": len(markup), "javascript_characters": len(javascript),
                            "stylesheet_characters": len(stylesheet), "theme": "light"}
                check("bundled research HTML, JavaScript and readable light styles", assets)

                def initial_status():
                    value = server.get("/api/research/status")
                    assert value["ai"]["mode"] == "mock" and value["ai"]["available"] is True
                    assert value["optimizer"]["mode"] == "heuristic"
                    assert value["buffer"]["policy"] == "lru"
                    assert set(value["optimizer"]["learning"]["cost_models"]) == {"TableScan", "IndexScan"}
                    assert value["semantic"]["operations"] == ["filter", "classify", "aggregate"]
                    assert value["semantic"]["entries"] == 0
                    assert "api_key" not in json.dumps(value) and "base_url" not in json.dumps(value)
                    return value
                check("research status contracts and explicit nonremote mock mode", initial_status)

                def csrf():
                    status, missing = server.request("POST", "/api/research/analyze", {}, token=False)
                    assert status == 403 and "error" in missing
                    status, cross = server.request("POST", "/api/research/analyze", {}, origin="https://example.invalid")
                    assert status == 403 and "error" in cross
                    assert server.get("/api/research/status")["optimizer"]["statistics"]["available"] is False
                    return {"missing_token_status": 403, "cross_origin_status": 403,
                            "statistics_unchanged": True}
                check("CSRF and cross-origin rejection before research mutations", csrf)

                def typed_sets_and_join():
                    results = server.query("BEGIN; CREATE TABLE review_types(id INTEGER,b BOOLEAN,n INTEGER); "
                        "INSERT INTO review_types VALUES (1,TRUE,NULL),(2,NULL,1),(3,FALSE,NULL),(4,NULL,0); "
                        "SELECT DISTINCT COALESCE(b,n) AS v FROM review_types; "
                        "SELECT COALESCE(b,n) AS v,COUNT(*) FROM review_types GROUP BY COALESCE(b,n); "
                        "SELECT l.id,r.id FROM review_types l JOIN review_types r ON (l.id=r.id)=TRUE ORDER BY l.id; "
                        "ROLLBACK;")
                    distinct, grouped, joined = results[3]['rows'], results[4]['rows'], results[5]['rows']
                    typed = {(type(row[0]).__name__,row[0]) for row in distinct}
                    assert len(distinct)==4 and typed=={('bool',True),('bool',False),('int',1),('int',0)}
                    assert len(grouped)==4 and all(row[1]==1 for row in grouped)
                    assert joined==[[1,1],[2,2],[3,3],[4,4]]
                    assert server.get('/api/stats')['table_count']==3
                    return {'distinct_typed_values':4,'groups':4,'boolean_join_rows':4,'rolled_back':True}
                check("typed DISTINCT/GROUP BY and nested boolean JOIN execute correctly in final EXE", typed_sets_and_join)

                def reject_unknown_request_before_write():
                    status,value = server.request('POST','/api/query',{'sql':'DELETE FROM courses','unexpected':True})
                    assert status==400 and 'error' in value
                    assert server.query('SELECT COUNT(*) FROM courses')[0]['rows']==[[5]]
                    return {'http_status':status,'stored_course_rows':5}
                check("strict request contract rejects unknown fields before packaged SQL writes", reject_unknown_request_before_write)

                def analyze():
                    value = server.post("/api/research/analyze", {})
                    assert value["table_count"] == 3
                    counts = {name: table["row_count"] for name, table in value["tables"].items()}
                    assert counts == {"students": 12, "courses": 5, "enrollments": 19}, counts
                    assert value["tables"]["students"]["columns"]["gpa"]["null_count"] == 2
                    assert server.get("/api/research/status")["optimizer"]["statistics"]["available"] is True
                    return {"counts": counts, "generation": value["generation"],
                            "elapsed_ms": value["elapsed_ms"], "page_reads": value["page_reads"]}
                check("real statistics from bundled three-table dataset", analyze)

                def modes():
                    adaptive = server.post("/api/research/optimizer", {"mode": "adaptive"})
                    assert adaptive["mode"] == "adaptive"
                    rows = server.query("SELECT id FROM students WHERE id=1005;")[0]["rows"]
                    assert rows == [[1005]]
                    heuristic = server.post("/api/research/optimizer", {"mode": "heuristic"})
                    assert heuristic["mode"] == "heuristic"
                    return {"adaptive_result": rows, "final_mode": heuristic["mode"],
                            "learning": heuristic["learning"]}
                check("adaptive and heuristic mode switching preserves query values", modes)

                def buffer():
                    predictive = server.post("/api/research/buffer", {"policy": "predictive", "sample_size": 8})
                    assert predictive["policy"] == "predictive" and predictive["sample_size"] == 8
                    assert server.get("/api/stats")["cache_policy"] == "predictive"
                    assert server.query("SELECT COUNT(*) FROM students;")[0]["rows"] == [[12]]
                    predictive_stats = server.get("/api/stats")
                    lru = server.post("/api/research/buffer", {"policy": "lru", "sample_size": 8})
                    assert lru["policy"] == "lru" and server.get("/api/stats")["cache_policy"] == "lru"
                    return {"predictive": predictive, "predictive_query_stats": predictive_stats,
                            "restored_policy": lru["policy"]}
                check("predictive and LRU policies through packaged HTTP controls", buffer)

                original = "SELECT id FROM students WHERE id=1001 OR id=1005 ORDER BY id;"

                def trusted():
                    value = server.post("/api/research/optimize", {"sql": original, "use_ai": False})
                    assert value["verification"]["level"] == "proven" and value["verification"]["matched"] is True
                    assert "or-equalities-to-in" in value["verification"]["rules"]
                    assert value["original"]["row_count"] == value["candidate"]["row_count"] == 2
                    assert value["model_calls"] == 0 and value["model_mode"] == "local-rules"
                    assert isinstance(value["recommendation"]["replace"], bool)
                    # Tiny demonstrations need not exceed the conservative
                    # speed/noise gate. Verify correctness, do not force replace.
                    return value
                check("trusted OR-to-IN proof with full submitted-data result comparison", trusted)

                def exact_cache():
                    value = server.post("/api/research/optimize", {"sql": original, "use_ai": False})
                    assert value["cache"]["hit"] is True and value["verification"]["matched"] is True
                    assert value["timings"]["validation_ms"] == 0
                    assert "cached_experiment_ms" in value["timings"]
                    assert any("未重复" in warning for warning in value["warnings"])
                    return {"cache": value["cache"], "timings": value["timings"], "warnings": value["warnings"]}
                check("exact-version conclusion cache is marked and does not pretend to remeasure", exact_cache)

                def mismatch():
                    value = server.post("/api/research/optimize", {
                        "sql": original, "candidate_sql": "SELECT id FROM students WHERE id=1002 OR id=1003 ORDER BY id;", "use_ai": False})
                    assert value["verification"]["matched"] is False
                    assert value["verification"]["level"] == "unknown"
                    assert value["recommendation"]["replace"] is False and value["recommendation"]["sql"] is None
                    return {"verification": value["verification"], "recommendation": value["recommendation"]}
                check("non-equivalent external candidate cannot become an importable recommendation", mismatch)

                def tested_only():
                    value = server.post("/api/research/optimize", {
                        "sql": "SELECT id FROM students WHERE id>1000;",
                        "candidate_sql": "SELECT id FROM students WHERE id>=1001;", "use_ai": False})
                    assert value["verification"]["matched"] is True and value["verification"]["level"] == "tested"
                    assert value["recommendation"]["replace"] is False and value["recommendation"]["sql"] is None
                    return {"verification": value["verification"], "recommendation": value["recommendation"]}
                check("current-data coincidence is tested and cannot authorize an unknown rewrite", tested_only)

                def write_candidate():
                    value = server.post("/api/research/optimize", {
                        "sql": original, "candidate_sql": "DELETE FROM students;", "use_ai": False}, expected=400)
                    assert isinstance(value.get("error"), str)
                    assert server.query("SELECT COUNT(*) FROM students;")[0]["rows"] == [[12]]
                    return {"status": 400, "error": value["error"], "student_rows": 12}
                check("write candidate rejected without executing model or candidate text", write_candidate)

                semantic_payload = {"sql": "SELECT id,title FROM courses ORDER BY id;", "column": "title",
                                    "prompt": "关键词：数据库|查询|索引", "batch_size": 8,
                                    "max_rows": 64, "max_calls": 8, "budget_seconds": 30}

                def semantic(operation):
                    server.post("/api/research/semantic/cache", {})
                    payload = dict(semantic_payload, operation=operation)
                    cold = server.post("/api/research/semantic", payload)
                    warm = server.post("/api/research/semantic", payload)
                    expected_rows = {"filter": 1, "classify": 5, "aggregate": 2}[operation]
                    assert cold["mode"] == warm["mode"] == "mock"
                    assert cold["input_rows"] == warm["input_rows"] == 5
                    assert cold["row_count"] == warm["row_count"] == expected_rows
                    assert cold["rows"] == warm["rows"] and cold["columns"] == warm["columns"]
                    assert cold["stats"]["model_calls"] == warm["stats"]["model_calls"] == 0
                    assert cold["stats"]["cache_hits"] == 0 and cold["stats"]["cache_misses"] == 5
                    assert cold["stats"]["batches"] == 1
                    assert warm["stats"]["cache_hits"] == 5 and warm["stats"]["cache_misses"] == 0
                    assert warm["stats"]["batches"] == 0
                    assert "没有调用大模型" in cold["explanation"]
                    if operation == "filter":
                        assert cold["rows"] == [[201, "数据库原理"]]
                    elif operation == "classify":
                        assert cold["columns"][-2:] == ["_semantic_match", "_semantic_label"]
                        assert [row[-2] for row in cold["rows"]] == [True, False, False, False, False]
                    else:
                        assert dict(cold["rows"]) == {"数据库": 1, "其他": 4}
                    return {"input_rows": 5, "columns": cold["columns"], "rows": cold["rows"],
                            "cold_stats": cold["stats"], "warm_stats": warm["stats"],
                            "mode": cold["mode"], "explanation": cold["explanation"],
                            "data_policy": cold["data_policy"]}
                for operation in ("filter", "classify", "aggregate"):
                    check(f"semantic {operation} cold and warm exact-text cache", lambda operation=operation: semantic(operation))

                def clear_cache():
                    cleared = server.post("/api/research/semantic/cache", {})
                    assert cleared == {"cleared": True, "entries": 0}
                    state = server.get("/api/research/status")["semantic"]
                    assert state["entries"] == 0
                    return {"response": cleared, "status": state}
                check("inference cache clear endpoint reports actual empty cache", clear_cache)

                def budgets():
                    failures = []
                    for override in ({"max_rows": 2}, {"batch_size": 1, "max_calls": 1}):
                        value = server.post("/api/research/semantic", dict(semantic_payload, **override), expected=400)
                        assert isinstance(value.get("error"), str) and "rows" not in value
                        failures.append({"parameters": override, "status": 400, "error": value["error"]})
                    assert server.get("/api/research/status")["semantic"]["entries"] == 0
                    assert server.query("SELECT COUNT(*) FROM courses;")[0]["rows"] == [[5]]
                    return {"rejections": failures, "ordinary_sql_available": True}
                check("row and call budgets reject partial results without breaking SQL", budgets)

                def active_transaction():
                    server.query("BEGIN; UPDATE courses SET title='待回滚研究测试' WHERE id=201;")
                    before = server.get("/api/stats")
                    assert before["transaction_active"] is True and before["staged_pages"] > 0
                    attempts = [
                        ("/api/research/analyze", {}),
                        ("/api/research/optimizer", {"mode": "adaptive"}),
                        ("/api/research/buffer", {"policy": "predictive", "sample_size": 8}),
                        ("/api/research/optimize", {"sql": original, "use_ai": False}),
                        ("/api/research/semantic", semantic_payload)]
                    rejections = []
                    for route, payload in attempts:
                        value = server.post(route, payload, expected=400)
                        assert "事务" in value["error"], value
                        state = server.get("/api/stats")
                        assert state["transaction_active"] is True and state["staged_pages"] > 0
                        assert server.query("SELECT title FROM courses WHERE id=201;")[0]["rows"] == [["待回滚研究测试"]]
                        rejections.append({"route": route, "status": 400, "error": value["error"],
                                           "transaction_preserved": True, "staged_pages": state["staged_pages"]})
                    server.query("ROLLBACK;")
                    assert server.get("/api/stats")["transaction_active"] is False
                    assert server.query("SELECT title FROM courses WHERE id=201;")[0]["rows"] == [["数据库原理"]]
                    return {"rejections": rejections, "rollback_restored": True}
                check("research rejections preserve active transaction and staged data until rollback", active_transaction)

                def integrity():
                    result = server.query("CHECK DATABASE;")[0]
                    assert "Database integrity OK" in result.get("message", ""), result
                    value = server.post("/api/integrity", {})
                    assert value["ok"] is True and value["summary"]["rows"] == 36
                    assert server.query("SELECT COUNT(*) FROM students;")[0]["rows"] == [[12]]
                    return {"sql_message": result["message"], "summary": value["summary"],
                            "issues": value["issue_count"], "elapsed_ms": value["elapsed_ms"]}
                check("rollback followed by physical CHECK and integrity HTTP verification", integrity)
            finally:
                server.stop()

            def reopen():
                result = subprocess.run(
                    [str(EXE), "--db", str(folder), "--execute", "CHECK DATABASE; SELECT COUNT(*) FROM courses;"],
                    cwd=tempfile.gettempdir(), env=server.environment,
                    capture_output=True, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                output = result.stdout.decode("utf-8", errors="replace")
                assert result.returncode == 0 and "Database integrity OK" in output, (result.returncode, output, result.stderr)
                return {"returncode": result.returncode, "stdout": output.strip()}
            check("same packaged EXE reopens the temporary database after owned server shutdown", reopen)

        assert hashlib.sha256(EXE.read_bytes()).hexdigest() == digest, "EXE changed during the smoke run"
        evidence["passed"] = True
    except Exception as error:
        evidence["error"] = f"{type(error).__name__}: {error}"
    finally:
        evidence["total_checks"] = len(checks)
        evidence["passed_checks"] = sum(item["passed"] for item in checks)
        evidence["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in evidence.items() if key != "checks"},
                         ensure_ascii=True, indent=2))
        for item in checks:
            print(f"{'PASS' if item['passed'] else 'FAIL'} {item['name']}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
