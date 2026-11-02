"""Picks how exact queries are run: SQL when SQLite is available, otherwise
the built-in query form."""

from . import query, sql


def current():
    return sql if sql.AVAILABLE else query


def describe():
    if sql.AVAILABLE:
        return "SQL (SQLite %s)" % sql.VERSION
    return "built-in queries (SQLite is not available in this LibreOffice)"
