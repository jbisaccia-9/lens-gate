"""What tables does this SQL read? Conservative by design: anything it cannot
classify is a refusal, not a pass.

This is a scope checker, not a SQL parser. It answers one question — "is
every relation this statement reads schema-qualified and inside the allowed
schemas?" — and refuses statements it can't answer that for: more than one
statement, anything that isn't SELECT/WITH, write or session keywords, table
functions, unqualified names that aren't CTEs. Metabase and the warehouse
login enforce scope again after this; the point of checking here is that the
refusal happens *before* anything runs, with a reason a person can read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "CREATE", "GRANT", "REVOKE", "TRUNCATE",
    "COPY", "ATTACH", "DETACH", "PRAGMA", "CALL", "EXECUTE", "EXEC", "SET", "RESET", "VACUUM", "INTO",
    "DO", "LISTEN", "NOTIFY", "LOCK", "COMMENT", "SECURITY", "REINDEX", "CLUSTER", "IMPORT", "LOAD",
}
CLAUSE_END = {"WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "OFFSET", "UNION", "EXCEPT", "INTERSECT",
              "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "ON", "USING", "WINDOW",
              "FETCH", "FOR", "RETURNING", "QUALIFY"}
# Functions a reporting query needs. Anything else called with "(" is refused:
# Postgres can run SQL hidden in a string (query_to_xml, dblink, ...), and a
# scope checker that strips string literals would never see those tables.
ALLOWED_FUNCTIONS = {
    "SUM", "AVG", "COUNT", "MIN", "MAX", "ROUND", "ABS", "COALESCE", "NULLIF", "IFNULL", "CAST",
    "LOWER", "UPPER", "TRIM", "LENGTH", "SUBSTRING", "SUBSTR", "CONCAT", "REPLACE", "LEFT", "RIGHT",
    "GREATEST", "LEAST", "FLOOR", "CEIL", "CEILING", "POWER", "SQRT", "LN", "LOG", "EXP", "SIGN", "MOD",
    "DATE_TRUNC", "DATE_PART", "EXTRACT", "TO_CHAR", "STRFTIME", "DATE", "AGE",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE",
    "PERCENTILE_CONT", "PERCENTILE_DISC", "STDDEV", "STDDEV_SAMP", "VARIANCE", "STRING_AGG",
    "NUMERIC", "DECIMAL", "VARCHAR", "CHAR",  # type modifiers: CAST(x AS NUMERIC(12,2))
}
# Keywords that are legitimately followed by "(".
PAREN_KEYWORDS = {
    "IN", "EXISTS", "ANY", "ALL", "SOME", "OVER", "FILTER", "WITHIN", "VALUES", "AS", "ON", "USING", "AND",
    "OR", "NOT", "WHEN", "THEN", "ELSE", "SELECT", "FROM", "JOIN", "WHERE", "LATERAL", "CASE", "BETWEEN",
    "IS", "LIKE", "DISTINCT", "UNION", "EXCEPT", "INTERSECT", "BY", "RECURSIVE", "WITH", "HAVING", "END",
}
_TOKEN = re.compile(r'"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*|\d+(?:\.\d+)?|<>|<=|>=|!=|\|\||::|[^\sA-Za-z0-9_]')


@dataclass
class Scope:
    tables: list[str] = field(default_factory=list)  # "schema.table", lower-cased
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _strip(sql: str) -> str:
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                elif sql[j] == "'":
                    break
                else:
                    j += 1
            out.append(" '' ")
            i = j + 1
        elif c == "$" and re.match(r"\$[A-Za-z_]*\$", sql[i:]):
            return "\0"  # dollar-quoted bodies: refuse rather than reason about them
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _ident(tok: str) -> str:
    return tok[1:-1].replace('""', '"').lower() if tok.startswith('"') else tok.lower()


def _is_ident(tok: str) -> bool:
    return tok.startswith('"') or bool(re.match(r"[A-Za-z_]", tok))


def scope(sql: str) -> Scope:
    s = Scope()
    clean = _strip(sql)
    if "\0" in clean:
        s.problems.append("dollar-quoted string")
        return s
    body = clean.strip().rstrip(";").strip()
    if ";" in body:
        s.problems.append("more than one statement")
        return s
    toks = _TOKEN.findall(body)
    if not toks or toks[0].upper() not in ("SELECT", "WITH"):
        s.problems.append("not a SELECT/WITH statement")
        return s
    bad = sorted({t.upper() for t in toks if not t.startswith('"') and t.upper() in FORBIDDEN})
    if bad:
        s.problems.append(f"forbidden keyword(s): {', '.join(bad)}")
        return s

    # CTE names: WITH [RECURSIVE] name [(cols)] AS ( ... ) , name AS ( ... )
    ctes: set[str] = set()
    depth = 0
    for i, t in enumerate(toks):
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        elif t.upper() == "AS" and i + 1 < len(toks) and toks[i + 1] == "(" and i >= 1:
            j = i - 1
            if toks[j] == ")":  # column list
                d = 0
                while j >= 0:
                    d += toks[j] == ")"
                    d -= toks[j] == "("
                    if d == 0:
                        break
                    j -= 1
                j -= 1
            if j >= 0 and _is_ident(toks[j]) and toks[j].upper() not in ("SELECT",):
                prev = toks[j - 1].upper() if j >= 1 else ""
                if prev in ("WITH", "RECURSIVE", ","):
                    ctes.add(_ident(toks[j]))

    # Every call: name( ... ) must be on the allowlist (CTE column lists excepted).
    for i, t in enumerate(toks[:-1]):
        if toks[i + 1] != "(" or not _is_ident(t) or t.startswith('"'):
            continue
        u = t.upper()
        if u in PAREN_KEYWORDS or u in ALLOWED_FUNCTIONS:
            continue
        if _ident(t) in ctes:
            continue  # WITH name (col, ...) AS (
        prev = toks[i - 1] if i else ""
        if prev == ".":
            s.problems.append(f"schema-qualified function call {toks[i - 2]}.{t}() is not allowed")
        else:
            s.problems.append(f"function {t}() is not on the allowlist")
        return s

    # FROM inside EXTRACT(... FROM x), SUBSTRING(x FROM n), TRIM(... FROM x) is not a table.
    not_a_table: set[int] = set()
    for i, t in enumerate(toks[:-1]):
        if t.upper() in ("EXTRACT", "SUBSTRING", "TRIM") and toks[i + 1] == "(":
            d, j = 0, i + 1
            while j < len(toks):
                d += toks[j] == "("
                d -= toks[j] == ")"
                if d == 1 and toks[j].upper() == "FROM":
                    not_a_table.add(j)
                if d == 0:
                    break
                j += 1

    i = 0
    while i < len(toks):
        t = toks[i].upper()
        if t in ("FROM", "JOIN") and i not in not_a_table:
            in_from = t == "FROM"
            i += 1
            while i < len(toks):
                if toks[i].upper() == "LATERAL":
                    i += 1
                if i < len(toks) and toks[i] == "(":
                    break  # subquery: its own FROM/JOIN is scanned on its own
                if i >= len(toks) or not _is_ident(toks[i]):
                    s.problems.append("unreadable FROM clause")
                    return s
                parts = [_ident(toks[i])]
                i += 1
                while i + 1 < len(toks) and toks[i] == "." and _is_ident(toks[i + 1]):
                    parts.append(_ident(toks[i + 1]))
                    i += 2
                if i < len(toks) and toks[i] == "(":
                    s.problems.append(f"table function {'.'.join(parts)}() is not allowed")
                    return s
                if len(parts) == 1:
                    if parts[0] not in ctes:
                        s.problems.append(f"unqualified table '{parts[0]}' (write schema.table)")
                elif len(parts) == 2:
                    s.tables.append(".".join(parts))
                else:
                    s.problems.append(f"three-part name {'.'.join(parts)} (cross-database) is not allowed")
                # optional alias
                if i < len(toks) and toks[i].upper() == "AS":
                    i += 1
                if i < len(toks) and _is_ident(toks[i]) and toks[i].upper() not in CLAUSE_END | {"FROM", "SELECT"}:
                    i += 1
                if in_from and i < len(toks) and toks[i] == ",":
                    i += 1
                    continue
                break
            continue
        i += 1
    s.tables = sorted(set(s.tables))
    return s


def check(sql: str, allowed_schemas: set[str]) -> list[str]:
    """Problems with running `sql` where only `allowed_schemas` may be read. Empty means in scope."""
    sc = scope(sql)
    problems = list(sc.problems)
    for t in sc.tables:
        schema = t.split(".")[0]
        if schema not in allowed_schemas:
            problems.append(f"{t}: schema '{schema}' is outside {sorted(allowed_schemas)}")
    return problems
