"""Opt-in, four-call campus API check through the packaged workbench.

Run explicitly with --live on the campus network. Credentials are read only
by the backend from an ignored local config. Evidence contains no credentials,
session token, private dataset, provider response body or reasoning text.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.research_executable_smoke import EXE, ExeWorkbench


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly spend up to four real model calls')
    parser.add_argument('--config', type=Path, default=ROOT/'ai.local.json')
    args = parser.parse_args()
    if not args.live:
        parser.error('Real API calls require --live; no network inference was started.')
    config = args.config.resolve()
    # Do not echo config contents or paths in an external provider request.
    local = json.loads(config.read_text(encoding='utf-8-sig'))
    if (local.get('mode') != 'remote' or local.get('model') != 'deepseek-chat'
            or local.get('base_url') != 'https://models.sjtu.edu.cn/api/v1'
            or local.get('requests_per_minute') != 10):
        parser.error('This evidence requires the campus deepseek-chat profile with a 10 RPM limit.')
    credential = local.get('api_key','')
    if not isinstance(credential,str) or not credential:
        parser.error('The local campus profile needs a nonempty credential.')
    checks = []
    evidence = {
        'format_version':1, 'generated_at':datetime.now(timezone.utc).isoformat(),
        'passed':False, 'provider':'SJTU campus API', 'model':'deepseek-chat',
        'scope':'Packaged EXE, actual campus model, synthetic course dataset only.',
        'requests_per_minute':10, 'planned_remote_calls':4,
        'configured_timeout_seconds':local.get('timeout',90),
        'exe_sha256':hashlib.sha256(EXE.read_bytes()).hexdigest(),
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                         for name in ('auroradb/ai.py','auroradb/__main__.py',
                                      'auroradb/server.py','tools/campus_api_smoke.py')},
        'limitations':['A small connectivity/function smoke check, not a model accuracy benchmark.',
                       'Other authorized models, token quota and long-run rate limits were not measured.',
                       'No user database or private records were sent.'],
        'checks':checks,
    }
    def check(name, action):
        start = time.monotonic()
        detail = action()
        entry = {'name':name,'passed':True,'elapsed_seconds':round(time.monotonic()-start,3)}
        if detail is not None:
            entry['observed'] = detail
        checks.append(entry)
        print(json.dumps(entry,ensure_ascii=False),flush=True)
        return detail
    with tempfile.TemporaryDirectory(prefix='auroradb-campus-') as folder:
        app = ExeWorkbench(Path(folder), config)
        try:
            app.launch()
            public_ai = app.get('/api/ai/status')
            http_timeout = float(public_ai.get('timeout_seconds',90)) + 15
            def post(route, payload):
                status, result = app.request('POST',route,payload,timeout=http_timeout)
                if status != 200:
                    # HTTP adapter already sanitizes all provider errors.
                    raise RuntimeError(f'{route}: HTTP {status}: '+str(result.get('error','request failed')))
                if credential in json.dumps(result,ensure_ascii=False):
                    raise AssertionError('Protected configuration reached a public API result')
                return result
            def configured():
                status = app.get('/api/ai/status')
                assert status['available'] and status['mode']=='remote'
                assert status['model']=='deepseek-chat' and status['requests_per_minute']==10
                assert credential not in json.dumps(status,ensure_ascii=False)
                return {'model':status['model'],'requests_per_minute':status['requests_per_minute']}
            check('school profile loads in packaged EXE without exposing the key',configured)
            counts = app.query('SELECT COUNT(*) FROM students; SELECT COUNT(*) FROM courses;')
            before = [result['rows'] for result in counts]
            def generate():
                result = post('/api/ai',{'action':'generate',
                    'prompt':'查询courses中学分不少于3的课程，仅返回id和title，按id升序，不要LIMIT。','sql':''})
                assert result['mode']=='remote' and result['validation']['valid'] and result['validation']['read_only']
                # Explicit test execution is a separate HTTP call, after compile validation.
                rows = app.query(result['sql'])[-1]['rows']
                expected = app.query('SELECT id,title FROM courses WHERE credits>=3 ORDER BY id')[-1]['rows']
                assert rows==expected
                return {'sql_compiled':True,'fixture_result_matches':True,'rows':len(rows)}
            check('real SQL generation compiles and matches the known fixture',generate)
            def explain():
                result = post('/api/ai',{'action':'explain','prompt':'',
                    'sql':'SELECT id,title FROM courses WHERE credits>=3 ORDER BY id;'})
                assert result['mode']=='remote' and result['explanation'].strip()
                assert result['validation']['valid'] and result['validation']['read_only']
                return {'nonempty_explanation':True,'sql_compiled':True}
            check('real SQL explanation preserves a valid read-only query',explain)
            def optimize():
                result = post('/api/research/optimize',{
                    'sql':'SELECT id FROM students WHERE id=1001 OR id=1005 ORDER BY id;',
                    'use_ai':True})
                assert result['model_mode']=='remote' and result['model_calls']==1
                assert result['verification']['matched']
                return {'matched':True,'verification_level':result['verification']['level'],
                        'recommended':bool(result['recommendation']['replace'])}
            check('real model candidate passes the local trusted optimizer pipeline',optimize)
            def semantic():
                payload = {'sql':'SELECT id,title FROM courses ORDER BY id','column':'title',
                           'prompt':'判断课程标题是否直接讨论数据库。数据库原理匹配true，其余课程匹配false。标签使用数据库或其他。',
                           'operation':'filter','batch_size':8,'max_rows':8,'max_calls':1,'budget_seconds':90}
                result = post('/api/research/semantic',payload)
                assert result['mode']=='remote' and result['stats']['model_calls']==1
                assert result['rows']==[[201,'数据库原理']]
                cached = post('/api/research/semantic',payload)
                assert cached['rows']==result['rows'] and cached['stats']['model_calls']==0
                assert cached['stats']['cache_hits']==5
                return {'fixture_result_matches':True,'first_calls':1,'warm_calls':0,'warm_cache_hits':5}
            check('real semantic filter and warm-cache reuse match the labelled fixture',semantic)
            def unchanged():
                after = [r['rows'] for r in app.query('SELECT COUNT(*) FROM students; SELECT COUNT(*) FROM courses;')]
                assert after==before
                assert app.get('/api/ai/status')['last_request_succeeded']
                return {'row_counts_unchanged':True,'last_request_succeeded':True}
            check('AI operations leave stored data intact and report successful transport',unchanged)
            evidence['passed'] = True
        except Exception as exc:
            message = str(exc).replace(credential,'[redacted]')
            evidence['error'] = message
            print(json.dumps({'passed':False,'error':message},ensure_ascii=False),flush=True)
        finally:
            app.stop()
    output = ROOT/'docs'/'campus_api_smoke.json'
    encoded = json.dumps(evidence,ensure_ascii=False,indent=2)
    if credential in encoded:
        raise AssertionError('Credential cannot be written into evidence')
    output.write_text(encoded+'\n',encoding='utf-8')
    print(json.dumps({'passed':evidence['passed'],'checks':len(checks),'evidence':'docs/campus_api_smoke.json'}),flush=True)
    return int(not evidence['passed'])


if __name__=='__main__':
    raise SystemExit(main())
