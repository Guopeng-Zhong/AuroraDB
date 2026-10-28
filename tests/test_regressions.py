"""Independent semantic and bounded-work regressions found during review."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from auroradb.engine import Database, DatabaseError, row_signature


class SemanticRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(self.directory.name)
        self.db.execute("CREATE TABLE t(a INTEGER,b INTEGER); INSERT INTO t VALUES (3,10),(1,10),(2,20)")

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def rows(self, sql):
        return self.db.execute(sql)[0]["rows"]

    def test_qualified_source_columns_do_not_bypass_group_validation(self):
        for sql in ("SELECT t.a,COUNT(*) FROM t", "SELECT t.a FROM t GROUP BY t.b",
                    "SELECT COUNT(*) FROM t HAVING t.a>0"):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                self.db.execute(sql)
        self.assertEqual(self.rows("SELECT t.b,COUNT(*) AS n FROM t GROUP BY t.b ORDER BY n DESC"),
                         [[10, 2], [20, 1]])

    def test_order_ordinal_is_validated_without_rows_or_execution(self):
        for sql in ("EXPLAIN SELECT a FROM t ORDER BY 99", "EXPLAIN SELECT a FROM t ORDER BY 0"):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                self.db.execute(sql)
        self.db.execute("DELETE FROM t")
        with self.assertRaises(DatabaseError):
            self.db.execute("SELECT a FROM t ORDER BY 99")

    def test_boolean_order_expression_is_not_a_positional_integer(self):
        self.assertEqual(self.rows("SELECT a FROM t ORDER BY TRUE"), [[3], [1], [2]])

    def test_sql_remainder_uses_dividend_sign(self):
        self.assertEqual(self.rows("SELECT -5 % 2, 5 % -2, -5 % -2, 5 % 2"), [[-1, 1, -1, 1]])

    def test_coalesce_stops_after_first_nonnull_value(self):
        self.assertEqual(self.rows("SELECT COALESCE(1, ABS('unused')), COALESCE(NULL, 2, ABS('unused'))"),
                         [[1, 2]])
        self.assertEqual(self.rows("SELECT COALESCE(NULL,NULL)"), [[None]])

    def test_distinct_and_grouping_preserve_boolean_numeric_types(self):
        self.db.execute("CREATE TABLE mixed(b BOOLEAN,n INTEGER,r REAL);"
                        "INSERT INTO mixed VALUES (TRUE,NULL,NULL),(NULL,1,NULL),(NULL,NULL,1.0),"
                        "(FALSE,NULL,NULL),(NULL,0,NULL),(NULL,NULL,0.0),(NULL,NULL,NULL),(NULL,NULL,NULL)")
        distinct = self.rows("SELECT DISTINCT COALESCE(b,n,r) FROM mixed")
        expected = [[True],[1],[False],[0],[None]]
        self.assertEqual([row_signature(row) for row in distinct],
                         [row_signature(row) for row in expected])
        grouped = self.rows("SELECT COALESCE(b,n,r),COUNT(*) FROM mixed GROUP BY COALESCE(b,n,r)")
        expected_groups = [[True,1],[1,2],[False,1],[0,2],[None,2]]
        self.assertEqual([row_signature(row) for row in grouped],
                         [row_signature(row) for row in expected_groups])

    def test_in_index_planning_does_not_hide_boolean_numeric_type_errors(self):
        self.db.execute("CREATE TABLE flags(b BOOLEAN); INSERT INTO flags VALUES (FALSE);"
                        "CREATE INDEX flags_ix ON flags(b)")
        for values in ("TRUE,1", "1,TRUE", "FALSE,0", "0,FALSE"):
            with self.subTest(values=values), self.assertRaisesRegex(DatabaseError,"incompatible types"):
                self.db.execute(f"SELECT b FROM flags WHERE b IN ({values})")
        self.assertEqual(self.rows("SELECT b FROM flags WHERE b IN (FALSE,FALSE,NULL)"), [[False]])

    def test_join_nested_comparisons_have_boolean_type(self):
        self.db.execute("CREATE TABLE left_numbers(n INTEGER); CREATE TABLE right_numbers(n REAL);"
                        "INSERT INTO left_numbers VALUES (1),(2),(NULL);"
                        "INSERT INTO right_numbers VALUES (1.0),(2.0),(NULL)")
        for predicate in ("(l.n=r.n)=TRUE", "(l.n=r.n)<>FALSE", "(l.n=r.n) IN (TRUE)",
                          "(l.n=r.n) NOT IN (FALSE)", "(l.n<r.n)=FALSE AND l.n=r.n"):
            with self.subTest(predicate=predicate):
                self.assertEqual(self.rows("SELECT l.n,r.n FROM left_numbers l JOIN right_numbers r ON "
                                           +predicate+" ORDER BY l.n"), [[1,1.0],[2,2.0]])
        for prefix in ("SELECT *", "EXPLAIN SELECT *"):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(DatabaseError,"incompatible types"):
                self.db.execute(prefix+" FROM left_numbers l JOIN right_numbers r ON (l.n=r.n)=1")

    def test_join_in_defers_unknown_coalesce_types_to_execution(self):
        self.db.execute("CREATE TABLE mixed_join(b BOOLEAN,n INTEGER);"
                        "CREATE TABLE singleton(n INTEGER); INSERT INTO singleton VALUES (1);"
                        "INSERT INTO mixed_join VALUES (TRUE,1),(FALSE,0)")
        self.assertEqual(self.rows("SELECT m.b FROM mixed_join m JOIN singleton s "
                                   "ON COALESCE(m.b,m.n) IN (TRUE)"), [[True]])
        self.db.execute("INSERT INTO mixed_join VALUES (NULL,1)")
        with self.assertRaisesRegex(DatabaseError,"incompatible types"):
            self.db.execute("SELECT m.b FROM mixed_join m JOIN singleton s ON COALESCE(m.b,m.n) IN (TRUE)")

    def test_nonfinite_expression_and_aggregate_results_are_rejected(self):
        for sql in ("SELECT 1e308*1e308", "SELECT 1e308+1e308"):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                self.db.execute(sql)
        self.db.execute("CREATE TABLE measures(value REAL); INSERT INTO measures VALUES (1e308),(1e308)")
        with self.assertRaises(DatabaseError):
            self.db.execute("SELECT SUM(value) FROM measures")

    def test_round_uses_decimal_half_away_from_zero(self):
        self.assertEqual(self.rows("SELECT ROUND(2.5),ROUND(-2.5),ROUND(1.25,1),"
                                   "ROUND(-1.25,1),ROUND(2.675,2),ROUND(250,-2),ROUND(-250,-2)"),
                         [[3.0,-3.0,1.3,-1.3,2.68,300,-300]])
        self.assertEqual(self.rows("SELECT ROUND(NULL),ROUND(1.5,NULL),ROUND(1e308,0)"),
                         [[None,None,1e308]])

    def test_round_precision_requires_bounded_nonboolean_integer(self):
        for precision in ("1.5", "TRUE", "'2'", "309", "-309"):
            with self.subTest(precision=precision), self.assertRaisesRegex(DatabaseError,"ROUND precision"):
                self.db.execute(f"SELECT ROUND(1.25,{precision})")
        with self.assertRaisesRegex(DatabaseError,"numeric value"):
            self.db.execute("SELECT ROUND('1.5')")
        self.assertEqual(self.rows("SELECT ROUND(1.25,308),ROUND(1.25,-308)"),[[1.25,0.0]])

    def test_join_comparison_types_do_not_depend_on_hash_or_nested_plan(self):
        self.db.execute("CREATE TABLE numeric_side(id INTEGER); CREATE TABLE text_side(k TEXT);"
                        "INSERT INTO numeric_side VALUES (1),(2),(NULL);"
                        "INSERT INTO text_side VALUES ('1'),('2'),(NULL)")
        for predicate in ("l.id=r.k", "l.id+0=r.k", "ABS(l.id)=r.k", "l.id=LOWER(r.k)",
                          "COALESCE(l.id,0)=r.k", "l.id<r.k"):
            for prefix in ("SELECT *", "EXPLAIN SELECT *"):
                with self.subTest(predicate=predicate,prefix=prefix), self.assertRaisesRegex(
                        DatabaseError,"incompatible types"):
                    self.db.execute(f"{prefix} FROM numeric_side l JOIN text_side r ON {predicate}")
        self.db.execute("DELETE FROM numeric_side; DELETE FROM text_side")
        with self.assertRaisesRegex(DatabaseError,"incompatible types"):
            self.db.execute("SELECT * FROM numeric_side l JOIN text_side r ON l.id=r.k")

    def test_join_type_check_preserves_numeric_scalar_and_null_semantics(self):
        self.db.execute("CREATE TABLE left_side(id INTEGER,label TEXT); CREATE TABLE right_side(id REAL,k TEXT);"
                        "INSERT INTO left_side VALUES (1,'A'),(2,'bb'),(NULL,NULL);"
                        "INSERT INTO right_side VALUES (1.0,'a'),(2.0,'bb'),(NULL,NULL)")
        self.assertEqual(self.rows("SELECT l.id,r.id FROM left_side l LEFT JOIN right_side r "
                                   "ON l.id=r.id ORDER BY l.id"),[[None,None],[1,1.0],[2,2.0]])
        self.assertEqual(self.rows("SELECT l.id,r.id FROM left_side l JOIN right_side r "
                                   "ON LENGTH(l.label)=r.id ORDER BY l.id"),[[1,1.0],[2,2.0]])
        self.assertEqual(self.rows("SELECT l.id,r.id FROM left_side l JOIN right_side r "
                                   "ON LOWER(l.label)=r.k ORDER BY l.id"),[[1,1.0],[2,2.0]])
        self.assertEqual(self.rows("SELECT l.id FROM left_side l JOIN right_side r ON l.id=NULL"),[])
        self.assertEqual(self.rows("SELECT l.id FROM left_side l JOIN right_side r "
                                   "ON r.k=(NULL=1)"),[])
        for constant_null in ("NULL LIKE 'x'", "NOT NULL", "NULL AND TRUE", "NULL OR FALSE"):
            with self.subTest(constant_null=constant_null):
                self.assertEqual(self.rows("SELECT l.id FROM left_side l JOIN right_side r "
                                           f"ON r.k=({constant_null})"),[])
        self.assertEqual(self.rows("SELECT l.id FROM left_side l JOIN right_side r "
                                   "ON r.k=(NULL LIKE l.label)"),[])
        self.assertEqual(self.rows("SELECT l.id FROM left_side l JOIN right_side r "
                                   "ON r.k=((NULL LIKE l.label) AND (NULL LIKE r.k))"),[])
        with self.assertRaisesRegex(DatabaseError,"incompatible types"):
            self.db.execute("SELECT l.id FROM left_side l JOIN right_side r ON r.k=(NULL AND FALSE)")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM left_side l JOIN right_side r "
                                   "ON COALESCE(1,ABS('unused'))=r.id"),[[3]])

    def test_order_alias_is_rejected_when_different_outputs_share_name(self):
        for order in ("x", "x+0", "ABS(x)"):
            for prefix in ("SELECT", "EXPLAIN SELECT"):
                with self.subTest(order=order,prefix=prefix), self.assertRaisesRegex(
                        DatabaseError,"Ambiguous output alias: x"):
                    self.db.execute(f"{prefix} a AS x,b AS x FROM t ORDER BY {order}")
        self.assertEqual(self.rows("SELECT a AS x,a AS x FROM t ORDER BY x"),[[1,1],[2,2],[3,3]])
        self.assertEqual(self.rows("SELECT a AS x,b AS x FROM t ORDER BY 1"),[[1,10],[2,20],[3,10]])
        self.db.execute("DELETE FROM t")
        with self.assertRaisesRegex(DatabaseError,"Ambiguous output alias"):
            self.db.execute("SELECT a AS x,b AS x FROM t ORDER BY x")

    def test_avg_survives_overflowing_intermediate_sum(self):
        self.db.execute("CREATE TABLE averages(value REAL); INSERT INTO averages VALUES (1e308),(1e308),(NULL)")
        self.assertEqual(self.rows("SELECT AVG(value),COUNT(value) FROM averages"),[[1e308,2]])
        self.db.execute("INSERT INTO averages VALUES (-1e308),(-1e308)")
        self.assertEqual(self.rows("SELECT AVG(value) FROM averages"),[[0.0]])
        self.db.execute("INSERT INTO averages VALUES (1e-20)")
        self.assertEqual(self.rows("SELECT AVG(value) FROM averages"),[[2e-21]])
        self.db.execute("DELETE FROM averages; INSERT INTO averages VALUES "
                        "(1e308),(1e308),(-1e308),(-1e308),"+', '.join('(5e-324)' for _ in range(5)))
        self.assertEqual(self.rows("SELECT AVG(value) FROM averages"),[[5e-324]])
        self.db.execute("DELETE FROM averages; INSERT INTO averages VALUES (5e-324),(5e-324)")
        self.assertEqual(self.rows("SELECT AVG(value) FROM averages"),[[5e-324]])
        self.db.execute("DELETE FROM averages; INSERT INTO averages VALUES (0),(0),(NULL)")
        self.assertEqual(self.rows("SELECT AVG(value) FROM averages"),[[0.0]])
        self.db.execute("DELETE FROM averages")
        self.assertEqual(self.rows("SELECT AVG(value) FROM averages"),[[None]])
        # INTEGER has no artificial 64-bit limit. A finite average must also
        # survive an integer sum that cannot be converted directly to float.
        self.db.execute("CREATE TABLE integer_averages(value INTEGER)")
        self.db.execute("INSERT INTO integer_averages VALUES ("+'1'+'0'*309+"),"+
                        ','.join('(0)' for _ in range(9)))
        self.assertEqual(self.rows("SELECT AVG(value) FROM integer_averages"),[[1e308]])

    def test_failed_explicit_write_syntax_rolls_back_transaction(self):
        self.db.execute("BEGIN; INSERT INTO t VALUES (99,99)")
        with self.assertRaises(DatabaseError):
            self.db.execute("UPDATE t SET a=;")
        self.assertFalse(self.db.stats()["transaction_active"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM t"), [[3]])

    def test_like_handles_adversarial_wildcards_with_bounded_work(self):
        # A regex implementation with repeated greedy wildcards can spend minutes
        # here. Isolate it so a regression fails promptly rather than hanging CI.
        script = (
            "from auroradb.engine import evaluate\n"
            "from auroradb.sql import parse\n"
            "pattern = '%a' * 18 + '%b'\n"
            "sql = \"SELECT '\" + 'a' * 100 + \"' LIKE '\" + pattern + \"'\"\n"
            "assert evaluate(parse(sql)[0]['columns'][0]['expr'], {}) is False\n"
        )
        completed = subprocess.run([sys.executable, "-c", script],
                                   cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, timeout=4)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
