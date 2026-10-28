"""Contract and adversarial grammar tests for the SQL frontend."""

import unittest

from auroradb.sql import SQLError, Token, parse, tokenize


def literal(value):
    return {"kind": "literal", "value": value}


def column(name):
    return {"kind": "column", "name": name}


class TokenizerTests(unittest.TestCase):
    def test_comments_quotes_and_locations(self):
        tokens = tokenize("-- first\nSELECT 'it''s; -- fine', \"ORDER\" /* outer /* inner */ end */;")
        self.assertIsInstance(tokens[0], Token)
        self.assertEqual((tokens[0].value, tokens[0].line, tokens[0].column), ("SELECT", 2, 1))
        self.assertEqual(tokens[1].value, "it's; -- fine")
        self.assertEqual((tokens[3].kind, tokens[3].value), ("IDENT", "order"))
        self.assertEqual(tokens[-1].kind, "EOF")

    def test_quoted_identifier_variants_and_unicode(self):
        statement = parse('SELECT `A``B`, [C]]D], 学号 FROM "学生表";')[0]
        self.assertEqual([item["expr"]["name"] for item in statement["columns"]],
                         ["a`b", "c]d", "学号"])
        self.assertEqual(statement["from"]["table"], "学生表")

    def test_numeric_literals(self):
        tokens = tokenize("0 42 1.5 .25 1. 4e2 2E-3")
        self.assertEqual([token.value for token in tokens[:-1]], [0, 42, 1.5, .25, 1., 400., .002])

    def test_lexical_errors_are_positioned(self):
        for text in ("SELECT 'unterminated", 'SELECT "bad', "/* no end", "SELECT 1e999",
                     "SELECT 1.2.3", "SELECT 12abc", "SELECT 1e", "SELECT ?", "SELECT \x00"):
            with self.subTest(text=text), self.assertRaises(SQLError) as caught:
                tokenize(text)
            self.assertGreaterEqual(caught.exception.column, 1)
            self.assertIn("line", str(caught.exception))
        with self.assertRaises(SQLError) as caught:
            tokenize("\n\n SELECT @")
        self.assertEqual((caught.exception.position, caught.exception.line, caught.exception.column),
                         (10, 3, 9))

    def test_non_string(self):
        with self.assertRaises(SQLError):
            tokenize(None)


class ParserTests(unittest.TestCase):
    def select_expr(self, sql):
        return parse(sql)[0]["columns"][0]["expr"]

    def test_multiple_statements_and_empty_script(self):
        self.assertEqual(parse("; -- empty\n /* empty */ ;"), [])
        statements = parse("BEGIN TRANSACTION; SELECT 1; COMMIT; ROLLBACK TRANSACTION;")
        self.assertEqual([statement["kind"] for statement in statements],
                         ["begin", "select", "commit", "rollback"])
        self.assertIsNone(statements[1]["from"])
        self.assertEqual(parse("START TRANSACTION"), [{"kind": "begin"}])

    def test_create_table_schema(self):
        result = parse("""CREATE TABLE Students (
            ID INT PRIMARY KEY, name VARCHAR(60) NOT NULL,
            score FLOAT DEFAULT -1.25, active BOOL DEFAULT TRUE,
            nickname TEXT UNIQUE DEFAULT 'O''Brien', note TEXT NULL
        )""")[0]
        self.assertEqual(result["name"], "students")
        self.assertEqual(result["columns"][0],
                         {"name": "id", "type": "INTEGER", "not_null": True,
                          "primary": True, "unique": True, "default": None})
        self.assertEqual(result["columns"][1]["max_length"], 60)
        self.assertEqual(result["columns"][2]["default"], -1.25)
        self.assertIs(result["columns"][3]["default"], True)
        self.assertEqual(result["columns"][4]["default"], "O'Brien")
        self.assertEqual([c["type"] for c in result["columns"]],
                         ["INTEGER", "TEXT", "REAL", "BOOLEAN", "TEXT", "TEXT"])

    def test_type_words_are_contextual_identifiers(self):
        result = parse("CREATE TABLE t(text TEXT, real REAL); SELECT text, real FROM t")
        self.assertEqual([c["name"] for c in result[0]["columns"]], ["text", "real"])
        self.assertEqual([c["expr"]["name"] for c in result[1]["columns"]], ["text", "real"])

    def test_quoted_dots_are_rejected_with_source_location(self):
        for sql, position in [('SELECT "a.b" FROM t', 7), ('CREATE TABLE "a.b"(a INT)', 13),
                              ('SELECT a FROM t AS "x.y"', 19)]:
            with self.subTest(sql=sql), self.assertRaises(SQLError) as caught:
                parse(sql)
            self.assertEqual(caught.exception.position, position)
            self.assertIn("Dots inside quoted identifiers", str(caught.exception))

    def test_create_index_and_drop(self):
        statements = parse("CREATE UNIQUE INDEX Ix_A ON Students (ID); DROP INDEX Ix_A; DROP TABLE Students")
        self.assertEqual(statements, [
            {"kind": "create_index", "name": "ix_a", "table": "students", "column": "id", "unique": True},
            {"kind": "drop_index", "name": "ix_a"},
            {"kind": "drop_table", "name": "students"},
        ])

    def test_insert_rows_and_literals(self):
        result = parse("INSERT INTO T (a,b,c) VALUES (1, 'x; DROP TABLE t;', NULL), (-2, 'y', FALSE)")[0]
        self.assertEqual(result["columns"], ["a", "b", "c"])
        self.assertEqual(result["values"][0], [literal(1), literal("x; DROP TABLE t;"), literal(None)])
        self.assertEqual(result["values"][1][0], {"kind": "unary", "op": "-", "expr": literal(2)})
        self.assertEqual(parse("INSERT INTO t VALUES (1)")[0]["columns"], None)

    def test_update_delete(self):
        update, delete = parse("UPDATE T SET a=a+1,b=NULL WHERE a>=2; DELETE FROM T WHERE b IS NULL")
        self.assertEqual(update["assignments"]["a"],
                         {"kind": "binary", "op": "+", "left": column("a"), "right": literal(1)})
        self.assertEqual(delete["where"], {"kind": "is_null", "expr": column("b"), "negated": False})

    def test_arithmetic_precedence_and_associativity(self):
        expr = self.select_expr("SELECT -2 + 3 * 4 / 2 % 5 - 1")
        self.assertEqual(expr["op"], "-")
        self.assertEqual(expr["left"]["op"], "+")
        product = expr["left"]["right"]
        self.assertEqual((product["op"], product["left"]["op"], product["left"]["left"]["op"]),
                         ("%", "/", "*"))
        self.assertEqual(expr["left"]["left"], {"kind": "unary", "op": "-", "expr": literal(2)})
        self.assertEqual(self.select_expr("SELECT (2+3)*4")["left"]["op"], "+")

    def test_boolean_precedence(self):
        expr = parse("SELECT a FROM t WHERE NOT a=1 OR b=2 AND c<>3")[0]["where"]
        self.assertEqual(expr["op"], "OR")
        self.assertEqual(expr["left"]["op"], "NOT")
        self.assertEqual(expr["left"]["expr"]["op"], "=")
        self.assertEqual(expr["right"]["op"], "AND")
        self.assertEqual(expr["right"]["right"]["op"], "<>")

    def test_in_null_like_and_between(self):
        result = parse("SELECT * FROM t WHERE a NOT IN (1,NULL,2+3) AND b IS NOT NULL AND name NOT LIKE 'A_%'")[0]
        expr = result["where"]
        self.assertEqual(expr["right"]["op"], "NOT")
        self.assertEqual(expr["right"]["expr"]["op"], "LIKE")
        self.assertEqual(expr["left"]["right"]["kind"], "is_null")
        self.assertTrue(expr["left"]["right"]["negated"])
        self.assertTrue(expr["left"]["left"]["negated"])
        between = parse("SELECT * FROM t WHERE a BETWEEN 1+2 AND 8 AND b=3")[0]["where"]
        self.assertEqual(between["left"]["left"]["op"], ">=")
        self.assertEqual(between["left"]["right"]["op"], "<=")
        self.assertEqual(parse("SELECT * FROM t WHERE a NOT BETWEEN 1 AND 2")[0]["where"]["op"], "NOT")

    def test_select_joins_group_order_and_limit(self):
        result = parse("""SELECT DISTINCT s.dept AS dept, COUNT(*) n, AVG(s.score) avg_score
            FROM Students AS s LEFT OUTER JOIN Departments d ON s.dept=d.id
            INNER JOIN Teachers AS x ON x.id=d.teacher
            WHERE s.score>=60 GROUP BY s.dept HAVING COUNT(*)>1
            ORDER BY avg_score DESC, dept ASC LIMIT 10 OFFSET 3""")[0]
        self.assertTrue(result["distinct"])
        self.assertEqual(result["from"], {"table": "students", "alias": "s"})
        self.assertEqual([join["type"] for join in result["joins"]], ["LEFT", "INNER"])
        self.assertEqual([item["alias"] for item in result["columns"]], ["dept", "n", "avg_score"])
        self.assertEqual(result["columns"][1]["expr"],
                         {"kind": "call", "name": "COUNT", "args": [{"kind": "star", "table": None}]})
        self.assertEqual(result["group_by"], [column("s.dept")])
        self.assertEqual([item["desc"] for item in result["order_by"]], [True, False])
        self.assertEqual((result["limit"], result["offset"]), (10, 3))

    def test_qualified_star_and_function_nesting(self):
        result = parse("SELECT t.*, COALESCE(LOWER(t.name), 'unknown') AS normalized FROM t")[0]
        self.assertEqual(result["columns"][0]["expr"], {"kind": "star", "table": "t"})
        expr = result["columns"][1]["expr"]
        self.assertEqual((expr["name"], expr["args"][0]["name"]), ("COALESCE", "LOWER"))

    def test_select_defaults_and_limit_comma(self):
        result = parse("SELECT 1 AS one LIMIT 4, 0")[0]
        self.assertEqual((result["limit"], result["offset"]), (0, 4))
        self.assertEqual(result["joins"], [])
        self.assertEqual(result["group_by"], [])
        self.assertEqual(result["order_by"], [])
        self.assertIsNone(result["where"])
        self.assertIsNone(result["having"])

    def test_metadata_and_explain(self):
        statements = parse("SHOW TABLES; DESCRIBE T; DESC T; CHECKPOINT; EXPLAIN ANALYZE SELECT * FROM T")
        self.assertEqual([statement["kind"] for statement in statements],
                         ["show_tables", "describe", "describe", "checkpoint", "explain"])
        self.assertTrue(statements[-1]["analyze"])
        self.assertFalse(parse("EXPLAIN SELECT 1")[0]["analyze"])

    def test_error_line_and_column(self):
        with self.assertRaises(SQLError) as caught:
            parse("SELECT a\nFROM t\nWHERE a = ;")
        self.assertEqual((caught.exception.line, caught.exception.column), (3, 11))

    def test_duplicate_and_invalid_definitions(self):
        invalid = [
            "CREATE TABLE t ()", "CREATE TABLE t (a INT, A TEXT)",
            "CREATE TABLE t (a INT PRIMARY KEY,b INT PRIMARY KEY)",
            "CREATE TABLE t (a INT NOT NULL NULL)", "CREATE TABLE t (a INT UNIQUE UNIQUE)",
            "CREATE TABLE t (a INT DEFAULT 1 DEFAULT 2)", "CREATE TABLE t (a VARCHAR(0))",
            "CREATE TABLE t (a VARCHAR(1.5))", "CREATE TABLE t (a DECIMAL)",
            "CREATE TABLE t (a INT, PRIMARY KEY(a))", "CREATE INDEX ix ON t(a,b)",
            "INSERT INTO t(a,a) VALUES (1,2)", "INSERT INTO t(a,b) VALUES (1)",
            "INSERT INTO t VALUES (1),(1,2)", "UPDATE t SET a=1,a=2",
            "CREATE TABLE t (a INT DEFAULT now())", "CREATE TABLE t (a TEXT DEFAULT -'x')",
        ]
        for sql in invalid:
            with self.subTest(sql=sql), self.assertRaises(SQLError):
                parse(sql)

    def test_unsupported_or_malformed_sql_is_not_silently_ignored(self):
        invalid = [
            "SELECT * FROM t RIGHT JOIN u ON t.a=u.a", "SELECT * FROM t CROSS JOIN u ON t.a=u.a",
            "SELECT * FROM t NATURAL JOIN u", "SELECT * FROM t,", "SELECT * FROM t JOIN u USING(a)",
            "SELECT * FROM t UNION SELECT * FROM u", "WITH q AS (SELECT 1) SELECT * FROM q",
            "SELECT (SELECT 1)", "SELECT * FROM t WHERE a IN (SELECT a FROM u)",
            "SELECT COUNT(DISTINCT a) FROM t", "SELECT CASE WHEN a=1 THEN 2 END FROM t",
            "SELECT * FROM t WHERE name LIKE 'a' ESCAPE '#'", "SELECT * FROM t WHERE a IS TRUE",
            "SELECT 1 LIMIT -1", "SELECT 1 LIMIT 1.5", "SELECT 1 OFFSET 2", "SELECT * + 1 FROM t",
            "SELECT a FROM t WHERE *", "SELECT SUM(*) FROM t", "SELECT COUNT(*,a) FROM t",
            "SELECT * AS all_rows FROM t", "SELECT 1 SELECT 2", "SELECT a FROM t ORDER BY a NULLS LAST",
            "SELECT a FROM t WHERE a IN ()", "SELECT a FROM t WHERE a NOT a",
            "DELETE t", "INSERT INTO t SELECT 1", "DROP TABLE IF EXISTS t", "ALTER TABLE t ADD a INT",
            "SELECT a.b.c FROM t", "SELECT a FROM t WHERE a=1=2", "CREATE DATABASE x",
        ]
        for sql in invalid:
            with self.subTest(sql=sql), self.assertRaises(SQLError):
                parse(sql)

    def test_excessive_nesting_is_a_sql_error(self):
        with self.assertRaises(SQLError):
            parse("SELECT " + "(" * 1200 + "1" + ")" * 1200)
        with self.assertRaises(SQLError) as caught:
            parse("SELECT " + "+".join(["1"] * 300))
        self.assertIn("maximum AST depth", str(caught.exception))
        self.assertGreater(caught.exception.column, 1)


if __name__ == "__main__":
    unittest.main()
