"""Real HTTP and isolation checks for the PPT's live optimizer example."""
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from auroradb.engine import Database
from auroradb.optimizer_demo import QUERY
from auroradb.server import WorkbenchServer


class OptimizerDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'user')
        self.server = WorkbenchServer(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.db.close()
        self.temp.cleanup()

    def request(self, payload=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=90)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Aurora-Token'] = self.server.token
        connection.request('POST', '/api/research/join-demo',
                           json.dumps(payload or {}), headers)
        response = connection.getresponse()
        value = response.status, json.loads(response.read())
        connection.close()
        return value

    def test_live_example_preserves_user_data_transaction_models_and_counters(self):
        # A colliding user table must never be reused, modified, or rolled back.
        self.db.execute("CREATE TABLE people(secret TEXT); INSERT INTO people VALUES('private');"
                        "BEGIN; INSERT INTO people VALUES('pending')")
        before = self.db.research_status()
        counters = dict(self.db.counters)
        pager_stats = self.db.pager.stats()
        temporary_paths = []
        factory = tempfile.TemporaryDirectory

        def record_directory(*args, **kwargs):
            directory = factory(*args, **kwargs)
            temporary_paths.append(Path(directory.name))
            return directory

        with patch('auroradb.optimizer_demo.tempfile.TemporaryDirectory', record_directory):
            status, result = self.request()
        self.assertEqual(status, 200, result)
        self.assertTrue(result['matched'])
        self.assertEqual(result['sql'], QUERY + ';')
        self.assertEqual(result['repetitions'], 3)
        self.assertEqual(len(result['rows']), 100)
        self.assertEqual(result['columns'], ['p.id', 'c.id'])
        control, adaptive = result['sql_order'], result['adaptive']
        self.assertEqual(control['stats']['join_comparisons'], 20000)
        self.assertEqual(adaptive['stats']['join_comparisons'], 200)
        self.assertEqual(control['join_outputs'], [10000, 10000])
        self.assertEqual(adaptive['join_outputs'], [100, 100])
        # Timing-trained access costs can reverse the first INNER JOIN's
        # operands. Both plans join filtered people with registrations before
        # classes; the work/result assertions above must still hold.
        self.assertIn(adaptive['join_order'], (['p', 'r', 'c'], ['r', 'p', 'c']))
        for run in (control, adaptive):
            self.assertEqual(run['row_count'], 100)
            self.assertEqual(len(run['runs_ms']), 3)
            self.assertGreater(run['elapsed_ms'], 0)
        self.assertTrue(result['learning']['cardinality_models']['people.department:eq']['ready'])
        self.assertTrue(temporary_paths)
        self.assertTrue(all(not path.exists() for path in temporary_paths))
        self.assertTrue(self.db.active)
        self.assertEqual(self.db.research_status(), before)
        self.assertEqual(self.db.counters, counters)
        self.assertEqual(self.db.pager.stats(), pager_stats)
        self.assertEqual(self.db.execute('SELECT secret FROM people')[0]['rows'],
                         [['private'], ['pending']])
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.execute('SELECT secret FROM people')[0]['rows'], [['private']])

    def test_fixed_route_rejects_injection_missing_token_and_duplicate_runs(self):
        with patch('auroradb.server.run_demo') as run:
            self.assertEqual(self.request(token=False)[0], 403)
            self.assertEqual(self.request({'sql': 'DROP TABLE people'})[0], 400)
            self.server.demo_gate.acquire()
            try:
                self.assertEqual(self.request()[0], 429)
            finally:
                self.server.demo_gate.release()
            run.assert_not_called()
            run.side_effect = RuntimeError('fixture failed')
            self.assertEqual(self.request(), (400, {'error': 'fixture failed'}))
            run.side_effect = None
            run.return_value = {'matched': True}
            self.assertEqual(self.request(), (200, {'matched': True}))

    def test_displayed_sql_matches_executed_case(self):
        html = (Path(__file__).resolve().parents[1] / 'auroradb/web/index.html').read_text(encoding='utf-8')
        displayed = re.search(r'id="join-demo-sql">(.*?)</pre>', html, re.S).group(1)
        self.assertEqual(displayed, QUERY + ';')


if __name__ == '__main__':
    unittest.main()
