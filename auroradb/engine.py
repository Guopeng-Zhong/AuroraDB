"""Relational execution, semantic binding and transparent physical planning.

All SQL execution uses this engine and our pager, heap and B+ tree. The only
external database used by the project is SQLite in differential tests.
"""
from __future__ import annotations

import copy
import bisect
import base64
import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from functools import cmp_to_key

from .sql import parse, SQLError
from .storage import DiskPager, HeapTable, BPlusTree, StorageError
from .learning import ResearchState, collect_statistics
from .join_optimizer import (choose_inner_join_order, extract_inner_join_edges,
                             predicates_for_next)
from .join_planning import (normalize_inner_joins, safe_input_filters, conjunction,
                            range_join_key)


class DatabaseError(Exception):
    """A user-facing SQL, schema, constraint or storage failure."""


AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}
SCALARS = {"LOWER", "UPPER", "LENGTH", "ABS", "ROUND", "COALESCE"}
METRIC_GAUGES = {'cache_size','cache_used','page_count','predictive_sample_size',
                 'cache_history_pages','active_scans','cached_pages','staged_pages',
                 'page_size','transaction_active','poisoned'}


def metric_delta(before, after):
    return {key:after[key]-before.get(key,0) for key in after
            if isinstance(after[key],(int,float)) and key not in METRIC_GAUGES}


def row_signature(row):
    """Observe output types: BOOLEAN differs from numeric 0/1 in JSON results.

    INTEGER and REAL retain SQL numeric comparison compatibility. The research
    comparison must not silently lose boolean differences through Python hashes.
    """
    return tuple(('null',None) if value is None else ('boolean',value) if isinstance(value,bool)
                 else ('number',value) if isinstance(value,(int,float)) else ('text',value)
                 for value in row)


def _is_numeric(value):
    """SQL numeric values exclude Python's bool subclass of int."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compatible_values(left, right):
    """Whether two non-NULL values have compatible AuroraDB SQL types."""
    if type(left) is type(right):
        return True
    return _is_numeric(left) and _is_numeric(right)


def node(operator, detail="", rows=0, children=None):
    return {"operator": operator, "detail": detail, "estimated_rows": rows,
            "children": children or []}


def walk(expr):
    if not expr:
        return
    yield expr
    for key in ("left", "right", "expr"):
        if isinstance(expr.get(key), dict):
            yield from walk(expr[key])
    for key in ("args", "values"):
        for item in expr.get(key, []):
            yield from walk(item)


def has_aggregate(expr):
    return any(e["kind"] == "call" and e["name"] in AGGREGATES for e in walk(expr))


def truth(value):
    return None if value is None else bool(value)


def sql_and(a, b):
    a, b = truth(a), truth(b)
    return False if a is False or b is False else None if a is None or b is None else True


def sql_or(a, b):
    a, b = truth(a), truth(b)
    return True if a is True or b is True else None if a is None or b is None else False


def label(expr):
    k = expr["kind"]
    if k == "column": return expr["name"]
    if k == "literal": return "NULL" if expr["value"] is None else str(expr["value"])
    if k == "star": return (expr.get("table") + "." if expr.get("table") else "") + "*"
    if k == "call": return expr["name"] + "(" + ", ".join(label(x) for x in expr["args"]) + ")"
    if k == "binary": return f"({label(expr['left'])} {expr['op']} {label(expr['right'])})"
    if k == "unary": return expr["op"] + " " + label(expr["expr"])
    if k == "is_null": return label(expr["expr"]) + (" IS NOT NULL" if expr["negated"] else " IS NULL")
    if k == "in": return label(expr["expr"]) + " IN (...)"
    return k


def like_match(value, pattern):
    """SQL LIKE with bounded dynamic programming, avoiding regex backtracking."""
    value, pattern = str(value).lower(), str(pattern).lower()
    previous = [True]+[False]*len(value)
    for token in pattern:
        current = [previous[0] if token=='%' else False]+[False]*len(value)
        for j,char in enumerate(value,1):
            current[j] = previous[j] or current[j-1] if token=='%' else previous[j-1] and (token=='_' or token==char)
        previous = current
    return previous[-1]


def _evaluate(expr, row, group=None):
    k = expr["kind"]
    if k == "literal": return expr["value"]
    if k == "column":
        name = expr["name"]
        if name in row: return row[name]
        keys = [key for key in row if key.rsplit(".", 1)[-1] == name]
        if len(keys) == 1: return row[keys[0]]
        raise DatabaseError(("Ambiguous" if keys else "Unknown") + f" column: {name}")
    if k == "unary":
        v = evaluate(expr["expr"], row, group)
        if expr["op"] == "NOT": return None if v is None else not truth(v)
        if v is None: return None
        if not _is_numeric(v): raise DatabaseError("Arithmetic requires numeric operands")
        return -v if expr["op"] == "-" else +v
    if k == "is_null":
        result = evaluate(expr["expr"], row, group) is None
        return not result if expr["negated"] else result
    if k == "in":
        v = evaluate(expr["expr"], row, group)
        values = [evaluate(x, row, group) for x in expr["values"]]
        if v is None:
            result = None
        else:
            candidates = [x for x in values if x is not None]
            for candidate in candidates:
                if not _compatible_values(v, candidate):
                    raise DatabaseError("Comparison operands have incompatible types")
            result = True if any(v == candidate for candidate in candidates) else None if None in values else False
        return (None if result is None else not result) if expr["negated"] else result
    if k == "binary":
        a, b = evaluate(expr["left"], row, group), evaluate(expr["right"], row, group)
        op = expr["op"]
        if op == "AND": return sql_and(a, b)
        if op == "OR": return sql_or(a, b)
        if a is None or b is None: return None
        if op in {"+", "-", "*", "/", "%"}:
            if not _is_numeric(a) or not _is_numeric(b):
                raise DatabaseError("Arithmetic requires numeric operands")
            if op in {"/", "%"} and b == 0: return None
            return {"+": lambda: a+b, "-": lambda: a-b, "*": lambda: a*b,
                    "/": lambda: a/b, "%": lambda: (abs(a)%abs(b))*(-1 if a<0 else 1) if isinstance(a,int) and isinstance(b,int) else math.fmod(a,b)}[op]()
        if op == "LIKE":
            return like_match(a,b)
        if op in {"=", "!=", "<>", "<", "<=", ">", ">="}:
            if not _compatible_values(a, b):
                raise DatabaseError("Comparison operands have incompatible types")
            return {"=":lambda:a==b, "!=":lambda:a!=b, "<>":lambda:a!=b,
                    "<":lambda:a<b, "<=":lambda:a<=b, ">":lambda:a>b, ">=":lambda:a>=b}[op]()
        raise DatabaseError(f"Unsupported operator: {op}")
    if k == "call":
        name, args = expr["name"], expr["args"]
        if name in AGGREGATES:
            if group is None: raise DatabaseError("Aggregate is not allowed in this clause")
            if len(args) != 1: raise DatabaseError(f"{name} needs one argument")
            if args[0]["kind"] == "star":
                if name != "COUNT": raise DatabaseError("Only COUNT accepts *")
                return len(group)
            values = [evaluate(args[0], r) for r in group]
            values = [v for v in values if v is not None]
            if name == "COUNT": return len(values)
            if not values: return None
            if name in {"SUM", "AVG"}:
                if any(not _is_numeric(v) for v in values): raise DatabaseError(f"{name} requires numbers")
                total = sum(values)
                if name == "SUM": return total
                try: finite_total = math.isfinite(total)
                except OverflowError: finite_total = False
                if finite_total: return total/len(values)
                # A finite mean can have an overflowing intermediate sum. Exact
                # Decimal inputs preserve tiny residuals after large values
                # cancel; normalizing by a float scale would underflow them.
                exact = [Decimal.from_float(v) if isinstance(v,float) else Decimal(v) for v in values]
                span = max(v.adjusted() for v in exact)-min(v.as_tuple().exponent for v in exact)
                with localcontext() as context:
                    context.prec = max(28,span+len(str(len(values)))+4)
                    return float(sum(exact,Decimal(0))/Decimal(len(values)))
            return min(values) if name == "MIN" else max(values)
        if name == "COALESCE":
            for arg in args:
                value=evaluate(arg,row,group)
                if value is not None: return value
            return None
        values = [evaluate(a, row, group) for a in args]
        if any(x is None for x in values): return None
        if name in {"LOWER", "UPPER", "LENGTH"}:
            if len(values) != 1 or not isinstance(values[0], str): raise DatabaseError(f"{name} needs one text argument")
            return {"LOWER":lambda:values[0].lower(), "UPPER":lambda:values[0].upper(), "LENGTH":lambda:len(values[0])}[name]()
        if name == "ABS":
            if not _is_numeric(values[0]): raise DatabaseError("ABS requires a numeric value")
            return abs(values[0])
        if name == "ROUND":
            value, digits = values[0], values[1] if len(values)>1 else 0
            if not _is_numeric(value): raise DatabaseError("ROUND requires a numeric value")
            if type(digits) is not int or not -308 <= digits <= 308:
                raise DatabaseError("ROUND precision must be an integer from -308 to 308")
            number = Decimal(str(int(value) if isinstance(value, bool) else value))
            try:
                with localcontext() as context:
                    context.prec = max(28, len(number.as_tuple().digits)+abs(number.as_tuple().exponent)+abs(digits)+4)
                    rounded = number.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
            except InvalidOperation as error:
                raise DatabaseError("ROUND value cannot be represented") from error
            return float(rounded) if isinstance(value, float) else int(rounded)
        raise DatabaseError(f"Unknown function: {name}")
    raise DatabaseError("Expression is not a scalar value")


def evaluate(expr, row, group=None):
    result=_evaluate(expr,row,group)
    if isinstance(result,float) and not math.isfinite(result):
        raise DatabaseError("Expression produced a non-finite number")
    return result


def conjuncts(expr):
    if expr and expr["kind"] == "binary" and expr["op"] == "AND":
        return conjuncts(expr["left"]) + conjuncts(expr["right"])
    return [expr] if expr else []


class Database:
    def __init__(self, directory, cache_size=64):
        self.lock = threading.RLock()
        self.pager = DiskPager(directory, cache_size=cache_size)
        self.active = False
        self.closed = False
        self.catalog_pages=[]
        try:
            stored=self.pager.read_page(0)
            if stored is None:
                self.catalog={"kind":"catalog","version":1,"tables":{}}
                self._begin()
                if self.pager.allocate({"kind":"catalog_root","version":1,"pages":[]})!=0:
                    raise DatabaseError("Invalid catalog page")
                self._commit()
            elif stored.get('kind')=='catalog_root' and stored.get('version')==1:
                ids=stored.get('pages',[])
                if not ids or len(set(ids))!=len(ids) or any(type(i) is not int or i<=0 for i in ids):
                    raise DatabaseError("Invalid catalog page manifest")
                data=[]
                for ordinal,page_id in enumerate(ids):
                    chunk=self.pager.read_page(page_id)
                    if not chunk or chunk.get('kind')!='catalog_chunk' or chunk.get('ordinal')!=ordinal:
                        raise DatabaseError("Invalid catalog chunk")
                    data.append(base64.b64decode(chunk['data'],validate=True))
                self.catalog=json.loads(b''.join(data).decode('utf-8'))
                self.catalog_pages=list(ids)
            else:
                # Read older v1 direct catalogs; the next write migrates atomically.
                self.catalog=stored
            if self.catalog.get("kind")!="catalog" or self.catalog.get("version")!=1:
                raise DatabaseError("Unsupported database catalog format")
        except Exception:
            self.pager.close()
            raise
        self.counters = {}
        self._research = ResearchState()
        self._research_budget = None
        self._research_current_cold = False
        self._research_sync_buffer()

    def _table(self, name):
        if name not in self.catalog["tables"]: raise DatabaseError(f"Unknown table: {name}")
        return self.catalog["tables"][name]

    def schema(self):
        with self.lock:
            if getattr(self.pager,'poisoned',False): raise DatabaseError("Commit outcome requires recovery; close and reopen the database")
            return [dict(copy.deepcopy(m), name=n) for n,m in self.catalog["tables"].items()]

    def stats(self):
        with self.lock:
            return dict(self.pager.stats(), page_count=self.pager.page_count,
                        transaction_active=self.active, table_count=len(self.catalog["tables"]))

    def _diagnostic_select(self, sql):
        """Bind a single read query without execute()'s transaction rollback path."""
        if self.closed: raise DatabaseError("Database is closed")
        if self.pager.poisoned: raise DatabaseError("Commit outcome requires recovery; close and reopen the database")
        if not isinstance(sql, str) or not sql.strip(): raise DatabaseError("请输入一个 SELECT 查询。")
        try: statements = parse(sql)
        except SQLError as error: raise DatabaseError(str(error)) from error
        if len(statements) != 1: raise DatabaseError("诊断仅支持一个 SELECT 查询，请移除其他语句。")
        statement = statements[0]
        if statement['kind'] == 'explain': statement = statement['statement']
        if statement['kind'] != 'select': raise DatabaseError("诊断仅支持 SELECT，不执行数据或结构修改。")
        return statement

    def preview_query(self, sql):
        """Compile a suggestion but never fetch rows or abort a user's transaction."""
        with self.lock:
            statement = self._diagnostic_select(sql)
            result = self._select(statement, explain_only=True)
            return {"valid": True, "read_only": True, "kind": "select", "plan": result['plan']}

    def _research_available(self):
        if self.closed: raise DatabaseError('Database is closed')
        if self.pager.poisoned: raise DatabaseError('Commit outcome requires recovery; close and reopen the database')

    def research_status(self):
        from .optimizer import RULES, LIMITATIONS
        with self.lock:
            self._research_available()
            self._research_sync_buffer()
            return dict(self._research.status(), rules=copy.deepcopy(RULES),
                        limitations=list(LIMITATIONS), transaction_active=self.active)

    def analyze_statistics(self):
        with self.lock:
            self._research_available()
            self._research_sync_buffer()
            if self.active: raise DatabaseError('统计采集请先提交或回滚事务；活动事务保持不变。')
            start = time.perf_counter()
            self.pager.clear_cache()
            before = self.pager.stats()
            tables, examined = collect_statistics(self)
            self._research.publish_statistics(tables,examined)
            after = self.pager.stats()
            return {'generation':self._research.generation, 'table_count':len(tables),
                    'tables':copy.deepcopy(tables), 'rows_examined':examined,
                    'elapsed_ms':round((time.perf_counter()-start)*1000,4),
                    'page_reads':after['disk_reads']-before.get('disk_reads',0),
                    'cache_policy':'采集前清空应用缓冲池；保留操作系统缓存；只读扫描提交后的堆表。'}

    def set_optimizer_mode(self, mode):
        with self.lock:
            self._research_available()
            if not isinstance(mode,str) or mode not in {'heuristic','adaptive'}: raise DatabaseError('优化模式仅支持 heuristic 或 adaptive。')
            if self.active: raise DatabaseError('切换研究模式请先提交或回滚事务；活动事务保持不变。')
            self._research.mode = mode
            self._research.cache.clear()
            return self.research_status()

    def _research_sync_buffer(self):
        status = self.pager.cache_policy_status()
        signature = (status['policy'],status['sample_size'],self.pager.cache_size)
        if signature != self._research.buffer_signature:
            self._research.reset_buffer_learning(signature)

    def set_buffer_policy(self, policy, sample_size=8):
        with self.lock:
            self._research_available()
            if self.active: raise DatabaseError('切换缓冲策略请先提交或回滚事务；活动事务保持不变。')
            try: buffer = self.pager.set_cache_policy(policy,sample_size)
            except StorageError as error: raise DatabaseError(str(error)) from error
            # The setter resets volatile prediction history even for the same
            # policy, so old timing conclusions are invalidated unconditionally.
            self._research.reset_buffer_learning((buffer['policy'],buffer['sample_size'],self.pager.cache_size))
            return dict(buffer, research=self.research_status())

    def optimize_query(self, sql, candidate_sql=None):
        from .optimizer import optimize
        with self.lock:
            self._research_available()
            self._research_sync_buffer()
            return optimize(self, sql, candidate_sql)

    def _research_check_budget(self, rows=None):
        budget = self._research_budget
        if not budget: return
        if (time.perf_counter()>budget['deadline'] or
            self.counters.get('rows_scanned',0)>budget['max_scanned'] or
            self.counters.get('join_comparisons',0)>budget['max_comparisons'] or
            rows is not None and rows>budget['max_rows']):
            raise DatabaseError('研究实验达到时间/扫描/连接/中间结果预算。')

    def _research_invalidate(self):
        if hasattr(self, '_research'): self._research.invalidate()

    def integrity_check(self):
        from .integrity import check_database
        with self.lock:
            if self.closed: raise DatabaseError("Database is closed")
            if self.pager.poisoned: raise DatabaseError("Commit outcome requires recovery; close and reopen the database")
            return check_database(self)

    def compare_query(self, sql):
        """Run the same read query through forced heap and automatic access paths.

        Clear the application buffers before each run; OS caches and staged pages
        remain. Compare bags of full rows, since tied SQL sort keys have no
        guaranteed relative order. LIMIT/OFFSET could select different legal
        subsets, so the experiment rejects them rather than reporting a bug.
        """
        with self.lock:
            statement = self._diagnostic_select(sql)
            if statement.get('limit') is not None or statement.get('offset', 0):
                raise DatabaseError("扫描对比请先移除 LIMIT/OFFSET，避免不同访问顺序选择不同的合法子集。")
            saved_counters = self.counters
            runs = {}
            rows = {}
            try:
                for name, force in [('scan', True), ('optimized', False)]:
                    self.pager.clear_cache()
                    before = self.pager.stats()
                    self.counters = {'rows_scanned':0, 'index_lookups':0, 'join_comparisons':0}
                    start = time.perf_counter()
                    result = self._select(statement, force_scan=force)
                    elapsed = round((time.perf_counter()-start)*1000, 3)
                    after = self.pager.stats()
                    delta = metric_delta(before,after)
                    rows[name] = Counter(row_signature(row) for row in result['rows'])
                    runs[name] = {'plan':result['plan'], 'stats':dict(delta, **self.counters),
                                  'elapsed_ms':elapsed, 'row_count':result['row_count']}
            except (StorageError, SQLError, ValueError, TypeError, OverflowError, KeyError) as error:
                raise DatabaseError(str(error)) from error
            finally:
                self.counters = saved_counters
            return dict(runs, sql=sql, matched=rows['scan']==rows['optimized'], comparison='multiset',
                        view='current transaction' if self.active else 'committed',
                        cache_policy='每次查询前清空应用缓冲池；保留操作系统缓存和事务暂存页。')

    def _begin(self):
        self._research_invalidate()
        # Snapshot the catalog before changing pager state.  If copying a very
        # large or malformed catalog fails, the database must remain usable
        # instead of exposing a pager transaction with active=False.
        saved_catalog = copy.deepcopy(self.catalog)
        saved_catalog_pages = list(self.catalog_pages)
        self.pager.begin()
        self.saved_catalog = saved_catalog
        self.saved_catalog_pages = saved_catalog_pages
        self.active = True

    def _commit(self):
        data=json.dumps(self.catalog,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        chunks=[data[i:i+2900] for i in range(0,len(data),2900)]
        for ordinal,chunk in enumerate(chunks):
            value={'kind':'catalog_chunk','ordinal':ordinal,'data':base64.b64encode(chunk).decode('ascii')}
            if ordinal<len(self.catalog_pages): self.pager.write_page(self.catalog_pages[ordinal],value)
            else: self.catalog_pages.append(self.pager.allocate(value))
        self.pager.write_page(0,{'kind':'catalog_root','version':1,'pages':self.catalog_pages[:len(chunks)]})
        self.pager.commit()
        self.active = False
        self._research_invalidate()

    def _rollback(self):
        self.pager.rollback()
        self.catalog = self.saved_catalog
        self.catalog_pages=self.saved_catalog_pages
        self.active = False
        self._research_invalidate()

    def close(self):
        with self.lock:
            if self.closed: return
            if self.active: self._rollback()
            self.pager.close()
            self.closed = True

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def execute(self, sql):
        with self.lock:
            if self.closed: raise DatabaseError("Database is closed")
            if getattr(self.pager,'poisoned',False): raise DatabaseError("Commit outcome requires recovery; close and reopen the database")
            try: statements = parse(sql)
            except SQLError as e:
                if self.active: self._rollback()
                raise DatabaseError(str(e)) from e
            results = []
            for statement in statements:
                start = time.perf_counter()
                before = self.pager.stats()
                self.counters = {"rows_scanned":0, "index_lookups":0, "join_comparisons":0}
                implicit = statement["kind"] in {"create_table","drop_table","create_index","drop_index","insert","update","delete"} and not self.active
                try:
                    if implicit: self._begin()
                    result = self._execute(statement)
                    if implicit: self._commit()
                except Exception as e:
                    if self.active: self._rollback()
                    if isinstance(e, DatabaseError): raise
                    if isinstance(e, (StorageError, SQLError, ValueError, TypeError, OverflowError, KeyError)):
                        raise DatabaseError(str(e)) from e
                    raise
                after = self.pager.stats()
                delta = metric_delta(before,after)
                result["stats"] = dict(delta, **self.counters)
                result["elapsed_ms"] = round((time.perf_counter()-start)*1000,3)
                results.append(result)
            return results

    @staticmethod
    def _result(columns=None, rows=None, message="", count=None, plan=None):
        rows = rows or []
        return {"columns":columns or [],"rows":rows,"row_count":len(rows) if count is None else count,"message":message,"plan":plan}

    def _execute(self, s):
        kind = s["kind"]
        if kind in {'create_table','drop_table','create_index','drop_index','insert','update','delete'}:
            self._research_invalidate()
        if kind == "begin":
            if self.active: raise DatabaseError("A transaction is already active")
            self._begin(); return self._result(message="Transaction started")
        if kind == "commit":
            if not self.active: raise DatabaseError("No active transaction")
            self._commit(); return self._result(message="Transaction committed")
        if kind == "rollback":
            if not self.active: raise DatabaseError("No active transaction")
            self._rollback(); return self._result(message="Transaction rolled back")
        if kind == "checkpoint":
            if self.active: raise DatabaseError("Commit or rollback before checkpoint")
            checkpoint = getattr(self.pager, "checkpoint", None)
            if checkpoint: checkpoint()
            return self._result(message="Checkpoint complete")
        if kind == "integrity_check":
            report = self.integrity_check()
            rows = [[check['name'],check['status'],check['detail']] for check in report['checks']]
            rows += [[issue['code'],issue['severity'],issue['message']] for issue in report['issues']]
            result = self._result(['check','status','detail'], rows,
                                  message='Database integrity OK' if report['ok'] else 'Database integrity issues found')
            result['integrity'] = report
            return result
        if kind == "show_tables":
            return self._result(["table","rows","pages","indexes"], [[n,m['row_count'],len(m['pages']),len(m['indexes'])] for n,m in self.catalog['tables'].items()])
        if kind == "describe":
            m = self._table(s['table'])
            return self._result(["column","type","not_null","primary_key","unique","default"], [[c['name'],c['type'],c.get('not_null',False),c.get('primary',False),c.get('unique',False),c.get('default')] for c in m['columns']])
        if kind == "create_table":
            name = s['name']
            if name in self.catalog['tables']: raise DatabaseError(f"Table already exists: {name}")
            cols = copy.deepcopy(s['columns'])
            if len({c['name'] for c in cols}) != len(cols): raise DatabaseError("Duplicate column names")
            if sum(bool(c.get('primary')) for c in cols)>1: raise DatabaseError("Composite primary keys are not supported")
            m = {"columns":cols,"pages":[],"next_rid":1,"row_count":0,"indexes":[]}
            self.catalog['tables'][name] = m
            for c in cols:
                if c.get('primary'): c['not_null'] = True; c['unique'] = True
                self._coerce(c.get('default'), c, enforce_null=False)
                if c.get('unique'):
                    self._create_index(f"{name}_{c['name']}_key",name,c['name'],True,protected=True)
            return self._result(message=f"Table {name} created")
        if kind == "drop_table":
            self._table(s['name']); del self.catalog['tables'][s['name']]
            return self._result(message=f"Table {s['name']} dropped; allocated pages retained")
        if kind == "create_index":
            self._create_index(s['name'],s['table'],s['column'],s.get('unique',False))
            return self._result(message=f"Index {s['name']} created")
        if kind == "drop_index":
            for m in self.catalog['tables'].values():
                for ix in m['indexes']:
                    if ix['name'] == s['name']:
                        if ix.get('protected'): raise DatabaseError("A constraint index cannot be dropped")
                        m['indexes'].remove(ix); return self._result(message=f"Index {s['name']} dropped")
            raise DatabaseError(f"Unknown index: {s['name']}")
        if kind in {"insert","update","delete"}: return self._write(s)
        if kind == "select": return self._select(s)
        if kind == "explain":
            result = self._select(s['statement'],explain_only=not s.get('analyze',False))
            if not s.get('analyze',False): result['columns'] = []; result['rows'] = []; result['row_count'] = 0
            result['message'] = "Physical query plan" + (" with measured execution" if s.get('analyze') else " without row execution")
            return result
        raise DatabaseError(f"Unsupported statement: {kind}")

    def _create_index(self, name, table, column, unique, protected=False):
        if any(ix['name']==name for m in self.catalog['tables'].values() for ix in m['indexes']): raise DatabaseError(f"Index already exists: {name}")
        m = self._table(table)
        if column not in [c['name'] for c in m['columns']]: raise DatabaseError(f"Unknown column: {column}")
        ix = {"name":name,"column":column,"root":None,"unique":unique,"protected":protected}
        tree = BPlusTree(self.pager,ix)
        for rid,row in HeapTable(self.pager,m).rows():
            key = row[column]
            if key is None: continue
            if unique and tree.search(key): raise DatabaseError(f"Duplicate value for unique index {name}")
            tree.insert(key,rid)
        m['indexes'].append(ix)

    @staticmethod
    def _coerce(value, column, enforce_null=True):
        if value is None:
            if enforce_null and column.get('not_null'): raise DatabaseError(f"NOT NULL constraint: {column['name']}")
            return None
        typ = column['type']
        valid = (typ == 'INTEGER' and isinstance(value,int) and not isinstance(value,bool)
                 or typ == 'REAL' and isinstance(value,(int,float)) and not isinstance(value,bool)
                 or typ == 'TEXT' and isinstance(value,str)
                 or typ == 'BOOLEAN' and (isinstance(value,bool) or isinstance(value,int) and value in (0,1)))
        if not valid: raise DatabaseError(f"{column['name']} expects {typ}, got {type(value).__name__}")
        if typ == 'REAL':
            value = float(value)
            if not math.isfinite(value): raise DatabaseError("Non-finite numbers are not supported")
        if typ == 'BOOLEAN': value = bool(value)
        if typ == 'TEXT' and column.get('max_length') and len(value)>column['max_length']:
            raise DatabaseError(f"{column['name']} exceeds length {column['max_length']}")
        return value

    def _check_row(self, m, row, own_rid=None):
        row = {c['name']:self._coerce(row[c['name']],c) for c in m['columns']}
        for ix in m['indexes']:
            key = row[ix['column']]
            if ix['unique'] and key is not None:
                found = BPlusTree(self.pager,ix).search(key)
                if any(rid!=own_rid for rid in found): raise DatabaseError(f"UNIQUE constraint: {ix['column']}")
        return row

    def _write(self, s):
        m = self._table(s['table']); heap = HeapTable(self.pager,m)
        names = [c['name'] for c in m['columns']]
        count = 0
        if s['kind']=='insert':
            columns = s['columns'] if s['columns'] is not None else names
            if len(set(columns)) != len(columns): raise DatabaseError("Duplicate INSERT columns")
            if set(columns)-set(names): raise DatabaseError("Unknown INSERT column")
            for vals in s['values']:
                if len(vals)!=len(columns): raise DatabaseError("INSERT column and value counts differ")
                for e in vals: self._bind(e,[],allow_aggregate=False)
                row = {c['name']:c.get('default') for c in m['columns']}
                row.update({n:evaluate(e,{}) for n,e in zip(columns,vals)})
                row = self._check_row(m,row)
                rid = heap.insert(row)
                for ix in m['indexes']:
                    if row[ix['column']] is not None: BPlusTree(self.pager,ix).insert(row[ix['column']],rid)
                count += 1
        else:
            available = [s['table']+'.'+n for n in names]
            self._bind(s.get('where'),available,allow_aggregate=False)
            for n,e in s.get('assignments',{}).items():
                if n not in names: raise DatabaseError(f"Unknown UPDATE column: {n}")
                self._bind(e,available,allow_aggregate=False)
            # Snapshot row references before mutation; Halloween effects are impossible.
            targets = [(rid,row) for rid,row in heap.rows() if not s.get('where') or truth(evaluate(s['where'],self._qualified(row,s['table']))) is True]
            for rid,row in targets:
                if s['kind']=='update':
                    updated = dict(row)
                    for n,e in s['assignments'].items(): updated[n] = evaluate(e,self._qualified(row,s['table']))
                    updated = self._check_row(m,updated,rid)
                for ix in m['indexes']:
                    if row[ix['column']] is not None: BPlusTree(self.pager,ix).remove(row[ix['column']],rid)
                if s['kind']=='delete': heap.delete(rid)
                else:
                    heap.update(rid,updated)
                    for ix in m['indexes']:
                        if updated[ix['column']] is not None: BPlusTree(self.pager,ix).insert(updated[ix['column']],rid)
                count += 1
        verb={'insert':'inserted','update':'updated','delete':'deleted'}[s['kind']]
        return self._result(message=f"{count} row(s) {verb}",count=count)

    @staticmethod
    def _qualified(row, alias): return {alias+'.'+k:v for k,v in row.items()}

    def _bind(self, expr, available, allow_aggregate=True, aliases=None):
        if expr is None: return
        if expr['kind']=='column':
            name = expr['name']
            if aliases and name in aliases: return
            matches = [n for n in available if n==name or '.' not in name and n.rsplit('.',1)[-1]==name]
            if len(matches)!=1: raise DatabaseError(("Ambiguous" if matches else "Unknown")+f" column: {name}")
            expr['name'] = matches[0]
        elif expr['kind']=='call':
            name = expr['name']
            if name not in AGGREGATES|SCALARS: raise DatabaseError(f"Unknown function: {name}")
            if name in AGGREGATES and not allow_aggregate: raise DatabaseError("Aggregate is not allowed in this clause")
            args = expr['args']
            minimum, maximum = (1,2) if name=='ROUND' else (1,100) if name=='COALESCE' else (1,1)
            if not minimum <= len(args) <= maximum: raise DatabaseError(f"Invalid argument count for {name}")
            if any(a['kind']=='star' for a in args) and (name!='COUNT' or len(args)!=1 or args[0].get('table')):
                raise DatabaseError("Only COUNT(*) supports a wildcard argument")
            for a in args:
                if a['kind']!='star': self._bind(a,available,allow_aggregate and name not in AGGREGATES,aliases)
        elif expr['kind']=='star': raise DatabaseError("Wildcard is only valid in SELECT or COUNT(*)")
        else:
            for key in ('left','right','expr'):
                if isinstance(expr.get(key),dict): self._bind(expr[key],available,allow_aggregate,aliases)
            for e in expr.get('values',[]): self._bind(e,available,allow_aggregate,aliases)

    def _join_expression_type(self, expr, column_types):
        """Check known comparison types before a join's physical plan is chosen.

        NULL has no conflicting type. Mixed COALESCE branches are unknown until
        execution; ordinary columns, arithmetic and scalar results are known.
        """
        if not expr: return None
        kind = expr['kind']
        if kind != 'literal' and not any(e['kind'] in {'column','star'} for e in walk(expr)):
            # Constant NULL/boolean expressions use the same three-valued
            # evaluator as execution (NULL AND FALSE is FALSE, for example).
            try: constant = evaluate(expr,{})
            except (DatabaseError,ValueError,TypeError,OverflowError): pass
            else: return ('NULL' if constant is None else 'BOOLEAN' if isinstance(constant,bool)
                          else 'TEXT' if isinstance(constant,str) else 'NUMERIC')
        if kind == 'literal':
            value = expr['value']
            return ('NULL' if value is None else 'BOOLEAN' if isinstance(value,bool)
                    else 'TEXT' if isinstance(value,str) else 'NUMERIC')
        if kind == 'column': return column_types.get(expr['name'])
        if kind == 'unary':
            operand = self._join_expression_type(expr['expr'],column_types)
            if expr['op'] == 'NOT': return 'NULL' if operand == 'NULL' else 'BOOLEAN'
            if operand in {'TEXT','BOOLEAN'}: raise DatabaseError("Arithmetic requires numeric operands")
            return 'NULL' if operand == 'NULL' else 'NUMERIC'
        if kind == 'binary':
            left = self._join_expression_type(expr['left'],column_types)
            right = self._join_expression_type(expr['right'],column_types)
            op = expr['op']
            if op in {'=', '!=', '<>', '<', '<=', '>', '>='}:
                if left not in {None,'NULL'} and right not in {None,'NULL'} and left != right:
                    raise DatabaseError("Comparison operands have incompatible types")
                if 'NULL' in {left,right}: return 'NULL'
                return 'BOOLEAN'
            elif op in {'+', '-', '*', '/', '%'}:
                if 'NULL' in {left,right}: return 'NULL'
                if {'TEXT','BOOLEAN'} & {left,right}: raise DatabaseError("Arithmetic requires numeric operands")
            elif op == 'LIKE' and 'NULL' in {left,right}: return 'NULL'
            elif op in {'AND','OR'}: return 'BOOLEAN' if 'NULL' not in {left,right} else 'NULL'
            elif op == 'LIKE': return 'BOOLEAN'
            else: return 'BOOLEAN'
            return 'NUMERIC'
        if kind in {'is_null','in'}:
            operand = self._join_expression_type(expr['expr'],column_types)
            types = [self._join_expression_type(value,column_types) for value in expr.get('values',[])]
            if kind == 'in':
                if operand not in {None, 'NULL'}:
                    for inferred in types:
                        if inferred not in {None, 'NULL'} and inferred != operand:
                            raise DatabaseError("Comparison operands have incompatible types")
                return 'BOOLEAN'
            return 'BOOLEAN'
        if kind == 'call':
            name = expr['name']
            if name == 'COALESCE':
                possible = set()
                for arg in expr['args']:
                    inferred = self._join_expression_type(arg,column_types)
                    if inferred != 'NULL': possible.add(inferred)
                    # A non-NULL literal makes later branches unreachable.
                    if arg['kind']=='literal' and arg['value'] is not None: break
                return next(iter(possible)) if len(possible)==1 else 'NULL' if not possible else None
            types = [self._join_expression_type(arg,column_types) for arg in expr['args']]
            if 'NULL' in types: return 'NULL'
            if name in {'LOWER','UPPER'}: return 'TEXT'
            if name == 'COALESCE': return next(iter(types), 'NULL') if len(set(t for t in types if t not in {None,'NULL'})) == 1 else None
            if name in {'ABS','ROUND','LENGTH'}: return 'NUMERIC'
            return 'NUMERIC'
        return None

    def _scan_choice(self, table, alias, where, cold=None):
        m = self._table(table); total=m['row_count']; best=None
        start = time.perf_counter()
        for e in conjuncts(where):
            if e['kind']=='in' and not e['negated'] and len(e['values'])<=32 and all(value['kind']=='literal' for value in e['values']):
                left, op = e['expr'], 'IN'
                # Validate every literal before deduplicating matching RIDs.
                # A Python set would hide an incompatible TRUE beside numeric 1.
                keys = [value['value'] for value in e['values'] if value['value'] is not None]
            elif e['kind']=='binary' and e['op'] in {'=','<','<=','>','>='}:
                left,right,op = e['left'],e['right'],e['op']
                if left['kind']=='literal' and right['kind']=='column':
                    left,right = right,left; op={'<':'>','>':'<','<=':'>=','>=':'<=','=':'='}[op]
                if right['kind']!='literal' or right['value'] is None: continue
                keys = [right['value']]
            else: continue
            if left['kind']!='column': continue
            if not left['name'].startswith(alias+'.'): continue
            col = left['name'].split('.',1)[1]
            for ix in m['indexes']:
                if ix['column']!=col: continue
                tree=BPlusTree(self.pager,ix)
                column=next(c for c in m['columns'] if c['name']==col)
                def key_matches(value):
                    typ = column['type']
                    if typ == 'TEXT': return isinstance(value,str)
                    if typ == 'BOOLEAN': return isinstance(value,bool)
                    if typ == 'INTEGER': return type(value) is int
                    return _is_numeric(value)
                if any(not key_matches(key) for key in keys):
                    continue
                try:
                    if op=='IN': rids=sorted({tuple(rid) for key in keys for rid in tree.search(key)})
                    else:
                        key=keys[0]
                        rids = tree.search(key) if op=='=' else tree.range(low=key,include_low=op=='>=') if op in {'>','>='} else tree.range(high=key,include_high=op=='<=')
                except TypeError: continue
                if best is None or len(rids)<len(best[1]): best=(ix,rids,e)
        cost = {'source':'heuristic', 'fallback_reason':'no compatible single-column index',
                'selected_access':'TableScan',
                'selected_cost_ms':round(self._research.heuristic_access_cost(
                    'TableScan',total,len(m['pages'])),4),
                'scan_cost_ms':round(self._research.heuristic_access_cost(
                    'TableScan',total,len(m['pages'])),4),
                'cost_confidence':'medium'}
        if best:
            ix,rids,e=best
            selected,cost = self._research.choose_access(total,len(rids),len(m['pages']),
                len(rids)<=max(1,total*.35),
                self._research_current_cold if cold is None else cold)
            if selected:
                plan=node('IndexScan',f"{table} using {ix['name']}; {label(e)}",len(rids))
                plan['access_cost']=cost
                self._research.estimation_ms += (time.perf_counter()-start)*1000
                return plan,rids
        plan=node('TableScan',table,total)
        plan['access_cost']=cost
        self._research.estimation_ms += (time.perf_counter()-start)*1000
        return plan,None

    def _read(self, table, alias, rids):
        start = time.perf_counter()
        heap=HeapTable(self.pager,self._table(table))
        if rids is None: pairs=heap.rows()
        else:
            self.counters['index_lookups']+=1
            pairs=((rid,heap.get(rid)) for rid in rids)
        result=[]
        for _,row in pairs:
            if row is not None:
                self.counters['rows_scanned']+=1
                result.append(self._qualified(row,alias))
                self._research_check_budget(len(result))
        pages=len(self._table(table)['pages'])
        self._research.observe_access('TableScan' if rids is None else 'IndexScan',
            self._table(table)['row_count'] if rids is None else len(rids),
            pages if rids is None else min(pages,len(rids)),
            (time.perf_counter()-start)*1000,self._research_current_cold)
        self._research_current_cold = False
        return result

    def _select(self, original, explain_only=False, force_scan=False):
        query_start = time.perf_counter()
        workload_before=self.pager.stats()
        counters_before=dict(self.counters)
        self._research_sync_buffer()
        self._research_check_budget()
        self._research_current_cold = self.pager.stats()['cache_used']==0
        estimation_before = self._research.estimation_ms
        s=copy.deepcopy(original)
        sources=([s['from']] if s.get('from') else [])+s.get('joins',[])
        available=[]; seen=set(); column_types={}
        for source in sources:
            alias=source.get('alias') or source['table']; source['resolved_alias']=alias
            if alias in seen: raise DatabaseError(f"Duplicate table alias: {alias}")
            seen.add(alias)
            for c in self._table(source['table'])['columns']:
                qualified = alias+'.'+c['name']
                available.append(qualified)
                column_types[qualified] = ('TEXT' if c['type']=='TEXT' else
                                           'BOOLEAN' if c['type']=='BOOLEAN' else 'NUMERIC')
        columns=[]
        for item in s['columns']:
            e=item['expr']
            if e['kind']=='star':
                expanded=[n for n in available if not e.get('table') or n.startswith(e['table']+'.')]
                if not expanded: raise DatabaseError("Wildcard has no matching source")
                columns.extend({'expr':{'kind':'column','name':n},'alias':n if len(sources)>1 else n.split('.',1)[1]} for n in expanded)
            else:
                output=item.get('alias') or label(e)
                self._bind(e,available); columns.append({'expr':e,'alias':output})
        aliases={c['alias'] for c in columns}
        alias_expressions=defaultdict(set)
        for c in columns: alias_expressions[c['alias']].add(json.dumps(c['expr'],sort_keys=True))
        ambiguous_aliases={name for name,expressions in alias_expressions.items() if len(expressions)>1}
        self._bind(s.get('where'),available,False)
        for e in s.get('group_by',[]): self._bind(e,available,False)
        self._bind(s.get('having'),available,True)
        for o in s.get('order_by',[]):
            e=o['expr']
            for reference in walk(e):
                if reference['kind']=='column' and reference['name'] in ambiguous_aliases:
                    raise DatabaseError(f"Ambiguous output alias: {reference['name']}")
            if e['kind']=='literal' and type(e['value']) is int and not 1<=e['value']<=len(columns):
                raise DatabaseError("ORDER BY position out of range")
            self._bind(e,available,True,aliases)
        for source_position,source in enumerate(sources):
            scope_start=source.get('scope_start',0)
            scoped=[item['resolved_alias']+'.'+c['name']
                    for item in sources[scope_start:source_position+1]
                    for c in self._table(item['table'])['columns']]
            self._bind(source.get('on'),scoped,False)
            self._join_expression_type(source.get('on'),column_types)
        s['where'],join_normalization=normalize_inner_joins(sources,s.get('where'))
        for source in sources[1:]:
            self._join_expression_type(source.get('on'),column_types)
        aggregate=bool(s.get('group_by')) or any(has_aggregate(c['expr']) for c in columns) or has_aggregate(s.get('having')) or any(has_aggregate(o['expr']) for o in s.get('order_by',[]))
        if s.get('having') and not aggregate: raise DatabaseError("HAVING requires grouping or aggregation")
        if aggregate:
            group_signatures={json.dumps(e,sort_keys=True) for e in s.get('group_by',[])}
            group_columns={e['name'] for e in s.get('group_by',[]) if e['kind']=='column'}
            def validate_group(e, alias_context=False):
                if json.dumps(e,sort_keys=True) in group_signatures: return
                if e['kind']=='call' and e['name'] in AGGREGATES: return
                if e['kind']=='column' and e['name'] not in group_columns and not (alias_context and e['name'] in aliases):
                    raise DatabaseError(f"Column must appear in GROUP BY: {e['name']}")
                for key in ('left','right','expr'):
                    if isinstance(e.get(key),dict): validate_group(e[key],alias_context)
                for a in e.get('args',[])+e.get('values',[]): validate_group(a,alias_context)
            for c in columns: validate_group(c['expr'])
            if s.get('having'): validate_group(s['having'])
            for o in s.get('order_by',[]): validate_group(o['expr'],True)
        execution_sources=sources
        join_optimization={'source':'heuristic-fallback' if self._research.mode=='adaptive' else 'original-order', 'selected':False,
                           'accepted':False,
                           'reason':('adaptive statistics unavailable' if self._research.mode=='adaptive'
                                     else 'adaptive multi-table search unavailable'),
                           'order':[source['resolved_alias'] for source in sources]}
        join_edges=None
        filter_by_alias={}
        pushed_where_ids=set()
        if self._research.mode=='adaptive' and not force_scan:
            filter_by_alias,pushed_where_ids=safe_input_filters(sources,s.get('where'))
        reordered=False
        preplanned_scans={}
        if len(sources)>=3 and self._research.mode=='adaptive' and self._research.tables and not force_scan:
            optimizer_started=time.perf_counter()
            optimizer_budget_ms=20.0
            planning_cold=self._research_current_cold
            base_rows={}; base_confidence={}
            for source in sources:
                alias=source['resolved_alias']; metadata=self._table(source['table'])
                source['_optimizer_pages']=len(metadata['pages'])
                predicates=filter_by_alias.get(alias,[])
                total=metadata['row_count']
                if predicates:
                    fallback=(max(1,total//(2**len(predicates))) if total else 0)
                    estimated,evidence,observation=self._research.estimate_filter(
                        source['table'],predicates,fallback)
                    base_rows[alias]=min(total,max(0,estimated))
                    base_confidence[alias]=evidence.get('confidence','low')
                else:
                    base_rows[alias]=total
                    base_confidence[alias]='high'
            supported_edges=extract_inner_join_edges(sources)
            if supported_edges is not None:
                for source in sources:
                    alias=source['resolved_alias']
                    if (time.perf_counter()-optimizer_started)*1000>=optimizer_budget_ms: break
                    preplanned_scans[alias]=self._scan_choice(source['table'],alias,s.get('where'),
                                                               cold=planning_cold)
                if len(preplanned_scans)==len(sources) and \
                        (time.perf_counter()-optimizer_started)*1000<optimizer_budget_ms:
                    access_costs={}; access_confidence={}; access_cost_sources={}
                    for source in sources:
                        alias=source['resolved_alias']; access=preplanned_scans[alias][0].get('access_cost',{})
                        access_costs[alias]=access.get('selected_cost_ms',
                            self._research.heuristic_access_cost('TableScan',
                                self._table(source['table'])['row_count'],
                                len(self._table(source['table'])['pages'])))
                        access_confidence[alias]=access.get('cost_confidence','medium')
                        access_cost_sources[alias]=access.get('source','heuristic-access-cost')
                    for alias in base_confidence:
                        if access_confidence.get(alias)=='low': base_confidence[alias]='low'
                    def estimate_edge(left_alias,left_column,right_alias,right_column,left_rows,right_rows):
                        estimated,evidence=self._research.estimate_join(
                            (left_column,right_column),sources,left_rows,right_rows,'INNER')
                        return {'rows':estimated,'confidence':evidence.get('confidence','low'),
                                'reason':evidence.get('fallback_reason')}
                    def estimate_join_cost(left_rows,right_rows,output_rows):
                        return self._research.estimate_join_cost(left_rows,right_rows,output_rows)
                    uncertainty=sum(model.mae or 0 for model in self._research.cost_models.values())
                    uncertainty+=self._research.join_cost_model.mae or 0
                    remaining=optimizer_budget_ms-(time.perf_counter()-optimizer_started)*1000
                    join_optimization=choose_inner_join_order(
                        sources,base_rows,estimate_edge,budget_ms=max(0,remaining),
                        preferred_order=self._research.preferred_join_order(original),
                        access_costs=access_costs,join_cost_estimator=estimate_join_cost,
                        base_confidence=base_confidence,access_cost_sources=access_cost_sources,
                        uncertainty_ms=uncertainty)
                    if not join_optimization.get('accepted'):
                        join_optimization.setdefault('source','heuristic-fallback')
                        join_optimization.setdefault('order',[source['resolved_alias'] for source in sources])
                    if (time.perf_counter()-optimizer_started)*1000>optimizer_budget_ms:
                        join_optimization.update(accepted=False,selected=False,
                            reason='overall join optimization budget exceeded',
                            source='heuristic-fallback',order=[source['resolved_alias'] for source in sources])
                    else:
                        join_optimization['elapsed_ms']=round((time.perf_counter()-optimizer_started)*1000,4)
                else:
                    join_optimization.update(source='heuristic-fallback',accepted=False,
                        reason='overall join optimization budget exceeded',
                        elapsed_ms=round((time.perf_counter()-optimizer_started)*1000,4))
            else:
                join_optimization.update(source='heuristic-fallback',accepted=False,
                    reason='unsupported join shape',
                    elapsed_ms=round((time.perf_counter()-optimizer_started)*1000,4))
            self._research.estimation_ms+=(time.perf_counter()-optimizer_started)*1000
            join_edges=supported_edges
            join_optimization.setdefault('relation_count',len(sources))
            join_optimization.setdefault('statistics_version',self._research.statistics_generation)
            if join_optimization.get('accepted'):
                by_alias={source['resolved_alias']:source for source in sources}
                execution_sources=[by_alias[alias] for alias in join_optimization['order']]
                reordered=True
        join_optimization.setdefault('order',[source['resolved_alias'] for source in execution_sources])
        join_optimization.setdefault('relation_count',len(sources))
        join_optimization.setdefault('statistics_version',self._research.statistics_generation)
        join_optimization.setdefault('search_budget_ms',20.0)
        join_optimization.setdefault('candidate_limit',512)
        if sources:
            base=execution_sources[0]; alias=base['resolved_alias']
            if reordered:
                plan,rids=preplanned_scans[alias]
            elif force_scan:
                plan,rids=node('TableScan',base['table']+'; forced heap scan',self._table(base['table'])['row_count']),None
            else:
                plan,rids=self._scan_choice(base['table'],alias,s.get('where'))
            rows=[] if explain_only else self._read(base['table'],alias,rids)
            if not explain_only: plan['actual_rows']=len(rows)
            push=(filter_by_alias.get(alias,[]) if self._research.mode=='adaptive' and not force_scan else
                  [e for e in conjuncts(s.get('where')) if
                   {x['name'].split('.',1)[0] for x in walk(e) if x['kind']=='column'} <= {alias}])
            pushed_where_ids.update(id(predicate) for predicate in push)
            if push:
                estimate=plan['estimated_rows']
                estimate_start=time.perf_counter()
                predicted,evidence,observation=self._research.estimate_filter(
                    base['table'],push,max(1,estimate//2) if estimate else 0)
                self._research.estimation_ms+=(time.perf_counter()-estimate_start)*1000
                plan=node('Filter',"Predicate pushed to base scan",min(estimate,predicted),[plan])
                plan['estimation']=evidence
                if not explain_only:
                    rows=[r for r in rows if all(truth(evaluate(e,r)) is True for e in push)]
                    self._research_check_budget(len(rows))
                    plan['actual_rows']=len(rows)
                    plan['q_error']=round(max((len(rows)+1)/(plan['estimated_rows']+1),(plan['estimated_rows']+1)/(len(rows)+1)),4)
                    self._research.observe_filter(observation,len(rows))
        else:
            plan=node('ConstantRow','',1); rows=[] if explain_only else [{}]; push=[]
            if not explain_only: plan['actual_rows']=1
        joined_aliases={execution_sources[0]['resolved_alias']} if sources else set()
        for source in execution_sources[1:]:
            alias=source['resolved_alias']; rightcols=[alias+'.'+c['name'] for c in self._table(source['table'])['columns']]
            if reordered:
                rightplan,right_rids=preplanned_scans[alias]
            elif self._research.mode=='adaptive' and not force_scan:
                rightplan,right_rids=self._scan_choice(source['table'],alias,
                    conjunction(filter_by_alias.get(alias,[])))
            else:
                rightplan=node('TableScan',source['table'],self._table(source['table'])['row_count'])
                right_rids=None
            right=[] if explain_only else self._read(source['table'],alias,right_rids)
            right_push=filter_by_alias.get(alias,[])
            if right_push:
                estimate_start=time.perf_counter()
                estimated,evidence,observation=self._research.estimate_filter(
                    source['table'],right_push,max(1,rightplan['estimated_rows']//2)
                    if rightplan['estimated_rows'] else 0)
                self._research.estimation_ms+=(time.perf_counter()-estimate_start)*1000
                if not explain_only: rightplan['actual_rows']=len(right)
                rightplan=node('Filter','Predicate pushed to base scan',
                               min(rightplan['estimated_rows'],estimated),[rightplan])
                rightplan['estimation']=evidence
                if not explain_only:
                    right=[row for row in right if all(truth(evaluate(e,row)) is True for e in right_push)]
                    rightplan['actual_rows']=len(right)
                    rightplan['q_error']=round(max((len(right)+1)/(rightplan['estimated_rows']+1),
                                                   (rightplan['estimated_rows']+1)/(len(right)+1)),4)
                    self._research.observe_filter(observation,len(right))
            on=(predicates_for_next(join_edges,alias,joined_aliases) if reordered else source['on'])
            pair=None
            for e in conjuncts(on):
                if e['kind']=='binary' and e['op']=='=' and e['left']['kind']=='column' and e['right']['kind']=='column':
                    a,b=e['left']['name'],e['right']['name']
                    if b in rightcols and a not in rightcols: pair=(a,b); break
                    if a in rightcols and b not in rightcols: pair=(b,a); break
            range_pair=(range_join_key(on,rightcols) if not pair and
                        self._research.mode=='adaptive' and not force_scan else None)
            estimate_start=time.perf_counter()
            join_type=source.get('type','INNER')
            estimated,evidence=self._research.estimate_join(pair,sources,plan['estimated_rows'],rightplan['estimated_rows'],join_type)
            self._research.estimation_ms+=(time.perf_counter()-estimate_start)*1000
            left_input_rows=len(rows) if not explain_only else plan['estimated_rows']
            right_input_rows=len(right) if not explain_only else rightplan['estimated_rows']
            plan=node('HashJoin' if pair else 'RangeJoin' if range_pair else 'NestedLoopJoin',
                      join_type+'; '+label(on),estimated,[plan,rightplan])
            plan['estimation']=evidence
            if explain_only:
                joined_aliases.add(alias)
                continue
            rightplan['actual_rows']=len(right)
            join_execution_start=time.perf_counter()
            buckets=defaultdict(list)
            if pair:
                for r in right:
                    if r[pair[1]] is not None: buckets[r[pair[1]]].append(r)
            range_rows=[]; range_values=[]
            if range_pair:
                # Sort only the right input. Binary search finds qualifying
                # candidates; full ON evaluation preserves additional bounds.
                range_rows=sorted((r for r in right if r[range_pair[1]] is not None),
                                  key=lambda r:r[range_pair[1]])
                range_values=[r[range_pair[1]] for r in range_rows]
            joined=[]
            for l in rows:
                candidates=buckets.get(l[pair[0]],[]) if pair else right
                if range_pair:
                    value=l[range_pair[0]]; op=range_pair[2]
                    if value is None:
                        candidates=()
                    elif op in {'<','<='}:
                        start=(bisect.bisect_right(range_values,value) if op=='<' else
                               bisect.bisect_left(range_values,value))
                        candidates=(range_rows[i] for i in range(start,len(range_rows)))
                    else:
                        stop=(bisect.bisect_left(range_values,value) if op=='>' else
                              bisect.bisect_right(range_values,value))
                        candidates=(range_rows[i] for i in range(stop))
                matched=False
                for r in candidates:
                    self.counters['join_comparisons']+=1
                    self._research_check_budget(len(joined))
                    merged=dict(l,**r)
                    if truth(evaluate(on,merged)) is True: joined.append(merged); matched=True
                if join_type=='LEFT' and not matched: joined.append(dict(l,**{c:None for c in rightcols}))
            rows=joined
            self._research_check_budget(len(rows))
            plan['actual_rows']=len(rows)
            plan['q_error']=round(max((len(rows)+1)/(plan['estimated_rows']+1),
                                      (plan['estimated_rows']+1)/(len(rows)+1)),4)
            plan['elapsed_ms']=round((time.perf_counter()-join_execution_start)*1000,4)
            if pair:
                self._research.observe_join_cost(left_input_rows,right_input_rows,
                                                 len(rows),plan['elapsed_ms'])
            if pair and len(conjuncts(on))==1:
                self._research.observe_join(pair,sources,left_input_rows,right_input_rows,
                                            len(rows),join_type)
            joined_aliases.add(alias)
        residual=[e for e in conjuncts(s.get('where')) if id(e) not in pushed_where_ids]
        if residual:
            estimate=plan['estimated_rows']
            plan=node('Filter',' AND '.join(label(e) for e in residual),max(1,estimate//2) if estimate else 0,[plan])
            if not explain_only:
                rows=[r for r in rows if all(truth(evaluate(e,r)) is True for e in residual)]
                plan['actual_rows']=len(rows)
        aggregate_plan=having_plan=distinct_plan=sort_plan=limit_plan=None
        if aggregate:
            plan=node('HashAggregate',', '.join(label(e) for e in s.get('group_by',[])) or 'global aggregation',1 if not s.get('group_by') else max(1,plan['estimated_rows']//3),[plan])
            aggregate_plan=plan
        if s.get('having'):
            plan=node('Having',label(s['having']),plan['estimated_rows'],[plan]); having_plan=plan
        plan=node('Project',', '.join(c['alias'] for c in columns),plan['estimated_rows'],[plan])
        project_plan=plan
        if s.get('distinct'):
            plan=node('Distinct','',plan['estimated_rows'],[plan]); distinct_plan=plan
        if s.get('order_by'):
            plan=node('Sort',', '.join(label(o['expr'])+(' DESC' if o['desc'] else ' ASC') for o in s['order_by']),plan['estimated_rows'],[plan]); sort_plan=plan
        if s.get('limit') is not None or s.get('offset',0):
            plan=node('Limit',f"limit={s.get('limit')} offset={s.get('offset',0)}",min(plan['estimated_rows'],s['limit']) if s.get('limit') is not None else plan['estimated_rows'],[plan]); limit_plan=plan
        join_search_plan=None
        if len(sources)>=3 and self._research.mode=='adaptive':
            order=' → '.join(join_optimization.get('order',[])) or '原 SQL 顺序'
            detail=(f"{join_optimization.get('source','original-order')}; relations={join_optimization.get('relation_count')}; "
                    f"order={order}; stats_version={join_optimization.get('statistics_version')}; "
                    f"budget_ms={join_optimization.get('search_budget_ms')}; "
                    f"candidate_limit={join_optimization.get('candidate_limit')}; "
                    f"candidates={join_optimization.get('candidate_prefixes',0)}; "
                    f"estimated_rows={join_optimization.get('estimated_rows','n/a')}; "
                    f"estimated_cost_ms={join_optimization.get('estimated_cost_ms','n/a')}; "
                    f"cost_sources={','.join(join_optimization.get('cost_sources',[])) or 'n/a'}; "
                    f"confidence={join_optimization.get('confidence','low')}; "
                    f"benefit_ms={join_optimization.get('benefit_ms','n/a')}; "
                    f"margin_ms={join_optimization.get('margin_ms','n/a')}; "
                    f"search_ms={join_optimization.get('elapsed_ms',0)}; "
                    f"fallback={join_optimization.get('reason') or 'none'}")
            plan=node('JoinOrderSearch',detail,plan['estimated_rows'],[plan])
            join_search_plan=plan
        research={'mode':self._research.mode,'generation':self._research.generation,
                  'join_normalization':join_normalization,
                  'statistics_available':bool(self._research.tables),
                  'statistics_generation':self._research.statistics_generation,
                  'estimation_ms':round(self._research.estimation_ms-estimation_before,4),
                  'join_optimization':copy.deepcopy(join_optimization)}
        if explain_only:
            if len(sources)>1: plan['join_optimization']=copy.deepcopy(join_optimization)
            result=self._result(plan=plan); result['research']=research; return result
        if aggregate:
            buckets=defaultdict(list)
            for r in rows:
                # Python considers True == 1 and False == 0. SQL BOOLEAN is
                # distinct from numeric values, including mixed COALESCE output.
                key=row_signature(evaluate(e,r) for e in s.get('group_by',[]))
                buckets[key].append(r)
            if not s.get('group_by') and not buckets: buckets[()]=[]
            groups=[(g[0] if g else {},g) for g in buckets.values()]
            aggregate_plan['actual_rows']=len(groups)
        else: groups=[(r,None) for r in rows]
        projected=[]
        for r,g in groups:
            if s.get('having') and truth(evaluate(s['having'],r,g)) is not True: continue
            values=[evaluate(c['expr'],r,g) for c in columns]
            output=dict(r)
            output.update({c['alias']:v for c,v in zip(columns,values)})
            sortkeys=[]
            for o in s.get('order_by',[]):
                e=o['expr']
                if e['kind']=='literal' and type(e['value']) is int:
                    position=e['value']
                    if not 1<=position<=len(values): raise DatabaseError("ORDER BY position out of range")
                    sortkeys.append(values[position-1])
                else: sortkeys.append(evaluate(e,output,g))
            projected.append((values,sortkeys))
            self._research_check_budget(len(projected))
        if having_plan: having_plan['actual_rows']=len(projected)
        project_plan['actual_rows']=len(projected)
        if s.get('distinct'):
            seen=set(); unique=[]
            for item in projected:
                signature=row_signature(item[0])
                if signature not in seen: unique.append(item); seen.add(signature)
            projected=unique
            distinct_plan['actual_rows']=len(projected)
        if s.get('order_by'):
            def compare(a,b):
                for x,y,o in zip(a[1],b[1],s['order_by']):
                    v=0 if x is None and y is None else -1 if x is None else 1 if y is None else (x>y)-(x<y)
                    if v: return -v if o['desc'] else v
                return 0
            projected.sort(key=cmp_to_key(compare))
            sort_plan['actual_rows']=len(projected)
        offset=s.get('offset',0); limit=s.get('limit')
        projected=projected[offset:] if limit is None else projected[offset:offset+limit]
        if limit_plan: limit_plan['actual_rows']=len(projected)
        if join_search_plan: join_search_plan['actual_rows']=len(projected)
        self._research_check_budget(len(projected))
        result=self._result([c['alias'] for c in columns],[p[0] for p in projected],plan=plan)
        if len(sources)>1: plan['join_optimization']=copy.deepcopy(join_optimization)
        result['research']=research
        workload_after=self.pager.stats()
        self._research.observe_workload(original,plan,(time.perf_counter()-query_start)*1000,
            max(0,workload_after.get('disk_reads',0)-workload_before.get('disk_reads',0)),
            max(0,self.counters.get('rows_scanned',0)-counters_before.get('rows_scanned',0)),
            max(0,self.counters.get('join_comparisons',0)-counters_before.get('join_comparisons',0)))
        return result
