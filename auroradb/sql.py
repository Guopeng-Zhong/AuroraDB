"""Position-aware SQL lexer and recursive-descent parser.

The grammar deliberately describes a finite SQL subset.  Every accepted
statement has a plain-data AST; SQL text is never evaluated as Python.  An
unsupported clause fails at its source location instead of being ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any


class SQLError(ValueError):
    """A syntax error with a zero-based offset and one-based line/column."""

    def __init__(self, message: str, position: int = 0,
                 line: int = 1, column: int = 1):
        self.message = message
        self.position = position
        self.line = line
        self.column = column
        super().__init__(f"{message} (line {line}, column {column})")


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    position: int
    line: int
    column: int
    end: int


# Reserving clause and unsupported SQL words makes implicit aliases unambiguous
# and gives a useful failure for syntax outside the supported subset.
_KEYWORDS = frozenset("""
    SELECT DISTINCT ALL FROM AS WHERE AND OR NOT NULL TRUE FALSE IS IN LIKE
    BETWEEN GROUP BY HAVING ORDER ASC DESC LIMIT OFFSET JOIN INNER LEFT OUTER
    RIGHT FULL CROSS NATURAL ON USING UNION INTERSECT EXCEPT WITH RECURSIVE
    CREATE TABLE INDEX UNIQUE PRIMARY KEY DEFAULT INTEGER INT REAL FLOAT TEXT
    VARCHAR BOOLEAN BOOL DROP INSERT INTO VALUES UPDATE SET DELETE BEGIN
    TRANSACTION START COMMIT ROLLBACK SHOW TABLES DESCRIBE EXPLAIN ANALYZE
    CHECKPOINT IF EXISTS ALTER ADD FOREIGN REFERENCES CHECK CONSTRAINT
    AUTOINCREMENT RETURNING CASE WHEN THEN ELSE END CAST COLLATE ESCAPE NULLS
    FIRST LAST REPLACE PRAGMA VACUUM SAVEPOINT RELEASE TRIGGER VIEW DATABASE
""".split())
_TYPE_NAMES = {"INTEGER": "INTEGER", "INT": "INTEGER", "REAL": "REAL", "FLOAT": "REAL",
               "TEXT": "TEXT", "VARCHAR": "TEXT", "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN"}
# Type names are contextual, so a column named "text" or "real" remains legal.
_KEYWORDS = _KEYWORDS - _TYPE_NAMES.keys()
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_IDENTIFIER = re.compile(r"[^\W\d]\w*", re.UNICODE)


def tokenize(sql: str) -> list[Token]:
    """Tokenize SQL, including an EOF sentinel; preserve accurate locations.

    Single-quoted strings use SQL's doubled-quote escaping.  Double quotes,
    backticks, and square brackets quote identifiers, including reserved words.
    Both line comments and nested block comments are accepted.
    """
    if not isinstance(sql, str):
        raise SQLError("SQL must be a string")
    tokens: list[Token] = []
    position = 0
    line = column = 1
    length = len(sql)

    def advance(end: int) -> None:
        nonlocal position, line, column
        segment = sql[position:end]
        count = segment.count("\n")
        if count:
            line += count
            column = len(segment) - segment.rfind("\n")
        else:
            column += len(segment)
        position = end

    while position < length:
        char = sql[position]
        if char.isspace():
            advance(position + 1)
            continue
        if sql.startswith("--", position):
            end = sql.find("\n", position + 2)
            advance(length if end < 0 else end)
            continue
        start, token_line, token_column = position, line, column

        def fail(message: str) -> None:
            raise SQLError(message, start, token_line, token_column)

        if sql.startswith("/*", position):
            end, depth = position + 2, 1
            while end < length and depth:
                if sql.startswith("/*", end):
                    depth += 1
                    end += 2
                elif sql.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            if depth:
                fail("Unterminated block comment")
            advance(end)
            continue
        if char in "'\"`[":
            closing = "]" if char == "[" else char
            end = position + 1
            pieces: list[str] = []
            while end < length:
                if sql[end] == "\x00":
                    fail("NUL characters are not allowed in SQL")
                if sql[end] == closing:
                    if end + 1 < length and sql[end + 1] == closing:
                        pieces.append(closing)
                        end += 2
                        continue
                    end += 1
                    break
                pieces.append(sql[end])
                end += 1
            else:
                fail("Unterminated string literal" if char == "'"
                     else "Unterminated quoted identifier")
            value = "".join(pieces)
            if char != "'" and not value:
                fail("An identifier cannot be empty")
            tokens.append(Token("STRING" if char == "'" else "IDENT",
                                value if char == "'" else value.lower(),
                                start, token_line, token_column, end))
            advance(end)
            continue
        if char.isdigit() or (char == "." and position + 1 < length
                              and sql[position + 1].isdigit()):
            match = _NUMBER.match(sql, position)
            if not match:
                fail("Invalid numeric literal")
            raw = match.group()
            end = match.end()
            if end < length and (sql[end].isalnum() or sql[end] in "_."):
                fail("Invalid numeric literal")
            try:
                value = float(raw) if any(c in raw for c in ".eE") else int(raw)
            except ValueError:
                fail("Invalid numeric literal")
            if isinstance(value, float) and not math.isfinite(value):
                fail("Numeric literal must be finite")
            tokens.append(Token("NUMBER", value, start, token_line, token_column, end))
            advance(end)
            continue
        match = _IDENTIFIER.match(sql, position)
        if match:
            raw = match.group()
            upper = raw.upper()
            kind = "KEYWORD" if upper in _KEYWORDS else "IDENT"
            tokens.append(Token(kind, upper if kind == "KEYWORD" else raw.lower(),
                                start, token_line, token_column, match.end()))
            advance(match.end())
            continue
        two = sql[position:position + 2]
        if two in ("<=", ">=", "<>", "!="):
            tokens.append(Token("OP", two, start, token_line, token_column, position + 2))
            advance(position + 2)
        elif char in "=<>+-*/%":
            tokens.append(Token("OP", char, start, token_line, token_column, position + 1))
            advance(position + 1)
        elif char in "(),.;":
            tokens.append(Token("PUNCT", char, start, token_line, token_column, position + 1))
            advance(position + 1)
        else:
            fail("NUL characters are not allowed in SQL" if char == "\x00"
                 else f"Unsupported character {char!r}")
    tokens.append(Token("EOF", "", position, line, column, position))
    return tokens


def _literal(value: Any) -> dict:
    return {"kind": "literal", "value": value}


def _binary(op: str, left: dict, right: dict) -> dict:
    return {"kind": "binary", "op": op, "left": left, "right": right}


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def error(self, message: str, token: Token | None = None) -> None:
        token = token or self.current
        raise SQLError(message, token.position, token.line, token.column)

    def at(self, value: str) -> bool:
        return self.current.kind in ("KEYWORD", "OP", "PUNCT") and self.current.value == value

    def accept(self, value: str) -> bool:
        if self.at(value):
            self.index += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            self.error(f"Expected {value}, found {self.display()}")

    def display(self) -> str:
        return "end of SQL" if self.current.kind == "EOF" else repr(self.current.value)

    def identifier(self) -> str:
        if self.current.kind != "IDENT":
            self.error(f"Expected an identifier, found {self.display()}")
        name = self.current.value
        if "." in name:
            self.error("Dots inside quoted identifiers are unsupported; use table.column qualification")
        self.index += 1
        return name

    def integer(self, label: str, positive: bool = False) -> int:
        token = self.current
        if token.kind != "NUMBER" or type(token.value) is not int:
            self.error(f"{label} must be an integer")
        if token.value < (1 if positive else 0):
            self.error(f"{label} must be {'positive' if positive else 'nonnegative'}")
        self.index += 1
        return token.value

    def parse(self) -> list[dict]:
        statements = []
        while self.current.kind != "EOF":
            if self.accept(";"):
                continue
            statements.append(self.statement())
            if self.current.kind != "EOF" and not self.accept(";"):
                self.error(f"Unexpected {self.display()}; expected a semicolon or end of SQL")
        return statements

    def statement(self) -> dict:
        if self.accept("SELECT"):
            return self.select()
        if self.accept("CREATE"):
            return self.create()
        if self.accept("DROP"):
            if self.accept("TABLE"):
                return {"kind": "drop_table", "name": self.identifier()}
            if self.accept("INDEX"):
                return {"kind": "drop_index", "name": self.identifier()}
            self.error("DROP supports only TABLE and INDEX")
        if self.accept("INSERT"):
            return self.insert()
        if self.accept("UPDATE"):
            return self.update()
        if self.accept("DELETE"):
            self.expect("FROM")
            table = self.identifier()
            where = self.expression() if self.accept("WHERE") else None
            return {"kind": "delete", "table": table, "where": where}
        for keyword, kind in (("BEGIN", "begin"), ("COMMIT", "commit"),
                              ("ROLLBACK", "rollback")):
            if self.accept(keyword):
                self.accept("TRANSACTION")
                return {"kind": kind}
        if self.accept("START"):
            self.expect("TRANSACTION")
            return {"kind": "begin"}
        if self.accept("SHOW"):
            self.expect("TABLES")
            return {"kind": "show_tables"}
        if self.accept("DESCRIBE") or self.accept("DESC"):
            return {"kind": "describe", "table": self.identifier()}
        if self.accept("CHECKPOINT"):
            return {"kind": "checkpoint"}
        if self.accept("CHECK"):
            self.expect("DATABASE")
            return {"kind": "integrity_check"}
        if self.accept("EXPLAIN"):
            analyze = self.accept("ANALYZE")
            self.expect("SELECT")
            return {"kind": "explain", "statement": self.select(), "analyze": analyze}
        self.error(f"Unsupported statement starting with {self.display()}")

    def create(self) -> dict:
        if self.accept("TABLE"):
            name = self.identifier()
            self.expect("(")
            columns = []
            names = set()
            while True:
                token = self.current
                column = self.column_definition()
                if column["name"] in names:
                    self.error(f"Duplicate column {column['name']!r}", token)
                names.add(column["name"])
                columns.append(column)
                if not self.accept(","):
                    break
            self.expect(")")
            if sum(column["primary"] for column in columns) > 1:
                self.error("Only one inline PRIMARY KEY column is supported")
            return {"kind": "create_table", "name": name, "columns": columns}
        unique = self.accept("UNIQUE")
        if self.accept("INDEX"):
            name = self.identifier()
            self.expect("ON")
            table = self.identifier()
            self.expect("(")
            column = self.identifier()
            if self.at(","):
                self.error("Indexes support exactly one column")
            self.expect(")")
            return {"kind": "create_index", "name": name, "table": table,
                    "column": column, "unique": unique}
        self.error("CREATE supports only TABLE and [UNIQUE] INDEX")

    def column_definition(self) -> dict:
        if any(self.at(word) for word in ("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT")):
            self.error("Table-level constraints are unsupported; use inline constraints")
        name = self.identifier()
        token = self.current
        type_name = str(token.value).upper()
        if token.kind not in ("KEYWORD", "IDENT") or type_name not in _TYPE_NAMES:
            self.error("Supported column types are INTEGER, REAL, TEXT, VARCHAR(n), and BOOLEAN")
        self.index += 1
        column = {"name": name, "type": _TYPE_NAMES[type_name], "not_null": False,
                  "primary": False, "unique": False, "default": None}
        if type_name == "VARCHAR" and self.accept("("):
            column["max_length"] = self.integer("VARCHAR length", positive=True)
            self.expect(")")
        seen = set()
        while True:
            if self.at("NOT") or self.at("NULL"):
                if "null" in seen:
                    self.error("Duplicate or conflicting NULL constraint")
                seen.add("null")
                if self.accept("NOT"):
                    self.expect("NULL")
                    column["not_null"] = True
                else:
                    self.expect("NULL")
            elif self.accept("PRIMARY"):
                if "primary" in seen:
                    self.error("Duplicate PRIMARY KEY constraint")
                seen.add("primary")
                self.expect("KEY")
                column["primary"] = column["unique"] = True
            elif self.accept("UNIQUE"):
                if "unique" in seen:
                    self.error("Duplicate UNIQUE constraint")
                seen.add("unique")
                column["unique"] = True
            elif self.accept("DEFAULT"):
                if "default" in seen:
                    self.error("Duplicate DEFAULT constraint")
                seen.add("default")
                column["default"] = self.default_value()
            else:
                break
        if column["primary"]:
            column["not_null"] = True
        return column

    def default_value(self) -> Any:
        # Defaults are stored as scalar metadata, not executable expression ASTs.
        sign = -1 if self.accept("-") else 1
        positive_sign = self.accept("+") if sign == 1 else False
        token = self.current
        if token.kind == "NUMBER":
            self.index += 1
            return sign * token.value
        if sign == -1 or positive_sign:
            self.error("A signed DEFAULT must be a numeric literal")
        if token.kind == "STRING":
            self.index += 1
            return token.value
        if self.accept("NULL"):
            return None
        if self.accept("TRUE"):
            return True
        if self.accept("FALSE"):
            return False
        self.error("DEFAULT supports only a literal value")

    def insert(self) -> dict:
        self.expect("INTO")
        table = self.identifier()
        columns = None
        if self.accept("("):
            columns = [self.identifier()]
            while self.accept(","):
                token = self.current
                column = self.identifier()
                if column in columns:
                    self.error(f"Duplicate INSERT column {column!r}", token)
                columns.append(column)
            self.expect(")")
        self.expect("VALUES")
        values = []
        width = len(columns) if columns is not None else None
        while True:
            self.expect("(")
            row_token = self.current
            row = [self.expression()]
            while self.accept(","):
                row.append(self.expression())
            self.expect(")")
            if width is None:
                width = len(row)
            if len(row) != width:
                self.error(f"INSERT row has {len(row)} values; expected {width}", row_token)
            values.append(row)
            if not self.accept(","):
                break
        return {"kind": "insert", "table": table, "columns": columns, "values": values}

    def update(self) -> dict:
        table = self.identifier()
        self.expect("SET")
        assignments = {}
        while True:
            token = self.current
            name = self.identifier()
            if name in assignments:
                self.error(f"Duplicate assignment for {name!r}", token)
            self.expect("=")
            assignments[name] = self.expression()
            if not self.accept(","):
                break
        where = self.expression() if self.accept("WHERE") else None
        return {"kind": "update", "table": table, "assignments": assignments, "where": where}

    def table_reference(self) -> dict:
        table = self.identifier()
        alias = None
        if self.accept("AS"):
            alias = self.identifier()
        elif self.current.kind == "IDENT":
            alias = self.identifier()
        return {"table": table, "alias": alias}

    def select(self) -> dict:
        distinct = self.accept("DISTINCT")
        columns = []
        while True:
            expr = self.expression(allow_star=True)
            alias = None
            if self.accept("AS"):
                alias = self.identifier()
            elif self.current.kind == "IDENT":
                alias = self.identifier()
            if expr["kind"] == "star" and alias is not None:
                self.error("A star selection cannot have an alias")
            columns.append({"expr": expr, "alias": alias})
            if not self.accept(","):
                break
        source = self.table_reference() if self.accept("FROM") else None
        joins = []
        if source is not None:
            scope_start = 0
            while self.at("JOIN") or self.at("INNER") or self.at("LEFT") or self.at("CROSS") or self.at(","):
                if self.accept(","):
                    scope_start = len(joins) + 1
                    join = self.table_reference()
                    join.update({"type": "INNER", "on": _literal(True), "syntax": "comma"})
                    joins.append(join)
                    continue
                join_type = "INNER"
                cross = self.accept("CROSS")
                if not cross and self.accept("LEFT"):
                    join_type = "LEFT"
                    self.accept("OUTER")
                elif not cross:
                    self.accept("INNER")
                self.expect("JOIN")
                join = self.table_reference()
                if cross:
                    join.update({"type": "INNER", "on": _literal(True), "syntax": "cross"})
                else:
                    self.expect("ON")
                    join.update({"type": join_type, "on": self.expression()})
                if scope_start:
                    # Explicit JOIN binds more tightly than a comma. Its ON
                    # cannot refer to a table in a preceding comma group.
                    join['scope_start'] = scope_start
                joins.append(join)
            if any(self.at(word) for word in ("RIGHT", "FULL", "NATURAL")):
                self.error("Only INNER, LEFT, CROSS and comma joins are supported")
        where = self.expression() if self.accept("WHERE") else None
        group_by = []
        if self.accept("GROUP"):
            self.expect("BY")
            group_by.append(self.expression())
            while self.accept(","):
                group_by.append(self.expression())
        having = self.expression() if self.accept("HAVING") else None
        order_by = []
        if self.accept("ORDER"):
            self.expect("BY")
            while True:
                expr = self.expression()
                desc = self.accept("DESC")
                if not desc:
                    self.accept("ASC")
                order_by.append({"expr": expr, "desc": desc})
                if not self.accept(","):
                    break
        limit, offset = None, 0
        if self.accept("LIMIT"):
            limit = self.integer("LIMIT")
            if self.accept(","):
                offset, limit = limit, self.integer("LIMIT")
            elif self.accept("OFFSET"):
                offset = self.integer("OFFSET")
        return {"kind": "select", "distinct": distinct, "columns": columns,
                "from": source, "joins": joins, "where": where, "group_by": group_by,
                "having": having, "order_by": order_by, "limit": limit, "offset": offset}

    def expression(self, allow_star: bool = False) -> dict:
        return self.logical_or(allow_star)

    def logical_or(self, allow_star: bool) -> dict:
        expr = self.logical_and(allow_star)
        while self.accept("OR"):
            self.no_star(expr)
            expr = _binary("OR", expr, self.logical_and(False))
        return expr

    def logical_and(self, allow_star: bool) -> dict:
        expr = self.logical_not(allow_star)
        while self.accept("AND"):
            self.no_star(expr)
            expr = _binary("AND", expr, self.logical_not(False))
        return expr

    def logical_not(self, allow_star: bool) -> dict:
        if self.accept("NOT"):
            return {"kind": "unary", "op": "NOT", "expr": self.logical_not(False)}
        return self.comparison(allow_star)

    def comparison(self, allow_star: bool) -> dict:
        expr = self.additive(allow_star)
        if self.current.kind == "OP" and self.current.value in ("=", "<>", "!=", "<", "<=", ">", ">="):
            self.no_star(expr)
            op = self.current.value
            self.index += 1
            expr = _binary(op, expr, self.additive(False))
        elif self.accept("IS"):
            self.no_star(expr)
            negated = self.accept("NOT")
            self.expect("NULL")
            expr = {"kind": "is_null", "expr": expr, "negated": negated}
        elif self.at("NOT") or self.at("IN") or self.at("LIKE") or self.at("BETWEEN"):
            self.no_star(expr)
            negated = self.accept("NOT")
            if self.accept("IN"):
                self.expect("(")
                if self.at("SELECT"):
                    self.error("IN subqueries are unsupported; provide a value list")
                values = [self.expression()]
                while self.accept(","):
                    values.append(self.expression())
                self.expect(")")
                expr = {"kind": "in", "expr": expr, "values": values, "negated": negated}
            elif self.accept("LIKE"):
                expr = _binary("LIKE", expr, self.additive(False))
                if negated:
                    expr = {"kind": "unary", "op": "NOT", "expr": expr}
            elif self.accept("BETWEEN"):
                low = self.additive(False)
                self.expect("AND")
                high = self.additive(False)
                expr = _binary("AND", _binary(">=", expr, low), _binary("<=", expr, high))
                if negated:
                    expr = {"kind": "unary", "op": "NOT", "expr": expr}
            else:
                self.error("Expected IN, LIKE, or BETWEEN after NOT")
        return expr

    def additive(self, allow_star: bool) -> dict:
        expr = self.multiplicative(allow_star)
        while self.at("+") or self.at("-"):
            self.no_star(expr)
            op = self.current.value
            self.index += 1
            expr = _binary(op, expr, self.multiplicative(False))
        return expr

    def multiplicative(self, allow_star: bool) -> dict:
        expr = self.unary(allow_star)
        while self.at("*") or self.at("/") or self.at("%"):
            self.no_star(expr)
            op = self.current.value
            self.index += 1
            expr = _binary(op, expr, self.unary(False))
        return expr

    def unary(self, allow_star: bool) -> dict:
        if self.at("+") or self.at("-"):
            op = self.current.value
            self.index += 1
            return {"kind": "unary", "op": op, "expr": self.unary(False)}
        return self.primary(allow_star)

    def primary(self, allow_star: bool) -> dict:
        token = self.current
        if token.kind in ("NUMBER", "STRING"):
            self.index += 1
            return _literal(token.value)
        if self.accept("NULL"):
            return _literal(None)
        if self.accept("TRUE"):
            return _literal(True)
        if self.accept("FALSE"):
            return _literal(False)
        if self.accept("("):
            if self.at("SELECT"):
                self.error("Subqueries are unsupported")
            expr = self.expression(False)
            self.expect(")")
            return expr
        if allow_star and self.accept("*"):
            return {"kind": "star", "table": None}
        if token.kind == "IDENT":
            name = self.identifier()
            if self.accept("("):
                args = []
                if not self.at(")"):
                    if self.accept("*"):
                        if name.upper() != "COUNT":
                            self.error("Only COUNT accepts a star argument", token)
                        args.append({"kind": "star", "table": None})
                    else:
                        if self.at("DISTINCT"):
                            self.error("DISTINCT aggregate arguments are unsupported")
                        args.append(self.expression())
                        while self.accept(","):
                            args.append(self.expression())
                self.expect(")")
                return {"kind": "call", "name": name.upper(), "args": args}
            if self.accept("."):
                if allow_star and self.accept("*"):
                    return {"kind": "star", "table": name}
                name += "." + self.identifier()
            return {"kind": "column", "name": name}
        self.error(f"Expected an expression, found {self.display()}")

    def no_star(self, expr: dict) -> None:
        if expr["kind"] == "star":
            self.error("A star must stand alone in the SELECT list or in COUNT(*)")


def parse(sql: str) -> list[dict]:
    """Parse one or more semicolon-separated statements into plain dictionaries."""
    parser = _Parser(tokenize(sql))
    try:
        statements = parser.parse()
    except RecursionError as error:
        token = parser.current
        raise SQLError("SQL expression nesting is too deep", token.position,
                       token.line, token.column) from error
    # Flat arithmetic chains also produce nested trees.  Check iteratively before
    # the engine's semantic visitors or deepcopy encounter Python's stack limit.
    pending = [(statement, 0) for statement in statements]
    while pending:
        value, depth = pending.pop()
        if depth > 128:
            parser.error("SQL expression nesting is too deep (maximum AST depth: 128)")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values()
                           if isinstance(child, (dict, list)))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value
                           if isinstance(child, (dict, list)))
    return statements
