"""Semantic operators: typed remote protocol, budgets, snapshots and exact cache."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from auroradb.ai import AIError,AIService
from auroradb.engine import Database,DatabaseError
from auroradb.semantic import SemanticExecutor
from auroradb.server import WorkbenchServer


class SemanticFixtureHandler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.received.append(request)
        self.server.entered.set()
        if self.server.release is not None: self.server.release.wait(2)
        if self.server.delay: time.sleep(self.server.delay)
        user=json.loads(request['messages'][1]['content'])
        values=[{'id':r['id'],'match':'数据库' in r['text'],
                 'label':'数据库' if '数据库' in r['text'] else '其他'} for r in user['records']]
        if self.server.variant=='reverse': values.reverse()
        elif self.server.variant=='duplicate': values[-1]['id']=values[0]['id']
        elif self.server.variant=='missing': values.pop()
        elif self.server.variant=='string_bool': values[0]['match']='true'
        elif self.server.variant=='credential': values[0]['label']='fixture-protected-key'
        content=json.dumps({'results':values},ensure_ascii=False)
        body=json.dumps({'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':content}}]},ensure_ascii=False).encode()
        self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except OSError: pass


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'db')
        self.db.execute("CREATE TABLE papers(id INTEGER PRIMARY KEY,title TEXT,secret TEXT);"
                        "INSERT INTO papers VALUES (1,'数据库查询优化','hidden-a'),"
                        "(2,'操作系统','hidden-b'),(3,'数据库查询优化','hidden-c'),(4,NULL,'hidden-d');")
        self.service=AIService(mode='mock')
        self.executor=SemanticExecutor(self.service)
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def run_operator(self,**kwargs):
        return self.executor.run(self.db,'SELECT id,title,secret FROM papers ORDER BY id;',
                                 'title','关键词：数据库',**kwargs)
    def test_filter_preserves_rows_duplicates_and_null_does_not_match(self):
        result=self.run_operator()
        self.assertEqual([r[0] for r in result['rows']],[1,3])
        self.assertEqual(result['stats']['unique_texts'],2)
        self.assertEqual(result['stats']['deduplicated_rows'],1)
        self.assertEqual(result['stats']['null_rows'],1)
        self.assertEqual(result['stats']['model_calls'],0)
        self.assertEqual(result['plan']['operator'],'SemanticFilter')
    def test_classify_and_aggregate_are_real_relational_operators(self):
        result=self.run_operator(operation='classify')
        self.assertEqual(result['columns'][-2:],['_semantic_match','_semantic_label'])
        self.assertEqual(result['rows'][-1][-2:],[False,'NULL'])
        result=self.run_operator(operation='aggregate')
        self.assertEqual(dict(result['rows']),{'数据库':2,'其他':1,'NULL':1})
        self.assertEqual(result['plan']['operator'],'SemanticAggregate')
    def test_second_run_uses_exact_text_cache_without_changing_result(self):
        first=self.run_operator()
        second=self.run_operator()
        self.assertEqual(second['rows'],first['rows'])
        self.assertEqual(second['stats']['batches'],0)
        self.assertEqual(second['stats']['cache_hits'],2)
    def test_changed_prompt_and_text_cannot_reuse_old_decisions(self):
        self.run_operator()
        result=self.executor.run(self.db,'SELECT title FROM papers','title','关键词：操作')
        self.assertEqual(result['rows'],[['操作系统']])
        self.assertEqual(result['stats']['cache_hits'],0)
        self.db.execute("UPDATE papers SET title='操作系统' WHERE id=1;")
        result=self.run_operator()
        self.assertEqual([r[0] for r in result['rows']],[3])
    def test_capacity_ttl_and_clear(self):
        with patch('auroradb.semantic.time.monotonic',return_value=100.) as monotonic:
            self.executor=SemanticExecutor(self.service,capacity=1,ttl_seconds=2.)
            self.run_operator(); self.assertEqual(self.executor.status()['entries'],1)
            self.assertEqual(self.executor.status()['evictions'],1)
            monotonic.return_value=102.01
            self.assertEqual(self.executor.status()['entries'],0)
            self.run_operator(); self.assertEqual(self.executor.clear()['entries'],0)
    def test_budgets_fail_before_inference(self):
        for kwargs in [{'max_rows':2},{'max_calls':1,'batch_size':1},{'max_rows':True},
                       {'batch_size':17},{'max_calls':0},{'budget_seconds':float('nan')},
                       {'use_cache':'true'}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(DatabaseError): self.run_operator(**kwargs)
        self.assertEqual(self.executor.status()['entries'],0)
    def test_text_and_output_column_validation(self):
        for sql,col in [('SELECT id FROM papers','id'),('SELECT title,title FROM papers','title'),
                        ('SELECT title FROM papers','missing')]:
            with self.subTest(sql=sql),self.assertRaises(DatabaseError):
                self.executor.run(self.db,sql,col,'关键词：数据库')
        self.db.execute("UPDATE papers SET title='"+('a'*2049)+"' WHERE id=1;")
        with self.assertRaises(DatabaseError): self.run_operator()
    def test_read_only_invalid_diagnostic_does_not_abort_transaction(self):
        self.db.execute('BEGIN;')
        with self.assertRaises(DatabaseError): self.run_operator()
        self.assertTrue(self.db.active)
        with self.assertRaises(DatabaseError):
            self.executor.run(self.db,'DELETE FROM papers','title','关键词：数据库')
        self.assertTrue(self.db.active)
        self.db.execute('ROLLBACK;')
        with self.assertRaises(DatabaseError):
            self.executor.run(self.db,'DELETE FROM papers','title','关键词：数据库')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM papers')[0]['rows'],[[4]])
    def test_mock_is_explicit_and_rejects_arbitrary_natural_language(self):
        with self.assertRaises(AIError):
            self.executor.run(self.db,'SELECT title FROM papers','title','找出前沿数据库论文')
        self.assertIn('没有调用大模型',self.run_operator()['explanation'])
    def test_no_semantic_column_collision(self):
        with self.assertRaises(DatabaseError):
            self.executor.run(self.db,'SELECT title,1 AS _semantic_label FROM papers',
                              'title','关键词：数据库',operation='classify')

    def test_semantic_result_parser_validates_content_without_network(self):
        service=AIService(mode='remote',base_url='https://example.com/v1',model='fixture')
        content=json.dumps({'results':[
            {'id':1,'match':False,'label':'其他'},
            {'id':0,'match':True,'label':'数据库'},
        ]},ensure_ascii=False)
        with patch.object(service,'_request',side_effect=AssertionError('unexpected network call')):
            result=service._parse_semantic_result(content,['数据库','操作系统'])
            self.assertEqual(result,[{'id':0,'match':True,'label':'数据库'},
                                     {'id':1,'match':False,'label':'其他'}])
            invalid=json.dumps({'results':[{'id':0,'match':'true','label':'数据库'}]})
            with self.assertRaises(AIError):
                service._parse_semantic_result(invalid,['数据库'])


class SemanticHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=Database(Path(self.tmp.name)/'db')
        self.db.execute("CREATE TABLE t(id INTEGER,title TEXT,secret TEXT);"
                        "INSERT INTO t VALUES(1,'数据库','private-data'),(2,'系统','private-b');")
        self.provider=ThreadingHTTPServer(('127.0.0.1',0),SemanticFixtureHandler)
        self.provider.daemon_threads=True; self.provider.received=[]; self.provider.delay=0
        self.provider.release=None; self.provider.entered=threading.Event(); self.provider.variant='reverse'
        self.thread=threading.Thread(target=self.provider.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()
        self.service=AIService(mode='remote',base_url=f'http://127.0.0.1:{self.provider.server_port}/v1',
                               model='school-fixture',api_key='fixture-protected-key',timeout=2)
        self.workbench=WorkbenchServer(self.db,0,ai_service=self.service)
        self.worker=threading.Thread(target=self.workbench.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.worker.start()
    def tearDown(self):
        if self.provider.release: self.provider.release.set()
        self.workbench.shutdown(); self.workbench.server_close(); self.worker.join(3)
        self.provider.shutdown(); self.provider.server_close(); self.thread.join(3)
        self.db.close(); self.tmp.cleanup()
    def request(self,payload=None,route='/api/research/semantic',token=True):
        conn=http.client.HTTPConnection('127.0.0.1',self.workbench.server_port,timeout=4)
        if payload is None: payload={'sql':'SELECT id,title,secret FROM t ORDER BY id','column':'title','prompt':'数据库研究'}
        headers={'Content-Type':'application/json'}
        if token: headers['X-Aurora-Token']=self.workbench.token
        conn.request('POST',route,json.dumps(payload).encode(),headers)
        response=conn.getresponse(); value=json.loads(response.read()); status=response.status
        conn.close(); return status,value
    def test_real_http_adapter_sends_only_selected_text_and_restores_ids(self):
        status,result=self.request()
        self.assertEqual(status,200,result); self.assertEqual(result['rows'],[[1,'数据库','private-data']])
        self.assertEqual(result['stats']['model_calls'],1)
        user=json.loads(self.provider.received[0]['messages'][1]['content'])
        self.assertEqual(user['records'],[{'id':0,'text':'数据库'},{'id':1,'text':'系统'}])
        self.assertNotIn('private-data',json.dumps(self.provider.received,ensure_ascii=False))
        self.assertNotIn('SELECT',json.dumps(user,ensure_ascii=False))
        status,second=self.request(); self.assertEqual(status,200)
        self.assertEqual(second['stats']['model_calls'],0)
    def test_invalid_provider_decisions_are_not_cached(self):
        for variant in ['duplicate','missing','string_bool','credential']:
            self.provider.variant=variant
            with self.subTest(variant=variant):
                status,result=self.request(); self.assertEqual(status,502,result)
                self.assertNotIn('fixture-protected-key',json.dumps(result))
                self.assertEqual(self.workbench.semantic.status()['entries'],0)
    def test_total_inference_budget_enforced_and_db_remains_available(self):
        self.provider.delay=.3
        status,result=self.request({'sql':'SELECT title FROM t','column':'title','prompt':'test','budget_seconds':.1})
        self.assertEqual(status,502,result)
        status,result=self.request({'sql':'SELECT COUNT(*) FROM t'},'/api/query')
        self.assertEqual(status,200); self.assertEqual(result['results'][0]['rows'],[[2]])
    def test_model_wait_does_not_hold_database_lock_and_result_is_snapshot(self):
        self.provider.release=threading.Event()
        output=[]; worker=threading.Thread(target=lambda:output.append(self.request()))
        worker.start(); self.assertTrue(self.provider.entered.wait(1))
        status,_=self.request({'sql':"UPDATE t SET title='changed' WHERE id=1"},'/api/query')
        self.assertEqual(status,200)
        self.provider.release.set(); worker.join(3)
        self.assertEqual(output[0][1]['rows'],[[1,'数据库','private-data']])
    def test_csrf_and_invalid_inputs_cannot_call_provider(self):
        self.assertEqual(self.request(token=False)[0],403)
        self.assertEqual(self.request({'sql':'DELETE FROM t','column':'title','prompt':'x'})[0],400)
        self.assertEqual(self.request({'sql':'SELECT title FROM t','column':'title','prompt':'x','secret':'y'})[0],400)
        self.assertEqual(self.provider.received,[])
    def test_clear_cache_endpoint(self):
        self.request()
        status,result=self.request({},'/api/research/semantic/cache')
        self.assertEqual(status,200); self.assertEqual(result,{'cleared':True,'entries':0})


if __name__=='__main__': unittest.main()
