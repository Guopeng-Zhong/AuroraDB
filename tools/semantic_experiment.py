"""Reproducible batching/cache experiment using a labelled local HTTP fixture.

This is a transport/cost experiment, not a measurement of school-model accuracy.
All connections are loopback; no API key, endpoint or source data are persisted.
"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from auroradb.ai import AIService
from auroradb.engine import Database
from auroradb.semantic import SemanticExecutor


class Fixture(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        records=json.loads(body['messages'][1]['content'])['records']
        # Artificial fixed request overhead makes the workload reproducible;
        # timings below must not be presented as a real LLM inference benchmark.
        time.sleep(.003)
        values=[{'id':r['id'],'match':'数据库' in r['text'],
                 'label':'数据库' if '数据库' in r['text'] else '其他'} for r in records]
        content=json.dumps({'results':values},ensure_ascii=False)
        response=json.dumps({'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':content}}]},
                            ensure_ascii=False).encode('utf-8')
        self.send_response(200); self.send_header('Content-Length',str(len(response))); self.end_headers()
        self.wfile.write(response)


def main():
    provider=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
    thread=threading.Thread(target=provider.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    thread.start()
    try:
        service=AIService(mode='remote',base_url=f'http://127.0.0.1:{provider.server_port}/v1',
                          model='local-keyword-protocol-fixture',timeout=2)
        with tempfile.TemporaryDirectory() as directory,Database(directory) as db:
            db.execute('CREATE TABLE papers(id INTEGER PRIMARY KEY,title TEXT); BEGIN;')
            for i in range(64):
                j=i%32; title=('数据库查询优化' if j%3==0 else '系统实验')+str(j)
                db.execute(f"INSERT INTO papers VALUES({i},'{title}');")
            db.execute('COMMIT;')
            runs=[]; expected=[i for i in range(64) if (i%32)%3==0]
            executor=SemanticExecutor(service)
            for name,batch,use_cache in [('individual',1,False),('batched_cold',8,True),('batched_warm',8,True)]:
                result=executor.run(db,'SELECT id,title FROM papers ORDER BY id','title',
                                    '包含数据库主题',batch_size=batch,max_rows=64,max_calls=32,use_cache=use_cache)
                ids=[r[0] for r in result['rows']]
                if ids!=expected: raise AssertionError('Fixture/operator result mismatch')
                runs.append({'name':name,'batch_size':batch,'rows':result['row_count'],
                             'fixture_agreement':True,'stats':result['stats']})
            for operation in ('classify','aggregate'):
                result=executor.run(db,'SELECT id,title FROM papers','title','包含数据库主题',operation=operation)
                if operation=='classify': assert result['row_count']==64
                else: assert sum(r[1] for r in result['rows'])==64
            value={'timestamp':datetime.now(timezone.utc).isoformat(),'seed':20261010,
                   'provider':'local deterministic keyword HTTP fixture',
                   'school_api_measured':False,'school_model_accuracy':None,
                   'input_rows':64,'unique_texts':32,'fixture_delay_ms':3,'passed':True,'runs':runs,
                   'limitations':['仅测批处理/精确缓存/协议，不测真实大模型语义准确率。',
                                  '调用计数包含本地HTTP假服务，耗时包含人为3ms请求开销。',
                                  '一次SELECT物化后推理；普通SQL语法未增加模型函数。'],
                   'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                                    for p in ['auroradb/semantic.py','auroradb/ai.py','auroradb/engine.py']}}
            (ROOT/'docs/semantic_research.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'passed':True,'calls':[r['stats']['model_calls'] for r in runs],
                              'fixture_only':True},ensure_ascii=False))
    finally:
        provider.shutdown(); provider.server_close(); thread.join(3)


if __name__=='__main__': main()
