"""Safe normalization and input filtering shared by join execution paths.

All expressions are already name-bound. Only top-level AND conjuncts move;
outer joins remain ordered and retain their WHERE predicates above the join.
"""
from __future__ import annotations


def conjuncts(expr):
    if expr and expr.get('kind') == 'binary' and expr.get('op') == 'AND':
        return conjuncts(expr['left']) + conjuncts(expr['right'])
    return [expr] if expr else []


def conjunction(expressions):
    result = None
    for expr in expressions:
        result = expr if result is None else {
            'kind': 'binary', 'op': 'AND', 'left': result, 'right': expr}
    return result


def references(expr):
    pending = [expr]
    aliases = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if value.get('kind') == 'column':
                aliases.add(value['name'].split('.', 1)[0])
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return aliases


def normalize_inner_joins(sources, where):
    """Unify ON/WHERE joins without splitting OR or crossing an outer barrier.

    Cross-table WHERE conjuncts become ON at the first evaluable prefix.
    Single-table ON conjuncts become filters. This is equivalent for an all-
    INNER product, including comma/CROSS syntax, duplicates and SQL NULLs.
    """
    if len(sources) < 2 or any(s.get('type', 'INNER') != 'INNER' for s in sources[1:]):
        return where, {'normalized': False, 'where_join_predicates': 0}
    positions = {s['resolved_alias']: i for i, s in enumerate(sources)}
    joins = {i: [] for i in range(1, len(sources))}
    filters = []
    promoted = 0
    for origin, expr in [('on', s.get('on')) for s in sources[1:]] + [('where', where)]:
        for atom in conjuncts(expr):
            refs = references(atom)
            if len(refs) >= 2:
                joins[max(positions[a] for a in refs)].append(atom)
                promoted += origin == 'where'
            elif not (atom.get('kind') == 'literal' and atom.get('value') is True):
                filters.append(atom)
    for i, source in enumerate(sources[1:], 1):
        source['on'] = conjunction(joins[i]) or {'kind': 'literal', 'value': True}
    return conjunction(filters), {'normalized': True, 'where_join_predicates': promoted}


def safe_input_filters(sources, where):
    """Return per-input filters and WHERE ids to remove after execution.

    An alias introduced by LEFT JOIN is nullable: its WHERE filters remain
    residual (IS NULL is a crucial counterexample). Right-only ON conjuncts
    can always filter that join's right input, including for LEFT JOIN.
    """
    by_alias = {}
    moved = set()
    nullable = {s['resolved_alias'] for s in sources[1:] if s.get('type') == 'LEFT'}
    for atom in conjuncts(where):
        refs = references(atom)
        if len(refs) == 1 and not refs & nullable:
            by_alias.setdefault(next(iter(refs)), []).append(atom)
            moved.add(id(atom))
    for source in sources[1:]:
        alias = source['resolved_alias']
        for atom in conjuncts(source.get('on')):
            if references(atom) == {alias}:
                by_alias.setdefault(alias, []).append(atom)
    return by_alias, moved


def range_join_key(on, right_columns):
    """Find one direct column range bound; the executor rechecks the full ON."""
    reverse = {'<': '>', '<=': '>=', '>': '<', '>=': '<='}
    for atom in conjuncts(on):
        if atom.get('kind') != 'binary' or atom.get('op') not in reverse:
            continue
        left, right = atom['left'], atom['right']
        if left.get('kind') != 'column' or right.get('kind') != 'column':
            continue
        a, b = left['name'], right['name']
        if b in right_columns and a not in right_columns:
            return a, b, atom['op']
        if a in right_columns and b not in right_columns:
            return b, a, reverse[atom['op']]
    return None
