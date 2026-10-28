"""Real local HTTP provider tests for optional, non-executing SQL advice."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from auroradb.ai import (AIError, AIRateLimitError, AIService, MAX_CONTEXT_BYTES, MAX_PROMPT_CHARS,
                        MAX_RESPONSE_BYTES, MAX_SQL_CHARS)
from auroradb.__main__ import load_ai_config


def envelope(content=None, finish='stop', **message_fields):
    if content is None:
        content = json.dumps({'explanation':'查询建议尚未执行','sql':'SELECT 1 AS n;',
                              'suggestions':['检查执行计划'],'warnings':[]},ensure_ascii=False)
    return {'choices':[{'finish_reason':finish,'message':{'role':'assistant','content':content,**message_fields}}]}


class FakeProviderHandler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.server.requests.append({'path':self.path,'headers':dict(self.headers),'body':body})
        response = self.server.response
        delay = response.get('delay',0)
        if delay: time.sleep(delay)
        payload = response.get('raw')
        if payload is None: payload = json.dumps(response.get('json',envelope()),ensure_ascii=False).encode('utf-8')
        self.send_response(response.get('status',200))
        self.send_header('Content-Type','application/json')
        if not response.get('omit_length'):
            self.send_header('Content-Length',str(response.get('declared_length',len(payload))))
        if response.get('location'): self.send_header('Location',response['location'])
        if 'retry_after' in response: self.send_header('Retry-After',response['retry_after'])
        self.end_headers()
        try: self.wfile.write(payload)
        except OSError: pass  # Client timeout/size rejection is intentional.


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1',0),FakeProviderHandler)
        self.server.daemon_threads = True
        self.server.requests = []
        self.server.response = {'json':envelope()}
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def remote(self, **changes):
        configuration = {'mode':'remote','base_url':self.url,'model':'school-open-model',
                         'api_key':'fake-private-test-key','timeout':2}
        configuration.update(changes)
        return AIService(**configuration)

    def generate(self, service=None, **kwargs):
        return (service or self.remote()).assist('generate',prompt='给出一个查询',**kwargs)

    def test_disabled_default_and_bad_configuration_do_not_break_startup(self):
        with patch.dict(os.environ,{},clear=True):
            service = AIService.from_env()
        self.assertEqual(service.status()['mode'],'disabled')
        self.assertFalse(service.status()['available'])
        with self.assertRaisesRegex(AIError,'未启用'): self.generate(service)
        for configuration in ({'mode':'nonsense'},{'timeout':'NaN'},{'timeout':True},
                              {'timeout':0},{'timeout':301},{'mode':'remote'},
                              {'mode':'remote','base_url':self.url}):
            with self.subTest(configuration=configuration):
                service = AIService(**configuration)
                self.assertFalse(service.status()['available'])
                self.assertTrue(service.status()['message'])
                with self.assertRaises(AIError): self.generate(service)
        self.assertEqual(self.server.requests,[])

    def test_from_env_and_status_never_return_credentials_or_endpoint(self):
        values = {'AURORA_AI_MODE':'remote','AURORA_AI_BASE_URL':self.url+'/v1',
                  'AURORA_AI_MODEL':'arbitrary-campus-model','AURORA_AI_API_KEY':'private-environment-key',
                  'AURORA_AI_TIMEOUT':'90','AURORA_AI_REQUESTS_PER_MINUTE':'10'}
        with patch.dict(os.environ,values,clear=True): service = AIService.from_env()
        status = service.status()
        self.assertEqual(status['model'],'arbitrary-campus-model')
        self.assertTrue(status['available'])
        self.assertEqual(status['requests_per_minute'],10)
        self.assertEqual(status['requests_remaining'],10)
        self.assertIsNone(status['last_request_succeeded'])
        encoded = json.dumps(status)
        self.assertNotIn('private-environment-key',encoded)
        self.assertNotIn(self.url,encoded)
        self.assertNotIn('api_key',encoded)

    def test_remote_payload_header_and_minimal_private_context(self):
        result = self.generate(context={
            'schema':[{'name':'students','columns':[{'name':'id','type':'INTEGER'}]}],
            'plan':{'operator':'TableScan','rows':[['private-row']]},
            'stats':{'rows_scanned':4,'api_key':'hidden-context-key'},
            'rows':[['private-top-row']], 'unrelated':'not-for-provider',
        })
        self.assertEqual(result['mode'],'remote')
        self.assertEqual(result['sql'],'SELECT 1 AS n;')
        request = self.server.requests[0]
        self.assertEqual(request['path'],'/chat/completions')
        self.assertEqual(request['headers']['Authorization'],'Bearer fake-private-test-key')
        payload = json.loads(request['body'])
        self.assertEqual(payload['model'],'school-open-model')
        self.assertIs(payload['stream'],False)
        self.assertNotIn('response_format',payload)
        self.assertEqual([message['role'] for message in payload['messages']],['system','user'])
        user = json.loads(payload['messages'][1]['content'])
        self.assertEqual(set(user['context']),{'schema','plan','stats'})
        self.assertEqual(user['context']['stats'],{'rows_scanned':4})
        self.assertNotIn('private-row',request['body'].decode())
        self.assertNotIn('private-top-row',request['body'].decode())
        self.assertNotIn('hidden-context-key',request['body'].decode())
        self.assertNotIn('fake-private-test-key',request['body'].decode())

    def test_root_v1_and_explicit_endpoint_and_optional_key(self):
        for suffix,expected in (('','/chat/completions'),('/v1/','/v1/chat/completions'),
                                ('/custom/chat/completions','/custom/chat/completions')):
            with self.subTest(suffix=suffix):
                self.generate(self.remote(base_url=self.url+suffix,api_key=''))
                self.assertEqual(self.server.requests[-1]['path'],expected)
                self.assertNotIn('Authorization',self.server.requests[-1]['headers'])

    def test_fenced_json_and_sql_and_final_content_only(self):
        structured = json.dumps({'explanation':'解释','sql':'SELECT 2;','suggestions':[],'warnings':[]},ensure_ascii=False)
        self.server.response = {'json':envelope('```json\n'+structured+'\n```',reasoning_content='PRIVATE_REASONING')}
        result = self.generate()
        self.assertEqual(result['sql'],'SELECT 2;')
        self.assertNotIn('PRIVATE_REASONING',json.dumps(result))
        self.server.response = {'json':envelope('```sql\nSELECT 3;\n```')}
        self.assertEqual(self.generate()['sql'],'SELECT 3;')
        self.server.response = {'json':envelope('说明仅为建议。\n```sql\nSELECT 4;\n```\n请检查执行计划。')}
        result = self.generate()
        self.assertEqual(result['sql'],'SELECT 4;')
        self.assertIn('说明仅为建议',result['explanation'])
        self.server.response = {'json':envelope('```\nSELECT 5;\n```')}
        self.assertEqual(self.generate()['sql'],'SELECT 5;')

    def test_actions_are_advice_and_do_not_call_database_or_execute_sql(self):
        for action in ('generate','explain','optimize'):
            with self.subTest(action=action):
                result = self.remote().assist(action,prompt='需求',sql='SELECT 1;')
                self.assertEqual(result['sql'],'SELECT 1 AS n;')
                self.assertNotIn('rows',result)
                payload = json.loads(self.server.requests[-1]['body'])
                self.assertEqual(json.loads(payload['messages'][1]['content'])['action'],action)

    def test_mock_is_explicit_local_rule_and_schema_grounded(self):
        service = AIService(mode='mock',api_key='unused-key',model='not-used')
        context = {'schema':[{'name':'courses','columns':[]},
                            {'name':'students','columns':[{'name':name} for name in ('id','name','gpa')]}]}
        result = service.assist('generate',prompt='查询绩点不低于3.8的学生',context=context)
        self.assertEqual(result['sql'],'SELECT id,name,gpa FROM "students" WHERE gpa >= 3.8 ORDER BY gpa DESC LIMIT 20;')
        self.assertEqual(result['provider'],'本地规则演示')
        self.assertEqual(result['model'],'')
        self.assertIn('没有调用远程模型',result['explanation'])
        self.assertIn('本地规则',result['warnings'][0])
        self.assertIn('不能泛化',result['suggestions'][0])
        self.assertIn('COUNT(*)',service.assist('generate',prompt='统计学生数量',context=context)['sql'])
        for action in ('explain','optimize'):
            self.assertEqual(service.assist(action,sql='SELECT 9;',context=context)['sql'],'SELECT 9;')
        self.assertEqual(self.server.requests,[])

    def test_input_boundaries_fail_before_provider_request(self):
        for args in ({'action':'other','prompt':'x'},{'action':'generate','prompt':''},
                     {'action':'generate','prompt':'x'*(MAX_PROMPT_CHARS+1)},
                     {'action':'explain','sql':'x'*(MAX_SQL_CHARS+1)},
                     {'action':'optimize','sql':''},{'action':'generate','prompt':17},
                     {'action':'generate','prompt':'x','context':[]},
                     {'action':'generate','prompt':'x','context':{'stats':{'x':float('inf')}}},
                     {'action':'generate','prompt':'x','context':{'schema':'x'*(MAX_CONTEXT_BYTES+1)}}):
            with self.subTest(args=str(args)[:80]), self.assertRaises(AIError):
                self.remote().assist(**args)
        self.assertEqual(self.server.requests,[])

    def test_unsafe_urls_and_header_credentials_are_not_echoed(self):
        for address in ('http://user:private-password@localhost/v1','file:///secret',
                        self.url+'/v1?key=private-url-key',self.url+'/v1#secret',
                        'http://localhost:99999',self.url+'\nprivate-newline'):
            with self.subTest(address=address):
                service = self.remote(base_url=address)
                self.assertFalse(service.status()['available'])
                with self.assertRaises(AIError) as raised: self.generate(service)
                self.assertNotIn(address,str(raised.exception))
                self.assertNotIn('private-password',str(raised.exception))
                self.assertNotIn('private-url-key',json.dumps(service.status()))
        service = self.remote(api_key='private-key\nInjected: yes')
        self.assertFalse(service.status()['available'])
        self.assertEqual(self.server.requests,[])

    def test_incomplete_finish_reasons_are_rejected(self):
        for reason in ('length','content_filter','tool_calls','aborted','insufficient_system_resource',None):
            with self.subTest(reason=reason):
                self.server.response = {'json':envelope(finish=reason)}
                with self.assertRaisesRegex(AIError,'未完整'): self.generate()

    def test_invalid_provider_envelopes_and_outputs_are_sanitized(self):
        bodies = [b'private-provider-body not json', b'{"choices":NaN}',
                  b'{"choices":[],"ignored":1e999}', b'{"choices":[],"choices":[]}',
                  json.dumps([]).encode(),json.dumps({'choices':[]}).encode(),
                  json.dumps(envelope(content=42)).encode(),
                  json.dumps(envelope(content='{"sql":17}')).encode(),
                  json.dumps(envelope(content='{"sql":"SELECT 1","warnings":[9]}')).encode(),
                  json.dumps(envelope(content='{"sql":"SELECT 1","unused":NaN}')).encode(),
                  json.dumps(envelope(content='{}')).encode(),
                  json.dumps(envelope(content='{"sql":"SELECT 1"}',tool_calls=[{'secret':'x'}])).encode()]
        for raw in bodies:
            with self.subTest(raw=raw[:60]):
                self.server.response = {'raw':raw}
                with self.assertRaises(AIError) as raised: self.generate()
                self.assertNotIn('private-provider-body',str(raised.exception))
                self.assertNotIn('fake-private-test-key',str(raised.exception))

    def test_provider_http_errors_and_secret_echo_are_sanitized(self):
        self.server.response = {'status':401,'raw':b'raw-body fake-private-test-key endpoint=secret'}
        with self.assertRaisesRegex(AIError,'HTTP 401') as raised: self.generate()
        self.assertNotIn('raw-body',str(raised.exception))
        self.assertNotIn('fake-private-test-key',str(raised.exception))
        self.server.response = {'json':envelope(content='{"explanation":"fake-private-test-key","sql":"SELECT 1"}')}
        with self.assertRaisesRegex(AIError,'受保护配置'): self.generate()
        key = 'special"private\\key'
        self.server.response = {'json':envelope(content=json.dumps({'explanation':key,'sql':'SELECT 1'}))}
        with self.assertRaisesRegex(AIError,'受保护配置'): self.generate(self.remote(api_key=key))

    def test_declared_and_actual_response_size_and_truncation_are_bounded(self):
        for response in ({'raw':b'','declared_length':MAX_RESPONSE_BYTES+1},
                         {'raw':b'x'*(MAX_RESPONSE_BYTES+1),'omit_length':True},
                         {'json':envelope(),'declared_length':MAX_RESPONSE_BYTES}):
            with self.subTest(response=list(response)), self.assertRaises(AIError):
                self.server.response = response
                self.generate()

    def test_timeout_is_bounded_and_errors_do_not_include_remote_content(self):
        self.server.response = {'delay':0.4,'raw':b'private-timeout-body'}
        start = time.monotonic()
        with self.assertRaisesRegex(AIError,'超时'): self.generate(self.remote(timeout=0.1))
        self.assertLess(time.monotonic()-start,1)

    def test_redirect_is_rejected_without_forwarding_bearer_credentials(self):
        target = ThreadingHTTPServer(('127.0.0.1',0),FakeProviderHandler)
        target.daemon_threads = True
        target.requests = []
        target.response = {'json':envelope()}
        thread = threading.Thread(target=target.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        thread.start()
        try:
            self.server.response = {'status':307,'location':'http://127.0.0.1:'+str(target.server_port)+'/chat/completions',
                                    'raw':b'private-redirect-body'}
            with self.assertRaisesRegex(AIError,'重定向'): self.generate()
            self.assertEqual(target.requests,[])
            self.assertEqual(len(self.server.requests),1)
        finally:
            target.shutdown(); target.server_close(); thread.join(timeout=2)

    def test_rolling_quota_is_shared_by_advice_and_semantic_batches(self):
        service = self.remote(requests_per_minute=10)
        clock = [100.0]
        with patch('auroradb.ai.time.monotonic',side_effect=lambda:clock[0]):
            for _ in range(5): self.generate(service)
            self.server.response = {'json':envelope(content=json.dumps(
                {'results':[{'id':0,'match':True,'label':'数据库'}]},ensure_ascii=False))}
            for _ in range(5):
                self.assertTrue(service.semantic_batch('属于数据库？',['数据库课程'])[0]['match'])
            self.assertEqual(len(self.server.requests),10)
            self.assertEqual(service.status()['requests_remaining'],0)
            with self.assertRaises(AIRateLimitError) as raised: self.generate(service)
            self.assertEqual(raised.exception.retry_after_seconds,60)
            clock[0] = 159.99
            with self.assertRaises(AIRateLimitError): service.semantic_batch('条件',['文本'])
            self.assertEqual(len(self.server.requests),10)
            clock[0] = 160.0
            self.server.response = {'json':envelope()}
            self.generate(service)
            self.assertEqual(len(self.server.requests),11)
            self.assertEqual(service.status()['requests_remaining'],9)
            self.assertEqual(service.status()['rate_limit_retry_after_seconds'],0)

    def test_concurrent_requests_cannot_exceed_shared_quota(self):
        service = self.remote(requests_per_minute=10,timeout=10)
        barrier = threading.Barrier(20)
        def call(_):
            barrier.wait(timeout=10)
            try:
                self.generate(service)
                return 'success'
            except AIRateLimitError:
                return 'limited'
        with ThreadPoolExecutor(max_workers=20) as executor:
            outcomes = list(executor.map(call,range(20)))
        self.assertEqual(outcomes.count('success'),10)
        self.assertEqual(outcomes.count('limited'),10)
        self.assertEqual(len(self.server.requests),10)

    def test_mock_and_input_errors_do_not_consume_remote_quota(self):
        mock = AIService(mode='mock',requests_per_minute=1)
        for _ in range(3):
            mock.assist('generate',prompt='查询')
            mock.semantic_batch('关键词：数据库',['数据库'])
        self.assertEqual(mock.status()['requests_remaining'],1)
        self.assertIsNone(mock.status()['last_request_succeeded'])
        service = self.remote(requests_per_minute=1)
        with self.assertRaises(AIError): service.assist('generate',prompt='')
        self.assertEqual(service.status()['requests_remaining'],1)
        self.assertEqual(self.server.requests,[])

    def test_provider_429_is_sanitized_bounded_and_does_not_retry(self):
        service = self.remote(requests_per_minute=10)
        clock = [100.0]
        self.server.response = {'status':429,'retry_after':'999999999',
                                'raw':b'private-provider-body fake-private-test-key endpoint=private'}
        with patch('auroradb.ai.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(AIRateLimitError) as raised: self.generate(service)
            self.assertIn('HTTP 429',str(raised.exception))
            self.assertEqual(raised.exception.retry_after_seconds,300)
            self.assertNotIn('private',str(raised.exception))
            self.assertNotIn(self.url,str(raised.exception))
            self.assertEqual(service.status()['rate_limit_retry_after_seconds'],300)
            self.assertIs(service.status()['last_request_succeeded'],False)
            self.assertIsNone(service.status()['last_success_age_seconds'])
            with self.assertRaises(AIRateLimitError): self.generate(service)
            self.assertEqual(len(self.server.requests),1)
            clock[0] = 400
            self.server.response = {'json':envelope()}
            self.generate(service)
            self.assertEqual(len(self.server.requests),2)
            self.assertIs(service.status()['last_request_succeeded'],True)
            self.assertEqual(service.status()['last_success_age_seconds'],0)
            self.assertIn('尚不代表已通过真实连通性验证',service.status()['message'])

    def test_retry_after_date_invalid_and_negative_headers_are_bounded(self):
        for header,expected in [('Thu, 01 Jan 1970 00:02:00 GMT',20),
                                ('invalid-private-content',60),('NaN',60),('-1',1)]:
            with self.subTest(header=header):
                service = self.remote()
                self.server.response = {'status':429,'retry_after':header,'raw':b'private'}
                with patch('auroradb.ai.time.monotonic',return_value=100), patch('auroradb.ai.time.time',return_value=100):
                    with self.assertRaises(AIRateLimitError) as raised: self.generate(service)
                self.assertEqual(raised.exception.retry_after_seconds,expected)

    def test_failed_or_malformed_responses_consume_slot_but_never_mark_success(self):
        for semantic,response in ((False,{'status':503,'raw':b'private'}),
                                  (False,{'json':envelope(content='{}')}),
                                  (True,{'json':envelope(content='{"results":[{"id":0,"match":17,"label":"x"}]}')})):
            with self.subTest(response=response):
                service = self.remote(requests_per_minute=1)
                self.server.response = response
                with self.assertRaises(AIError):
                    if semantic: service.semantic_batch('条件',['文本'])
                    else: self.generate(service)
                self.assertEqual(service.status()['requests_remaining'],0)
                self.assertIs(service.status()['last_request_succeeded'],False)
                self.assertIsNone(service.status()['last_success_age_seconds'])

    def test_requests_per_minute_configuration_validation_and_local_file(self):
        for value in (True,False,1.5,None,-1,10001,'NaN','1.0','-1','10001',''):
            with self.subTest(value=value):
                service = self.remote(requests_per_minute=value)
                self.assertFalse(service.status()['available'])
                with self.assertRaisesRegex(AIError,'每分钟请求数'): self.generate(service)
        for value in (0,10,10000,'10'):
            self.assertTrue(self.remote(requests_per_minute=value).status()['available'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rate-config.json'
            path.write_text(json.dumps({'mode':'mock','requests_per_minute':10}),encoding='utf-8')
            self.assertEqual(load_ai_config(path).status()['requests_per_minute'],10)
        self.assertEqual(self.server.requests,[])


if __name__ == '__main__': unittest.main()
