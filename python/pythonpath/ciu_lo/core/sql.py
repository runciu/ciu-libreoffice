"""Read-only SQL over the selection, in a locked-down in-memory SQLite.

The selection is loaded into table ``t``. The model writes one SELECT; it
runs with an authorizer that permits only reading, a time limit, and a cap
on returned rows. SQLite has no network access, and with ATTACH denied and
extension loading left disabled it cannot open files either.
"""

import json
import math
import time

from .context import Table, fmt, is_empty
from .query import QueryError, to_number

try:
    import sqlite3
    AVAILABLE = True
    VERSION = sqlite3.sqlite_version
except ImportError:
    sqlite3 = None
    AVAILABLE = False
    VERSION = None

NAME = "SQL"
MAX_ROWS = 20
TIME_LIMIT = 2.0
SHEET_TIME_LIMIT = 10.0
MAX_SHEET_ROWS = 100000
MAX_SQL_LEN = 2000
RESERVE = 900  # tokens for one retry plus the result

SCHEMA = {
    "type": "object",
    "properties": {
        "sql_needed": {"type": "boolean"},
        "sql": {"type": "string"},
    },
    "required": ["sql_needed"],
}

_KEYWORDS = {"as", "by", "do", "if", "in", "is", "no", "of", "on", "or", "to"}
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit",
                     "fts3_tokenizer", "zipfile", "sqlar_compress", "sqlar_uncompress"}


def column_names(table):
    """SQL names for the selection's columns: the sheet letters, lower case."""
    from .context import col_letter
    names = []
    for c in range(table.width):
        name = col_letter(table.snapshot.start_col + c).lower()
        names.append(name + "_" if name in _KEYWORDS else name)
    return names


def describe_columns(table):
    cols = ["row (sheet row number)"]
    for c, name in enumerate(column_names(table)):
        full = table.names[c]
        label = full.split(" (", 1)[1][:-1] if " (" in full else ""
        cols.append("%s (%s)" % (name, label) if label else name)
    return ", ".join(cols)


def sheet_instructions(table):
    """The sql_to_sheet tool, for change requests."""
    totals = ", ".join(table.where(i) for i in sorted(table.totals))
    return (
        '- {"tool": "sql_to_sheet", "sql": "SELECT ...", "sheet": "Cleaned"}  writes '
        "the result of one SQLite SELECT to a NEW sheet and leaves this sheet "
        "unchanged. Use it to clean or reshape data: remove duplicates, drop rows "
        "with missing values, filter, fill blanks, trim text, summarise. It must be "
        "the only operation.\n"
        "  The selection is the table t with columns: %s. Empty cells are NULL; "
        "missing values written as text (like 'N/A' or '-') are listed in the "
        "column statistics. Numbers stored as text, like \"+4.68%%\", are numbers; in "
        "number columns, other text is NULL.%s\n"
        "  Keep the original column order and ORDER BY row unless asked otherwise. "
        "Do not select row unless asked.\n"
        "  Remove duplicates of a, keeping the first: SELECT a, b, c FROM (SELECT *, "
        "ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(a)) ORDER BY row) AS n FROM t) "
        "WHERE n = 1 ORDER BY row\n"
        "  Drop rows with a missing value: SELECT a, b, c FROM t WHERE b IS NOT NULL "
        "AND c IS NOT NULL ORDER BY row"
        % (describe_columns(table),
           " These total rows are included in t: %s." % totals if totals else "")
    )


def instructions(table):
    cols = describe_columns(table)
    return (
        "Some questions need every row, not just the rows shown: filtering, "
        "ranking, grouping, or totals over a subset. For those, first reply "
        "with one SQLite SELECT as JSON:\n"
        '{"sql_needed": true, "sql": "SELECT ..."}\n'
        "The selection is the table t with columns: %s.\n"
        "- Numbers stored as text, like \"+4.68%%\", are already numbers (4.68). "
        "In number columns, other text (like '-' or 'N/A') is NULL.\n"
        "- Total rows are not in t.\n"
        "- Text comparisons: use LOWER(x) = 'value' to ignore case.\n"
        "- Return at most %d rows and include the name column so results are "
        "readable.\n"
        "If the shown cells and statistics already answer the question, reply "
        '{"sql_needed": false}.' % (cols, MAX_ROWS)
    )


class SqlQuery(object):
    def __init__(self, sql, table):
        self.sql = sql.strip().rstrip(";").strip()
        self.table = table
        if not self.sql:
            raise QueryError("Empty SQL.")
        if len(self.sql) > MAX_SQL_LEN:
            raise QueryError("SQL is too long.")

    def describe(self):
        return self.sql

    def run(self):
        cols, rows = execute(self.sql, self.table, MAX_ROWS + 1, TIME_LIMIT)
        return self._format(cols, rows)

    def _format(self, cols, rows):
        t = self.table
        snap = t.snapshot
        lines = ["SQL: %s" % self.sql]
        if snap.n_read < snap.total_rows:
            lines.append("Only the first %d of %d rows of the selection were read."
                         % (snap.n_read, snap.total_rows))
        if t.totals:
            lines.append("Total rows were left out.")
        if not cols:
            lines.append("No result.")
            return "\n".join(lines)
        shown = rows[:MAX_ROWS]
        lines.append("%d row%s:" % (len(shown), "" if len(shown) == 1 else "s"))
        lines.append(" | ".join(cols))
        for r in shown:
            lines.append(" | ".join(format_cell(v) for v in r))
        if len(rows) > MAX_ROWS:
            lines.append("(more rows not shown)")
        return "\n".join(lines)


def execute(sql, table, max_rows, time_limit, include_totals=False):
    """Run one read-only statement. Returns (column names, rows)."""
    conn = _load(table, include_totals)
    try:
        deadline = time.monotonic() + time_limit
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
        conn.set_authorizer(_authorize)
        try:
            cur = conn.execute(sql)
            rows = cur.fetchmany(max_rows)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise QueryError("The query took longer than %g seconds." % time_limit)
            raise QueryError("SQLite error: %s" % e)
        except (sqlite3.DatabaseError, sqlite3.Warning) as e:
            raise QueryError("SQLite error: %s" % e)
        cols = [d[0] for d in cur.description or []]
    finally:
        conn.close()
    return cols, rows


class SheetResult(object):
    def __init__(self, headers, rows, source_rows):
        self.headers = headers
        self.rows = rows
        self.source_rows = source_rows

    def values(self):
        """Header plus rows, as Calc wants them: floats and strings only."""
        def cell(v):
            if v is None:
                return ""
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            return str(v)
        return [tuple(self.headers)] + [tuple(cell(v) for v in r) for r in self.rows]


def run_to_sheet(sql, table):
    """Run sql over every row, total rows included, for writing to a new
    sheet. Output columns named after sheet letters get the original header."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise QueryError("Empty SQL.")
    cols, rows = execute(sql, table, MAX_SHEET_ROWS + 1, SHEET_TIME_LIMIT, include_totals=True)
    if not cols:
        raise QueryError("The SQL returned no columns.")
    if len(rows) > MAX_SHEET_ROWS:
        raise QueryError("The result has more than %d rows." % MAX_SHEET_ROWS)
    titles = {}
    for c, name in enumerate(column_names(table)):
        full = table.names[c]
        titles[name] = full.split(" (", 1)[1][:-1] if " (" in full else name.upper()
    headers = [titles.get(c.lower(), c) for c in cols]
    return SheetResult(headers, rows, len(table.body))


def format_cell(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        if math.isfinite(v) and v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return "%.6g" % v
    s = str(v)
    return s if len(s) <= 40 else s[:39] + "~"


def numeric_columns(table):
    """Columns where most filled cells hold numbers (or text that reads as one)."""
    found = set()
    for c in range(table.width):
        nums = text = 0
        for i, row in enumerate(table.body):
            if i in table.totals or c >= len(row) or is_empty(row[c]):
                continue
            if to_number(row[c]) is None:
                text += 1
            else:
                nums += 1
        if nums > text:
            found.add(c)
    return found


def _load(table, include_totals=False):
    if not AVAILABLE:
        raise QueryError("SQLite is not available in this LibreOffice.")
    conn = sqlite3.connect(":memory:")
    names = column_names(table)
    conn.execute("CREATE TABLE t (row INTEGER, %s)" % ", ".join('"%s"' % n for n in names))
    numeric = numeric_columns(table)
    data = []
    for i, row in enumerate(table.body):
        if i in table.totals and not include_totals:
            continue
        values = [table.row_number(i)]
        for c in range(table.width):
            v = row[c] if c < len(row) else ""
            n = None if is_empty(v) else to_number(v)
            if n is not None:
                values.append(n)
            elif is_empty(v) or c in numeric:
                # Text in a number column ("-", "N/A", "TBC") is a missing value.
                # Left as text, SQLite would count it as 0 in SUM and AVG.
                values.append(None)
            else:
                values.append(fmt(v))
        data.append(values)
    conn.executemany("INSERT INTO t VALUES (%s)" % ", ".join("?" * (table.width + 1)), data)
    conn.commit()
    return conn


_ALLOWED = None


def _authorize(action, arg1, arg2, db, source):
    """Permit reading and functions only. Everything else, including ATTACH,
    PRAGMA and any write, is denied."""
    global _ALLOWED
    if _ALLOWED is None:
        _ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
    if action not in _ALLOWED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and db not in (None, "main"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def parse(raw, snapshot):
    """Returns a SqlQuery, or None if the model said no query is needed."""
    if not raw or "{" not in raw:
        return None
    try:
        from .plan import extract_json
        data = extract_json(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("sql_needed"):
        return None
    return SqlQuery(str(data.get("sql") or ""), Table(snapshot))


def no_query_reply():
    return json.dumps({"sql_needed": False})
