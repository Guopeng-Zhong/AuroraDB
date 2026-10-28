"""Bounded, literal-free workload profiles used by the adaptive optimizer."""
from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict


def _shape(value):
    if isinstance(value, dict):
        if value.get('kind') == 'literal':
            literal=value.get('value')
            kind=('null' if literal is None else 'boolean' if isinstance(literal,bool) else
                  'integer' if type(literal) is int else
                  'real' if isinstance(literal,float) else 'text')
            return {'kind':'literal','type':kind}
        return {key:_shape(item) for key,item in sorted(value.items())}
    if isinstance(value,list): return [_shape(item) for item in value]
    return value


def fingerprint(statement):
    encoded=json.dumps(_shape(statement),sort_keys=True,ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]


class WorkloadManager:
    """Keep bounded aggregate evidence; never retain SQL text or result rows."""
    def __init__(self, capacity=128):
        self.capacity=capacity
        self.profiles=OrderedDict()

    def clear(self):
        self.profiles.clear()

    def preferred_order(self, statement, data_generation, statistics_generation,
                        optimizer_revision=None):
        profile=self.profiles.get(fingerprint(statement))
        if not profile or profile['data_generation']!=data_generation or \
                profile['statistics_generation']!=statistics_generation or \
                optimizer_revision is not None and \
                profile['optimizer_revision']!=optimizer_revision:
            return None
        return list(profile['last_join_order']) or None

    def observe(self, statement, plan, elapsed_ms, page_reads, rows_scanned,
                join_comparisons, data_generation, statistics_generation,
                optimizer_revision):
        template=fingerprint(statement)
        node_errors=[]
        access_paths=[]

        def visit(item):
            if not isinstance(item,dict): return
            error=item.get('q_error')
            if isinstance(error,(int,float)) and math.isfinite(error): node_errors.append(float(error))
            if item.get('operator') in {'TableScan','IndexScan'}:
                access_paths.append(item['operator'])
            for child in item.get('children',[]): visit(child)

        visit(plan)
        optimizer=plan.get('join_optimization',{}) if isinstance(plan,dict) else {}
        profile=self.profiles.get(template)
        if profile is None:
            profile={'fingerprint':template,'executions':0,'mean_elapsed_ms':0.0,
                     'mean_page_reads':0.0,'mean_rows_scanned':0.0,
                     'mean_join_comparisons':0.0,'mean_q_error':None,
                     'q_error_samples':0,'last_join_order':[],
                     'last_access_paths':[],'last_optimizer_source':'original-order',
                     'data_generation':data_generation,
                     'statistics_generation':statistics_generation,
                     'optimizer_revision':optimizer_revision}
        if profile['data_generation']!=data_generation or \
                profile['statistics_generation']!=statistics_generation:
            profile.update(executions=0,mean_elapsed_ms=0.0,mean_page_reads=0.0,
                           mean_rows_scanned=0.0,mean_join_comparisons=0.0,
                           mean_q_error=None,q_error_samples=0,last_join_order=[],
                           last_access_paths=[])
        n=profile['executions']+1
        for key,value in (('mean_elapsed_ms',elapsed_ms),('mean_page_reads',page_reads),
                          ('mean_rows_scanned',rows_scanned),
                          ('mean_join_comparisons',join_comparisons)):
            profile[key]+=(float(value)-profile[key])/n
        if node_errors:
            observed=sum(node_errors)/len(node_errors)
            qn=profile['q_error_samples']+1
            profile['mean_q_error']=(observed if profile['mean_q_error'] is None else
                profile['mean_q_error']+(observed-profile['mean_q_error'])/qn)
            profile['q_error_samples']=qn
        profile.update(executions=n,last_join_order=list(optimizer.get('order',[])),
                       last_access_paths=access_paths,
                       last_optimizer_source=optimizer.get('source','original-order'),
                       data_generation=data_generation,
                       statistics_generation=statistics_generation,
                       optimizer_revision=optimizer_revision)
        self.profiles[template]=profile
        self.profiles.move_to_end(template)
        while len(self.profiles)>self.capacity: self.profiles.popitem(last=False)

    def status(self):
        top=sorted(self.profiles.values(),key=lambda p:(-p['executions'],p['fingerprint']))[:8]
        return {'templates':len(self.profiles),
                'executions':sum(p['executions'] for p in self.profiles.values()),
                'capacity':self.capacity,
                'top_templates':[{'fingerprint':p['fingerprint'],'executions':p['executions'],
                    'mean_elapsed_ms':round(p['mean_elapsed_ms'],4),
                    'mean_page_reads':round(p['mean_page_reads'],3),
                    'mean_rows_scanned':round(p['mean_rows_scanned'],3),
                    'mean_join_comparisons':round(p['mean_join_comparisons'],3),
                    'mean_q_error':round(p['mean_q_error'],4) if p['mean_q_error'] is not None else None,
                    'last_join_order':p['last_join_order'],
                    'last_access_paths':p['last_access_paths'],
                    'last_optimizer_source':p['last_optimizer_source'],
                    'data_generation':p['data_generation'],
                    'statistics_generation':p['statistics_generation']}
                    for p in top]}
