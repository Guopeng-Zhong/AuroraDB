"""Reproducible local-only SQL edge comparisons against an external SQLite oracle."""
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from auroradb.engine import Database


def main():
    rng = random.Random(88310)
    failures, cases = [], []
    with tempfile.TemporaryDirectory(prefix='auroradb-sql-edge-') as directory:
        with Database(directory) as db, sqlite3.connect(':memory:') as oracle:
            for definition in ('CREATE TABLE l(id INTEGER,k INTEGER,v REAL,s TEXT)',
                               'CREATE TABLE r(id INTEGER,k REAL,v REAL,s TEXT)'):
                db.execute(definition)
                oracle.execute(definition)
            def literal(value):
                return 'NULL' if value is None else repr(value) if isinstance(value,str) else str(value)
            for table in ('l','r'):
                rows = [(i,rng.choice([None,-2,-1,0,1,2]),rng.choice([None,-2.5,0,1.5,3]),
                         rng.choice([None,'x','y','A'])) for i in range(24)]
                sql = 'INSERT INTO '+table+' VALUES '+','.join(
                    '('+','.join(literal(value) for value in row)+')' for row in rows)
                db.execute(sql)
                oracle.execute(sql)
            db.execute('CREATE INDEX lk ON l(k); CREATE INDEX lv ON l(v); CREATE INDEX ls ON l(s)')
            predicates = []
            for column,values in (('k',['-2','0','2','NULL']),('v',['-2.5','0','3','NULL']),
                                  ('s',["'x'","'y'","'A'",'NULL'])):
                for permutation in itertools.permutations(values,3):
                    predicates.append(f'{column} IN ('+','.join(permutation)+','+permutation[0]+')')
                    predicates.append(f'{column} NOT IN ('+','.join(permutation)+')')
                for operator in ('=','!=','<','<=','>','>='):
                    for value in values:
                        predicates.extend((f'{column}{operator}{value}',f'{value}{operator}{column}'))
            for predicate in predicates:
                cases.extend((f'SELECT id,k,v,s FROM l WHERE {predicate} ORDER BY id',
                    f'SELECT k,COUNT(*),COUNT(v),SUM(v),AVG(v),MIN(v),MAX(v) FROM l '
                    f'WHERE {predicate} GROUP BY k ORDER BY k'))
            for predicate in ('l.k=r.k','l.k<r.k','l.v>r.v','l.s=r.s','l.k IS NULL','r.k IS NULL'):
                for wrapper in ('{p}','({p})=TRUE','({p})=FALSE','({p}) IN(TRUE)',
                        '({p}) IN(FALSE)','({p}) NOT IN(TRUE)','NOT ({p})','({p}) OR NULL','({p}) AND TRUE'):
                    for join in ('JOIN','LEFT JOIN'):
                        cases.append('SELECT l.id,r.id,l.k,r.k FROM l '+join+' r ON '+
                                     wrapper.format(p=predicate)+' ORDER BY l.id,r.id')
            for sql in cases:
                try:
                    expected = [list(row) for row in oracle.execute(sql)]
                    actual = db.execute(sql)[0]['rows']
                    if actual != expected:
                        failures.append({'sql':sql,'expected':expected,'actual':actual})
                except Exception as error:
                    failures.append({'sql':sql,'error':str(error)})
    result = {'generated_at':datetime.now(timezone.utc).isoformat(),'passed':not failures,
        'seed':88310,'comparisons':len(cases),'mismatches':len(failures),'failures':failures,
        'scope':'Shared supported SQL subset: index IN/NOT IN, NULL, aggregates and nested BOOLEAN JOIN predicates.',
        'case_sha256':hashlib.sha256('\n'.join(cases).encode('utf-8')).hexdigest(),
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                         for name in ('auroradb/engine.py','auroradb/sql.py','tools/sql_edge_audit.py')}}
    (ROOT/'docs/sql_edge_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'comparisons':len(cases),'mismatches':len(failures)}))
    return int(bool(failures))


if __name__=='__main__':
    raise SystemExit(main())
