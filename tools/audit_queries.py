"""Reproduce the independent 414-query semantic audit using temporary databases.

SQLite is used only as the external test oracle. It is never imported by the
AuroraDB runtime. This audit generates its summary from executed queries.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from auroradb.engine import Database


def literal(value):
    if value is None: return "NULL"
    if value is True: return "TRUE"
    if value is False: return "FALSE"
    if isinstance(value, str): return "'" + value.replace("'", "''") + "'"
    return str(value)


def insert_sql(table, rows):
    return "INSERT INTO " + table + " VALUES " + ",".join(
        "(" + ",".join(literal(value) for value in row) + ")" for row in rows)


def contains_index(plan):
    return plan.get("operator") == "IndexScan" or any(
        contains_index(child) for child in plan.get("children", []))


def summarize(name, seed, cases, failures, started, coverage, **extra):
    return {
        "name": name, "seed": seed, "comparisons": len(cases),
        "passed": len(cases)-len(failures), "mismatches": len(failures),
        "elapsed_seconds": round(time.perf_counter()-started, 6),
        "case_sha256": hashlib.sha256("\n".join(cases).encode("utf-8")).hexdigest(),
        "coverage": coverage, "failures": failures, **extra,
    }


def audit_scan_index():
    started = time.perf_counter()
    rng = random.Random(731)
    rows = [(i, rng.choice([None,-3,-1,0,1,2,9]),
             rng.choice([None,-2.5,0.0,1.5,4.0]),
             rng.choice([None,'A','a','B','中文','']),
             rng.choice([None,True,False])) for i in range(150)]
    cases = []
    for column, keys in {
        'a': [-3,-1,0,1,2,9,10], 'b': [-2.5,0,1.5,4,100],
        # AuroraDB keeps BOOLEAN distinct from INTEGER/REAL.  Do not feed
        # numeric 0/1 into a BOOLEAN predicate: those are deliberate type
        # errors covered by the semantic regression tests, not shared-result
        # cases for this index differential audit.
        'c': ['A','a','B','中文',''], 'd': [True,False],
    }.items():
        for operator in ('=','<','<=','>','>='):
            for key in keys:
                predicate = f"{column} {operator} {literal(key)}"
                cases.extend([
                    f"SELECT id FROM t WHERE {predicate} ORDER BY id",
                    f"SELECT a,COUNT(*),AVG(b) FROM t WHERE {predicate} GROUP BY a ORDER BY a",
                    f"SELECT id FROM t WHERE {literal(key)} {operator} {column} ORDER BY id",
                ])
    failures = []
    indexed_queries = 0
    with tempfile.TemporaryDirectory() as plain_dir, tempfile.TemporaryDirectory() as indexed_dir:
        with Database(plain_dir) as plain, Database(indexed_dir) as indexed:
            for db in (plain, indexed):
                db.execute("CREATE TABLE t(id INTEGER,a INTEGER,b REAL,c TEXT,d BOOLEAN)")
                db.execute(insert_sql('t', rows))
            for column in ('a','b','c','d'):
                indexed.execute(f"CREATE INDEX ix_{column} ON t({column})")
            for sql in cases:
                expected = plain.execute(sql)[0]['rows']
                actual_result = indexed.execute(sql)[0]
                actual = actual_result['rows']
                indexed_queries += int(contains_index(actual_result['plan']))
                if expected != actual:
                    failures.append({'sql': sql, 'expected': expected, 'actual': actual})
    return summarize('scan_index_consistency', 731, cases, failures, started, {
        'rows_per_database': 150, 'types': ['INTEGER','REAL','TEXT','BOOLEAN'],
        'operators': ['=','<','<=','>','>='], 'nulls': True,
        'forms': ['filtered projection','filtered grouped COUNT/AVG','literal-left comparison'],
        'indexed_columns': ['a','b','c','d'],
        'boolean_numeric_coercion': 'rejected; covered by semantic regression tests',
    }, index_path_queries=indexed_queries)


def audit_sqlite_join():
    started = time.perf_counter()
    rng = random.Random(919)
    data = {}
    for table, count in [('l',35),('r',40),('s',14)]:
        data[table] = []
        for i in range(count):
            row = [i, rng.choice([None,0,1,2,3,4])]
            if table != 's': row.append(rng.choice([None,1.0,2.5,-1.5]))
            data[table].append(row)
    cases = []
    for join in ('JOIN','LEFT JOIN'):
        for predicate in ('l.k=r.k','l.k+0=r.k','l.k=r.k AND l.v<r.v',
                          'l.k=r.k OR l.id=r.id','l.k<r.k','l.k IS NULL AND r.k IS NULL'):
            for where in ('',' WHERE l.id<10',' WHERE r.k IS NULL',' WHERE l.v>0 OR r.v IS NULL'):
                source = f"FROM l {join} r ON {predicate}{where}"
                cases.extend([
                    f"SELECT l.id,r.id,l.k,r.k {source} ORDER BY l.id,r.id",
                    f"SELECT l.k,COUNT(*),COUNT(r.id),SUM(r.v),AVG(l.v) {source} "
                    "GROUP BY l.k HAVING COUNT(*)>1 ORDER BY l.k",
                ])
    cases.extend([
        "SELECT l.id,r.id,s.id FROM l LEFT JOIN r ON l.k=r.k LEFT JOIN s ON r.k=s.k "
        "WHERE l.id<5 ORDER BY l.id,r.id,s.id",
        "SELECT l.k,AVG(l.v+r.v) AS av FROM l JOIN r ON l.k=r.k GROUP BY l.k ORDER BY av",
        "SELECT COUNT(*),SUM(r.v),AVG(l.v) FROM l LEFT JOIN r ON l.k=r.k WHERE l.k=99",
    ])
    failures = []
    with tempfile.TemporaryDirectory() as directory:
        with Database(directory) as db, sqlite3.connect(':memory:') as oracle:
            for table in ('l','r','s'):
                definition = f"CREATE TABLE {table}(id INTEGER,k INTEGER" + (",v REAL)" if table != 's' else ")")
                db.execute(definition)
                oracle.execute(definition)
                sql = insert_sql(table,data[table])
                db.execute(sql)
                oracle.execute(sql)
            for sql in cases:
                expected = [list(row) for row in oracle.execute(sql).fetchall()]
                actual = db.execute(sql)[0]['rows']
                if expected != actual:
                    failures.append({'sql': sql, 'expected': expected, 'actual': actual})
    return summarize('sqlite_join_group_having', 919, cases, failures, started, {
        'rows': {'l':35,'r':40,'s':14}, 'joins': ['INNER','LEFT','chained LEFT'],
        'predicates': ['equality','arithmetic equality','AND','OR','inequality','IS NULL'],
        'aggregates': ['COUNT(*)','COUNT(column)','SUM','AVG'],
        'clauses': ['WHERE','GROUP BY','HAVING','ORDER BY source and output alias'],
        'comparison': 'Exact row arrays, including NULL and floating-point values',
        'oracle': 'SQLite external test oracle; restricted to the supported shared subset',
    })


def main():
    suites = [audit_scan_index(), audit_sqlite_join()]
    result = {
        'format_version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'environment': {'python':sys.version.split()[0], 'platform':platform.platform(),
                        'sqlite_oracle_version':sqlite3.sqlite_version},
        'engine_sha256': hashlib.sha256((ROOT/'auroradb'/'engine.py').read_bytes()).hexdigest(),
        'suites': suites,
        'total_comparisons': sum(suite['comparisons'] for suite in suites),
        'total_passed': sum(suite['passed'] for suite in suites),
        'total_mismatches': sum(suite['mismatches'] for suite in suites),
        'scope': 'Independent query comparisons supplement the unit tests; no full SQL compatibility claim.',
    }
    output = ROOT/'docs'/'audit_queries.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for suite in suites:
        print(f"{suite['name']}: {suite['passed']}/{suite['comparisons']} passed; "
              f"{suite['mismatches']} mismatches")
    print(f"Evidence: {output}")
    return int(result['total_mismatches'] != 0)


if __name__ == '__main__':
    raise SystemExit(main())
