"""Fixed, isolated CS enrollment example shared by the workbench and study."""
from __future__ import annotations

from collections import Counter
import statistics
import tempfile
import time

from .engine import Database, row_signature


QUERY = (
    "SELECT p.id, c.id\nFROM registrations r\n"
    "JOIN classes c ON r.class_id=c.id\n"
    "JOIN people p ON r.person_id=p.id\n"
    "WHERE p.department='CS'"
)
TRAINING = [(department, threshold) for department in ('CS', 'ART', 'BIO', 'EE')
            for threshold in (90, 85, 60)]


def build_database(folder):
    """Create the PPT's deterministic fixture in a new, empty directory."""
    db = Database(folder, cache_size=32)
    try:
        db.execute('''
            CREATE TABLE people(id INTEGER, department TEXT);
            CREATE TABLE classes(id INTEGER, score INTEGER);
            CREATE TABLE registrations(person_id INTEGER, class_id INTEGER);
            CREATE INDEX people_department_idx ON people(department);
            CREATE INDEX classes_score_idx ON classes(score)
        ''')
        people = []
        for person in range(1, 1001):
            band = person % 100
            department = 'CS' if band == 0 else 'ART' if band < 3 else 'BIO' if band < 8 else 'EE'
            people.append((person, "'" + department + "'"))
        classes = [(i, 95 if i % 100 == 0 else 85 if i % 100 < 6 else 60)
                   for i in range(1, 101)]
        registrations = [(person, ((person * 7 + slot * 13) % 100) + 1)
                         for person in range(1, 1001) for slot in range(10)]
        for table, rows in (('people', people), ('classes', classes),
                            ('registrations', registrations)):
            for offset in range(0, len(rows), 250):
                values = ','.join('(' + ','.join(map(str, row)) + ')'
                                  for row in rows[offset:offset + 250])
                db.execute(f'INSERT INTO {table} VALUES {values}')
        return db
    except Exception:
        db.close()
        raise


def _cold_execute(db, sql):
    db.pager.clear_cache()
    return db.execute(sql)[0]


def _join_outputs(plan):
    rows = []
    for child in plan.get('children', []):
        rows.extend(_join_outputs(child))
    if plan.get('operator') == 'HashJoin':
        rows.append(plan['actual_rows'])
    return rows


def run_demo():
    """Measure both policies three times, without accessing the user's DB."""
    with tempfile.TemporaryDirectory(prefix='auroradb-ppt-') as folder:
        db = build_database(folder)
        try:
            db.analyze_statistics()
            db.set_optimizer_mode('adaptive')
            for _ in range(3):
                for table, column in (('people', 'id'), ('classes', 'id'),
                                      ('registrations', 'person_id')):
                    _cold_execute(db, f'SELECT {column} FROM {table}')
                for department in ('CS', 'ART', 'BIO'):
                    _cold_execute(db, f"SELECT id FROM people WHERE department='{department}'")
            for department, threshold in TRAINING:
                _cold_execute(db,
                    "SELECT p.id,c.id FROM registrations r "
                    "JOIN classes c ON r.class_id=c.id "
                    "JOIN people p ON r.person_id=p.id "
                    f"WHERE p.department='{department}' AND c.score>={threshold}")
            learning = db.research_status()['learning']
            db._research.training_enabled = False
            policies = [('sql_order', 'heuristic'), ('adaptive', 'adaptive')]
            reference = None
            matched = True
            # Give both policies one unmeasured run, then alternate run order.
            for _, mode in policies:
                db.set_optimizer_mode(mode)
                result = _cold_execute(db, QUERY)
                bag = Counter(row_signature(row) for row in result['rows'])
                if reference is None:
                    reference = bag
                matched = matched and bag == reference
            runs = {name: [] for name, _ in policies}
            summaries = {}
            for trial in range(3):
                for name, mode in policies if trial % 2 == 0 else reversed(policies):
                    db.set_optimizer_mode(mode)
                    db.pager.clear_cache()
                    started = time.perf_counter()
                    result = db.execute(QUERY)[0]
                    elapsed = (time.perf_counter() - started) * 1000
                    runs[name].append(elapsed)
                    matched = matched and Counter(row_signature(row) for row in result['rows']) == reference
                    summaries[name] = {
                        'row_count': result['row_count'], 'stats': result['stats'],
                        'plan': result['plan'], 'repetitions': 3,
                        'join_order': result['research']['join_optimization']['order'],
                        'join_outputs': _join_outputs(result['plan']),
                    }
            for name, _ in policies:
                summaries[name]['elapsed_ms'] = statistics.median(runs[name])
                summaries[name]['runs_ms'] = runs[name]
            return {
                'sql': QUERY + ';', 'matched': matched, 'repetitions': 3,
                'sql_order': summaries['sql_order'], 'adaptive': summaries['adaptive'],
                'columns': result['columns'], 'rows': result['rows'], 'learning': learning,
            }
        finally:
            db.close()
