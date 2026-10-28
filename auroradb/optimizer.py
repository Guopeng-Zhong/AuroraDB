"""Trusted finite rewrites, versioned templates and bounded read experiments.

The trusted certificate checks a derivation in this project's own SQL dialect.
It is not QO-Verify's logical-memo prover. Matching current results is reported
separately and never proves arbitrary SQL equivalence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import time
from collections import Counter


RULES = [
    {'id':'or-equalities-to-in', 'description':'同列、类型兼容的等值 OR 合并为 IN；保留 NULL'},
    {'id':'duplicate-and', 'description':'WHERE 中重复的确定性 AND 条件消除；三值逻辑保持'},
    {'id':'where-true', 'description':'WHERE 的 TRUE 条件消除；只改变过滤谓词'}]

LIMITATIONS = [
    '只覆盖当前有限 SQL 子集和三条可信规则，未复现顶会完整系统。',
    'proven 是可重放规则证据；tested 仅表示当前提交数据上的实测相同。',
    '未知外部候选不会自动推荐替换；不执行数据或结构修改。',
    'LIMIT/OFFSET 不参与对比；ORDER BY 必须保持并能由输出列核验。',
    '模型不是跨数据库预训练模型，不保证任意 OOD 查询或数据漂移。',
    '可信改写只改写 WHERE；连接顺序由 adaptive 模式的连接规划另行选择，准确 RID 计数保留。']


def signature(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def template_signature(statement):
    def mask(value):
        if isinstance(value, dict):
            if value.get('kind') == 'literal':
                literal = value['value']
                return {'kind':'literal', 'type':type(literal).__name__}
            return {key:mask(item) for key, item in value.items()}
        if isinstance(value, list):
            return [mask(item) for item in value]
        return value
    return signature(mask(statement))


def _identifier(name):
    return '.'.join('"'+piece.replace('"','""')+'"' for piece in name.split('.'))


def expression_sql(expr):
    kind = expr['kind']
    if kind == 'literal':
        value = expr['value']
        if value is None: return 'NULL'
        if isinstance(value, bool): return 'TRUE' if value else 'FALSE'
        if isinstance(value, str): return "'"+value.replace("'", "''")+"'"
        return repr(value)
    if kind == 'column': return _identifier(expr['name'])
    if kind == 'star': return (_identifier(expr['table'])+'.' if expr.get('table') else '')+'*'
    if kind == 'binary':
        return '('+expression_sql(expr['left'])+' '+expr['op']+' '+expression_sql(expr['right'])+')'
    if kind == 'unary': return '('+expr['op']+' '+expression_sql(expr['expr'])+')'
    if kind == 'is_null': return '('+expression_sql(expr['expr'])+(' IS NOT NULL)' if expr['negated'] else ' IS NULL)')
    if kind == 'in':
        return '('+expression_sql(expr['expr'])+(' NOT IN (' if expr['negated'] else ' IN (')+','.join(expression_sql(value) for value in expr['values'])+'))'
    if kind == 'call': return expr['name']+'('+','.join(expression_sql(arg) for arg in expr['args'])+')'
    raise ValueError('Unsupported expression in trusted serializer')


def select_sql(statement):
    def source_sql(source):
        return _identifier(source['table'])+(' AS '+_identifier(source['alias']) if source.get('alias') else '')
    sql = 'SELECT '+('DISTINCT ' if statement.get('distinct') else '')
    sql += ', '.join(expression_sql(column['expr'])+(' AS '+_identifier(column['alias']) if column.get('alias') else '') for column in statement['columns'])
    if statement.get('from'): sql += ' FROM '+source_sql(statement['from'])
    for source in statement.get('joins', []):
        if source.get('syntax') == 'comma':
            sql += ', '+source_sql(source)
        elif source.get('syntax') == 'cross':
            sql += ' CROSS JOIN '+source_sql(source)
        else:
            sql += ' '+source['type']+' JOIN '+source_sql(source)+' ON '+expression_sql(source['on'])
    if statement.get('where'): sql += ' WHERE '+expression_sql(statement['where'])
    if statement.get('group_by'): sql += ' GROUP BY '+', '.join(expression_sql(e) for e in statement['group_by'])
    if statement.get('having'): sql += ' HAVING '+expression_sql(statement['having'])
    if statement.get('order_by'): sql += ' ORDER BY '+', '.join(expression_sql(o['expr'])+(' DESC' if o['desc'] else ' ASC') for o in statement['order_by'])
    if statement.get('limit') is not None: sql += ' LIMIT '+str(statement['limit'])
    if statement.get('offset'): sql += ' OFFSET '+str(statement['offset'])
    return sql+';'


def _column_types(db, statement):
    available = {}
    unqualified = {}
    for source in ([statement['from']] if statement.get('from') else [])+statement.get('joins', []):
        alias = source.get('alias') or source['table']
        for column in db._table(source['table'])['columns']:
            qualified = alias+'.'+column['name']
            available[qualified] = column['type']
            unqualified.setdefault(column['name'], []).append(qualified)
    def resolve(name):
        if name in available: return name, available[name]
        choices = unqualified.get(name, [])
        return (choices[0], available[choices[0]]) if len(choices)==1 else (None, None)
    return resolve


def _literal_matches_type(datatype, value):
    if value is None:
        return True
    if datatype == 'TEXT':
        return isinstance(value, str)
    if datatype == 'BOOLEAN':
        return isinstance(value, bool)
    if datatype == 'INTEGER':
        return type(value) is int
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def trusted_rewrite(db, statement):
    """Only WHERE transforms; projection/order/group/join syntax is untouched."""
    resolve = _column_types(db, statement)
    rules = []
    def flatten(expr, operator):
        if expr['kind']=='binary' and expr['op']==operator:
            return flatten(expr['left'], operator)+flatten(expr['right'], operator)
        return [expr]
    def chain(items, operator):
        result = items[0]
        for item in items[1:]:
            result = {'kind':'binary', 'op':operator, 'left':result, 'right':item}
        return result
    def rewrite(expr):
        if expr is None: return None
        if expr['kind']=='binary' and expr['op']=='AND':
            items = [rewrite(item) for item in flatten(expr, 'AND')]
            unique, seen = [], set()
            for item in items:
                if item is None or item == {'kind':'literal','value':True}:
                    rules.append('where-true')
                    continue
                key = signature(item)
                if key in seen:
                    rules.append('duplicate-and')
                else:
                    seen.add(key); unique.append(item)
            return chain(unique, 'AND') if unique else None
        if expr['kind']=='binary' and expr['op']=='OR':
            items = flatten(expr, 'OR')
            if not 2 <= len(items) <= 32: return expr
            names, literals, first = set(), [], None
            for item in items:
                if item['kind']!='binary' or item['op']!='=': return expr
                left, right = item['left'], item['right']
                if left['kind']=='literal' and right['kind']=='column': left, right = right, left
                if left['kind']!='column' or right['kind']!='literal': return expr
                name, datatype = resolve(left['name'])
                value = right['value']
                if name is None or not _literal_matches_type(datatype, value):
                    return expr
                names.add(name); literals.append(copy.deepcopy(right)); first = first or left
            if len(names) == 1:
                rules.append('or-equalities-to-in')
                return {'kind':'in', 'expr':copy.deepcopy(first), 'values':literals, 'negated':False}
        if expr == {'kind':'literal','value':True}:
            rules.append('where-true'); return None
        return expr
    candidate = copy.deepcopy(statement)
    candidate['where'] = rewrite(candidate.get('where'))
    return candidate, rules


def _order_selectors(db, statement, columns):
    """Map ORDER keys to output fields; ties are compared as bags, not sequences."""
    from .engine import label
    sources = ([statement['from']] if statement.get('from') else [])+statement.get('joins', [])
    available = [(source.get('alias') or source['table'])+'.'+column['name']
                 for source in sources for column in db._table(source['table'])['columns']]
    output = []
    for item in statement['columns']:
        expr = copy.deepcopy(item['expr'])
        if expr['kind']=='star':
            expanded = [name for name in available if not expr.get('table') or name.startswith(expr['table']+'.')]
            output.extend({'kind':'column','name':name} for name in expanded)
        else:
            db._bind(expr, available)
            output.append(expr)
    selectors = []
    for order in statement.get('order_by', []):
        expr = order['expr']
        if expr['kind']=='literal' and type(expr['value']) is int:
            selectors.append(expr['value']-1); continue
        if expr['kind']=='column' and columns.count(expr['name']) == 1:
            selectors.append(columns.index(expr['name'])); continue
        bound = copy.deepcopy(expr)
        db._bind(bound, available, True)
        matches = [i for i, item in enumerate(output) if signature(item)==signature(bound)]
        if not matches:
            return None
        selectors.append(matches[0])
    return selectors


def _result_signature(rows, selectors):
    from .engine import row_signature
    if not selectors:
        return Counter(row_signature(row) for row in rows)
    groups = []
    for row in rows:
        key = tuple(row[index] for index in selectors)
        if not groups or groups[-1][0] != key:
            groups.append((key, Counter()))
        groups[-1][1][row_signature(row)] += 1
    return groups


def _measure(db, statement, deadline):
    from .engine import metric_delta
    timings, runs, first_result = [], [], None
    for _ in range(3):
        db.pager.clear_cache()
        before = db.pager.stats()
        db.counters = {'rows_scanned':0, 'index_lookups':0, 'join_comparisons':0}
        start = time.perf_counter()
        db._research_budget = {'deadline':deadline, 'max_scanned':100000,
                               'max_comparisons':200000, 'max_rows':20000}
        result = db._select(statement)
        elapsed = (time.perf_counter()-start)*1000
        after = db.pager.stats()
        delta = metric_delta(before,after)
        runs.append({'elapsed_ms':round(elapsed, 4), 'stats':dict(delta, **db.counters)})
        timings.append(elapsed)
        if first_result is None: first_result = result
    median = statistics.median(timings)
    mad = statistics.median(abs(value-median) for value in timings)
    evidence = {'plan':first_result['plan'], 'row_count':first_result['row_count'],
                'elapsed_ms':round(median,4), 'mad_ms':round(mad,4), 'runs':runs,
                'stats':runs[1]['stats'], 'measurements':3}
    return evidence, first_result


def optimize(db, sql, candidate_sql=None):
    from .engine import DatabaseError, StorageError, SQLError
    if db.active:
        raise DatabaseError('研究优化请先提交或回滚事务；活动事务保持不变。')
    if not isinstance(sql, str) or len(sql) > 20000:
        raise DatabaseError('研究查询必须是最多 20000 字符的 SELECT。')
    if candidate_sql is not None and (not isinstance(candidate_sql, str) or len(candidate_sql)>20000):
        raise DatabaseError('候选查询必须是最多 20000 字符的 SELECT。')
    start = time.perf_counter()
    original = db._diagnostic_select(sql)
    original_preview = db._select(original, explain_only=True)
    if original.get('limit') is not None or original.get('offset', 0):
        raise DatabaseError('研究优化请移除 LIMIT/OFFSET；不比较可能由访问顺序选取的子集。')
    generated, rules = trusted_rewrite(db, original)
    candidate_text = candidate_sql if candidate_sql is not None else select_sql(generated)
    candidate = db._diagnostic_select(candidate_text)
    candidate_preview = db._select(candidate, explain_only=True)
    if candidate.get('limit') is not None or candidate.get('offset', 0):
        raise DatabaseError('候选查询不允许 LIMIT/OFFSET。')
    compile_ms = (time.perf_counter()-start)*1000
    state = db._research
    key = signature([state.generation, state.mode, state.model_revision, state.buffer_signature, original, candidate])
    template = template_signature(original)
    template_hit = template in state.templates
    if template_hit: state.template_hits += 1
    if key in state.cache:
        state.cache_hits += 1
        result = copy.deepcopy(state.cache[key])
        state.cache.move_to_end(key)
        result['cache'].update(hit=True, template_hit=template_hit)
        result['timings']['cached_experiment_ms'] = result['timings']['total_ms']
        result['timings']['cached_validation_ms'] = result['timings']['validation_ms']
        result['timings']['compile_ms'] = round(compile_ms,4)
        result['timings']['validation_ms'] = 0
        result['timings']['total_ms'] = round((time.perf_counter()-start)*1000, 4)
        result['timings']['request_ms'] = result['timings']['total_ms']
        result['warnings'].append('缓存结论来自同一数据/结构版本；当前请求未重复执行实验。')
        return result
    state.cache_misses += 1
    same = signature(original)==signature(candidate)
    derived = signature(generated)==signature(candidate)
    proven = same or derived
    proof_rules = ['structural-identity'] if same else rules if derived else []
    result = {'original_sql':sql, 'candidate_sql':candidate_text,
              'verification':{'level':'proven' if proven else 'unknown', 'matched':None,
                'reason':'可信规则证据可重放' if proven else '外部候选没有可信规则推导；仅允许实测比较',
                'rules':proof_rules,
                'certificate':{'original_ast_sha256':hashlib.sha256(signature(original).encode()).hexdigest(),
                               'candidate_ast_sha256':hashlib.sha256(signature(candidate).encode()).hexdigest(),
                               'scope':'AuroraDB finite dialect; WHERE-only deterministic rules'} if proven else None},
              'original':{'plan':original_preview['plan']}, 'candidate':{'plan':candidate_preview['plan']},
              'recommendation':{'replace':False, 'sql':None, 'reason':'尚未完成验证'},
              'timings':{'compile_ms':round(compile_ms, 4), 'validation_ms':0, 'total_ms':0},
              'cache':{'hit':False, 'template_hit':template_hit, 'generation':state.generation,
                       'model_revision':state.model_revision},
              'warnings':['当前数据实测不构成任意数据库上的等价证明。'],
              'cache_policy':'每次测量前清空应用缓冲池；操作系统缓存保留；三次中位数与 MAD。'}
    if signature(original.get('order_by')) != signature(candidate.get('order_by')):
        result['verification'].update(level='unknown', reason='ORDER BY 必须在两个查询中保持一致；未执行候选比较。')
        result['recommendation']['reason'] = '无法核验排序语义'
        result['timings']['total_ms'] = round((time.perf_counter()-start)*1000,4)
        return result
    saved_counters, saved_budget = db.counters, getattr(db, '_research_budget', None)
    saved_training = state.training_enabled
    # Compare both plans against the same learned model snapshot. Ordinary
    # queries/scan comparisons provide feedback; validation does not train.
    state.training_enabled = False
    validation_start = time.perf_counter()
    try:
        deadline = validation_start+6.0
        original_evidence, original_result = _measure(db, original, deadline)
        selectors = _order_selectors(db, original, original_result['columns'])
        if selectors is None:
            result['verification'].update(level='unknown', reason='ORDER BY 键必须出现在输出中；未执行候选比较。')
            result['recommendation']['reason'] = '无法核验排序键'
            result['original'] = original_evidence
        else:
            candidate_evidence, candidate_result = _measure(db, candidate, deadline)
            result['original'], result['candidate'] = original_evidence, candidate_evidence
            matched = (original_result['columns']==candidate_result['columns'] and
                       _result_signature(original_result['rows'], selectors)==
                       _result_signature(candidate_result['rows'], selectors))
            result['verification'].update(matched=matched, comparison='ordered-key-groups' if selectors else 'multiset',
                                          level='proven' if proven and matched else 'tested' if matched else 'unknown')
            if not matched:
                result['verification']['reason'] = '当前提交数据发现结果或输出列差异；拒绝候选。'
                result['recommendation']['reason'] = '结果不一致'
            elif not proven:
                result['verification']['reason'] = '当前完整提交数据实测相同；没有通用等价证明。'
                result['recommendation']['reason'] = '未知改写只提供实验结果，不推荐自动替换'
            elif same:
                result['recommendation']['reason'] = '查询结构相同，无需替换'
            else:
                baseline, proposed = original_evidence['elapsed_ms'], candidate_evidence['elapsed_ms']
                benefit = baseline-proposed
                margin = max(1.0, baseline*.15, 3*(original_evidence['mad_ms']+candidate_evidence['mad_ms']))
                replace = baseline >= 2.0 and benefit >= margin
                result['recommendation'].update(replace=replace, sql=candidate_text if replace else None,
                    reason='可信规则、结果核验及保守收益门槛全部通过' if replace else '查询过短或收益未超过保守噪声门槛',
                    benefit_ms=round(benefit,4), margin_ms=round(margin,4),
                    speedup=round(baseline/proposed,4) if proposed else None)
    except (DatabaseError, StorageError, SQLError, ValueError, TypeError, OverflowError, KeyError) as error:
        result['verification'].update(level='unknown', matched=None, reason='实验未完成：'+str(error))
        result['recommendation']['reason'] = '执行错误或研究预算不足；保留原查询'
    finally:
        db.counters, db._research_budget = saved_counters, saved_budget
        state.training_enabled = saved_training
    result['timings']['validation_ms'] = round((time.perf_counter()-validation_start)*1000,4)
    result['timings']['total_ms'] = round((time.perf_counter()-start)*1000,4)
    benefit = result['recommendation'].get('benefit_ms', 0)
    result['timings']['estimated_break_even_calls'] = math.ceil(result['timings']['total_ms']/benefit) if benefit>0 else None
    result['cache']['model_revision'] = state.model_revision
    final_key = signature([state.generation, state.mode, state.model_revision, state.buffer_signature, original, candidate])
    state.put_bounded(state.cache, final_key, result)
    # Store a recipe, never literals, result rows or reusable proof/performance.
    state.put_bounded(state.templates, template, {'rules':rules, 'generation':state.generation})
    return result
