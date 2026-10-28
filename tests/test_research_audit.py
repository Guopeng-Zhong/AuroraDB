"""Independent interaction regressions for bounded research diagnostics."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from auroradb.ai import AIError, AIService
from auroradb.engine import Database, DatabaseError
from auroradb.semantic import SemanticExecutor
from auroradb.server import WorkbenchHandler


class ResearchInteractionAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name, cache_size=8)
        self.db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, n INTEGER, title TEXT, secret TEXT);"
                        "INSERT INTO t VALUES(1,1,'database','hidden-A'),(2,1,'systems','hidden-B'),"
                        "(3,NULL,'database','hidden-C'),(4,NULL,NULL,'hidden-D');")
        self.service = AIService(mode="mock")
        self.semantic = SemanticExecutor(self.service)
        self.provider_calls = []
        def advice(*args, **kwargs):
            self.provider_calls.append((args, kwargs))
            return {"sql": "SELECT id,title FROM t", "explanation": "audit fixture"}
        self.service.assist = advice
        self.server = SimpleNamespace(database=self.db, database_lock=threading.RLock(),
                                      ai=self.service, ai_gate=threading.BoundedSemaphore(2),
                                      semantic=self.semantic)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def dispatch(self, route, payload):
        # Exercise the real route implementation without a socket or model API.
        handler = object.__new__(WorkbenchHandler)
        handler.server = self.server
        responses = []
        handler._json = lambda status, value: responses.append((status, value))
        handler._research(route, payload)
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_http_buffer_switch_preserves_active_transaction_and_policy(self):
        self.db.execute("BEGIN; INSERT INTO t VALUES(5,1,'pending','hidden-E');")
        status, result = self.dispatch("/api/research/buffer", {"policy": "predictive", "sample_size": 4})
        self.assertEqual(status, 400, result)
        self.assertTrue(self.db.active)
        self.assertEqual(self.db.pager.cache_policy_status()["policy"], "lru")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM t")[0]["rows"], [[5]])

    def test_http_buffer_switch_immediately_invalidates_learned_conclusions(self):
        self.db.analyze_statistics()
        self.db.optimize_query("SELECT id FROM t WHERE n=1 OR n=2")
        self.assertTrue(self.db._research.cache)
        status, result = self.dispatch("/api/research/buffer", {"policy": "predictive", "sample_size": 4})
        self.assertEqual(status, 200, result)
        # Do not call research_status here: its lazy sync would mask a route bug.
        self.assertFalse(self.db._research.cache)
        self.assertTrue(self.db._research.tables)
        self.assertEqual(self.db._research.buffer_signature, ("predictive", 4, 8))

    def test_provider_not_called_for_active_transaction_or_limit(self):
        self.db.execute("BEGIN; INSERT INTO t VALUES(5,1,'pending','hidden-E');")
        status, result = self.dispatch("/api/research/optimize", {"sql": "SELECT id,title FROM t", "use_ai": True})
        self.assertEqual(status, 400, result)
        self.assertTrue(self.db.active)
        self.assertEqual(self.provider_calls, [])
        self.db.execute("ROLLBACK")
        for sql in ("SELECT id FROM t LIMIT 1", "SELECT id FROM t OFFSET 1", "DELETE FROM t"):
            with self.subTest(sql=sql):
                status, result = self.dispatch("/api/research/optimize", {"sql": sql, "use_ai": True})
                self.assertEqual(status, 400, result)
                self.assertEqual(self.provider_calls, [])

    def test_bad_read_diagnostics_never_abort_or_publish_pending_rows(self):
        before = (Path(self.temp.name)/"pages.dat").read_bytes()
        self.db.execute("BEGIN; INSERT INTO t VALUES(5,1,'pending','hidden-E');")
        for action in (lambda: self.db.preview_query("SELECT missing FROM t"),
                       lambda: self.db.optimize_query("SELECT id FROM t"),
                       lambda: self.semantic.run(self.db, "SELECT title FROM t", "title", "关键词：database")):
            with self.assertRaises(DatabaseError):
                action()
            self.assertTrue(self.db.active)
            self.assertEqual((Path(self.temp.name)/"pages.dat").read_bytes(), before)
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM t")[0]["rows"], [[4]])

    def test_external_candidates_keep_bool_and_integer_outputs_distinct(self):
        for literal, numeric in (("TRUE", "1"), ("FALSE", "0")):
            with self.subTest(literal=literal):
                result = self.db.optimize_query(f"SELECT {literal} AS value", f"SELECT {numeric} AS value")
                self.assertFalse(result["verification"]["matched"])
                self.assertEqual(result["verification"]["level"], "unknown")
                self.assertFalse(result["recommendation"]["replace"])

    def test_null_bag_order_ties_and_snapshot_coincidence_are_not_proofs(self):
        result = self.db.optimize_query("SELECT n FROM t", "SELECT DISTINCT n FROM t")
        self.assertFalse(result["verification"]["matched"])
        result = self.db.optimize_query("SELECT n,title FROM t WHERE id>0 ORDER BY n",
                                        "SELECT n,title FROM t WHERE id>=0 ORDER BY n")
        self.assertTrue(result["verification"]["matched"])
        self.assertEqual(result["verification"]["level"], "tested")
        self.assertFalse(result["recommendation"]["replace"])
        changed_order = self.db.optimize_query("SELECT n,title FROM t ORDER BY n",
                                                "SELECT n,title FROM t ORDER BY n DESC")
        self.assertIsNone(changed_order["verification"]["matched"])
        self.assertFalse(changed_order["recommendation"]["replace"])

    def test_direct_integrity_scan_ignores_predictive_buffer_training(self):
        self.db.set_buffer_policy("predictive", 4)
        before = self.db.pager.stats()
        result = self.db.integrity_check()
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["summary"]["rows"], 4)
        after = self.db.pager.stats()
        for metric in ("cache_hits", "cache_misses", "cache_policy_cpu_ns", "scan_contexts",
                       "scan_page_accesses", "predictive_samples", "predictive_evictions"):
            self.assertEqual(after[metric], before[metric])
        self.assertEqual(after["active_scans"], 0)

    def test_semantic_intermediate_budget_rejects_before_provider_and_restores_db(self):
        values = ",".join(f"({i},1,'database','hidden')" for i in range(5, 61))
        self.db.execute("INSERT INTO t VALUES " + values)
        batch_calls = []
        original_batch = self.service.semantic_batch
        def batch(*args, **kwargs):
            batch_calls.append((args, kwargs))
            return original_batch(*args, **kwargs)
        self.service.semantic_batch = batch
        counters = self.db.counters
        self.assertIsNone(self.db._research_budget)
        query = "SELECT MAX(t.title) AS title FROM t JOIN t AS x ON TRUE JOIN t AS y ON TRUE"
        with self.assertRaisesRegex(DatabaseError, "预算"):
            self.semantic.run(self.db, query, "title", "关键词：database", max_rows=1)
        self.assertEqual(batch_calls, [])
        self.assertIs(self.db.counters, counters)
        self.assertIsNone(self.db._research_budget)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM t")[0]["rows"], [[60]])

    def test_semantic_query_stats_do_not_misrepresent_gauges_as_counters(self):
        result = self.semantic.run(self.db, "SELECT title FROM t", "title", "关键词：database")
        for name in ("page_size", "predictive_sample_size", "active_scans", "cache_history_pages", "cached_pages"):
            self.assertNotIn(name, result["stats"]["query"])


class SemanticProtocolAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name)
        self.db.execute("CREATE TABLE t(id INTEGER,title TEXT,secret TEXT);"
                        "INSERT INTO t VALUES(1,'first','hidden-A'),(2,'second','hidden-B'),"
                        "(3,'first','hidden-C'),(4,NULL,'hidden-D');")
        self.service = AIService(mode="remote", base_url="http://127.0.0.1:1/v1", model="audit-v1",
                                 api_key="private-audit-token", timeout=1)
        self.requests = []
        def reply(body, timeout=None):
            request = json.loads(body)
            self.requests.append(request)
            records = json.loads(request["messages"][1]["content"])["records"]
            decisions = [{"id": item["id"], "match": item["text"] == "first",
                          "label": request["model"] + ":" + item["text"]} for item in reversed(records)]
            return json.dumps({"results": decisions})
        self.service._request = reply
        self.executor = SemanticExecutor(self.service)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def classify(self):
        return self.executor.run(self.db, "SELECT id,title,secret FROM t ORDER BY id", "title",
                                 "classify selected text", operation="classify")

    def test_reordered_ids_duplicates_null_and_exact_selected_data_scope(self):
        result = self.classify()
        self.assertEqual([row[-2:] for row in result["rows"]],
                         [[True, "audit-v1:first"], [False, "audit-v1:second"],
                          [True, "audit-v1:first"], [False, "NULL"]])
        sent = json.loads(self.requests[0]["messages"][1]["content"])
        self.assertEqual(set(sent), {"condition", "records"})
        self.assertEqual(sent["records"], [{"id": 0, "text": "first"}, {"id": 1, "text": "second"}])
        self.assertNotIn("hidden-", json.dumps(self.requests))
        self.assertNotIn("private-audit-token", json.dumps(result))
        self.assertEqual(result["stats"]["deduplicated_rows"], 1)

    def test_model_endpoint_and_credential_change_cannot_hit_other_provider_cache(self):
        self.classify()
        self.assertEqual(self.classify()["stats"]["cache_hits"], 2)
        for attribute, value in (("model", "audit-v2"),
                                 ("_endpoint", "http://127.0.0.1:2/v1/chat/completions"),
                                 ("_api_key", "different-private-audit-token")):
            with self.subTest(attribute=attribute):
                setattr(self.service, attribute, value)
                result = self.classify()
                self.assertEqual(result["stats"]["cache_hits"], 0)
                self.assertEqual(result["stats"]["batches"], 1)
                self.assertNotIn(value if attribute != "model" else "private-audit-token", json.dumps(result))

    def test_unknown_boolean_and_duplicate_ids_cannot_train_cache(self):
        for ids in ([False, 1], [0, 2], [0, 0], [-1, 1]):
            with self.subTest(ids=ids):
                self.executor.clear()
                self.service._request = lambda body, timeout=None: json.dumps({"results": [
                    {"id": index, "match": True, "label": "label"} for index in ids]})
                with self.assertRaises(AIError):
                    self.classify()
                self.assertEqual(self.executor.status()["entries"], 0)


if __name__ == "__main__":
    unittest.main()
