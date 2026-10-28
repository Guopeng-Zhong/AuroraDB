"""Semantic counterexamples and version/budget boundaries for research APIs."""
import tempfile
import unittest

from auroradb.engine import Database, DatabaseError
from auroradb.learning import RidgeModel, ResearchState


def plans(plan):
    yield plan
    for child in plan.get('children', []):
        yield from plans(child)


class ResearchOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(self.directory.name, cache_size=8)
        self.db.execute('CREATE TABLE t(id INTEGER PRIMARY KEY,a INTEGER,b TEXT); CREATE INDEX ix_a ON t(a)')
        values = ','.join(f"({i},{'NULL' if i==100 else i%20},'v{i%7}')" for i in range(1,101))
        self.db.execute('INSERT INTO t VALUES '+values)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_statistics_are_real_and_read_only(self):
        before = self.db.execute('SELECT COUNT(*) FROM t')[0]['rows']
        report = self.db.analyze_statistics()
        table = report['tables']['t']
        self.assertEqual(table['row_count'],100)
        self.assertEqual(table['columns']['a']['ndv'],20)
        self.assertEqual(table['columns']['a']['null_count'],1)
        self.assertEqual(sum(part['count'] for part in table['columns']['a']['histogram']),99)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM t')[0]['rows'],before)
        self.assertTrue(self.db.research_status()['statistics']['available'])

    def test_research_rejections_preserve_active_transaction(self):
        self.db.execute('BEGIN; INSERT INTO t VALUES (101,1,\'pending\')')
        for action in (self.db.analyze_statistics,
                       lambda:self.db.set_optimizer_mode('adaptive'),
                       lambda:self.db.optimize_query('DELETE FROM t'),
                       lambda:self.db.optimize_query('SELECT * FROM t')):
            with self.subTest(action=action), self.assertRaises(DatabaseError): action()
            self.assertTrue(self.db.active)
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM t')[0]['rows'],[[100]])

    def test_default_mode_and_no_stats_fallback(self):
        self.assertEqual(self.db.research_status()['mode'],'heuristic')
        self.db.set_optimizer_mode('adaptive')
        plan = self.db.preview_query('SELECT id FROM t WHERE a=1')['plan']
        self.assertTrue(any(p['operator']=='IndexScan' for p in plans(plan)))
        self.assertTrue(any(p.get('estimation',{}).get('source')=='heuristic' for p in plans(plan)))
        with self.assertRaises(DatabaseError): self.db.set_optimizer_mode('magic')

    def test_trusted_or_to_in_preserves_null_duplicates_and_uses_index(self):
        report = self.db.optimize_query('SELECT a,b FROM t WHERE a=1 OR a=2 OR a=NULL')
        self.assertEqual(report['verification']['level'],'proven')
        self.assertTrue(report['verification']['matched'])
        self.assertIn('or-equalities-to-in',report['verification']['rules'])
        candidate = report['candidate']['plan']
        scan = next(p for p in plans(candidate) if p['operator']=='IndexScan')
        self.assertEqual(scan['estimated_rows'],10)
        self.assertEqual(scan['actual_rows'],10)
        self.assertTrue(self.db.compare_query('SELECT a,b FROM t WHERE a IN (1,1,2,NULL)')['matched'])
        self.assertTrue(self.db.compare_query('SELECT a,b FROM t WHERE a NOT IN (1,NULL)')['matched'])

    def test_true_and_duplicate_rules_preserve_three_valued_logic(self):
        report = self.db.optimize_query('SELECT a FROM t WHERE TRUE AND (a=1 OR a=NULL) AND (a=1 OR a=NULL)')
        self.assertTrue(report['verification']['matched'])
        self.assertEqual(set(report['verification']['rules']),{'where-true','duplicate-and','or-equalities-to-in'})
        self.assertEqual(report['candidate']['row_count'],5)

    def test_left_join_and_bag_results_are_preserved(self):
        self.db.execute('CREATE TABLE r(k INTEGER); INSERT INTO r VALUES (1),(1),(2)')
        report = self.db.optimize_query('SELECT t.a,r.k FROM t LEFT JOIN r ON t.a=r.k WHERE r.k=1 OR r.k=NULL')
        self.assertTrue(report['verification']['matched'])
        self.assertEqual(report['verification']['level'],'proven')
        self.assertEqual(report['original']['row_count'],10)

    def test_incompatible_or_types_are_not_proved_by_in_rule(self):
        report = self.db.optimize_query("SELECT a FROM t WHERE a='1' OR a=2")
        self.assertEqual(report['verification']['level'],'unknown')
        self.assertNotIn('or-equalities-to-in',report['verification']['rules'])
        self.assertFalse(report['recommendation']['replace'])
        self.assertFalse(self.db.active)

    def test_external_coincidence_is_only_tested_and_data_change_invalidates(self):
        first = self.db.optimize_query('SELECT id FROM t WHERE id>0','SELECT id FROM t WHERE id>=0')
        self.assertEqual(first['verification']['level'],'tested')
        self.assertTrue(first['verification']['matched'])
        self.assertFalse(first['recommendation']['replace'])
        generation = first['cache']['generation']
        self.db.execute("INSERT INTO t VALUES (0,0,'zero')")
        second = self.db.optimize_query('SELECT id FROM t WHERE id>0','SELECT id FROM t WHERE id>=0')
        self.assertFalse(second['verification']['matched'])
        self.assertGreater(second['cache']['generation'],generation)
        self.assertFalse(second['cache']['hit'])

    def test_external_candidate_distinct_detects_bag_counterexample(self):
        report = self.db.optimize_query('SELECT a FROM t','SELECT DISTINCT a FROM t')
        self.assertFalse(report['verification']['matched'])
        self.assertEqual(report['verification']['level'],'unknown')

    def test_output_boolean_and_number_are_not_equivalent_python_hashes(self):
        for boolean,number in [('TRUE','1'),('FALSE','0')]:
            for order in ('',' ORDER BY value'):
                with self.subTest(boolean=boolean,order=order):
                    report=self.db.optimize_query(f'SELECT {boolean} AS value'+order,
                                                  f'SELECT {number} AS value'+order)
                    self.assertFalse(report['verification']['matched'])
                    self.assertEqual(report['verification']['level'],'unknown')
        numeric=self.db.optimize_query('SELECT 1 AS value','SELECT 1.0 AS value')
        self.assertTrue(numeric['verification']['matched'])
        self.assertEqual(numeric['verification']['level'],'tested')

    def test_null_and_outer_join_counterexample_is_detected(self):
        self.db.execute('CREATE TABLE r(k INTEGER); INSERT INTO r VALUES (1)')
        original = 'SELECT t.id,r.k FROM t LEFT JOIN r ON t.a=r.k'
        candidate = 'SELECT t.id,r.k FROM t INNER JOIN r ON t.a=r.k'
        report = self.db.optimize_query(original,candidate)
        self.assertFalse(report['verification']['matched'])
        self.assertFalse(report['recommendation']['replace'])

    def test_exact_cache_and_template_recipe_do_not_reuse_literals(self):
        one = self.db.optimize_query('SELECT id FROM t WHERE a=1 OR a=2')
        again = self.db.optimize_query('SELECT id FROM t WHERE a=1 OR a=2')
        self.assertFalse(one['cache']['hit'])
        self.assertTrue(again['cache']['hit'])
        self.assertEqual(again['timings']['validation_ms'],0)
        self.assertIn('cached_experiment_ms',again['timings'])
        another = self.db.optimize_query('SELECT id FROM t WHERE a=3 OR a=4')
        self.assertFalse(another['cache']['hit'])
        self.assertTrue(another['cache']['template_hit'])
        self.assertIn('(3,4)',another['candidate_sql'])
        self.assertTrue(another['verification']['matched'])

    def test_ddl_and_reanalysis_clear_cached_conclusions(self):
        self.db.optimize_query('SELECT id FROM t WHERE a=1 OR a=2')
        self.db.analyze_statistics()
        self.assertEqual(self.db.research_status()['cache']['entries'],0)
        self.db.optimize_query('SELECT id FROM t WHERE a=1 OR a=2')
        self.db.execute('DROP INDEX ix_a')
        status = self.db.research_status()
        self.assertEqual(status['cache']['entries'],0)
        self.assertFalse(status['statistics']['available'])

    def test_unknown_order_semantics_are_not_experimentally_accepted(self):
        report = self.db.optimize_query('SELECT id FROM t ORDER BY id','SELECT id FROM t ORDER BY id DESC')
        self.assertEqual(report['verification']['level'],'unknown')
        self.assertIsNone(report['verification']['matched'])
        self.assertFalse(report['recommendation']['replace'])
        hidden = self.db.optimize_query('SELECT b FROM t WHERE a=1 OR a=2 ORDER BY a')
        self.assertEqual(hidden['verification']['level'],'unknown')
        self.assertIn('输出',hidden['verification']['reason'])

    def test_order_ties_are_bags_within_identical_order_keys(self):
        for order in ('a','1','t.a'):
            with self.subTest(order=order):
                report = self.db.optimize_query('SELECT a,b FROM t WHERE a=1 OR a=2 ORDER BY '+order)
                self.assertTrue(report['verification']['matched'])
                self.assertEqual(report['verification']['comparison'],'ordered-key-groups')

    def test_limit_offset_and_write_candidates_are_rejected_without_writes(self):
        for sql, candidate in [('SELECT * FROM t LIMIT 1',None),
                               ('SELECT * FROM t','SELECT * FROM t LIMIT 2'),
                               ('SELECT * FROM t','DELETE FROM t'),
                               ('SELECT * FROM t','SELECT * FROM t; DELETE FROM t')]:
            with self.subTest(sql=sql,candidate=candidate), self.assertRaises(DatabaseError):
                self.db.optimize_query(sql,candidate)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM t')[0]['rows'],[[100]])

    def test_short_queries_keep_original_despite_trusted_rule(self):
        report = self.db.optimize_query('SELECT 1 WHERE TRUE')
        self.assertEqual(report['verification']['level'],'proven')
        self.assertFalse(report['recommendation']['replace'])
        self.assertIn('门槛',report['recommendation']['reason'])

    def test_execution_error_does_not_poison_database_or_leave_budget(self):
        report = self.db.optimize_query('SELECT ABS(b) FROM t')
        self.assertEqual(report['verification']['level'],'unknown')
        self.assertIsNone(self.db._research_budget)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM t')[0]['rows'],[[100]])

    def test_budget_rejects_large_join_and_preserves_following_queries(self):
        report = self.db.optimize_query('SELECT t.id,x.id FROM t JOIN t AS x ON TRUE JOIN t AS y ON TRUE')
        self.assertEqual(report['verification']['level'],'unknown')
        self.assertIn('预算',report['verification']['reason'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM t')[0]['rows'],[[100]])

    def test_explain_exposes_statistics_estimates_actual_rows_and_error_feedback(self):
        self.db.analyze_statistics()
        self.db.set_optimizer_mode('adaptive')
        result = self.db.execute('EXPLAIN ANALYZE SELECT id FROM t WHERE a<8')[0]
        filter_node = next(p for p in plans(result['plan']) if p['operator']=='Filter')
        self.assertEqual(filter_node['actual_rows'],39)
        self.assertEqual(filter_node['estimation']['source'],'histogram-independence')
        self.assertTrue(result['research']['statistics_available'])
        self.assertGreater(self.db.research_status()['learning']['observations'],0)

    def test_monotone_calibrator_and_out_of_training_range_fallback(self):
        model = RidgeModel(2,monotone=True)
        for i in range(12): model.observe([1,i/20],i/25)
        self.assertTrue(model.status()['ready'])
        estimates = [model.predict([1,i/20]) for i in range(12)]
        self.assertEqual(estimates,sorted(estimates))
        self.assertIsNone(model.predict([1,.99]))

    def test_access_model_is_conservative_and_never_changes_exact_candidate_count(self):
        state = ResearchState()
        state.tables = {'t':{}}
        state.mode = 'adaptive'
        for i in range(12):
            features = state.cost_features(100+i*100,10+i)
            state.cost_models['TableScan'].observe(features,10+i)
            state.cost_models['IndexScan'].observe(features,1+i*.1)
        selected, detail = state.choose_access(500,500,14,False,True)
        self.assertTrue(selected)
        self.assertEqual(detail['exact_candidate_rows'],500)
        _, warm = state.choose_access(500,500,14,False,False)
        self.assertIn('warm',warm['fallback_reason'])
        selected, outside = state.choose_access(999999,999999,14,False,True)
        self.assertFalse(selected)
        self.assertIn('range',outside['fallback_reason'])

    def test_join_ndv_estimate_keeps_left_join_lower_bound(self):
        self.db.execute('CREATE TABLE r(k INTEGER); INSERT INTO r VALUES (1),(1),(2),(NULL)')
        self.db.analyze_statistics(); self.db.set_optimizer_mode('adaptive')
        result = self.db.execute('EXPLAIN ANALYZE SELECT t.id,r.k FROM t LEFT JOIN r ON t.a=r.k')[0]
        join = next(p for p in plans(result['plan']) if p['operator']=='HashJoin')
        self.assertGreaterEqual(join['estimated_rows'],100)
        self.assertEqual(join['actual_rows'],105)
        self.assertEqual(join['estimation']['source'],'NDV-uniform-equijoin')

    def test_buffer_policy_and_sample_size_invalidate_costs_not_statistics(self):
        self.db.analyze_statistics()
        self.db.optimize_query('SELECT id FROM t WHERE a=1 OR a=2')
        self.assertGreater(self.db.research_status()['cache']['entries'],0)
        status = self.db.set_buffer_policy('predictive',4)
        self.assertEqual(status['sample_size'],4)
        self.assertTrue(status['research']['statistics']['available'])
        self.assertEqual(status['research']['cache']['entries'],0)
        self.assertTrue(all(model['samples']==0 for model in status['research']['learning']['cost_models'].values()))
        first = self.db.optimize_query('SELECT id FROM t WHERE a=1 OR a=2')
        self.db.set_buffer_policy('predictive',8)
        second = self.db.optimize_query('SELECT id FROM t WHERE a=1 OR a=2')
        self.assertFalse(second['cache']['hit'])
        self.assertGreater(second['cache']['generation'],first['cache']['generation'])
        self.db.execute('BEGIN')
        with self.assertRaises(DatabaseError): self.db.set_buffer_policy('lru')
        self.assertTrue(self.db.active)
        self.db.execute('ROLLBACK')

    def test_histogram_boundary_fallback_retains_global_monotonicity(self):
        state = ResearchState(); state.mode='adaptive'
        state.tables={'t':{'row_count':100,'columns':{'a':{'type':'INTEGER','ndv':100,
            'null_count':0,'min':0,'max':99,'frequent_values':[],
            'histogram':[{'low':0,'high':99,'count':100,'ndv':100}]}}}}
        model = RidgeModel(2,monotone=True)
        for i in range(12): model.observe([1,.2+i*.04],.3+i*.02)
        state.cardinality_models['t.a:range']=model
        estimates=[]
        for value in range(101):
            expr={'kind':'binary','op':'<','left':{'kind':'column','name':'t.a'},
                  'right':{'kind':'literal','value':value}}
            estimate,_,_=state.estimate_filter('t',[expr],50)
            estimates.append(estimate)
        self.assertEqual(estimates,sorted(estimates))
        self.assertEqual(estimates[0],0)
        self.assertEqual(estimates[-1],100)

    def test_query_metric_deltas_omit_gauges(self):
        result=self.db.execute('SELECT id FROM t WHERE a=1')[0]
        for name in ('predictive_sample_size','cache_history_pages','active_scans','page_size','cached_pages'):
            self.assertNotIn(name,result['stats'])

    def test_boolean_and_integer_expression_types_do_not_collapse(self):
        self.db.execute('CREATE TABLE flags(flag BOOLEAN); CREATE TABLE ints(value INTEGER);'
                        'INSERT INTO flags VALUES (TRUE),(FALSE); INSERT INTO ints VALUES (1),(0)')
        for sql in ('SELECT TRUE=1', 'SELECT FALSE=0', 'SELECT TRUE+1',
                    'SELECT ABS(TRUE)', 'SELECT ROUND(FALSE)',
                    'SELECT TRUE IN (1,TRUE)', 'SELECT flag FROM flags WHERE flag IN (1)'):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                self.db.execute(sql)
        with self.assertRaisesRegex(DatabaseError, 'incompatible types'):
            self.db.execute('SELECT f.flag FROM flags f JOIN ints i ON f.flag=i.value')
        self.assertEqual(self.db.execute('SELECT TRUE=TRUE,FALSE=FALSE')[0]['rows'], [[True,True]])
        self.assertEqual(self.db.execute('SELECT TRUE IN (TRUE),FALSE IN (FALSE)')[0]['rows'], [[True,True]])

    def test_boolean_or_rule_accepts_boolean_literals_and_keeps_index_plan_safe(self):
        self.db.execute('CREATE TABLE flags(flag BOOLEAN); CREATE INDEX flags_idx ON flags(flag);'
                        'INSERT INTO flags VALUES(TRUE),(FALSE),(NULL),(TRUE),(FALSE),(TRUE),'
                        '(FALSE),(TRUE),(FALSE),(TRUE),(FALSE),(TRUE),(FALSE),(TRUE),(FALSE)'
                        + ',' + ','.join('(NULL)' for _ in range(100)))
        report=self.db.optimize_query('SELECT flag FROM flags WHERE flag=TRUE OR flag=FALSE')
        self.assertEqual(report['verification']['level'],'proven')
        self.assertTrue(report['verification']['matched'])
        self.assertIn('or-equalities-to-in',report['verification']['rules'])
        self.assertEqual(report['candidate']['row_count'],14)
        self.assertTrue(any(p['operator']=='IndexScan' for p in plans(report['candidate']['plan'])))

    def test_adaptive_boolean_statistics_reject_numeric_literal_without_cache_lookup(self):
        self.db.execute('CREATE TABLE flags(flag BOOLEAN); CREATE INDEX flags_idx ON flags(flag);'
                        'INSERT INTO flags VALUES(TRUE),(FALSE),(NULL)')
        self.db.analyze_statistics(); self.db.set_optimizer_mode('adaptive')
        with self.assertRaisesRegex(DatabaseError, 'incompatible types'):
            self.db.execute('EXPLAIN ANALYZE SELECT flag FROM flags WHERE flag=1')
        self.assertEqual(self.db.research_status()['cache']['entries'],0)


if __name__ == '__main__':
    unittest.main()
