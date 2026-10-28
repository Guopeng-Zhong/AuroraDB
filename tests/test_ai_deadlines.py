"""Adversarial, local-only AI deadlines and decoded protocol boundaries."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock,patch

from auroradb.ai import AIError,AIService
from auroradb.engine import Database
from auroradb.semantic import SemanticExecutor


class DripProvider(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.server.requests.append(self.path)
        raw=json.dumps({'choices':[{'finish_reason':'stop','message':{'role':'assistant',
            'content':'{"sql":"SELECT 1"}'}}]}).encode()
        headers=f'HTTP/1.1 200 OK\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n'.encode()
        try:
            if self.server.drip:
                for char in headers:
                    self.connection.sendall(bytes([char]))
                    time.sleep(0.04)
            else: self.connection.sendall(headers)
            self.connection.sendall(raw)
        except OSError: pass


class AIDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),DripProvider)
        self.server.daemon_threads=True
        self.server.drip=False
        self.server.requests=[]
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
    def service(self,timeout=0.15,**kwargs):
        return AIService(mode='remote',base_url=f'http://127.0.0.1:{self.server.server_port}/v1',
            model='local-test-model',api_key='fake-private-test-key',timeout=timeout,**kwargs)
    def generate(self,service):
        return service.assist('generate',prompt='local test')
    def wait_for_transport_cleanup(self,service):
        # Both slots must be reusable after the cancelled worker unwinds.
        acquired=0
        try:
            for _ in range(2):
                self.assertTrue(service._transport_slots.acquire(timeout=2))
                acquired+=1
        finally:
            for _ in range(acquired):service._transport_slots.release()

    def test_status_exposes_only_finite_validated_timeout(self):
        self.assertEqual(self.service(timeout=250).status()['timeout_seconds'],250)
        for value in ('NaN',True,301):
            status=self.service(timeout=value).status()
            self.assertFalse(status['available'])
            self.assertEqual(status['timeout_seconds'],90)
        invalid=AIService(mode='remote',base_url='http://127.0.0.1:1',model='bad\ud800model')
        self.assertFalse(invalid.status()['available'])
        self.assertEqual(invalid.status()['model'],'')

    def test_dripping_headers_cannot_extend_total_deadline(self):
        self.server.drip=True
        service=self.service()
        start=time.monotonic()
        with self.assertRaisesRegex(AIError,'超时'):self.generate(service)
        self.assertLess(time.monotonic()-start,0.9)
        self.assertEqual(len(self.server.requests),1)
        self.wait_for_transport_cleanup(service)

    def test_dns_timeouts_are_bounded_no_late_post_and_no_unbounded_workers(self):
        service=self.service(requests_per_minute=10)
        release=threading.Event();entered=threading.Event();lock=threading.Lock()
        count=[0]
        original=socket.getaddrinfo
        def resolve(*args,**kwargs):
            with lock:
                count[0]+=1
                if count[0]==2:entered.set()
            release.wait(3)
            return original(*args,**kwargs)
        def request():
            start=time.monotonic()
            try:self.generate(service)
            except AIError as exc:return str(exc),time.monotonic()-start
            return 'unexpected success',time.monotonic()-start
        try:
            with patch('auroradb.ai.socket.getaddrinfo',side_effect=resolve),ThreadPoolExecutor(max_workers=2) as executor:
                calls=[executor.submit(request) for _ in range(2)]
                self.assertTrue(entered.wait(1))
                for call in calls:
                    message,elapsed=call.result(timeout=2)
                    self.assertIn('超时',message);self.assertLess(elapsed,0.9)
                message,elapsed=request()
                self.assertIn('超时',message);self.assertLess(elapsed,0.9)
                self.assertEqual(count[0],2)
                self.assertEqual(service.status()['requests_remaining'],8)
                release.set()
                self.wait_for_transport_cleanup(service)
        finally:release.set()
        self.assertEqual(self.server.requests,[])
        self.assertEqual(self.generate(service)['sql'],'SELECT 1')

    def test_request_write_stall_is_included_and_cancelled_before_response(self):
        service=self.service()
        connection=MagicMock();release=threading.Event()
        connection.request.side_effect=lambda *args,**kwargs:release.wait(2)
        try:
            with patch('auroradb.ai.http.client.HTTPConnection',return_value=connection):
                start=time.monotonic()
                with self.assertRaisesRegex(AIError,'超时'):self.generate(service)
                self.assertLess(time.monotonic()-start,0.9)
                self.assertTrue(connection.close.called)
                release.set();self.wait_for_transport_cleanup(service)
        finally:release.set()
        connection.getresponse.assert_not_called()

    def test_tls_handshake_stall_is_bounded_and_cannot_send_late_body(self):
        service=AIService(mode='remote',base_url='https://127.0.0.1:1/v1',model='local-test-model',timeout=0.15)
        connection=MagicMock();release=threading.Event();sent=[]
        connection.connect.side_effect=lambda:release.wait(2)
        def send(*args,**kwargs):
            connection.connect()
            sent.append('body')
        connection.request.side_effect=send
        try:
            with patch('auroradb.ai.http.client.HTTPSConnection',return_value=connection):
                start=time.monotonic()
                with self.assertRaisesRegex(AIError,'超时'):self.generate(service)
                self.assertLess(time.monotonic()-start,0.9)
                release.set();self.wait_for_transport_cleanup(service)
        finally:release.set()
        self.assertEqual(sent,[])
        connection.getresponse.assert_not_called()

    def test_invalid_timeout_override_is_a_sanitized_error_without_network(self):
        for value in (True,float('nan'),float('inf'),'invalid',0):
            with self.subTest(value=value),self.assertRaises(AIError):
                self.service().semantic_batch('condition',['text'],timeout=value)
        self.assertEqual(self.server.requests,[])

    def test_escaped_secret_and_surrogate_labels_are_rejected_before_cache(self):
        for encoded_label in ('\\u0066ake-private-test-key','\\ud800'):
            with self.subTest(encoded_label=encoded_label),tempfile.TemporaryDirectory() as directory:
                service=self.service()
                executor=SemanticExecutor(service)
                database=Database(Path(directory)/'db')
                try:
                    database.execute("CREATE TABLE t(text TEXT);INSERT INTO t VALUES ('hello');")
                    content='{"results":[{"id":0,"match":true,"label":"'+encoded_label+'"}]}'
                    with patch.object(service,'_request',return_value=content),self.assertRaises(AIError):
                        executor.run(database,'SELECT text FROM t','text','condition')
                    self.assertEqual(executor.status()['entries'],0)
                    self.assertIs(service.status()['last_request_succeeded'],False)
                finally:database.close()

    def test_lone_surrogate_request_is_rejected_without_network(self):
        with self.assertRaises(AIError):self.service().semantic_batch('condition',['\ud800'])
        self.assertEqual(self.server.requests,[])

    def test_only_final_assistant_without_legacy_function_call_is_accepted(self):
        for fields in ({'role':'tool'},{'role':'system'},{'role':None},
                       {'role':'assistant','function_call':{'name':'tool','arguments':'{}'}},
                       {'role':'assistant','function_call':{}}):
            with self.subTest(fields=fields):
                raw=json.dumps({'choices':[{'finish_reason':'stop','message':{
                    'content':'{"sql":"SELECT 1"}',**fields}}]}).encode()
                connection=MagicMock();response=MagicMock()
                response.status=200
                response.getheader.side_effect=lambda name:str(len(raw)) if name=='Content-Length' else None
                response.isclosed.side_effect=[False,True];response.read1.return_value=raw
                connection.getresponse.return_value=response
                with patch('auroradb.ai.http.client.HTTPConnection',return_value=connection),self.assertRaises(AIError):
                    self.generate(self.service())


if __name__=='__main__':unittest.main()
