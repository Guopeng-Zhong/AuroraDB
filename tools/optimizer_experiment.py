"""Reproducible research prototype evidence; never calls a remote model.

All timings and pass/fail facts come from real AuroraDB execution. Performance
improvement is an observation, not a required assertion or universal guarantee.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from auroradb.engine import Database, DatabaseError, row_signature
from auroradb.sql import parse


def nodes(plan):
    yield plan
    for child in plan.get('children',[]): yield from nodes(child)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, max(0, math.ceil(len(ordered)*fraction)-1))]


def compact(result):
    return {key:result[key] for key in ('verification','recommendation','timings','cache','warnings')}


def populate(db, table, count):
    db.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY,v INTEGER,note TEXT); CREATE INDEX {table}_v ON {table}(v)')
    values=[]
    for i in range(1,count+1):
        value = 'NULL' if i%17==0 else '0' if i%5==0 else str(i%1000)
        values.append(f"({i},{value},'correlated-{i%1000//10:03d}-{'x'*36}')")
    db.execute(f'INSERT INTO {table} VALUES '+','.join(values))


def heap_estimate(db, query, mode):
    db.set_optimizer_mode(mode)
    db.pager.clear_cache()
    db.counters={'rows_scanned':0,'index_lookups':0,'join_comparisons':0}
    result=db._select(parse(query)[0],force_scan=True)
    filter_node=next(node for node in nodes(result['plan']) if node['operator']=='Filter')
    return {'estimated_rows':filter_node['estimated_rows'], 'actual_rows':filter_node['actual_rows'],
            'q_error':filter_node['q_error'], 'source':filter_node.get('estimation',{}).get('source'),
            'estimation_ms':result['research']['estimation_ms']}


def timed_read(db, query, mode):
    db.set_optimizer_mode(mode)
    runs=[]; output=None
    for _ in range(3):
        db.pager.clear_cache()
        result=db.execute(query)[0]
        access=next(node for node in nodes(result['plan']) if node['operator'] in {'IndexScan','TableScan'})
        runs.append({'elapsed_ms':result['elapsed_ms'],'stats':result['stats'],
                     'access':access['operator'],'cost':access.get('access_cost',{}),
                     'estimation_ms':result['research']['estimation_ms']})
        output=result['rows']
    return {'median_ms':statistics.median(run['elapsed_ms'] for run in runs),'runs':runs}, output


def experiment():
    seed=20261010
    rng=random.Random(seed)
    temporary=ROOT/'.build'/'tmp'
    temporary.mkdir(parents=True,exist_ok=True)
    cases=[]
    with tempfile.TemporaryDirectory(dir=temporary,prefix='optimizer-research-') as folder:
        with Database(folder,cache_size=16) as db:
            populate(db,'t',5000)
            def case(name, query, candidate=None, expected_level=None, expected_match=True):
                result=db.optimize_query(query,candidate)
                passed=(expected_level is None or result['verification']['level']==expected_level)
                passed &= result['verification']['matched'] is expected_match
                evidence=compact(result)
                evidence.update(name=name,sql=query,candidate_sql=result['candidate_sql'],passed=bool(passed),
                                matched=result['verification']['matched'])
                for key in ('original','candidate'):
                    evidence[key]=result[key]
                cases.append(evidence)
                return result
            rewritten=case('trusted_or_index','SELECT id,v FROM t WHERE id=10 OR id=42',expected_level='proven')
            case('exact_same_version_cache','SELECT id,v FROM t WHERE id=10 OR id=42',expected_level='proven')
            case('literal_template_recipe','SELECT id,v FROM t WHERE id=43 OR id=55',expected_level='proven')
            case('null_duplicate_and','SELECT v,note FROM t WHERE TRUE AND (v=1 OR v=NULL) AND (v=1 OR v=NULL)',expected_level='proven')
            case('ordered_tie_bags','SELECT v,note FROM t WHERE v=1 OR v=2 ORDER BY v',expected_level='proven')
            db.execute('CREATE TABLE r(k INTEGER); INSERT INTO r VALUES (1),(1),(2),(NULL)')
            case('left_join_nullable_or','SELECT t.id,r.k FROM t LEFT JOIN r ON t.v=r.k WHERE r.k=1 OR r.k=NULL',expected_level='proven')
            case('external_current_data_only','SELECT id FROM t WHERE id>0','SELECT id FROM t WHERE id>=0',expected_level='tested')
            db.execute("INSERT INTO t VALUES (0,0,'counterexample')")
            case('external_counterexample_after_insert','SELECT id FROM t WHERE id>0','SELECT id FROM t WHERE id>=0',expected_level='unknown',expected_match=False)
            db.execute('DELETE FROM t WHERE id=0')
            case('bag_distinct_counterexample','SELECT v FROM t','SELECT DISTINCT v FROM t',expected_level='unknown',expected_match=False)
            case('boolean_numeric_counterexample','SELECT TRUE AS value','SELECT 1 AS value',expected_level='unknown',expected_match=False)
            case('outer_join_counterexample','SELECT t.id,r.k FROM t LEFT JOIN r ON t.v=r.k',
                 'SELECT t.id,r.k FROM t INNER JOIN r ON t.v=r.k',expected_level='unknown',expected_match=False)
            case('changed_order_unknown','SELECT id FROM t ORDER BY id','SELECT id FROM t ORDER BY id DESC',expected_level='unknown',expected_match=None)
            case('short_query_fallback','SELECT 1 WHERE TRUE',expected_level='proven')
            transaction_checks=[]
            db.execute('BEGIN')
            for name,action in [('analyze',db.analyze_statistics),
                                ('optimize',lambda:db.optimize_query('SELECT id FROM t')),
                                ('mode',lambda:db.set_optimizer_mode('adaptive'))]:
                try: action(); rejected=False
                except DatabaseError: rejected=True
                transaction_checks.append({'action':name,'rejected':rejected,'transaction_preserved':db.active})
            db.execute('ROLLBACK')
            populate(db,'medium',2500); populate(db,'small',1000)
            statistics_report=db.analyze_statistics()
            db.set_optimizer_mode('adaptive')
            training=[]
            cutoffs=[10,25,50,75,100,150,200,300,500,650,800,950]
            # Three table sizes supply actual scan-cost feature variation.
            for table in ('small','medium','t'):
                shuffled=cutoffs[:]; rng.shuffle(shuffled)
                for cutoff in shuffled:
                    query=f'SELECT id FROM {table} WHERE v<{cutoff}'
                    result=db.compare_query(query)
                    training.append({'table':table,'cutoff':cutoff,'matched':result['matched'],
                        'scan_ms':result['scan']['elapsed_ms'],'optimized_ms':result['optimized']['elapsed_ms'],
                        'scan_stats':result['scan']['stats'],'optimized_stats':result['optimized']['stats']})
            trained_status=db.research_status()
            # Hold out thresholds and freeze coefficients, avoiding evaluation
            # leakage while showing histogram and learned calibration sources.
            db._research.training_enabled=False
            heldout=[3,17,29,61,109,211,499,777,925]
            cardinality=[]; execution=[]
            for cutoff in heldout:
                query=f'SELECT id FROM t WHERE v<{cutoff}'
                heuristic=heap_estimate(db,query,'heuristic')
                adaptive=heap_estimate(db,query,'adaptive')
                cardinality.append({'cutoff':cutoff,'heuristic':heuristic,'adaptive':adaptive})
                modes=['heuristic','adaptive']; rng.shuffle(modes)
                actual={}; rows={}
                for mode in modes: actual[mode],rows[mode]=timed_read(db,query,mode)
                execution.append({'cutoff':cutoff,'heuristic':actual['heuristic'],'adaptive':actual['adaptive'],
                                  'comparison':'multiset',
                                  'ordered_rows_same':rows['heuristic']==rows['adaptive'],
                                  'matched':Counter(row_signature(row) for row in rows['heuristic'])==Counter(row_signature(row) for row in rows['adaptive'])})
            db.set_optimizer_mode('adaptive')
            monotone=[]
            for cutoff in range(0,1001,10):
                result=db._select(parse(f'SELECT id FROM t WHERE v<{cutoff}')[0],explain_only=True,force_scan=True)
                filter_node=next(node for node in nodes(result['plan']) if node['operator']=='Filter')
                monotone.append({'cutoff':cutoff,'estimated_rows':filter_node['estimated_rows'],
                                 'source':filter_node['estimation']['source']})
            violations=sum(left['estimated_rows']>right['estimated_rows'] for left,right in zip(monotone,monotone[1:]))
            drift_before=db.research_status()
            db.execute("INSERT INTO t VALUES (5001,1,'distribution-change')")
            drift_after=db.research_status()
            refreshed=db.analyze_statistics()
            learning={'statistics':statistics_report,'training_queries':len(training),'training':training,
                      'trained_status':trained_status,'heldout_cardinality':cardinality,
                      'heldout_execution':execution,
                      'monotonicity':{'samples':len(monotone),'violations':violations,'trace':monotone},
                      'data_change':{'before_generation':drift_before['generation'],
                                     'after_generation':drift_after['generation'],
                                     'old_statistics_invalidated':not drift_after['statistics']['available'],
                                     'models_cleared':all(not value['samples'] for value in drift_after['learning']['cost_models'].values()),
                                     'refreshed_rows':refreshed['tables']['t']['row_count']}}
            learning['summary']={}
            for mode in ('heuristic','adaptive'):
                errors=[item[mode]['q_error'] for item in cardinality]
                times=[item[mode]['median_ms'] for item in execution]
                learning['summary'][mode]={'q_error_median':statistics.median(errors),
                                          'q_error_p95':percentile(errors,.95),
                                          'execution_ms_median':statistics.median(times),
                                          'execution_ms_p95':percentile(times,.95),
                                          'execution_ms_total':round(sum(times),4)}
            learning['summary']['regressions_15pct_and_1ms']=sum(
                item['adaptive']['median_ms']>item['heuristic']['median_ms']*1.15
                and item['adaptive']['median_ms']-item['heuristic']['median_ms']>1 for item in execution)
            learning['summary']['access_path_changes']=sum(
                item['adaptive']['runs'][0]['access']!=item['heuristic']['runs'][0]['access'] for item in execution)
            learning['summary']['all_results_matched']=all(item['matched'] for item in execution)
            passed=(all(item['passed'] for item in cases) and all(item['matched'] for item in training)
                    and learning['summary']['all_results_matched'] and violations==0
                    and all(item['rejected'] and item['transaction_preserved'] for item in transaction_checks)
                    and learning['data_change']['old_statistics_invalidated']
                    and learning['data_change']['models_cleared']
                    and learning['data_change']['refreshed_rows']==5001)
    files=['auroradb/engine.py','auroradb/sql.py','auroradb/optimizer.py','auroradb/learning.py','auroradb/storage.py','tools/optimizer_experiment.py']
    return {'passed':bool(passed),'generated_at':datetime.now(timezone.utc).isoformat(),
            'seed':seed,'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files},
            'rows':5000,'cache_pages':16,'cases':cases,'transaction_checks':transaction_checks,'learning':learning,
            'scope':'paper-inspired finite-dialect prototype; no remote model invocation',
            'cache_policy':'application buffers/history cleared before each measurement; OS cache retained',
            'timing_note':'cache_policy_cpu_ns is elapsed bookkeeping wall-clock ns, not hardware CPU cycles',
            'limits':['Current-data equality is empirical, not universal equivalence.',
                      'Trusted certificates replay only three WHERE rules in AuroraDB semantics.',
                      'Cost learning targets read-path latency; exact index RID enumeration remains.',
                      'Held-out thresholds on deterministic synthetic data do not demonstrate arbitrary OOD generalization.',
                      'Timing includes local execution; LLM invocation/cost is absent and cannot be inferred.',
                      'Validation budget is six seconds, 100000 scanned rows, 200000 join comparisons and 20000 intermediate rows.',
                      'Performance regressions are measured and reported rather than hidden.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'docs'/'optimizer_research.json')
    args=parser.parse_args()
    result=experiment()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases']),
                      'training_queries':result['learning']['training_queries'],
                      'summary':result['learning']['summary'],'output':str(args.output)},ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
