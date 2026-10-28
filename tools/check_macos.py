"""Check source workbench portability; only Darwin runs verify native macOS."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import platform
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check():
    report = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'system': platform.system(), 'machine': platform.machine(),
        'python': platform.python_version(), 'checks': [],
        'browser_visual_check': 'not performed; open PPT 用例 in the target browser',
    }

    def passed(name, detail):
        report['checks'].append({'name': name, 'passed': True, 'detail': detail})

    try:
        if sys.version_info < (3, 12):
            raise RuntimeError('需要 Python 3.12 或以上。')
        passed('python', 'Python >= 3.12')
        from auroradb.ai import AIService
        from auroradb.engine import Database
        from auroradb.server import WorkbenchServer, resource_directory
        from auroradb.storage import StorageError

        assets = resource_directory()
        for filename in ('web/index.html', 'web/app.js', 'web/styles.css', 'examples/demo.sql'):
            if not (assets / filename).is_file():
                raise RuntimeError(f'缺少工作台资源：{filename}')
        launcher = (ROOT / '启动工作台.command').read_bytes()
        if not launcher.startswith(b'#!/bin/bash\n') or b'\r' in launcher:
            raise RuntimeError('Mac 启动脚本必须使用 LF 换行。')
        passed('source_resources', '源码资源完整，Mac 启动脚本使用 LF')

        with tempfile.TemporaryDirectory(prefix='auroradb-macos-check-') as folder:
            # Also exercise paths containing Unicode and spaces.
            db_path = Path(folder) / '答辩 数据库'
            db = Database(db_path)
            server = None
            thread = None
            try:
                db.execute("CREATE TABLE people(secret TEXT); INSERT INTO people VALUES('kept');")
                try:
                    second = Database(db_path)
                except StorageError as exc:
                    if 'already open' not in str(exc):
                        raise
                else:
                    second.close()
                    raise RuntimeError('数据库写入锁未阻止第二个连接。')
                db.execute("BEGIN; INSERT INTO people VALUES('pending')")
                passed('storage_and_lock', '中文/空格路径、写入及独占锁通过')
                before = db.research_status()
                server = WorkbenchServer(db, port=0, ai_service=AIService(mode='disabled'))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                def request(method, path, payload=None, token=None):
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=120)
                    try:
                        headers = {}
                        body = None
                        if payload is not None:
                            body = json.dumps(payload).encode('utf-8')
                            headers['Content-Type'] = 'application/json'
                            headers['X-Aurora-Token'] = token or ''
                        connection.request(method, path, body, headers)
                        response = connection.getresponse()
                        data = response.read()
                        if response.status != 200:
                            raise RuntimeError(f'{path}: HTTP {response.status}: {data.decode("utf-8")}')
                        return data
                    finally:
                        connection.close()

                html = request('GET', '/').decode('utf-8')
                js = request('GET', '/app.js').decode('utf-8')
                request('GET', '/styles.css')
                if 'toggle-join-demo' not in html or '正在执行策略' not in js:
                    raise RuntimeError('PPT 演示资源不是当前版本。')
                passed('http_assets', 'HTTP 页面、脚本和样式加载通过')
                token = json.loads(request('GET', '/api/session'))['token']
                result = json.loads(request('POST', '/api/research/join-demo', {}, token))
                if not result['matched'] or len(result['rows']) != 100:
                    raise RuntimeError('PPT 用例完整结果核对未通过。')
                control, adaptive = result['sql_order'], result['adaptive']
                if control['stats']['join_comparisons'] != 20000 or adaptive['stats']['join_comparisons'] != 200:
                    raise RuntimeError('PPT 用例未得到预期连接工作量；请查看机器负载与规划回退原因。')
                report['demo'] = {
                    'matched': True, 'rows': 100,
                    'sql_order_ms': control['elapsed_ms'], 'adaptive_ms': adaptive['elapsed_ms'],
                    'comparisons': [control['stats']['join_comparisons'], adaptive['stats']['join_comparisons']],
                    'adaptive_order': adaptive['join_order'],
                }
                passed('ppt_demo', '完整结果一致，返回 100 行，连接比较 20000 → 200')
                if not db.active or db.research_status() != before:
                    raise RuntimeError('演示改变了当前数据库事务或学习状态。')
                if db.execute('SELECT secret FROM people')[0]['rows'] != [['kept'], ['pending']]:
                    raise RuntimeError('演示改变了当前数据。')
                db.execute('ROLLBACK')
                passed('user_database_isolation', '同名表、未提交事务和学习状态保持原样')
            finally:
                if server is not None:
                    if thread is not None:
                        server.shutdown()
                        thread.join(timeout=5)
                    server.server_close()
                db.close()
            reopened = Database(db_path)
            try:
                if reopened.execute('SELECT secret FROM people')[0]['rows'] != [['kept']]:
                    raise RuntimeError('关闭后重新打开的数据核对未通过。')
            finally:
                reopened.close()
            passed('reopen', '文件锁已释放，重新打开的数据与回滚结果一致')
        passed('cleanup', '兼容性检查临时目录已清理')
        report['passed'] = True
    except Exception as exc:
        report['passed'] = False
        report['error'] = str(exc)
    report['native_macos_verified'] = report['passed'] and sys.platform == 'darwin'
    return report


def main():
    parser = argparse.ArgumentParser(description='检查 Mac 源码启动与 PPT 用例；不访问现有数据库或 AI 配置。')
    parser.add_argument('--output', type=Path, default=ROOT / '.build/macos-compatibility.json')
    args = parser.parse_args()
    report = check()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'检查记录：{args.output.resolve()}')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
