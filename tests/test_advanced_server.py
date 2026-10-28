"""HTTP integration: suggestions are never execution or transaction control."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from auroradb.ai import AIError, AIRateLimitError, AIService
from auroradb.__main__ import load_ai_config
from auroradb.engine import Database
from auroradb.server import WorkbenchServer, resource_directory


class AdvancedServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name)/'db',cache_size=8)
        self.db.execute((resource_directory()/'examples'/'demo.sql').read_text(encoding='utf-8'))
        self.server = WorkbenchServer(self.db,port=0,ai_service=AIService(mode='mock'))
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.db.close()
        self.temp.cleanup()

    def request(self,method,path,payload=None,headers=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        body = json.dumps(payload).encode('utf-8') if payload is not None else None
        fields = {'Content-Type':'application/json','X-Aurora-Token':self.server.token}
        fields.update(headers or {})
        connection.request(method,path,body,fields)
        response = connection.getresponse()
        status,data = response.status,json.loads(response.read())
        connection.close()
        return status,data

    def test_real_integrity_and_comparison_http(self):
        status,data = self.request('POST','/api/integrity',{})
        self.assertEqual(status,200)
        self.assertTrue(data['ok'],data['issues'])
        self.assertEqual(data['summary']['tables'],3)
        self.assertEqual(data['summary']['rows'],36)
        status,data = self.request('POST','/api/compare',{'sql':'EXPLAIN ANALYZE SELECT id FROM students WHERE id=1005'})
        self.assertEqual(status,200,data)
        self.assertTrue(data['matched'])
        self.assertEqual(data['scan']['stats']['rows_scanned'],12)
        self.assertEqual(data['optimized']['stats']['rows_scanned'],1)

    def test_all_new_posts_require_token_and_same_origin(self):
        for route,payload in [('/api/ai',{'action':'generate','prompt':'查看学生'}),
                              ('/api/integrity',{}),('/api/compare',{'sql':'SELECT 1'})]:
            for fields in ({'X-Aurora-Token':'wrong'},{'Origin':'https://other.example'}):
                with self.subTest(route=route,fields=fields):
                    self.assertEqual(self.request('POST',route,payload,fields)[0],403)

    def test_rate_limit_is_429_and_preserves_following_sql(self):
        error = AIRateLimitError('AI 调用过于频繁，请稍后重试。', 12)
        for route, payload in (
                ('/api/ai', {'action':'generate','prompt':'查看学生'}),
                ('/api/research/optimize', {'sql':'SELECT id FROM students','use_ai':True})):
            with self.subTest(route=route), patch.object(self.server.ai,'assist',side_effect=error):
                status,data = self.request('POST',route,payload)
                self.assertEqual(status,429)
                self.assertEqual(data['retry_after_seconds'],12)
        with patch.object(self.server.ai,'semantic_batch',side_effect=error):
            status,data = self.request('POST','/api/research/semantic',{
                'sql':'SELECT title FROM courses','column':'title','prompt':'关键词：数据库'})
            self.assertEqual(status,429)
            self.assertEqual(data['retry_after_seconds'],12)
        status,data = self.request('POST','/api/query',{'sql':'SELECT COUNT(*) FROM students'})
        self.assertEqual(status,200)
        self.assertEqual(data['results'][0]['rows'],[[12]])

    def test_mock_actions_compile_without_executing_or_changing_data(self):
        self.db.execute('BEGIN; UPDATE students SET gpa=4.0 WHERE id=1001')
        before = copy.deepcopy(self.db.pager._staged)
        for action in ('generate','explain','optimize'):
            status,data = self.request('POST','/api/ai',{'action':action,'prompt':'查询绩点不低于3.8的学生',
                                                        'sql':'' if action=='generate' else 'SELECT id,name FROM students WHERE id=1005'})
            self.assertEqual(status,200,data)
            self.assertEqual(data['mode'],'mock')
            self.assertIn('validation',data)
            if data['sql']: self.assertTrue(data['validation']['valid'],data['validation'])
            self.assertTrue(self.db.active)
            self.assertEqual(self.db.pager._staged,before)
        self.db.execute('ROLLBACK')

    def test_untrusted_model_script_is_not_executed_or_importable(self):
        class UnsafeModel:
            def status(self): return {'mode':'remote'}
            def assist(self,*args,**kwargs):
                return {'provider':'school','mode':'remote','model':'fixture','explanation':'<script>fake</script>',
                        'sql':'SELECT 1; DELETE FROM students;','suggestions':[],'warnings':[]}
        self.server.ai = UnsafeModel()
        self.db.execute('BEGIN; UPDATE students SET gpa=4.0 WHERE id=1001')
        before = copy.deepcopy(self.db.pager._staged)
        status,data = self.request('POST','/api/ai',{'action':'generate','prompt':'hello'})
        self.assertEqual(status,200)
        self.assertFalse(data['validation']['valid'])
        self.assertFalse(data['validation']['read_only'])
        self.assertEqual(self.db.pager._staged,before)
        self.assertTrue(self.db.active)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM students')[0]['rows'],[[12]])
        self.db.execute('ROLLBACK')

    def test_context_contains_metadata_and_plan_but_no_rows_or_defaults(self):
        captured = {}
        class CaptureModel:
            def status(self): return {'mode':'remote'}
            def assist(self,*args,**kwargs):
                captured.update(kwargs['context'])
                return {'provider':'school','mode':'remote','model':'fixture','explanation':'OK',
                        'sql':'SELECT id FROM students','suggestions':[],'warnings':[]}
        self.server.ai = CaptureModel()
        status,data = self.request('POST','/api/ai',{'action':'explain','sql':'SELECT id FROM students WHERE id=1005'})
        self.assertEqual(status,200)
        self.assertIsInstance(captured['plan'],dict)
        serialized = json.dumps(captured,ensure_ascii=False)
        for field in ('"rows":','"default":','api_key','pages.dat'):
            self.assertNotIn(field,serialized)

    def test_network_wait_does_not_lock_database(self):
        entered,release = threading.Event(),threading.Event()
        class SlowModel:
            def status(self): return {'mode':'remote'}
            def assist(self,*args,**kwargs):
                entered.set()
                if not release.wait(4): raise AIError('Fixture timeout')
                return {'provider':'school','mode':'remote','model':'fixture','explanation':'OK',
                        'sql':'SELECT id FROM students','suggestions':[],'warnings':[]}
        self.server.ai = SlowModel()
        response = []
        caller = threading.Thread(target=lambda:response.append(self.request('POST','/api/ai',{'action':'generate','prompt':'学生'})))
        caller.start()
        try:
            self.assertTrue(entered.wait(2))
            status,data = self.request('POST','/api/query',{'sql':'SELECT COUNT(*) FROM students'})
            self.assertEqual(status,200)
            self.assertEqual(data['results'][0]['rows'],[[12]])
        finally:
            release.set()
            caller.join(timeout=5)
        self.assertEqual(response[0][0],200)

    def test_disabled_and_bad_input_leave_sql_usable(self):
        self.server.ai = AIService(mode='disabled')
        status,data = self.request('GET','/api/ai/status')
        self.assertEqual(status,200)
        self.assertFalse(data['available'])
        self.assertEqual(self.request('POST','/api/ai',{'action':'generate','prompt':'hello'})[0],400)
        for payload in ({'action':'unknown'},{'action':'generate','prompt':False},
                        {'action':'generate','prompt':'x'*12001}):
            self.assertEqual(self.request('POST','/api/ai',payload)[0],400)
        self.assertEqual(self.request('POST','/api/query',{'sql':'SELECT 1'})[0],200)

    def test_local_config_secrets_are_absent_from_public_status(self):
        file = Path(self.temp.name)/'ai.local.json'
        file.write_text(json.dumps({'mode':'remote','base_url':'http://127.0.0.1:8000/v1',
                                    'model':'school-model','api_key':'fixture-secret'}),encoding='utf-8')
        self.server.ai = load_ai_config(file)
        status,data = self.request('GET','/api/ai/status')
        self.assertEqual(status,200)
        self.assertNotIn('fixture-secret',json.dumps(data))
        self.assertNotIn('base_url',data)
        self.assertEqual(data['model'],'school-model')
        file.write_text('{invalid',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'AI 配置'): load_ai_config(file)


if __name__=='__main__': unittest.main()
