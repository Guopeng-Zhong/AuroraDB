"""Reproducible baseline, held-out, learning, and drift join-planning study."""
from __future__ import annotations

import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from auroradb.engine import Database, row_signature
from auroradb.optimizer_demo import QUERY as HELD_OUT, TRAINING, build_database


OOD = (
    "SELECT p.id, c.id FROM registrations r "
    "JOIN classes c ON r.class_id=c.id "
    "JOIN people p ON r.person_id=p.id "
    "WHERE p.department='UNSEEN' AND c.score>=85"
)


def insert_rows(db, table, rows, chunk=250):
    for offset in range(0, len(rows), chunk):
        values=','.join('('+','.join(map(str,row))+')' for row in rows[offset:offset+chunk])
        db.execute(f'INSERT INTO {table} VALUES {values}')


def result_bag(result):
    return Counter(row_signature(row) for row in result['rows'])


def plan_nodes(plan):
    yield plan
    for child in plan.get('children',[]):
        yield from plan_nodes(child)


def summarize(result):
    plan=result['plan']
    joins=[item for item in plan_nodes(plan)
           if item['operator'] in {'HashJoin','NestedLoopJoin'}]
    estimates=[item['q_error'] for item in plan_nodes(plan) if 'q_error' in item]
    return {'elapsed_ms':result['elapsed_ms'],
            'rows_scanned':result['stats'].get('rows_scanned'),
            'join_comparisons':result['stats'].get('join_comparisons'),
            'page_reads':result['stats'].get('disk_reads'),
            'result_rows':len(result['rows']),
            'mean_node_q_error':sum(estimates)/len(estimates) if estimates else None,
            'join_order':result['research']['join_optimization'].get('order'),
            'optimizer_source':result['research']['join_optimization'].get('source'),
            'optimizer_reason':result['research']['join_optimization'].get('reason'),
            'candidate_prefixes':result['research']['join_optimization'].get('candidate_prefixes',0),
            'search_ms':result['research']['join_optimization'].get('elapsed_ms',0),
            'cost_sources':result['research']['join_optimization'].get('cost_sources',[]),
            'join_q_errors':[item.get('q_error') for item in joins]}


def cold_execute(db, sql):
    db.pager.clear_cache()
    return db.execute(sql)[0]


def run():
    with tempfile.TemporaryDirectory(prefix='auroradb-join-study-') as root:
        cases={}
        db=build_database(str(Path(root)/'heuristic'))
        try:
            db.set_optimizer_mode('heuristic')
            cases['heuristic']=cold_execute(db,HELD_OUT)
            heuristic_status=db.research_status()
        finally: db.close()

        db=build_database(str(Path(root)/'statistics-only'))
        try:
            db.analyze_statistics(); db.set_optimizer_mode('adaptive')
            db._research.training_enabled=False
            cases['statistics_only']=cold_execute(db,HELD_OUT)
            statistics_status=db.research_status()
        finally: db.close()

        db=build_database(str(Path(root)/'learned'))
        try:
            analysis=db.analyze_statistics()
            db.set_optimizer_mode('adaptive')
            for _ in range(3):
                for table,column in (('people','id'),('classes','id'),
                                     ('registrations','person_id')):
                    cold_execute(db,f'SELECT {column} FROM {table}')
            for _ in range(3):
                for department in ('CS','ART','BIO'):
                    cold_execute(db,
                        f"SELECT id FROM people WHERE department='{department}'")
            for department,threshold in TRAINING:
                cold_execute(db,
                    "SELECT p.id,c.id FROM registrations r "
                    "JOIN classes c ON r.class_id=c.id "
                    "JOIN people p ON r.person_id=p.id "
                    f"WHERE p.department='{department}' AND c.score>={threshold}")
            training_status=db.research_status()
            cases['learned_adaptive']=cold_execute(db,HELD_OUT)
            heldout_status=db.research_status()
            ood_result=cold_execute(db,OOD)
            ood_fallbacks=[item['estimation'].get('fallback_reason')
                for item in plan_nodes(ood_result['plan'])
                if item.get('estimation',{}).get('fallback_reason')]

            # Shift department frequency and relation sizes in committed data.
            # The write invalidates statistics and every learned conclusion.
            new_people=[(person,"'CS'") for person in range(1001,1101)]
            new_registrations=[(person,((person*7+slot*13)%100)+1)
                               for person in range(1001,1101) for slot in range(10)]
            insert_rows(db,'people',new_people)
            insert_rows(db,'registrations',new_registrations)
            after_drift_status=db.research_status()
            db.set_optimizer_mode('heuristic')
            shifted_baseline=cold_execute(db,HELD_OUT)
            db.set_optimizer_mode('adaptive')
            shifted_adaptive=cold_execute(db,HELD_OUT)
            cases['post_write_heuristic']=shifted_baseline
            cases['post_write_adaptive']=shifted_adaptive
            drift={'inserted_people':len(new_people),
                   'inserted_registrations':len(new_registrations),
                   'statistics_invalidated':not after_drift_status['statistics']['available'],
                   'learned_join_cost_samples':after_drift_status['learning']['join_cost_model']['samples'],
                   'optimizer':summarize(shifted_adaptive),
                   'result_matches_heuristic':result_bag(shifted_baseline)==result_bag(shifted_adaptive)}
            learned_status=db.research_status()
        finally: db.close()

        matched_before_drift=(result_bag(cases['heuristic'])==result_bag(cases['statistics_only'])==
                              result_bag(cases['learned_adaptive']))
        matched_after_drift=drift['result_matches_heuristic']
        report={'matched_results':matched_before_drift and matched_after_drift,
                'matched_before_drift':matched_before_drift,
                'dataset':{'people':1000,'classes':100,'registrations':10000,
                           'distribution':'four department frequencies and three score bands',
                           'statistics_rows_examined':analysis['rows_examined']},
                'heldout_query':HELD_OUT,
                'comparisons':{'heuristic':summarize(cases['heuristic']),
                    'statistics_only':summarize(cases['statistics_only']),
                    'trained_adaptive':summarize(cases['learned_adaptive'])},
                'training':{'join_workload_executions':len(TRAINING),
                    'access_cost_warmup_queries':18,
                    'ready_models':{name:model['ready'] for name,model in
                        training_status['learning']['cost_models'].items()},
                    'join_cost_model':training_status['learning']['join_cost_model'],
                    'join_calibrators':training_status['learning']['join_models']},
                'heldout_feedback':{'workload_templates':heldout_status['workload']['templates'],
                    'optimizer':summarize(cases['learned_adaptive'])},
                'out_of_domain':{'query':OOD,'fallback_reasons':ood_fallbacks,
                    'join_order':summarize(ood_result)['join_order']},
                'distribution_shift':drift,
                'statistics_only_workload_templates':statistics_status['workload']['templates'],
                'heuristic_statistics_available':heuristic_status['statistics']['available'],
                'training_query_fingerprints_are_literal_free':True,
                'limits':['one deterministic synthetic database and one held-out query template',
                          'latencies are local observations, not a general performance claim',
                          'post-write drift is handled by invalidation and heuristic fallback']}
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if matched_before_drift and matched_after_drift else 1


if __name__=='__main__':
    raise SystemExit(run())
