"""Read-only guard for SQL written by the LLM.

This is a second line of defence. The first one is a database login that only
has SELECT permissions (see README). Never rely on this module alone.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

# Map SQLAlchemy dialect names to sqlglot dialect names.
SQLGLOT_DIALECTS = {
    "mssql": "tsql",
    "sqlite": "sqlite",
    "postgresql": "postgres",
    "mysql": "mysql",
}

_FORBIDDEN_NODE_NAMES = [
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter",
    "AlterTable", "Command", "Into", "TruncateTable", "Grant", "Revoke",
    "Set", "Use", "Transaction", "Commit", "Rollback", "Execute", "Copy",
    "LoadData", "Pragma",
]
FORBIDDEN_NODES = tuple(
    getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n)
)

_QUERY_ROOTS = tuple(
    getattr(exp, n) for n in ("Query", "Select", "Union", "Subquery") if hasattr(exp, n)
)

# Dangerous T-SQL features that may hide inside an otherwise valid SELECT.
_FORBIDDEN_PATTERNS = re.compile(
    r"\b(openrowset|opendatasource|openquery|openxml|sp_executesql|xp_\w+|"
    r"waitfor|dbcc|shutdown|reconfigure)\b",
    re.IGNORECASE,
)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class UnsafeQuery(ValueError):
    """Raised when a query is not a single read-only SELECT."""


def validate(sql: str, dialect: str = "tsql") -> None:
    """Raise UnsafeQuery unless `sql` is exactly one read-only query."""
    if not sql or not sql.strip():
        raise UnsafeQuery("Empty query.")

    without_literals = _STRING_LITERAL.sub("''", sql)
    match = _FORBIDDEN_PATTERNS.search(without_literals)
    if match:
        raise UnsafeQuery(f"Forbidden construct: {match.group(0)}")

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as e:  # sqlglot raises several error types
        raise UnsafeQuery(
            f"Could not parse the query ({type(e).__name__}). Simplify it."
        ) from e

    if len(statements) != 1:
        raise UnsafeQuery("Exactly one statement is allowed.")

    root = statements[0]
    if not isinstance(root, _QUERY_ROOTS):
        raise UnsafeQuery(f"Only SELECT queries are allowed (got {root.key.upper()}).")

    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeQuery(f"Forbidden statement inside query: {node.key.upper()}")
