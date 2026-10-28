"""Bounded dynamic-programming search for safe inner-join orders.

The search builds candidate plan descriptions only. It does not execute query
rows; the caller executes the single selected left-deep order.
"""
from __future__ import annotations

import time


def _conjuncts(expr):
    if expr and expr.get('kind') == 'binary' and expr.get('op') == 'AND':
        return _conjuncts(expr['left']) + _conjuncts(expr['right'])
    return [expr] if expr else []


def _and(expressions):
    result = None
    for expression in expressions:
        result = expression if result is None else {
            'kind':'binary', 'op':'AND', 'left':result, 'right':expression}
    return result


def _column_alias(expression):
    if expression.get('kind') != 'column' or '.' not in expression.get('name', ''):
        return None
    return expression['name'].split('.', 1)[0]


def extract_inner_join_edges(sources):
    """Return safe equality edges, or None when any join is outside scope."""
    if len(sources) < 3 or any(source.get('type', 'INNER') != 'INNER'
                               for source in sources[1:]):
        return None
    prior = {sources[0]['resolved_alias']}
    edges = []
    for source in sources[1:]:
        current = source['resolved_alias']
        atoms = _conjuncts(source.get('on'))
        if not atoms:
            return None
        source_edges = []
        for atom in atoms:
            if atom.get('kind') == 'literal' and atom.get('value') is True:
                continue  # comma/CROSS inputs can acquire edges at a later prefix
            if atom.get('kind') != 'binary' or atom.get('op') != '=':
                return None
            left, right = atom.get('left', {}), atom.get('right', {})
            a, b = _column_alias(left), _column_alias(right)
            if not a or not b or a == b:
                return None
            if a == current and b in prior:
                source_edges.append((a, left['name'], b, right['name'], atom))
            elif b == current and a in prior:
                source_edges.append((b, right['name'], a, left['name'], atom))
            else:
                return None
        edges.extend(source_edges)
        prior.add(current)
    return edges


def choose_inner_join_order(sources, base_rows, edge_estimator,
                            max_relations=6, budget_ms=20.0, preferred_order=None,
                            access_costs=None, join_cost_estimator=None,
                            base_confidence=None, access_cost_sources=None,
                            uncertainty_ms=0.0,
                            candidate_limit=512):
    """Choose a connected left-deep order with bounded subset dynamic programming.

    `base_rows` maps source aliases to their filtered cardinality estimates.
    `edge_estimator` receives (left alias, left column, right alias, right
    column, left rows, right rows) and returns an output-row estimate.
    """
    started = time.perf_counter()
    edges = extract_inner_join_edges(sources)
    elapsed = lambda: (time.perf_counter() - started) * 1000
    if edges is None:
        return {'eligible':False, 'reason':'unsupported join shape', 'elapsed_ms':round(elapsed(),4)}
    if len(sources) > max_relations:
        return {'eligible':False, 'reason':f'join search limit is {max_relations} relations',
                'elapsed_ms':round(elapsed(),4)}

    aliases = [source['resolved_alias'] for source in sources]
    index = {alias:position for position,alias in enumerate(aliases)}
    by_alias = {source['resolved_alias']:source for source in sources}
    access_costs=access_costs or {alias:float(base_rows[alias])+
        by_alias[alias].get('_optimizer_pages',0) for alias in aliases}
    join_cost_estimator=join_cost_estimator or (lambda left,right,out:
        .001*(max(0,left)+max(0,right)+max(0,out)))
    base_confidence=base_confidence or {}
    access_cost_sources=access_cost_sources or {}
    confidence_rank={'low':0,'medium':1,'high':2}
    def lower_confidence(left,right):
        return left if confidence_rank.get(left,0)<=confidence_rank.get(right,0) else right
    # Keep a Pareto frontier per relation set: a cheaper prefix can still have
    # more rows than another prefix and therefore lose after the next join.
    best = {}
    for position, alias in enumerate(aliases):
        rows = max(0.0, float(base_rows[alias]))
        best[1 << position] = [{'order':(alias,), 'rows':rows,
                                'cost':float(access_costs[alias]),
                                'confidence':base_confidence.get(alias,'medium'),
                                'cost_sources':(access_cost_sources.get(alias,'unspecified'),)}]

    full = (1 << len(aliases)) - 1
    preferred_order=tuple(preferred_order or ())
    def rank(candidate):
        follows_workload=(bool(preferred_order) and
                          candidate['order']==preferred_order[:len(candidate['order'])])
        return (round(candidate['cost'],6),-confidence_rank.get(candidate['confidence'],0),
                0 if follows_workload else 1,candidate['order'])
    generated=0
    for size in range(1, len(aliases)):
        for mask in range(1, full + 1):
            states = best.get(mask)
            if not states or mask.bit_count() != size:
                continue
            joined = {aliases[i] for i in range(len(aliases)) if mask & (1 << i)}
            for state in states:
                for alias in aliases:
                    bit = 1 << index[alias]
                    if mask & bit:
                        continue
                    connecting = [edge for edge in edges
                                  if ((edge[0] == alias and edge[2] in joined) or
                                      (edge[2] == alias and edge[0] in joined))]
                    if not connecting:
                        continue
                    if elapsed() > budget_ms:
                        return {'eligible':True, 'selected':False,
                                'reason':'join search budget exceeded', 'elapsed_ms':round(elapsed(),4),
                                'candidate_prefixes':generated}
                    if generated >= candidate_limit:
                        return {'eligible':True, 'selected':False,
                                'reason':f'join candidate limit {candidate_limit} exceeded',
                                'elapsed_ms':round(elapsed(),4),
                                'candidate_prefixes':generated}
                    right_rows = max(0.0, float(base_rows[alias]))
                    output_rows = state['rows']
                    confidence=lower_confidence(state['confidence'],
                                                base_confidence.get(alias,'medium'))
                    for edge in connecting:
                        if edge[0] == alias:
                            left_alias, left_col, right_alias, right_col = edge[2], edge[3], edge[0], edge[1]
                        else:
                            left_alias, left_col, right_alias, right_col = edge[0], edge[1], edge[2], edge[3]
                        edge_result=edge_estimator(left_alias,left_col,right_alias,
                                                   right_col,output_rows,right_rows)
                        if isinstance(edge_result,dict):
                            output_rows=edge_result['rows']
                            confidence=lower_confidence(confidence,edge_result.get('confidence','medium'))
                        else:
                            output_rows=edge_result
                    output_rows = max(0.0, float(output_rows))
                    join_cost=join_cost_estimator(state['rows'],right_rows,output_rows)
                    cost=state['cost']+float(access_costs[alias])+float(join_cost['cost_ms']
                        if isinstance(join_cost,dict) else join_cost)
                    cost_sources=state['cost_sources']+(access_cost_sources.get(alias,'unspecified'),)
                    if isinstance(join_cost,dict):
                        confidence=lower_confidence(confidence,join_cost.get('confidence','medium'))
                        cost_sources+=(join_cost.get('source','rule-cost'),)
                    else:
                        cost_sources+=('rule-cost',)
                    next_mask = mask | bit
                    candidate = {'order':state['order'] + (alias,), 'rows':output_rows,
                                 'cost':cost,'confidence':confidence,
                                 'cost_sources':cost_sources}
                    generated+=1
                    frontier=best.setdefault(next_mask,[])
                    if any(existing['cost'] <= candidate['cost'] and
                           existing['rows'] <= candidate['rows'] and
                           confidence_rank.get(existing['confidence'],0) >=
                           confidence_rank.get(candidate['confidence'],0) and
                           (existing['cost'] < candidate['cost'] or
                            existing['rows'] < candidate['rows'] or
                            confidence_rank.get(existing['confidence'],0) >
                            confidence_rank.get(candidate['confidence'],0))
                           for existing in frontier):
                        continue
                    frontier[:]=[existing for existing in frontier
                        if not (candidate['cost'] <= existing['cost'] and
                                candidate['rows'] <= existing['rows'] and
                                confidence_rank.get(candidate['confidence'],0) >=
                                confidence_rank.get(existing['confidence'],0) and
                                (candidate['cost'] < existing['cost'] or
                                 candidate['rows'] < existing['rows'] or
                                 confidence_rank.get(candidate['confidence'],0) >
                                 confidence_rank.get(existing['confidence'],0)))]
                    equal=next((i for i,existing in enumerate(frontier)
                                if round(existing['cost'],6)==round(candidate['cost'],6) and
                                   round(existing['rows'],6)==round(candidate['rows'],6)),None)
                    if equal is None:
                        frontier.append(candidate)
                    elif rank(candidate) < rank(frontier[equal]):
                        frontier[equal]=candidate
    result_states=best.get(full,[])
    if not result_states:
        return {'eligible':True, 'selected':False, 'reason':'join graph is disconnected',
                'elapsed_ms':round(elapsed(),4), 'candidate_prefixes':generated}
    result=min(result_states,key=rank)
    original_order=tuple(aliases)
    def order_score(order):
        joined={order[0]}
        rows=max(0.0,float(base_rows[order[0]]))
        cost=float(access_costs[order[0]])
        confidence=base_confidence.get(order[0],'medium')
        cost_sources=[access_cost_sources.get(order[0],'unspecified')]
        for alias in order[1:]:
            right_rows=max(0.0,float(base_rows[alias]))
            left_rows=rows
            confidence=lower_confidence(confidence,base_confidence.get(alias,'medium'))
            connecting=[edge for edge in edges if
                ((edge[0]==alias and edge[2] in joined) or
                 (edge[2]==alias and edge[0] in joined))]
            if not connecting:
                rows=left_rows*right_rows
            for edge in connecting:
                if edge[0]==alias:
                    args=(edge[2],edge[3],edge[0],edge[1])
                else:
                    args=(edge[0],edge[1],edge[2],edge[3])
                estimated=edge_estimator(*args,rows,right_rows)
                if isinstance(estimated,dict):
                    rows=estimated['rows']
                    confidence=lower_confidence(confidence,estimated.get('confidence','medium'))
                else: rows=estimated
            operation=(join_cost_estimator(left_rows,right_rows,rows) if connecting else
                {'cost_ms':.02+.001*(left_rows+right_rows+rows),
                 'source':'heuristic-cartesian-cost','confidence':'medium'})
            cost+=float(access_costs[alias])+float(operation['cost_ms'] if isinstance(operation,dict) else operation)
            cost_sources.append(access_cost_sources.get(alias,'unspecified'))
            if isinstance(operation,dict):
                confidence=lower_confidence(confidence,operation.get('confidence','medium'))
                cost_sources.append(operation.get('source','rule-cost'))
            else: cost_sources.append('rule-cost')
            joined.add(alias)
        return cost,rows,confidence,cost_sources

    baseline_cost,baseline_rows,baseline_confidence,baseline_sources=order_score(original_order)
    margin=max(.05,baseline_cost*.15,float(uncertainty_ms)*2)
    candidate_order=tuple(result['order'])
    benefit=baseline_cost-result['cost']
    if result['confidence']=='low' or baseline_confidence=='low':
        return {'eligible':True,'accepted':False,'selected':False,
                'reason':'plan confidence is low','order':list(original_order),
                'candidate_order':list(candidate_order),'confidence':'low',
                'estimated_cost_ms':round(baseline_cost,4),
                'cost_sources':sorted(set(baseline_sources)),
                'candidate_cost_ms':round(result['cost'],4),
                'baseline_cost_ms':round(baseline_cost,4),'margin_ms':round(margin,4),
                'benefit_ms':round(benefit,4),'candidate_prefixes':generated,
                'elapsed_ms':round(elapsed(),4),'source':'heuristic-fallback'}
    if candidate_order==original_order:
        return {'eligible':True,'accepted':True,'selected':False,
                'reason':'original order is lowest-cost candidate','order':list(original_order),
                'candidate_order':list(candidate_order),'confidence':result['confidence'],
                'estimated_rows':round(result['rows'],3),
                'estimated_cost_ms':round(result['cost'],4),
                'cost_sources':sorted(set(result['cost_sources'])),
                'baseline_cost_ms':round(baseline_cost,4),'margin_ms':round(margin,4),
                'benefit_ms':round(benefit,4),'candidate_prefixes':generated,
                'elapsed_ms':round(elapsed(),4),'source':'original-order'}
    if benefit<=margin:
        return {'eligible':True,'accepted':False,'selected':False,
                'reason':'predicted benefit below uncertainty margin','order':list(original_order),
                'candidate_order':list(candidate_order),'confidence':result['confidence'],
                'estimated_cost_ms':round(baseline_cost,4),
                'candidate_cost_ms':round(result['cost'],4),
                'cost_sources':sorted(set(result['cost_sources'])),
                'baseline_cost_ms':round(baseline_cost,4),'margin_ms':round(margin,4),
                'benefit_ms':round(benefit,4),'candidate_prefixes':generated,
                'elapsed_ms':round(elapsed(),4),'source':'heuristic-fallback'}
    return {'eligible':True,'accepted':True,'selected':True,'order':list(candidate_order),
            'candidate_order':list(candidate_order),'estimated_rows':round(result['rows'],3),
            'cost_sources':sorted(set(result['cost_sources'])),
            'estimated_cost_ms':round(result['cost'],4),
            'baseline_cost_ms':round(baseline_cost,4),'margin_ms':round(margin,4),
            'benefit_ms':round(benefit,4),'confidence':result['confidence'],
            'source':'dynamic-programming','candidate_prefixes':generated,
            'elapsed_ms':round(elapsed(),4)}


def predicates_for_next(edges, new_alias, joined_aliases):
    """Return join predicates that become evaluable when `new_alias` is added."""
    selected = []
    for edge in edges:
        a, b = edge[0], edge[2]
        if (a == new_alias and b in joined_aliases) or (b == new_alias and a in joined_aliases):
            selected.append(edge[4])
    return _and(selected)
