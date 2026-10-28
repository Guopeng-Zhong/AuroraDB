"""Access-path experiments and AI compilation must preserve transaction state."""
import copy
from pathlib import Path
import tempfile
import unittest

from auroradb.engine import Database, DatabaseError
from auroradb.sql import parse


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name, cache_size=8)
        self.db.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, group_id INTEGER, label TEXT);')
        values = ','.join(f"({i},{i%10},'row-{i}')" for i in range(250))
        self.db.execute('INSERT INTO t VALUES '+values)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_real_scan_comparison_preserves_files_catalog_and_results(self):
        catalog = copy.deepcopy(self.db.catalog)
        data = (Path(self.temp.name)/'pages.dat').read_bytes()
        before = self.db.stats()
        value = self.db.compare_query('EXPLAIN ANALYZE SELECT id,label FROM t WHERE id=73;')
        self.assertTrue(value['matched'])
        self.assertEqual(value['scan']['stats']['rows_scanned'],250)
        self.assertEqual(value['optimized']['stats']['rows_scanned'],1)
        self.assertEqual(value['optimized']['stats']['index_lookups'],1)
        self.assertNotIn('rows',value['scan'])
        self.assertEqual(self.db.catalog,catalog)
        self.assertEqual((Path(self.temp.name)/'pages.dat').read_bytes(),data)
        for field in ('commits','rollbacks','disk_writes'):
            self.assertEqual(self.db.stats()[field],before[field])

    def test_unordered_results_compare_as_multisets(self):
        self.db.execute('CREATE TABLE shuffled(id INTEGER PRIMARY KEY); INSERT INTO shuffled VALUES (3),(1),(2),(8),(5),(9),(4),(7),(6),(10)')
        result = self.db.compare_query('SELECT id FROM shuffled WHERE id<=3')
        self.assertTrue(result['matched'])
        self.assertEqual(result['comparison'],'multiset')
        self.assertEqual(result['optimized']['stats']['index_lookups'],1)

    def test_join_aggregate_comparison_preserves_duplicate_rows(self):
        result = self.db.compare_query('SELECT a.group_id,COUNT(*),AVG(b.id) FROM t a JOIN t b ON a.group_id=b.group_id WHERE a.id<4 GROUP BY a.group_id')
        self.assertTrue(result['matched'])
        self.assertEqual(result['scan']['row_count'],4)

    def test_preview_and_failed_comparison_preserve_active_transaction(self):
        self.db.execute("BEGIN; INSERT INTO t VALUES (999,9,'pending')")
        staged = copy.deepcopy(self.db.pager._staged)
        for sql in ('SELECT missing FROM t','CREATE TABLE bad(id INTEGER)','SELECT 1; DELETE FROM t;',
                    'SELECT id FROM t ORDER BY id LIMIT 3','SELECT id FROM t LIMIT 3 OFFSET 1'):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                self.db.compare_query(sql)
            self.assertTrue(self.db.active)
            self.assertEqual(self.db.pager._staged,staged)
        with self.assertRaises(DatabaseError): self.db.preview_query('SELECT missing FROM t')
        self.assertTrue(self.db.active)
        self.assertTrue(self.db.preview_query('SELECT label FROM t WHERE id=999')['read_only'])
        self.assertTrue(self.db.compare_query('SELECT label FROM t WHERE id=999')['matched'])
        self.assertEqual(self.db.execute('SELECT label FROM t WHERE id=999')[0]['rows'],[['pending']])
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.execute('SELECT id FROM t WHERE id=999')[0]['rows'],[])

    def test_check_database_sql_returns_structured_real_report(self):
        self.assertEqual(parse('CHECK DATABASE;')[0]['kind'],'integrity_check')
        value = self.db.execute('CHECK DATABASE;')[0]
        self.assertTrue(value['integrity']['ok'],value['integrity']['issues'])
        self.assertEqual(value['integrity']['summary']['rows'],250)
        self.assertTrue(value['rows'])

    def test_diagnostics_reject_closed_or_poisoned_instance(self):
        self.db.pager._poisoned = True
        for operation in (self.db.integrity_check,lambda:self.db.preview_query('SELECT 1'),lambda:self.db.compare_query('SELECT 1')):
            with self.assertRaises(DatabaseError): operation()
        self.db.pager._poisoned = False
        self.db.close()
        with self.assertRaises(DatabaseError): self.db.preview_query('SELECT 1')


if __name__=='__main__': unittest.main()
