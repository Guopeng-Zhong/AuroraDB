"""End-to-end HTTP: workbench -> remote adapter -> real local chat provider.

These tests do not replace AIService with a stub. They validate metadata privacy,
local compilation, transaction preservation and database availability through
the same two HTTP boundaries used by a configured school API.
"""
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from auroradb.ai import AIService
from auroradb.engine import Database
from auroradb.server import WorkbenchServer


class ChatCompletionHandler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.server.received.append({'path':self.path,'headers':dict(self.headers),'raw':raw})
        self.server.entered.set()
        if self.server.release is not None: self.server.release.wait(3)
        if self.server.delay: time.sleep(self.server.delay)
        raw_response = self.server.raw_response
        if raw_response is None:
            answer = json.dumps({'explanation':'学校模型接口测试建议；未执行。',
                                 'sql':self.server.suggestion,'suggestions':['核验编译计划'],
                                 'warnings':[]},ensure_ascii=False)
            raw_response = json.dumps({'choices':[{'finish_reason':'stop','message':{
                'role':'assistant','content':answer,'reasoning_content':'private-reasoning-fixture'}}]},
                ensure_ascii=False).encode('utf-8')
        self.send_response(self.server.status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw_response)))
        self.end_headers()
        try: self.wfile.write(raw_response)
        except OSError: pass


class AIHTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.directory.name)/'db')
        self.db.execute("CREATE TABLE students(id INTEGER PRIMARY KEY,"
                        "name TEXT DEFAULT 'private-default-fixture',gpa REAL);"
                        "INSERT INTO students VALUES (1,'private-row-fixture',3.9),"
                        "(2,'another-private-row',3.6)")
        self.provider = ThreadingHTTPServer(('127.0.0.1',0),ChatCompletionHandler)
        self.provider.daemon_threads = True
        self.provider.received = []
        self.provider.entered = threading.Event()
        self.provider.release = None
        self.provider.delay = 0
        self.provider.status = 200
        self.provider.raw_response = None
        self.provider.suggestion = 'SELECT id,name,gpa FROM students WHERE gpa>=3.8 ORDER BY id;'
        self.provider_thread = threading.Thread(target=self.provider.serve_forever,
                                                kwargs={'poll_interval':0.01},daemon=True)
        self.provider_thread.start()
        self.api_key = 'private-school-credential-fixture'
        self.provider_url = 'http://127.0.0.1:'+str(self.provider.server_port)+'/v1'
        self.workbench = WorkbenchServer(self.db,port=0,ai_service=self.service())
        self.workbench_thread = threading.Thread(target=self.workbench.serve_forever,
                                                 kwargs={'poll_interval':0.01},daemon=True)
        self.workbench_thread.start()

    def tearDown(self):
        if self.provider.release is not None: self.provider.release.set()
        self.workbench.shutdown(); self.workbench.server_close()
        self.workbench_thread.join(timeout=3)
        self.provider.shutdown(); self.provider.server_close()
        self.provider_thread.join(timeout=3)
        self.db.close()
        self.directory.cleanup()

    def service(self, timeout=2):
        return AIService(mode='remote',base_url=self.provider_url,
                         model='campus-arbitrary-model',api_key=self.api_key,timeout=timeout)

    def request(self, route, payload=None, timeout=3):
        connection = http.client.HTTPConnection('127.0.0.1',self.workbench.server_port,timeout=timeout)
        try:
            raw = json.dumps(payload,ensure_ascii=False).encode('utf-8') if payload is not None else None
            headers = {'Content-Type':'application/json','X-Aurora-Token':self.workbench.token}
            connection.request('POST' if payload is not None else 'GET',route,raw,headers)
            response = connection.getresponse()
            return response.status,json.loads(response.read())
        finally: connection.close()

    def advise(self, action='generate', prompt='查询高绩点学生', sql=''):
        return self.request('/api/ai',{'action':action,'prompt':prompt,'sql':sql})

    def test_client_context_is_rejected_before_provider_or_database_access(self):
        with patch.object(self.db,'execute',wraps=self.db.execute) as execute, \
             patch.object(self.db,'_read',wraps=self.db._read) as read:
            status,answer = self.request('/api/ai',{
                'action':'explain','sql':'SELECT id FROM students',
                'context':{'schema':[{'name':'injected-table'}],
                           'rows':[['injected-private-row']],'api_key':'injected-key'},
            })
            self.assertEqual(status,400,answer)
            self.assertEqual(execute.call_count,0)
            self.assertEqual(read.call_count,0)
        self.assertEqual(self.provider.received,[])
        self.assertNotIn('injected-private-row',json.dumps(answer))
        self.assertNotIn('injected-key',json.dumps(answer))

    def test_real_pipeline_receives_schema_and_plan_without_rows_defaults_or_credentials(self):
        query = 'SELECT id,name,gpa FROM students WHERE id=1'
        with patch.object(self.db,'execute',wraps=self.db.execute) as execute, \
             patch.object(self.db,'_read',wraps=self.db._read) as read:
            status,answer = self.request('/api/ai',{
                'action':'explain','sql':query,
            })
            self.assertEqual(status,200,answer)
            self.assertEqual(execute.call_count,0)
            self.assertEqual(read.call_count,0)
        self.assertEqual(len(self.provider.received),1)
        received = self.provider.received[0]
        self.assertEqual(received['path'],'/v1/chat/completions')
        self.assertEqual(received['headers']['Authorization'],'Bearer '+self.api_key)
        payload = json.loads(received['raw'])
        self.assertEqual(payload['model'],'campus-arbitrary-model')
        self.assertIs(payload['stream'],False)
        user = json.loads(payload['messages'][1]['content'])
        self.assertEqual(user['sql'],query)
        context = user['context']
        self.assertEqual({table['name'] for table in context['schema']},{'students'})
        self.assertEqual({column['name'] for column in context['schema'][0]['columns']},{'id','name','gpa'})
        self.assertIsInstance(context['plan'],dict)
        self.assertIn('operator',context['plan'])
        encoded = received['raw'].decode('utf-8')
        for private in ('private-row-fixture','another-private-row','private-default-fixture',
                        self.api_key,self.workbench.token,'injected-table','injected-private-row',
                        'injected-key','"default"','"rows"','"api_key"'):
            self.assertNotIn(private,encoded)
        self.assertEqual(answer['mode'],'remote')
        self.assertEqual(answer['model'],'campus-arbitrary-model')
        self.assertTrue(answer['validation']['valid'])
        self.assertTrue(answer['validation']['read_only'])
        self.assertEqual(answer['validation']['kind'],'select')
        self.assertIn('plan',answer['validation'])
        self.assertNotIn('private-reasoning-fixture',json.dumps(answer))
        self.assertNotIn('rows',answer)

    def test_remote_generation_is_compiled_locally_and_never_executed(self):
        with patch.object(self.db,'execute',wraps=self.db.execute) as execute, \
             patch.object(self.db,'_read',wraps=self.db._read) as read:
            status,answer = self.advise()
            self.assertEqual(status,200,answer)
            self.assertTrue(answer['validation']['valid'])
            self.assertEqual(execute.call_count,0)
            self.assertEqual(read.call_count,0)
        self.assertEqual(answer['sql'],self.provider.suggestion)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM students')[0]['rows'],[[2]])

    def test_unsupported_unknown_and_write_suggestions_cannot_mutate_active_transaction(self):
        self.db.execute('BEGIN; UPDATE students SET gpa=4.0 WHERE id=1;'
                        'INSERT INTO students(id,gpa) VALUES (3,3.7)')
        staged = copy.deepcopy(self.db.pager._staged)
        catalog = copy.deepcopy(self.db.catalog)
        suggestions = ('SELECT missing_column FROM students;',
                       'SELECT id FROM missing_table;',
                       'SELECT id FROM students WHERE (',
                       'WITH x AS (SELECT 1) SELECT * FROM x;',
                       'DELETE FROM students;',
                       'SELECT id FROM students; DELETE FROM students;',
                       'COMMIT;')
        for sql in suggestions:
            with self.subTest(sql=sql):
                self.provider.suggestion = sql
                with patch.object(self.db,'execute',wraps=self.db.execute) as execute:
                    status,answer = self.advise()
                    self.assertEqual(status,200,answer)
                    self.assertFalse(answer['validation']['valid'])
                    self.assertFalse(answer['validation']['read_only'])
                    self.assertEqual(execute.call_count,0)
                self.assertTrue(self.db.active)
                self.assertEqual(self.db.pager._staged,staged)
                self.assertEqual(self.db.catalog,catalog)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM students')[0]['rows'],[[3]])
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM students')[0]['rows'],[[2]])

    def test_all_actions_and_invalid_input_preview_preserve_uncommitted_work(self):
        self.db.execute('BEGIN; UPDATE students SET gpa=4.0 WHERE id=1')
        staged = copy.deepcopy(self.db.pager._staged)
        for action in ('generate','explain','optimize'):
            with self.subTest(action=action):
                status,answer = self.advise(action,sql='' if action=='generate' else 'SELECT bad FROM students')
                self.assertEqual(status,200,answer)
                self.assertTrue(answer['validation']['valid'])
                self.assertEqual(json.loads(json.loads(self.provider.received[-1]['raw'])['messages'][1]['content'])['action'],action)
                self.assertTrue(self.db.active)
                self.assertEqual(self.db.pager._staged,staged)
        self.db.execute('ROLLBACK')

    def test_provider_failures_leave_transaction_and_normal_sql_available(self):
        self.db.execute('BEGIN; UPDATE students SET gpa=4.0 WHERE id=1')
        staged = copy.deepcopy(self.db.pager._staged)
        failures = ((503,b'private-provider-body '+self.api_key.encode(),0),
                    (200,b'private-provider-body invalid JSON',0),
                    (200,None,0.35))
        for provider_status,raw,delay in failures:
            with self.subTest(provider_status=provider_status,delay=delay):
                self.provider.status,self.provider.raw_response,self.provider.delay = provider_status,raw,delay
                self.workbench.ai = self.service(timeout=0.1 if delay else 2)
                status,answer = self.advise()
                self.assertEqual(status,502,answer)
                error = answer['error']
                for private in ('private-provider-body',self.api_key,self.provider_url):
                    self.assertNotIn(private,error)
                status,data = self.request('/api/query',{'sql':'SELECT COUNT(*) FROM students'})
                self.assertEqual(status,200,data)
                self.assertEqual(data['results'][0]['rows'],[[2]])
                self.assertTrue(self.db.active)
                self.assertEqual(self.db.pager._staged,staged)
        self.provider.status,self.provider.raw_response,self.provider.delay = 200,None,0
        self.workbench.ai = self.service()
        self.assertEqual(self.advise()[0],200)  # Request permits are released after faults.
        self.db.execute('ROLLBACK')

    def test_remote_http_wait_does_not_hold_database_lock(self):
        self.provider.release = threading.Event()
        self.workbench.ai = self.service(timeout=3)
        result = []
        caller = threading.Thread(target=lambda:result.append(self.advise()),daemon=True)
        caller.start()
        try:
            self.assertTrue(self.provider.entered.wait(1),'Provider did not receive the actual HTTP call')
            status,data = self.request('/api/query',{'sql':'SELECT COUNT(*) FROM students'},timeout=1)
            self.assertEqual(status,200,data)
            self.assertEqual(data['results'][0]['rows'],[[2]])
        finally:
            self.provider.release.set()
            caller.join(timeout=4)
        self.assertFalse(caller.is_alive())
        self.assertEqual(result[0][0],200,result)

    def test_public_remote_status_has_no_endpoint_or_credentials(self):
        status,data = self.request('/api/ai/status')
        self.assertEqual(status,200)
        self.assertEqual(data['mode'],'remote')
        self.assertTrue(data['available'])
        self.assertEqual(data['model'],'campus-arbitrary-model')
        serialized = json.dumps(data)
        self.assertNotIn(self.api_key,serialized)
        self.assertNotIn(self.provider_url,serialized)
        self.assertNotIn('api_key',data)
        self.assertNotIn('base_url',data)


if __name__=='__main__': unittest.main()
