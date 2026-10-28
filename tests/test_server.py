"""HTTP boundary and packaged demo regression checks using the real engine."""

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from auroradb.__main__ import complete_statement
from auroradb.engine import Database
from auroradb.server import MAX_REQUEST_BYTES, WorkbenchServer, resource_directory


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "db", cache_size=8)
        self.server = WorkbenchServer(self.database, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.database.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        default_headers = {}
        body = raw
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            default_headers["Content-Type"] = "application/json"
            default_headers["X-Aurora-Token"] = self.server.token
        default_headers.update(headers or {})
        connection.request(method, path, body=body, headers=default_headers)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_static_schema_stats_and_session(self):
        for route, mime in [("/", "text/html"), ("/app.js", "text/javascript"), ("/styles.css", "text/css"), ("/favicon.svg", "image/svg+xml")]:
            with self.subTest(route=route):
                status, body, headers = self.request("GET", route)
                self.assertEqual(status, 200)
                self.assertIn(mime, headers["Content-Type"])
                self.assertTrue(body)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, body, _ = self.request("GET", "/api/schema")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tables"], [])
        status, body, _ = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertIn("disk_reads", json.loads(body))
        status, body, _ = self.request("GET", "/api/session")
        self.assertEqual(json.loads(body)["token"], self.server.token)

    def test_query_and_sql_error(self):
        status, body, _ = self.request("POST", "/api/query", {"sql": "CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO test VALUES (1, '<script>alert(1)</script>'); SELECT * FROM test;"})
        self.assertEqual(status, 200)
        results = json.loads(body)["results"]
        self.assertEqual(results[-1]["rows"], [[1, "<script>alert(1)</script>"]])
        status, body, _ = self.request("POST", "/api/query", {"sql": "SELECT * FROM missing;"})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        status, body, _ = self.request("GET", "/api/schema")
        self.assertEqual(json.loads(body)["tables"][0]["name"], "test")

    def test_token_origin_host_and_route_protection(self):
        for headers in [
            {"X-Aurora-Token": ""},
            {"X-Aurora-Token": "wrong"},
            {"X-Aurora-Token": "é"},
            {"Origin": "https://other.example"},
            {"Host": f"other.example:{self.server.server_port}"},
            {"Sec-Fetch-Site": "cross-site"},
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/query", {"sql": "SHOW TABLES;"}, headers=headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/query", {"sql": "SHOW TABLES;"}, headers={"Origin": self.server.url})[0], 200)
        self.assertEqual(self.request("OPTIONS", "/api/query")[0], 403)
        for path in ["/../auroradb/engine.py", "/CONTRACT.txt", "/api/unknown", "/%2e%2e/engine.py"]:
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_request_validation(self):
        for payload in [{}, {"sql": ""}, {"sql": 1}, []]:
            self.assertEqual(self.request("POST", "/api/query", payload=payload)[0], 400)
        self.assertEqual(self.request("POST", "/api/query", raw=b"{broken", headers={"Content-Type": "application/json", "X-Aurora-Token": self.server.token})[0], 400)
        self.assertEqual(self.request("POST", "/api/query", raw=b"x", headers={"Content-Type": "text/plain", "X-Aurora-Token": self.server.token})[0], 415)
        self.assertEqual(self.request("POST", "/api/query", raw=b"", headers={"Content-Length": str(MAX_REQUEST_BYTES + 1), "Content-Type": "application/json", "X-Aurora-Token": self.server.token})[0], 413)

    def test_ambiguous_nonfinite_and_invalid_unicode_json_never_executes(self):
        self.database.execute("CREATE TABLE safe(id INTEGER PRIMARY KEY)")
        headers = {"Content-Type": "application/json", "X-Aurora-Token": self.server.token}
        # Invalid JSON must be rejected before even a valid SQL member can run.
        cases = [
            b'{"sql":"INSERT INTO safe VALUES(1)","sql":"INSERT INTO safe VALUES(2)"}',
            b'{"sql":"INSERT INTO safe VALUES(3)","extra":NaN}',
            b'{"sql":"INSERT INTO safe VALUES(4)","extra":Infinity}',
            b'{"sql":"INSERT INTO safe VALUES(5)","extra":1e9999}',
            b'{"sql":"INSERT INTO safe VALUES(6); SELECT \'\\ud800\'"}',
            b'{"sql":"INSERT INTO safe VALUES(7)","extra":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}',
        ]
        for raw in cases:
            with self.subTest(raw=raw[:70]):
                status, body, _ = self.request("POST", "/api/query", raw=raw, headers=headers)
                self.assertEqual(status, 400, body)
                self.assertIsInstance(json.loads(body)["error"], str)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM safe")[0]["rows"], [[0]])
        self.assertEqual(self.request("POST", "/api/query", {"sql": "SELECT 1"})[0], 200)
        status, body, _ = self.request("POST", "/api/query", raw=b'{"sql":"SELECT \'\\ud83d\\ude00\' AS emoji"}',
                                       headers=headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['results'][0]['rows'], [['😀']])

    def test_malformed_request_target_is_a_json_error(self):
        headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, body, _ = self.request(method, "http://[", payload={} if method == "POST" else None,
                                               headers=headers)
                self.assertEqual(status, 400, body)
                self.assertIn("error", json.loads(body))

    def test_unknown_route_parameters_do_not_change_data_or_cache(self):
        for path, payload in (
                ("/api/query", {"sql":"CREATE TABLE ignored(id INTEGER)", "typo":True}),
                ("/api/integrity", {"sql":"SELECT 1"}),
                ("/api/research/semantic/cache", {"clear":True}),
                ("/api/research/analyze", {"unexpected":True})):
            with self.subTest(path=path):
                status, body, _ = self.request("POST", path, payload)
                self.assertEqual(status, 400, body)
        self.assertEqual(self.database.schema(), [])

    def test_demo_queries_and_rollback(self):
        self.database.execute((resource_directory() / "examples" / "demo.sql").read_text(encoding="utf-8"))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM students;")[0]["rows"], [[12]])
        queries = [
            "EXPLAIN ANALYZE SELECT id, name, department, gpa FROM students WHERE id = 1005;",
            "EXPLAIN ANALYZE SELECT student_id, course_id, score FROM enrollments WHERE score >= 97 AND score <= 98 ORDER BY score DESC;",
            "SELECT s.name AS student, c.title AS course, e.score FROM students s JOIN enrollments e ON s.id = e.student_id JOIN courses c ON e.course_id = c.id WHERE e.score >= 90 ORDER BY e.score DESC, s.id;",
            "SELECT department, COUNT(*) AS students, AVG(gpa) AS avg_gpa FROM students GROUP BY department HAVING COUNT(*) >= 3 ORDER BY avg_gpa DESC;",
            "SELECT s.id, s.name, COUNT(e.id) AS course_count, AVG(e.score) AS avg_score FROM students s LEFT JOIN enrollments e ON s.id = e.student_id GROUP BY s.id, s.name ORDER BY s.id;",
            "BEGIN; UPDATE enrollments SET score = 100 WHERE id = 1; SELECT id, score FROM enrollments WHERE id = 1; ROLLBACK; SELECT id, score FROM enrollments WHERE id = 1;",
        ]
        for query in queries:
            with self.subTest(query=query):
                status, body, _ = self.request("POST", "/api/query", {"sql": query})
                self.assertEqual(status, 200, body.decode("utf-8"))
        self.assertEqual(self.database.execute("SELECT score FROM enrollments WHERE id = 1;")[0]["rows"], [[96.0]])

    def test_range_preset_demonstrates_real_index_scan(self):
        self.database.execute((resource_directory() / "examples" / "demo.sql").read_text(encoding="utf-8"))
        # Execute the actual editor preset so a future UI-only edit cannot drift
        # from the demonstrated execution path.
        source = (resource_directory() / "web" / "app.js").read_text(encoding="utf-8")
        match = re.search(r"\{ name: '03 · 索引范围查询', sql: (\"(?:\\.|[^\"\\])*\") \}", source)
        self.assertIsNotNone(match)
        sql = json.loads(match.group(1))
        status, body, _ = self.request("POST", "/api/query", {"sql": sql})
        self.assertEqual(status, 200, body.decode("utf-8"))
        result = json.loads(body)["results"][0]
        nodes = [result["plan"]]
        indexes = []
        while nodes:
            node = nodes.pop()
            if node["operator"] == "IndexScan":
                indexes.append(node["detail"])
            nodes.extend(node.get("children", []))
        self.assertTrue(any("idx_enrollments_score" in detail for detail in indexes))
        self.assertEqual(result["stats"]["rows_scanned"], 2)
        self.assertEqual(result["stats"]["index_lookups"], 1)
        self.assertEqual(len(result["rows"]), 2)


class CLITests(unittest.TestCase):
    def test_demo_is_idempotent_and_batch_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, "-m", "auroradb", "--db", str(Path(directory) / "db"), "--demo", "--execute", "SELECT COUNT(*) FROM students;"]
            environment = dict(os.environ, PYTHONIOENCODING="utf-8")
            first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=environment)
            second = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=environment)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            database = Database(Path(directory) / "db")
            try:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM students;")[0]["rows"], [[12]])
            finally:
                database.close()
            bad = subprocess.run([sys.executable, "-m", "auroradb", "--db", str(Path(directory) / "db"), "--execute", "SELECT * FROM missing;"], capture_output=True)
            self.assertEqual(bad.returncode, 1)

    def test_repl_terminators_handle_strings_and_comments(self):
        self.assertTrue(complete_statement("SELECT 'a;''b'; -- done"))
        self.assertTrue(complete_statement("SELECT 'a'; /* done */"))
        self.assertFalse(complete_statement("SELECT 'a;"))
        self.assertFalse(complete_statement("SELECT 1 -- ;"))
        self.assertFalse(complete_statement("SELECT 1 /* ;"))

    def test_repl_quoted_identifiers_and_nested_comments(self):
        cases = [
            ("SELECT 1 AS `x;", False),
            ("SELECT 1 AS `x``;", False),
            ("SELECT 1 AS `x``;y`;", True),
            ('SELECT 1 AS "x"";y";', True),
            ("SELECT 1 AS [x;", False),
            ("SELECT 1 AS [x]];", False),
            ("SELECT 1 AS [x]];y];", True),
            ("SELECT 1 /* outer /* nested */ ;", False),
            ("SELECT 1 /* outer /* nested */ ;\n*/;", True),
            ("SELECT 1; /* outer /* nested ; */ done */", True),
            ("SELECT 1; /* outer /* nested ; */", False),
            ("SELECT '/* ; */'; -- ` [ /*", True),
        ]
        for sql, expected in cases:
            with self.subTest(sql=sql):
                self.assertEqual(complete_statement(sql), expected)

    def test_repl_multiline_nested_comment_round_trip(self):
        # The semicolon inside the still-open outer comment must not submit a
        # truncated SELECT. Quoted table/column names also contain semicolons.
        script = "\n".join([
            "CREATE TABLE [odd;name] (`v;col` INTEGER);",
            "INSERT INTO [odd;name] VALUES (7);",
            "SELECT `v;col`",
            "/* outer /* inner */ ;",
            "*/",
            "FROM [odd;name];",
            ".quit", "",
        ])
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "auroradb", "--db", str(Path(directory) / "db")],
                input=script, capture_output=True, text=True, encoding="utf-8",
                env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertRegex(result.stdout, r"\n7\s*\n")


if __name__ == "__main__":
    unittest.main()
