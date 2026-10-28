"""Join normalization, range execution and outer-join safety vs SQLite."""
import random
import sqlite3
import tempfile
import unittest
from collections import Counter

from auroradb.engine import Database, DatabaseError, row_signature
from auroradb.optimizer import select_sql
from auroradb.sql import parse


def nodes(plan):
    yield plan
    for child in plan.get('children', []):
        yield from nodes(child)


class JoinFormsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name, cache_size=8)
        self.oracle = sqlite3.connect(':memory:')
        script = '''
            CREATE TABLE a(id INTEGER, x INTEGER, name TEXT);
            CREATE TABLE b(id INTEGER, x INTEGER, name TEXT);
            CREATE TABLE c(id INTEGER, x INTEGER, name TEXT);
            INSERT INTO a VALUES (1,1,'a'),(2,2,'b'),(2,2,'b'),(3,NULL,NULL),(NULL,9,'z');
            INSERT INTO b VALUES (1,1,'b'),(1,2,'b'),(2,NULL,NULL),(4,4,'d'),(NULL,9,'z');
            INSERT INTO c VALUES (1,1,'a'),(2,2,'b'),(2,3,'c'),(5,5,'e'),(NULL,NULL,NULL);
            CREATE INDEX bx ON b(x);
            CREATE INDEX cx ON c(x);
        '''
        self.db.execute(script)
        self.oracle.executescript(script)
        self.db.analyze_statistics()

    def tearDown(self):
        self.oracle.close()
        self.db.close()
        self.tmp.cleanup()

    def match(self, sql):
        expected = Counter(row_signature(row) for row in self.oracle.execute(sql))
        for mode in ('heuristic', 'adaptive'):
            self.db.set_optimizer_mode(mode)
            result = self.db.execute(sql)[0]
            self.assertEqual(Counter(row_signature(row) for row in result['rows']), expected,
                             (mode, sql))
        return result

    def test_parser_serializer_preserve_comma_join_scopes(self):
        for sql in (
            'SELECT * FROM a,b,c WHERE a.id=b.id AND b.id=c.id',
            'SELECT * FROM a CROSS JOIN b WHERE a.id=b.id',
            'SELECT * FROM a,b LEFT JOIN c ON b.id=c.id WHERE a.x=1',
            'SELECT * FROM a JOIN b ON a.id=b.id,c CROSS JOIN a z',
        ):
            with self.subTest(sql=sql):
                statement = parse(sql)[0]
                self.assertEqual(parse(select_sql(statement))[0], statement)
                self.match(sql)
        with self.assertRaises(DatabaseError):
            self.db.execute('SELECT * FROM a,b JOIN c ON a.id=c.id')

    def test_equivalent_comma_cross_and_on_where_joins(self):
        forms = [
            'a JOIN b ON a.id=b.id JOIN c ON b.id=c.id WHERE c.x=2',
            'a,b,c WHERE a.id=b.id AND b.id=c.id AND c.x=2',
            'a CROSS JOIN b CROSS JOIN c WHERE a.id=b.id AND b.id=c.id AND c.x=2',
            'a JOIN b ON TRUE JOIN c ON TRUE WHERE a.id=b.id AND b.id=c.id AND c.x=2',
            'a,b,c WHERE a.id=c.id AND b.id=c.id AND c.x=2',
        ]
        for form in forms:
            with self.subTest(form=form):
                result = self.match('SELECT a.id,b.x,c.x FROM '+form)
                info = result['research']['join_normalization']
                self.assertTrue(info['normalized'])
                self.assertNotEqual(result['research']['join_optimization']['reason'],
                                    'unsupported join shape')
                self.assertGreaterEqual(sum(n['operator']=='HashJoin' for n in nodes(result['plan'])), 1)

    def test_or_predicate_and_disconnected_products_stay_correct(self):
        for suffix in ('a,b WHERE a.id=b.id OR a.x=b.x',
                       'a,b,c WHERE a.id=b.id', 'a CROSS JOIN b WHERE FALSE',
                       'a,b WHERE a.x IS NULL AND b.x IS NULL'):
            with self.subTest(suffix=suffix):
                self.match('SELECT a.id,b.id FROM '+suffix)

    def test_theta_join_filters_and_range_candidates(self):
        for op in ('<','<=','>','>='):
            for condition in (f'a.x {op} b.x', f'b.x {op} a.x',
                              f'a.name {op} b.name', f'a.x {op} b.x AND a.id<>b.id'):
                with self.subTest(condition=condition):
                    result = self.match('SELECT a.id,b.id FROM a JOIN b ON '+condition)
                    self.assertTrue(any(n['operator']=='RangeJoin' for n in nodes(result['plan'])))
        self.match('SELECT a.id,b.id FROM a,b WHERE a.x<b.x AND b.x>=4')

    def test_range_join_rechecks_second_bound_and_preserves_duplicates(self):
        self.match('SELECT a.id,b.id FROM a JOIN b ON a.x<=b.x AND a.id>b.id')
        self.match('SELECT a.id,b.id FROM a JOIN b ON a.id=b.id AND a.x<b.x')

    def test_range_join_feedback_does_not_train_hash_cost(self):
        self.db.set_optimizer_mode('adaptive')
        self.db.execute('SELECT a.id,b.id FROM a JOIN b ON a.x<b.x')
        self.assertEqual(self.db.research_status()['learning']['join_cost_model']['samples'],0)
        self.assertEqual(self.db.research_status()['workload']['executions'],1)

    def test_left_join_filters_never_remove_null_extended_rows(self):
        conditions = (
            'a.id=b.id AND b.x>=2',
            'a.id=b.id AND a.x>1',
            'a.id=b.id AND b.x IS NULL',
            'a.x>b.x AND b.x>=2',
            'a.x<b.x AND b.id<>4',
        )
        for on in conditions:
            for where in ('', ' WHERE b.x IS NULL', ' WHERE b.x>=2',
                          ' WHERE COALESCE(b.x,0)=0', ' WHERE a.x>=2'):
                with self.subTest(on=on,where=where):
                    self.match('SELECT a.id,b.id,b.x FROM a LEFT JOIN b ON '+on+where)

    def test_outer_join_chain_and_mixed_comma_precedence(self):
        for suffix in (
            'a LEFT JOIN b ON a.id=b.id LEFT JOIN c ON b.id=c.id WHERE b.x IS NULL',
            'a LEFT JOIN b ON a.id=b.id JOIN c ON a.id=c.id WHERE c.x=2',
            'a,b LEFT JOIN c ON b.id=c.id WHERE a.x=1 AND c.x IS NULL',
            'a LEFT JOIN b ON a.id=b.id,c WHERE c.x=1 AND b.x IS NULL',
        ):
            with self.subTest(suffix=suffix):
                result = self.match('SELECT a.id,b.id,c.id FROM '+suffix)
                self.assertFalse(result['research']['join_optimization']['accepted'])

    def test_two_table_right_index_and_filter_are_used(self):
        result = self.match('SELECT a.id,b.id FROM a JOIN b ON a.id=b.id WHERE b.x=4')
        self.assertTrue(any(n['operator']=='IndexScan' and 'bx' in n['detail']
                            for n in nodes(result['plan'])))
        result = self.match('SELECT a.id,b.id FROM a LEFT JOIN b ON a.id=b.id AND b.x=4')
        self.assertTrue(any(n['operator']=='IndexScan' and 'bx' in n['detail']
                            for n in nodes(result['plan'])))

    def test_range_join_reduces_comparisons_without_changing_results(self):
        self.db.execute('CREATE TABLE nums(x INTEGER); INSERT INTO nums VALUES '+
                        ','.join(f'({i})' for i in range(100)))
        self.db.analyze_statistics()
        sql='SELECT l.x,r.x FROM nums l JOIN nums r ON l.x<r.x'
        self.db.set_optimizer_mode('heuristic')
        old=self.db.execute(sql)[0]
        self.db.set_optimizer_mode('adaptive')
        new=self.db.execute(sql)[0]
        self.assertEqual(Counter(map(tuple,old['rows'])),Counter(map(tuple,new['rows'])))
        self.assertEqual(old['stats']['join_comparisons'],10000)
        self.assertEqual(new['stats']['join_comparisons'],4950)

    def test_explain_does_not_execute_or_observe_and_write_invalidates(self):
        self.db.set_optimizer_mode('adaptive')
        preview=self.db.preview_query('SELECT a.id,b.id FROM a,b WHERE a.x<b.x')
        self.assertTrue(any(n['operator']=='RangeJoin' for n in nodes(preview['plan'])))
        self.assertEqual(self.db.research_status()['workload']['executions'],0)
        self.db.execute('INSERT INTO b VALUES (9,9,\'n\')')
        self.oracle.execute('INSERT INTO b VALUES (9,9,\'n\')')
        self.assertFalse(self.db.research_status()['statistics']['available'])
        self.match('SELECT a.id,b.id FROM a,b WHERE a.x<b.x')

    def test_known_type_mismatch_still_fails_before_empty_join(self):
        self.db.execute('CREATE TABLE empty(x TEXT)')
        for sql in ('SELECT * FROM a JOIN empty ON a.id<empty.x',
                    'SELECT * FROM a,empty WHERE a.id=empty.x'):
            with self.subTest(sql=sql),self.assertRaises(DatabaseError):
                self.db.execute(sql)

    def test_seeded_nullable_differential_queries(self):
        rng=random.Random(2810)
        for _ in range(120):
            op=rng.choice(('=','<','<=','>','>=','<>'))
            on=f'a.x{op}b.x'
            if rng.randrange(2): on+=' AND b.id>=1'
            join=rng.choice(('JOIN','LEFT JOIN'))
            where=rng.choice(('', ' WHERE b.x IS NULL', ' WHERE a.x>=2',
                              ' WHERE b.x>=2', ' WHERE a.id=2 OR b.id=4'))
            sql=f'SELECT a.id,b.id,a.x,b.x FROM a {join} b ON {on}{where}'
            with self.subTest(sql=sql): self.match(sql)

    def test_new_syntax_reuses_dp_and_executes_each_input_once(self):
        for table,count in (('large',2000),('middle',500),('small',1)):
            self.db.execute(f'CREATE TABLE {table}(id INTEGER)')
            for start in range(1,count+1,250):
                self.db.execute(f'INSERT INTO {table} VALUES '+
                    ','.join(f'({i})' for i in range(start,min(count+1,start+250))))
        self.db.analyze_statistics()
        self.db.set_optimizer_mode('adaptive')
        read=self.db._read
        calls=[]
        def counted(table,*args,**kwargs):
            calls.append(table)
            return read(table,*args,**kwargs)
        self.db._read=counted
        for form in (
            'large l,middle m,small s WHERE l.id=m.id AND m.id=s.id',
            'large l CROSS JOIN middle m CROSS JOIN small s WHERE l.id=m.id AND m.id=s.id',
            'large l,middle m,small s WHERE l.id=s.id AND m.id=s.id',
            'large l JOIN middle m ON TRUE JOIN small s ON m.id=s.id WHERE l.id=m.id',
        ):
            with self.subTest(form=form):
                calls.clear()
                sql='SELECT l.id,m.id,s.id FROM '+form
                preview=self.db.preview_query(sql)
                self.assertEqual(calls,[])
                self.assertTrue(preview['plan']['join_optimization']['accepted'])
                result=self.db.execute(sql)[0]
                self.assertEqual(result['rows'],[[1,1,1]])
                self.assertEqual(Counter(calls),Counter({'large':1,'middle':1,'small':1}))
                self.assertEqual(sum(n['operator']=='HashJoin' for n in nodes(result['plan'])),2)
                self.assertLessEqual(result['stats']['join_comparisons'],2)

    def test_empty_inputs_numeric_compatibility_and_boolean_ranges(self):
        script='''CREATE TABLE empty(x INTEGER);
            CREATE TABLE real_values(x REAL); INSERT INTO real_values VALUES (1.0),(2.5),(NULL);
            CREATE TABLE flags(x BOOLEAN); INSERT INTO flags VALUES (TRUE),(FALSE),(NULL);'''
        self.db.execute(script)
        self.oracle.executescript(script)
        for sql in (
            'SELECT a.id,e.x FROM a LEFT JOIN empty e ON a.x<e.x',
            'SELECT a.id,e.x FROM empty e LEFT JOIN a ON a.x<e.x',
            'SELECT a.id,r.x FROM a JOIN real_values r ON a.x<=r.x',
        ):
            with self.subTest(sql=sql): self.match(sql)
        sql='SELECT l.x,r.x FROM flags l LEFT JOIN flags r ON l.x<r.x'
        # SQLite represents BOOLEAN as integers; restore the declared output
        # type before using Aurora's deliberately type-sensitive bag signature.
        expected=Counter(row_signature([bool(v) if v is not None else None for v in row])
                         for row in self.oracle.execute(sql))
        for mode in ('heuristic','adaptive'):
            self.db.set_optimizer_mode(mode)
            result=self.db.execute(sql)[0]
            self.assertEqual(Counter(map(row_signature,result['rows'])),expected)


if __name__ == '__main__':
    unittest.main()
