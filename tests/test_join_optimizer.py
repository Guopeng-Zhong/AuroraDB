"""Bounded multi-table join planning and workload-feedback regressions."""
import tempfile
import unittest
from collections import Counter

from auroradb.engine import Database, row_signature
from auroradb.join_optimizer import choose_inner_join_order
from auroradb.workload import WorkloadManager, fingerprint


def source(alias, on=None, join_type='INNER'):
    return {'table': alias, 'resolved_alias': alias, 'type': join_type, 'on': on}


def edge(a, b):
    return ('binary', {'kind':'binary','op':'=',
        'left':{'kind':'column','name':a+'.id'},
        'right':{'kind':'column','name':b+'.id'}})


def join_sources(aliases):
    result=[source(aliases[0])]
    prior={aliases[0]}
    parent=aliases[0]
    for alias in aliases[1:]:
        result.append(source(alias,edge(parent,alias)[1]))
        prior.add(alias)
        parent=alias
    return result


class JoinOptimizerTests(unittest.TestCase):
    def test_dp_reorders_without_executing_candidates_and_honors_cost(self):
        sources=join_sources(['large','middle','small'])
        calls=[]
        def cardinality(left_alias,left_column,right_alias,right_column,left,right):
            calls.append((left_alias,right_alias,left,right))
            return {'rows':left*right*.001,'confidence':'high'}
        result=choose_inner_join_order(sources,
            {'large':100000,'middle':1000,'small':10},cardinality,
            access_costs={'large':1,'middle':1,'small':1},
            join_cost_estimator=lambda left,right,out:{'cost_ms':.001*(left+right+out),
                                                        'confidence':'high'})
        self.assertTrue(result['accepted'])
        self.assertTrue(result['selected'])
        self.assertEqual(result['candidate_order'],['middle','small','large'])
        self.assertGreater(result['candidate_prefixes'],0)
        self.assertGreater(len(calls),3)  # estimates only; the executor is not invoked here

    def test_rejects_outer_joins_disconnected_graphs_and_relation_limit(self):
        sources=join_sources(['a','b','c'])
        sources[1]['type']='LEFT'
        result=choose_inner_join_order(sources,{'a':1,'b':1,'c':1},lambda *args:1)
        self.assertFalse(result['eligible'])
        self.assertIn('unsupported',result['reason'])

        too_many=join_sources([f't{i}' for i in range(7)])
        result=choose_inner_join_order(too_many,{f't{i}':1 for i in range(7)},lambda *args:1)
        self.assertFalse(result['eligible'])
        self.assertIn('limit',result['reason'])

    def test_low_confidence_budget_and_small_benefit_fall_back(self):
        sources=join_sources(['large','middle','small'])
        estimator=lambda *args:{'rows':1,'confidence':'high'}
        low=choose_inner_join_order(sources,{'large':1000,'middle':100,'small':1},estimator,
             base_confidence={'large':'low'},
             join_cost_estimator=lambda *args:{'cost_ms':1,'confidence':'high'})
        self.assertFalse(low['accepted'])
        self.assertEqual(low['source'],'heuristic-fallback')
        self.assertIn('confidence',low['reason'])

        budget=choose_inner_join_order(sources,{'large':1000,'middle':100,'small':1},estimator,
             budget_ms=0)
        self.assertFalse(budget.get('accepted',False))
        self.assertIn('budget',budget['reason'])

        candidates=choose_inner_join_order(sources,
             {'large':1000,'middle':100,'small':1},estimator,candidate_limit=1)
        self.assertFalse(candidates.get('accepted',False))
        self.assertIn('candidate limit',candidates['reason'])

        no_benefit=choose_inner_join_order(sources,{'large':100000,'middle':1000,'small':10},
             lambda left_alias,left_column,right_alias,right_column,left,right:
                 {'rows':left*right*.001,'confidence':'high'},
             join_cost_estimator=lambda left,right,out:{'cost_ms':.001*(left+right+out),
                 'confidence':'high'},uncertainty_ms=10000)
        self.assertFalse(no_benefit['accepted'])
        self.assertIn('margin',no_benefit['reason'])

    def test_frontier_keeps_a_higher_confidence_prefix(self):
        sources=join_sources(['large','middle','small'])
        def cardinality(left_alias,left_column,right_alias,right_column,left,right):
            return {'rows':left*right*.001,
                    'confidence':'low' if (left_alias,right_alias)==('small','middle')
                    else 'high'}
        result=choose_inner_join_order(sources,
            {'large':100000,'middle':1000,'small':10},cardinality,
            join_cost_estimator=lambda left,right,out:{'cost_ms':.001*(left+right+out),
                                                       'confidence':'high'})
        self.assertFalse(result['accepted'])
        self.assertEqual(result['confidence'],'medium')
        self.assertIn('margin',result['reason'])
        self.assertEqual(result['candidate_order'],['middle','small','large'])

    def test_workload_fingerprint_masks_literal_values_and_is_bounded(self):
        a={'where':{'kind':'binary','op':'=','left':{'kind':'column','name':'t.x'},
                    'right':{'kind':'literal','value':7}}}
        b={'where':{'kind':'binary','op':'=','left':{'kind':'column','name':'t.x'},
                    'right':{'kind':'literal','value':99}}}
        self.assertEqual(fingerprint(a),fingerprint(b))
        self.assertNotEqual(fingerprint(a),fingerprint({'where':{'kind':'binary','op':'=',
            'left':{'kind':'column','name':'t.x'},'right':{'kind':'literal','value':'99'}}}))
        manager=WorkloadManager(capacity=2)
        for i in range(3):
            statement={'column':{'kind':'column','name':f't{i}.x'},
                       'literal':{'kind':'literal','value':i}}
            manager.observe(statement,{'operator':'JoinOrderSearch','join_optimization':
                {'order':['a','b','c'],'source':'dynamic-programming'},'children':[]},
                1,2,3,4,1,1,0)
        self.assertEqual(manager.status()['templates'],2)
        self.assertEqual(manager.preferred_order(a,1,1),None)

    def test_integrated_three_table_plan_preserves_bag_and_reports_feedback(self):
        with tempfile.TemporaryDirectory() as folder, Database(folder,cache_size=16) as db:
            db.execute('''
                CREATE TABLE large(id INTEGER);
                CREATE TABLE middle(id INTEGER);
                CREATE TABLE small(id INTEGER);
            ''')
            for table,count in [('large',2000),('middle',500),('small',1)]:
                for offset in range(0,count,250):
                    values=','.join(f'({value})' for value in
                                    range(offset+1,min(count,offset+250)+1))
                    db.execute(f'INSERT INTO {table} VALUES {values}')
            db.analyze_statistics(); db.set_optimizer_mode('adaptive')
            sql=('SELECT l.id,m.id,s.id FROM large l '
                 'JOIN middle m ON l.id=m.id JOIN small s ON m.id=s.id')
            original_read=db._read
            read_count={}
            def count_read(table,*args,**kwargs):
                read_count[table]=read_count.get(table,0)+1
                return original_read(table,*args,**kwargs)
            db._read=count_read
            preview=db.preview_query(sql)
            self.assertEqual(read_count,{})
            self.assertEqual(db.research_status()['workload']['executions'],0)
            self.assertEqual(db.research_status()['learning']['join_cost_model']['samples'],0)
            self.assertEqual(preview['plan']['join_optimization']['statistics_version'],
                             db.research_status()['statistics']['generation'])
            result=db.execute(sql)[0]
            db._read=original_read
            self.assertTrue(result['research']['join_optimization']['accepted'])
            self.assertTrue(result['research']['join_optimization']['selected'])
            self.assertEqual(result['research']['join_optimization']['order'],['m','s','l'])
            self.assertEqual(read_count,{'middle':1,'small':1,'large':1})
            join_nodes=[item for item in walk(result['plan']) if item['operator'] in {'HashJoin','NestedLoopJoin'}]
            self.assertEqual(len(join_nodes),2)
            self.assertTrue(all('actual_rows' in item and 'q_error' in item for item in join_nodes))
            self.assertEqual(len(result['rows']),1)
            self.assertIn('cost_sources=',next(item for item in walk(result['plan'])
                                                if item['operator']=='JoinOrderSearch')['detail'])
            self.assertGreaterEqual(db.research_status()['workload']['executions'],1)
            self.assertGreater(db.research_status()['learning']['join_cost_model']['samples'],0)
            self.assertNotIn('sql',next(iter(db._research.workload.profiles.values())))

            db.set_optimizer_mode('heuristic')
            baseline=db.execute(sql)[0]
            self.assertEqual(Counter(row_signature(row) for row in baseline['rows']),
                             Counter(row_signature(row) for row in result['rows']))

            db.execute('INSERT INTO large VALUES (2001)')
            invalidated=db.research_status()
            self.assertFalse(invalidated['statistics']['available'])
            self.assertEqual(invalidated['workload']['templates'],0)
            self.assertEqual(invalidated['learning']['join_cost_model']['samples'],0)
            db.set_optimizer_mode('adaptive')
            fallback=db.preview_query(sql)['plan']['join_optimization']
            self.assertFalse(fallback['accepted'])
            self.assertEqual(fallback['source'],'heuristic-fallback')
            self.assertEqual(fallback['reason'],'adaptive statistics unavailable')


def walk(plan):
    yield plan
    for child in plan.get('children',[]):
        yield from walk(child)


if __name__=='__main__':
    unittest.main()
