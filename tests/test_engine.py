"""End-to-end correctness and seeded SQLite differential checks.

SQLite is an independent test oracle only; it is never loaded by runtime code.
"""
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from auroradb.engine import Database, DatabaseError


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name, cache_size=3)
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def rows(self, sql): return self.db.execute(sql)[-1]['rows']
    def setup_people(self):
        self.db.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, score REAL, dept TEXT DEFAULT 'CS'); INSERT INTO people VALUES (1,'Ada',90.0,'CS'),(2,'Bob',80.0,'Math'),(3,'Cleo',NULL,'CS');")

    def test_crud_and_persistence(self):
        self.setup_people()
        self.db.execute("UPDATE people SET score=score+5 WHERE id=1; DELETE FROM people WHERE id=2;")
        expected=[[1,'Ada',95.0,'CS'],[3,'Cleo',None,'CS']]
        self.assertEqual(self.rows("SELECT * FROM people ORDER BY id;"), expected)
        self.db.close(); self.db=Database(self.tmp.name)
        self.assertEqual(self.rows("SELECT * FROM people ORDER BY id;"),expected)

    def test_defaults_types_and_constraints(self):
        self.setup_people()
        self.db.execute("INSERT INTO people (id,name) VALUES (4,'Dee');")
        self.assertEqual(self.rows("SELECT dept, score FROM people WHERE id=4;"),[['CS',None]])
        for sql in ["INSERT INTO people VALUES (1,'Dup',1,'CS');", "INSERT INTO people VALUES (5,NULL,1,'CS');", "INSERT INTO people VALUES ('5','bad',1,'CS');"]:
            with self.assertRaises(DatabaseError): self.db.execute(sql)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM people;"),[[4]])

    def test_multirow_insert_atomic(self):
        self.setup_people()
        with self.assertRaises(DatabaseError): self.db.execute("INSERT INTO people VALUES (4,'New',5,'CS'),(1,'Duplicate',6,'CS');")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM people;"),[[3]])

    def test_explicit_transaction_and_failed_write(self):
        self.setup_people()
        self.db.execute("BEGIN; UPDATE people SET score=100 WHERE id=1;")
        self.assertEqual(self.rows("SELECT score FROM people WHERE id=1;"),[[100.0]])
        self.db.execute("ROLLBACK;")
        self.assertEqual(self.rows("SELECT score FROM people WHERE id=1;"),[[90.0]])
        self.db.execute("BEGIN; DELETE FROM people WHERE id=3; COMMIT;")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM people;"),[[2]])
        self.db.execute("BEGIN; INSERT INTO people VALUES (8,'New',8,'CS');")
        with self.assertRaises(DatabaseError): self.db.execute("INSERT INTO people VALUES (1,'Dup',8,'CS');")
        self.assertFalse(self.db.active)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM people;"),[[2]])

    def test_close_rolls_back(self):
        self.setup_people(); self.db.execute("BEGIN; DELETE FROM people;")
        self.db.close(); self.db=Database(self.tmp.name)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM people;"),[[3]])

    def test_begin_snapshot_failure_does_not_leave_pager_transaction_open(self):
        with patch("auroradb.engine.copy.deepcopy", side_effect=MemoryError("snapshot")):
            with self.assertRaises(MemoryError):
                self.db._begin()
        self.assertFalse(self.db.active)
        self.assertFalse(self.db.pager.in_transaction)
        self.assertEqual(self.rows("SELECT 1;"), [[1]])

    def test_null_three_valued_logic(self):
        self.setup_people()
        self.assertEqual(self.rows("SELECT id FROM people WHERE score = NULL;"),[])
        self.assertEqual(self.rows("SELECT id FROM people WHERE score IS NULL;"),[[3]])
        self.assertEqual(self.rows("SELECT id FROM people WHERE NOT (score > 85) ORDER BY id;"),[[2]])
        self.assertEqual(self.rows("SELECT id FROM people WHERE id NOT IN (1,NULL);"),[])
        self.assertEqual(self.rows("SELECT NULL AND FALSE, NULL OR TRUE, NOT NULL;"),[[False,True,None]])

    def test_join_left_join_and_ambiguity(self):
        self.setup_people()
        self.db.execute("CREATE TABLE departments (code TEXT PRIMARY KEY, title TEXT); INSERT INTO departments VALUES ('CS','Computing');")
        self.assertEqual(self.rows("SELECT p.name,d.title FROM people p LEFT JOIN departments d ON p.dept=d.code ORDER BY p.id;"),[['Ada','Computing'],['Bob',None],['Cleo','Computing']])
        self.assertEqual(self.rows("SELECT p.name FROM people p INNER JOIN departments d ON p.dept=d.code WHERE d.title='Computing' ORDER BY p.id;"),[['Ada'],['Cleo']])
        with self.assertRaises(DatabaseError): self.db.execute("SELECT name FROM people p JOIN people q ON p.id=q.id;")

    def test_aggregate_distinct_sort_limit(self):
        self.setup_people()
        self.assertEqual(self.rows("SELECT dept,COUNT(*) AS n,AVG(score) AS mean FROM people GROUP BY dept HAVING COUNT(*)>=1 ORDER BY n DESC,dept;"),[['CS',2,90.0],['Math',1,80.0]])
        self.assertEqual(self.rows("SELECT DISTINCT dept FROM people ORDER BY dept LIMIT 1 OFFSET 1;"),[['Math']])
        self.assertEqual(self.rows("SELECT name,score FROM people ORDER BY 2 DESC LIMIT 2;"),[['Ada',90.0],['Bob',80.0]])
        with self.assertRaises(DatabaseError): self.db.execute("SELECT name, COUNT(*) FROM people;")
        self.db.execute("DELETE FROM people;")
        self.assertEqual(self.rows("SELECT COUNT(*),SUM(score),AVG(score),MIN(score),MAX(score) FROM people;"),[[0,None,None,None,None]])

    def test_empty_table_still_binds(self):
        self.db.execute("CREATE TABLE t (id INTEGER);")
        for sql in ["SELECT missing FROM t;", "SELECT * FROM t WHERE missing=1;", "SELECT nofunc(id) FROM t;", "SELECT * FROM t ORDER BY missing;"]:
            with self.assertRaises(DatabaseError): self.db.execute(sql)

    def test_index_maintenance_and_actual_scan(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, bucket INTEGER, payload TEXT);")
        vals=','.join(f"({i},{i%10},'v{i}')" for i in range(250))
        self.db.execute('INSERT INTO t VALUES '+vals+'; CREATE INDEX bucket_ix ON t(bucket);')
        result=self.db.execute("EXPLAIN ANALYZE SELECT payload FROM t WHERE id=142;")[-1]
        self.assertEqual(result['rows'],[['v142']])
        self.assertEqual(result['stats']['rows_scanned'],1)
        self.assertIn('IndexScan',str(result['plan']))
        self.db.execute("UPDATE t SET bucket=99 WHERE id=142; DELETE FROM t WHERE id=143;")
        self.assertEqual(self.rows("SELECT id FROM t WHERE bucket=99;"),[[142]])
        self.assertEqual(self.rows("SELECT id FROM t WHERE id>=240 AND id<244 ORDER BY id;"),[[240],[241],[242],[243]])
        self.assertEqual(self.rows("SELECT id FROM t WHERE id=143;"),[])
        self.db.close(); self.db=Database(self.tmp.name,cache_size=2)
        self.assertEqual(self.rows("SELECT id FROM t WHERE bucket=99;"),[[142]])

    def test_unique_index_and_constraint_index(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE); INSERT INTO t VALUES (1,NULL),(2,NULL),(3,'x');")
        with self.assertRaises(DatabaseError): self.db.execute("INSERT INTO t VALUES (4,'x');")
        with self.assertRaises(DatabaseError): self.db.execute("DROP INDEX t_id_key;")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM t;"),[[3]])

    def test_explain_does_not_execute_or_mutate(self):
        self.setup_people()
        r=self.db.execute("EXPLAIN SELECT * FROM people WHERE id=1;")[-1]
        self.assertEqual(r['rows'],[]); self.assertEqual(r['stats']['rows_scanned'],0)
        self.assertIsNotNone(r['plan'])

    def test_literals_comments_and_safe_text(self):
        self.db.execute("CREATE TABLE t (id INTEGER, s TEXT); -- comment\nINSERT INTO t VALUES (1,'a; b -- ''quoted'''),(2,'<script>alert(1)</script>');")
        self.assertEqual(self.rows("SELECT s FROM t WHERE id=1;"),[["a; b -- 'quoted'"]])
        self.assertEqual(self.rows("SELECT UPPER('ab'),LOWER('CD'),LENGTH('中文'),COALESCE(NULL,5),ABS(-4),ROUND(1.234,2);"),[['AB','cd',2,5,4,1.23]])

    def test_varchar_and_boolean(self):
        self.db.execute("CREATE TABLE t (v VARCHAR(3), b BOOLEAN); INSERT INTO t VALUES ('abc',TRUE);")
        self.assertEqual(self.rows("SELECT b FROM t;"),[[True]])
        with self.assertRaises(DatabaseError): self.db.execute("INSERT INTO t VALUES ('abcd',FALSE);")

    def test_seeded_differential_queries_and_mutations(self):
        rng=random.Random(20261009)
        oracle=sqlite3.connect(':memory:')
        ddl='CREATE TABLE items (id INTEGER PRIMARY KEY, category TEXT, value REAL, qty INTEGER);'
        self.db.execute(ddl); oracle.execute(ddl)
        for i in range(1,151):
            category=rng.choice(['A','B','C',None]); value=rng.choice([None,round(rng.uniform(-20,100),2)]); qty=rng.randint(0,10)
            literal=lambda v: 'NULL' if v is None else "'"+v+"'" if isinstance(v,str) else str(v)
            q=f"INSERT INTO items VALUES ({i},{literal(category)},{literal(value)},{qty});"
            self.db.execute(q); oracle.execute(q)
        self.db.execute('CREATE INDEX value_ix ON items(value);')
        for step in range(180):
            ident=rng.randint(1,150)
            if step%4==0:
                q=f'UPDATE items SET qty=qty+1 WHERE id={ident};'
                self.db.execute(q); oracle.execute(q)
            if step%13==0:
                q=f'DELETE FROM items WHERE id={ident};'
                self.db.execute(q); oracle.execute(q)
            lo=rng.randint(-20,90); hi=lo+rng.randint(1,30)
            queries=[f'SELECT id,category,value,qty FROM items WHERE value>={lo} AND value<{hi} ORDER BY value,id;',
                     f'SELECT id FROM items WHERE qty>={step%8} AND (category=\'A\' OR category IS NULL) ORDER BY id LIMIT 8;',
                     'SELECT category,COUNT(*),SUM(qty),AVG(value),MIN(value),MAX(value) FROM items GROUP BY category ORDER BY category;',
                     'SELECT DISTINCT qty FROM items ORDER BY qty DESC;',
                     f'SELECT id FROM items WHERE id IN ({ident},1,150,NULL) ORDER BY id;']
            q=queries[step%len(queries)]
            expected=[list(r) for r in oracle.execute(q).fetchall()]
            self.assertEqual(self.rows(q),expected,q)
        oracle.close()


if __name__=='__main__': unittest.main()
