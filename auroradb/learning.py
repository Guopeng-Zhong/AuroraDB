"""Small, inspectable statistics and online models for the research workbench.

This is a teaching prototype inspired by learned estimation research, not a
reproduction of PRICE or CardOOD. Everything is bounded and standard-library
only. Models never decide which rows qualify or replace exact index RID counts.
"""
from __future__ import annotations

import copy
import math
from collections import Counter, OrderedDict
from .workload import WorkloadManager


def _solve(matrix, target):
    """Pivoted elimination for the tiny regularized normal equations."""
    width = len(target)
    rows = [list(row)+[value] for row, value in zip(matrix, target)]
    for column in range(width):
        pivot = max(range(column, width), key=lambda i: abs(rows[i][column]))
        if abs(rows[pivot][column]) < 1e-12:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        rows[column] = [value/divisor for value in rows[column]]
        for i in range(width):
            if i != column:
                factor = rows[i][column]
                rows[i] = [a-factor*b for a, b in zip(rows[i], rows[column])]
    answer = [row[-1] for row in rows]
    return answer if all(math.isfinite(value) for value in answer) else None


class RidgeModel:
    def __init__(self, width, monotone=False):
        self.width, self.monotone = width, monotone
        self.samples = []
        self.weights = None
        self.mae = None

    def observe(self, features, target):
        if not all(math.isfinite(value) for value in [*features, target]) or target < 0:
            return
        self.samples.append((list(features), float(target)))
        self.samples = self.samples[-96:]
        if len(self.samples) < 8 or len({tuple(x) for x, _ in self.samples}) < 3:
            self.weights = None
            return
        matrix = [[sum(x[i]*x[j] for x, _ in self.samples)
                   + (1e-8 if i == 0 else 1e-4) * (i == j)
                   for j in range(self.width)] for i in range(self.width)]
        target_vector = [sum(x[i]*y for x, y in self.samples) for i in range(self.width)]
        self.weights = _solve(matrix, target_vector)
        if self.weights is not None and self.monotone and self.weights[1] < 0:
            self.weights = None
        if self.weights is not None:
            self.mae = sum(abs(sum(a*b for a, b in zip(self.weights, x))-y)
                           for x, y in self.samples)/len(self.samples)

    def predict(self, features):
        if self.weights is None:
            return None
        # Reject extrapolation, rather than pretending arbitrary OOD robustness.
        for i in range(1, self.width):
            values = [x[i] for x, _ in self.samples]
            if features[i] < min(values)-1e-9 or features[i] > max(values)+1e-9:
                return None
        result = sum(a*b for a, b in zip(features, self.weights))
        return result if math.isfinite(result) and result >= 0 else None

    def status(self):
        return {"samples": len(self.samples), "ready": self.weights is not None,
                "weights": [round(v, 8) for v in self.weights] if self.weights else None,
                "training_mae": round(self.mae, 6) if self.mae is not None else None,
                "training_range":self.feature_ranges()}

    def feature_ranges(self):
        if not self.samples: return None
        return [[round(min(features[index] for features,_ in self.samples),8),
                 round(max(features[index] for features,_ in self.samples),8)]
                for index in range(1,self.width)]


def collect_statistics(db, max_rows=200000, bins=12):
    """Read committed heaps; bounded exact NDV and equi-depth histograms."""
    from .storage import HeapTable
    from .engine import DatabaseError
    tables = {}
    examined = 0
    for table, metadata in db.catalog['tables'].items():
        columns = {column['name']: [] for column in metadata['columns']}
        count = 0
        for _, row in HeapTable(db.pager, metadata).rows():
            examined += 1
            if examined > max_rows:
                raise DatabaseError(f"研究统计最多扫描 {max_rows} 行；统计未发布。")
            count += 1
            for name in columns:
                columns[name].append(row[name])
        output = {}
        for column in metadata['columns']:
            values = columns[column['name']]
            present = sorted(value for value in values if value is not None)
            frequencies = Counter(present)
            histogram = []
            if present:
                chunk = max(1, math.ceil(len(present)/bins))
                for offset in range(0, len(present), chunk):
                    part = present[offset:offset+chunk]
                    histogram.append({'low':part[0], 'high':part[-1], 'count':len(part),
                                      'ndv':len(set(part))})
            output[column['name']] = {
                'type':column['type'], 'null_count':len(values)-len(present),
                'ndv':len(frequencies), 'min':present[0] if present else None,
                'max':present[-1] if present else None, 'histogram':histogram,
                'frequent_values':[{'value':value, 'count':frequency}
                                   for value, frequency in frequencies.most_common(24)]}
        tables[table] = {'row_count':count, 'page_count':len(metadata['pages']), 'columns':output}
    return tables, examined


def _atomic(expr):
    if not expr:
        return None
    if expr['kind'] == 'binary' and expr['op'] in {'=', '<', '<=', '>', '>='}:
        left, right, operator = expr['left'], expr['right'], expr['op']
        if left['kind'] == 'literal' and right['kind'] == 'column':
            left, right = right, left
            operator = {'<':'>', '>':'<', '<=':'>=', '>=':'<=', '=':'='}[operator]
        if left['kind'] == 'column' and right['kind'] == 'literal':
            return left['name'], operator, right['value']
    return None


def _value_matches_column(column_type, value):
    if column_type == 'TEXT':
        return isinstance(value, str)
    if column_type == 'BOOLEAN':
        return isinstance(value, bool)
    if column_type == 'INTEGER':
        return type(value) is int
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fraction_less(column, value, inclusive):
    if value is None or column['min'] is None:
        return 0.0
    total = sum(part['count'] for part in column['histogram'])
    count = 0.0
    for part in column['histogram']:
        low, high = part['low'], part['high']
        if high < value or inclusive and high == value:
            count += part['count']
        elif low > value or not inclusive and low == value:
            continue
        elif low == high:
            count += part['count'] if inclusive and low == value else 0
        elif isinstance(value, (int, float)) and isinstance(low, (int, float)):
            # Decimal-like normalization avoids overflow for finite extremes.
            scale = max(abs(low), abs(high), abs(value), 1)
            ratio = ((value/scale)-(low/scale))/((high/scale)-(low/scale))
            count += part['count']*min(1.0, max(0.0, ratio))
        else:
            count += part['count']*.5
    return count/total if total else 0.0


class ResearchState:
    def __init__(self):
        self.mode = 'heuristic'
        self.generation = 0
        self.tables = {}
        self.statistics_generation = None
        self.cost_models = {name:RidgeModel(3) for name in ('TableScan', 'IndexScan')}
        self.join_cost_model = RidgeModel(4)
        self.cardinality_models = {}
        self.join_models = {}
        self.cardinality_observations = []
        self.join_observations = []
        self.join_validation_errors = {}
        self.cache = OrderedDict()
        self.templates = OrderedDict()
        self.cache_hits = self.cache_misses = self.template_hits = self.invalidations = 0
        self.estimation_ms = 0.0
        self.model_revision = 0
        self.training_enabled = True
        self.buffer_signature = None
        self.statistics_metadata = {'version':None,'analyzed_rows':0,'table_count':0}
        self.workload = WorkloadManager()

    def invalidate(self):
        self.generation += 1
        self.tables = {}
        self.statistics_generation = None
        self.cost_models = {name:RidgeModel(3) for name in ('TableScan', 'IndexScan')}
        self.join_cost_model = RidgeModel(4)
        self.cardinality_models.clear()
        self.join_models.clear()
        self.cardinality_observations.clear()
        self.join_observations.clear()
        self.join_validation_errors.clear()
        self.cache.clear()
        self.templates.clear()
        self.workload.clear()
        self.statistics_metadata = {'version':None,'analyzed_rows':0,'table_count':0}
        self.invalidations += 1
        self.model_revision += 1

    def publish_statistics(self, tables, examined=0):
        # Re-analysis invalidates conclusions and trained coefficients too.
        self.invalidate()
        self.tables = tables
        self.statistics_generation = self.generation
        self.statistics_metadata = {'version':self.generation,
                                    'analyzed_rows':examined,'table_count':len(tables)}

    def reset_buffer_learning(self, signature):
        tables = self.tables
        metadata = self.statistics_metadata
        self.invalidate()
        self.tables = tables
        self.statistics_generation = self.generation if tables else None
        self.statistics_metadata = dict(metadata,version=self.statistics_generation)
        self.buffer_signature = signature

    def _column(self, table, name):
        return self.tables.get(table, {}).get('columns', {}).get(name.rsplit('.', 1)[-1])

    def estimate_atomic(self, table, expr):
        atomic = _atomic(expr)
        if atomic is None:
            if expr and expr['kind'] == 'is_null' and expr['expr']['kind'] == 'column':
                column = self._column(table, expr['expr']['name'])
                if column:
                    count = column['null_count']
                    total = self.tables[table]['row_count']
                    return (total-count if expr['negated'] else count), None
            if expr and expr['kind'] == 'in' and not expr['negated'] and expr['expr']['kind'] == 'column' and all(e['kind']=='literal' for e in expr['values']):
                values = {e['value'] for e in expr['values'] if e['value'] is not None}
                estimates = [self.estimate_atomic(table, {'kind':'binary', 'op':'=',
                             'left':expr['expr'], 'right':{'kind':'literal','value':value}})[0]
                             for value in values]
                if all(value is not None for value in estimates):
                    return min(self.tables[table]['row_count'], sum(estimates)), None
            return None, None
        name, operator, value = atomic
        column = self._column(table, name)
        if not column:
            return None, None
        total = self.tables[table]['row_count']
        present = total-column['null_count']
        if value is None or not present:
            return 0.0, None
        if not _value_matches_column(column['type'], value):
            return None, None
        try:
            if operator == '=':
                common = {entry['value']:entry['count'] for entry in column['frequent_values']}
                if value in common:
                    count = common[value]
                elif value < column['min'] or value > column['max']:
                    count = 0
                else:
                    remaining = present-sum(common.values())
                    count = remaining/max(1, column['ndv']-len(common))
            else:
                less = _fraction_less(column, value, operator in {'<=', '>'})
                count = present*(1-less if operator in {'>', '>='} else less)
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            return None, None
        family = 'eq' if operator == '=' else 'range'
        key = table+'.'+name.rsplit('.', 1)[-1]+':'+family
        return min(float(total), max(0.0, count)), key

    def estimate_filter(self, table, predicates, fallback):
        if self.mode != 'adaptive' or not self.tables:
            return fallback, {'source':'heuristic', 'confidence':'low',
                              'fallback_reason':'adaptive statistics unavailable'}, None
        total = self.tables.get(table, {}).get('row_count', 0)
        estimates = [self.estimate_atomic(table, predicate) for predicate in predicates]
        if not total or any(value is None for value, _ in estimates):
            return fallback, {'source':'heuristic', 'confidence':'low',
                              'fallback_reason':'unsupported predicate or no rows'}, None
        estimate = total
        for value, _ in estimates:
            estimate *= value/total
        source = 'histogram-independence'
        raw_estimate = estimate
        observation = None
        fallback_reason=None
        training_range=None
        if len(estimates) == 1 and estimates[0][1] and total:
            raw, key = estimates[0]
            model = self.cardinality_models.get(key)
            learned = model.predict([1, raw/total]) if model else None
            if learned is not None:
                estimate = min(total, max(0.0, learned*total))
                source = 'monotone-ridge-calibration'
                training_range=[min(x[1] for x,_ in model.samples),
                                max(x[1] for x,_ in model.samples)]
            elif model is not None and model.weights is not None:
                fractions = [features[1] for features, _ in model.samples]
                training_range=[min(fractions),max(fractions)]
                fallback_reason='predicate feature outside calibration range'
                lower, upper = min(fractions), max(fractions)
                fraction = raw/total
                boundary = lower if fraction<lower else upper
                anchor = model.predict([1,boundary])
                if anchor is not None:
                    anchor = min(1.0,max(0.0,anchor))
                    # Anchored histogram fallback, not learned extrapolation.
                    # It prevents a downward jump when leaving training range.
                    estimate = total*(min(fraction,anchor) if fraction<lower else max(fraction,anchor))
                    source = 'histogram-monotone-boundary-fallback'
            observation = (key, raw/total, total, estimate)
        confidence='high' if source=='monotone-ridge-calibration' else 'medium'
        return round(estimate), {'source':source, 'confidence':confidence,
                                'raw_rows':round(raw_estimate, 3),
                                'training_range':training_range,
                                'fallback_reason':fallback_reason,
                                'assumption':'AND independence; no general OOD guarantee'}, observation

    def observe_filter(self, observation, actual):
        if observation is None or not self.training_enabled:
            return
        key, fraction, total, estimated = observation
        model = self.cardinality_models.setdefault(key, RidgeModel(2, monotone=True))
        model.observe([1, fraction], actual/total)
        self.model_revision += 1
        self.cardinality_observations.append({'key':key, 'estimated_rows':estimated,
            'actual_rows':actual, 'q_error':max((actual+1)/(estimated+1),(estimated+1)/(actual+1))})
        self.cardinality_observations = self.cardinality_observations[-96:]

    def estimate_join(self, pair, sources, left_rows, right_rows, join_type):
        if self.mode != 'adaptive' or not self.tables or not pair:
            return max(left_rows, right_rows), {'source':'heuristic','confidence':'low',
                                               'fallback_reason':'adaptive statistics unavailable'}
        raw,key,product,features = self._join_raw(pair,sources,left_rows,right_rows,join_type)
        if raw is None:
            return max(left_rows, right_rows), {'source':'heuristic','confidence':'low',
                                                'fallback_reason':'statistics unavailable'}
        estimate=raw
        evidence={'source':'NDV-uniform-equijoin',
                  'confidence':'medium',
                  'assumption':'uniformity and independence; skew may underestimate',
                  'raw_rows':round(raw,3)}
        model=self.join_models.get(key)
        fallback_reason=None
        training_range=None
        if model:
            learned=model.predict(features)
            recent_errors=self.join_validation_errors.get(key,[])
            median_error=sorted(recent_errors)[len(recent_errors)//2] if recent_errors else None
            unreliable=len(recent_errors)>=5 and median_error>2.5
            training_range=model.feature_ranges()
            if learned is not None and not unreliable:
                estimate=product*min(1.0,max(0.0,learned))
                evidence.update(source='join-ridge-calibration',
                                training_samples=len(model.samples),
                                training_mae=round(model.mae or 0,6),
                                validation_samples=len(recent_errors),
                                validation_q_error_median=round(median_error,4)
                                    if median_error is not None else None,
                                confidence='high' if median_error is not None and median_error<=1.5
                                    else 'medium')
            elif unreliable:
                fallback_reason='recent out-of-sample join Q-error exceeded 2.5'
            elif model.weights is not None:
                fallback_reason='join features outside training range'
        if join_type == 'LEFT':
            estimate = max(left_rows, estimate)
        evidence.update(training_range=training_range,fallback_reason=fallback_reason)
        return round(estimate), evidence

    def _join_raw(self, pair, sources, left_rows, right_rows, join_type='INNER'):
        if not self.tables or not pair: return None,None,None,None
        aliases = {source['resolved_alias']:source['table'] for source in sources}
        columns = []
        for name in pair:
            alias = name.split('.', 1)[0]
            column = self._column(aliases.get(alias, ''), name)
            if not column:
                return None,None,None,None
            columns.append(column)
        denominator = max(columns[0]['ndv'], columns[1]['ndv'], 1)
        present_fractions = []
        for name, column in zip(pair, columns):
            count = self.tables[aliases[name.split('.',1)[0]]]['row_count']
            present_fractions.append((count-column['null_count'])/count if count else 0)
        product=max(0.0,float(left_rows)*float(right_rows))
        estimate=product*math.prod(present_fractions)/denominator
        pair_values=[aliases[name.split('.',1)[0]]+'.'+name.split('.',1)[1] for name in pair]
        pair_key=tuple(sorted(pair_values))
        key='join:'+join_type+':'+pair_key[0]+'='+pair_key[1]
        ratios=[(pair_values[0],float(left_rows)/max(1,self.tables[aliases[pair[0].split('.',1)[0]]]['row_count'])),
                (pair_values[1],float(right_rows)/max(1,self.tables[aliases[pair[1].split('.',1)[0]]]['row_count']))]
        ratios.sort(key=lambda item:(item[0],item[1]))
        features=[1,estimate/product if product else 0,ratios[0][1],ratios[1][1]]
        return estimate,key,product,features

    def observe_join(self, pair, sources, left_rows, right_rows, actual, join_type='INNER'):
        if not self.tables or not self.training_enabled or not pair:
            return
        raw,key,product,features=self._join_raw(pair,sources,left_rows,right_rows,join_type)
        if raw is None or not product:
            return
        model=self.join_models.setdefault(key,RidgeModel(4,monotone=True))
        predicted=model.predict(features)
        if predicted is not None:
            predicted_rows=product*min(1.0,max(0.0,predicted))
            q_error=max((actual+1)/(predicted_rows+1),(predicted_rows+1)/(actual+1))
            errors=self.join_validation_errors.setdefault(key,[])
            errors.append(q_error)
            del errors[:-24]
        model.observe(features,actual/product)
        self.model_revision+=1
        self.join_observations.append({'key':key,'estimated_rows':raw,'actual_rows':actual,
            'q_error':max((actual+1)/(raw+1),(raw+1)/(actual+1)),
            'calibration_validation_q_error':errors[-1] if predicted is not None else None})
        self.join_observations=self.join_observations[-96:]

    @staticmethod
    def cost_features(rows, pages):
        return [1, rows/1000, pages/100]

    @staticmethod
    def join_cost_features(left_rows,right_rows,output_rows):
        return [1,left_rows/1000,right_rows/1000,output_rows/1000]

    @staticmethod
    def heuristic_access_cost(operator,rows,pages):
        pages=max(0,float(pages)); rows=max(0,float(rows))
        if operator=='IndexScan':
            return .025 + rows*.002 + min(pages,rows)*.04
        return .025 + rows*.001 + pages*.04

    def estimate_join_cost(self,left_rows,right_rows,output_rows):
        features=self.join_cost_features(left_rows,right_rows,output_rows)
        prediction=self.join_cost_model.predict(features)
        if prediction is not None and self.join_cost_model.mae is not None and \
                self.join_cost_model.mae<=max(.05,prediction*.5):
            return {'cost_ms':prediction,'source':'ridge-hash-join-cost',
                    'confidence':'high' if self.join_cost_model.mae<=max(.02,prediction*.2)
                    else 'medium','uncertainty_ms':self.join_cost_model.mae,
                    'fallback_reason':None}
        reason=('join cost features outside training range' if self.join_cost_model.weights else
                'insufficient join cost samples')
        return {'cost_ms':.02+.001*(max(0,left_rows)+max(0,right_rows)+max(0,output_rows)),
                'source':'heuristic-hash-join-cost','confidence':'medium',
                'uncertainty_ms':None,'fallback_reason':reason}

    def observe_join_cost(self,left_rows,right_rows,output_rows,elapsed_ms):
        if not self.training_enabled: return
        self.join_cost_model.observe(self.join_cost_features(left_rows,right_rows,output_rows),elapsed_ms)
        self.model_revision+=1

    def observe_access(self, operator, rows, pages, elapsed_ms, cold):
        if self.tables and cold and self.training_enabled:
            self.cost_models[operator].observe(self.cost_features(rows, pages), elapsed_ms)
            self.model_revision += 1

    def choose_access(self, total, candidate_count, pages, heuristic, cold):
        scan_fallback=self.heuristic_access_cost('TableScan',total,pages)
        index_fallback=self.heuristic_access_cost('IndexScan',candidate_count,
                                                  min(pages,candidate_count))
        detail = {'source':'heuristic', 'exact_candidate_rows':candidate_count,
                  'threshold_fraction':.35, 'fallback_reason':None,
                  'scan_cost_ms':round(scan_fallback,4),
                  'index_cost_ms':round(index_fallback,4),
                  'cost_confidence':'medium'}
        if self.mode != 'adaptive' or not self.tables or not cold:
            detail['fallback_reason'] = ('application cache warm' if not cold else
                'adaptive statistics unavailable')
            detail.update(selected_access='IndexScan' if heuristic else 'TableScan',
                          selected_cost_ms=round(index_fallback if heuristic else scan_fallback,4))
            return heuristic, detail
        scan = self.cost_models['TableScan'].predict(self.cost_features(total, pages))
        index = self.cost_models['IndexScan'].predict(self.cost_features(candidate_count, min(pages, candidate_count)))
        if scan is None or index is None:
            reason=('feature outside training range' if all(model.weights for model in self.cost_models.values())
                    else 'insufficient samples')
            detail['fallback_reason'] = reason
            detail.update(selected_access='IndexScan' if heuristic else 'TableScan',
                          selected_cost_ms=round(index_fallback if heuristic else scan_fallback,4))
            return heuristic, detail
        uncertainty = sum(model.mae or 0 for model in self.cost_models.values())
        detail.update(scan_cost_ms=round(scan, 4), index_cost_ms=round(index, 4),
                      uncertainty_ms=round(uncertainty, 4),cost_confidence='medium')
        if abs(scan-index) <= max(.25, max(scan, index)*.2, uncertainty*2):
            detail['fallback_reason'] = 'predicted benefit below noise/uncertainty margin'
            detail.update(selected_access='IndexScan' if heuristic else 'TableScan',
                          selected_cost_ms=round(index_fallback if heuristic else scan_fallback,4))
            return heuristic, detail
        selected=index<scan
        detail.update(source='ridge-read-cost', fallback_reason=None,
                      cost_confidence='high' if uncertainty<=max(.05,min(scan,index)*.2)
                      else 'medium',selected_access='IndexScan' if selected else 'TableScan',
                      selected_cost_ms=round(index if selected else scan,4))
        return selected, detail

    def status(self):
        return {'mode':self.mode, 'generation':self.generation,
                'statistics':{'available':bool(self.tables), 'generation':self.statistics_generation,
                              'tables':len(self.tables),**self.statistics_metadata},
                'learning':{'cost_models':{name:model.status() for name,model in self.cost_models.items()},
                            'join_cost_model':self.join_cost_model.status(),
                            'revision':self.model_revision,
                            'cardinality_models':{name:model.status() for name,model in self.cardinality_models.items()},
                            'observations':len(self.cardinality_observations),
                            'join_models':{name:model.status() for name,model in self.join_models.items()},
                            'join_observations':len(self.join_observations),
                            'join_validation_q_errors':{key:[round(value,4) for value in errors]
                                for key,errors in self.join_validation_errors.items()},
                            'estimation_ms':round(self.estimation_ms, 3),
                            'cost_target':'cold application-buffer read time; OS cache retained'},
                'cache':{'entries':len(self.cache), 'templates':len(self.templates),
                         'hits':self.cache_hits, 'misses':self.cache_misses,
                         'template_hits':self.template_hits, 'invalidations':self.invalidations},
                'workload':self.workload.status(),
                'buffer_signature':list(self.buffer_signature) if self.buffer_signature else None}

    def preferred_join_order(self, statement):
        return self.workload.preferred_order(statement,self.generation,
                                             self.statistics_generation,
                                             self.model_revision)

    def observe_workload(self, statement, plan, elapsed_ms, page_reads, rows_scanned,
                         join_comparisons):
        if not self.training_enabled:
            return
        self.workload.observe(statement,plan,elapsed_ms,page_reads,rows_scanned,
            join_comparisons,self.generation,self.statistics_generation,self.model_revision)

    @staticmethod
    def put_bounded(cache, key, value, limit=64):
        cache[key] = copy.deepcopy(value)
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
