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
from .query import QueryError
from .values import format_number, is_iso_date, iso_to_serial, to_number

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
RESERVE = 1000  # tokens for one retry plus the result

SCHEMA = {
    "type": "object",
    "properties": {
        "change_request": {"type": "boolean"},
        "sql_needed": {"type": "boolean"},
        "values": {"type": "string", "maxLength": 300},
        "sql": {"type": "string"},
    },
    "required": ["sql_needed"],
}

# Names that would clash with SQL: they get a trailing underscore.
_KEYWORDS = {
    "abort", "action", "add", "after", "all", "alter", "and", "as", "asc", "attach",
    "before", "begin", "between", "by", "case", "cast", "check", "collate", "column",
    "commit", "conflict", "constraint", "create", "cross", "current", "date", "default",
    "delete", "desc", "distinct", "do", "drop", "each", "else", "end", "escape", "except",
    "exists", "filter", "first", "for", "foreign", "from", "full", "glob", "group", "having",
    "if", "in", "index", "inner", "insert", "intersect", "into", "is", "join", "key", "last",
    "left", "like", "limit", "match", "natural", "no", "not", "null", "of", "offset", "on",
    "or", "order", "outer", "over", "partition", "primary", "range", "references", "regexp",
    "replace", "right", "row", "rows", "select", "set", "t", "table", "then", "time", "to",
    "union", "unique", "update", "using", "values", "view", "when", "where", "window", "with"}
MAX_NAME = 30
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit",
                     "fts3_tokenizer", "zipfile", "sqlar_compress", "sqlar_uncompress"}


def column_names(table):
    """SQL names for the selection's columns, from their headers: "Unit price"
    becomes unit_price. A meaningful name is far harder for a small model to
    confuse than a letter (it averaged column g for VAT when VAT was h).
    Columns without a usable header keep their sheet letter."""
    import re
    from .context import col_letter
    names, used = [], set()
    header = table.snapshot.rows[0] if table.header and table.snapshot.rows else []
    for c in range(table.width):
        letter = col_letter(table.snapshot.start_col + c).lower()
        raw = header[c] if c < len(header) else ""
        name = re.sub(r"[^0-9a-z]+", "_", fmt(raw).strip().lower()).strip("_")[:MAX_NAME].strip("_") \
            if isinstance(raw, str) else ""
        if not name or not re.search(r"[a-z]", name):
            name = letter
        if name[0].isdigit():
            name = "c_" + name
        if name in _KEYWORDS:
            name += "_"
        base, n = name, 2
        while name in used:
            name = "%s_%d" % (base, n)
            n += 1
        used.add(name)
        names.append(name)
    return names


_KIND_NOTES = {"date": "date", "datetime": "date and time", "time": "time",
               "percent": "fraction shown as %", "logical": "TRUE/FALSE stored as 1/0"}


MAX_LISTED_VALUES = 8
MAX_LISTED_LENGTH = 24   # longer values are free text, never listed


def stored_spellings(table):
    """{(column, lowered text): text as stored in t}. Text spelled differently
    only by case or spaces is stored in its most common spelling, so
    paid = 'No' also finds 'no' and ' No '. What a value means ('Y' for yes)
    is left to the model, which can read messy data as a person would."""
    numeric = numeric_columns(table)
    out = {}
    for c in range(min(table.width, len(table.profiles))):
        if c in numeric or table.column_kind(c) == "logical":
            continue
        for _, shown, spellings in table.profiles[c].groups:
            if len(spellings) > 1:
                out[(c, shown.strip().lower())] = shown
    return out


def describe_columns(table):
    cols = ["row (sheet row number)"]
    spelling = stored_spellings(table)
    for c, name in enumerate(column_names(table)):
        full = table.names[c]
        label = full.split(" (", 1)[1][:-1] if " (" in full else ""
        note = _KIND_NOTES.get(table.column_kind(c))
        bits = [b for b in (label, note) if b]
        p = table.profiles[c] if c < len(table.profiles) else None
        if p is not None and p.kind == "text" and 1 < p.unique <= MAX_LISTED_VALUES \
                and p.unique < p.count and table.column_kind(c) != "logical" \
                and all(len(shown) <= MAX_LISTED_LENGTH for _, shown, _ in p.groups):
            # Small category columns: listing the values as stored stops the
            # model guessing spellings.
            stored = []
            for _, shown, _ in p.groups:
                value = spelling.get((c, shown.strip().lower()), shown)
                if value not in stored:
                    stored.append(value)
            listed = ", ".join("'%s'" % v for v in stored)
            bits.append("values: %s%s" % (listed, ", some empty" if p.missing else ""))
        cols.append("%s (%s)" % (name, "; ".join(bits)) if bits else name)
    return ", ".join(cols)


def sample_rows(table, n=2):
    """The first rows of t exactly as SQL stores them, so the model sees real
    values and types rather than guessing from column names."""
    try:
        cols, rows, _ = execute("SELECT * FROM t LIMIT %d" % n, table, n, 1.0)
    except QueryError:
        return []
    def show(v):
        if v is None:
            return "NULL"
        if isinstance(v, (int, float)):
            return format_cell(float(v))
        return "'%s'" % (v if len(str(v)) <= 30 else str(v)[:29] + "\u2026")
    return ["(%s)" % ", ".join("%s=%s" % (c, show(v)) for c, v in zip(cols, r)) for r in rows]


def value_rules(table):
    """How values appear in t, for both instructions."""
    rules = ["t holds only data rows: no header row and no total rows. Never compare "
             "a column with its own header name.",
             "Spellings that differ only in case or spaces are merged into the most common "
             "one ('no' is stored as 'No'), so compare with the values listed for t.",
             "Empty cells are NULL; = and != are never true for NULL, so use IS NULL, "
             "IS NOT NULL or COALESCE(x, '').",
             "Do all arithmetic in SQL (SUM, COUNT, AVG, ROUND); never add up results yourself.",
             "Percentages are fractions: '10%' is 0.1; multiply by 100 to report one.",
             "Text amounts ('£1,200.50', '(120.00)') are numbers; other text in number "
             "columns ('-', 'TBC') is NULL. Codes like '00123' stay text."]
    dated = [c for c in range(table.width) if table.column_kind(c) in ("date", "datetime")]
    if dated:
        name = column_names(table)[dated[0]]
        rules.append("Dates are text like '2026-10-02', so compare them as text "
                     "(%s < '2026-12-01') or use date() and strftime()." % name)
    if any(table.column_kind(c) == "logical" for c in range(table.width)):
        rules.append("TRUE/FALSE cells are 1 and 0, as in Calc: SUM counts the TRUE ones.")
    return rules


def table_note(table):
    """What t holds, for the user message. It quotes cell values, so it
    never goes into the system prompt, where text carries more authority."""
    return ("Table t for SQL. Columns: %s.\nFirst rows of t, as stored: %s" % (
        describe_columns(table), " ".join(sample_rows(table)) or "(none)"))


def sheet_instructions(table):
    """The sql_to_sheet tool, for change requests."""
    return (
        '- {"tool": "sql_to_sheet", "sql": "SELECT ...", "sheet": "Cleaned"}  writes '
        "the result of one SQLite SELECT to a NEW sheet and leaves this sheet "
        "unchanged. Use it to clean or reshape data: remove duplicates, drop rows "
        "with missing values, filter, fill blanks, trim text, summarise. It must be "
        "the only operation.\n"
        "  The selection is the table t, described with the selection. Missing values "
        "written as text in text columns (like 'N/A') stay text and are listed in the "
        "column statistics. Copies leave total rows out.\n  %s\n"
        "  Keep the original column order and ORDER BY row unless asked otherwise. "
        "Do not select row unless asked.\n"
        "  Remove duplicates of %(key)s, keeping the first: SELECT %(cols)s FROM (SELECT *, "
        "ROW_NUMBER() OVER (PARTITION BY %(key)s ORDER BY row) AS n FROM t) "
        "WHERE n = 1 ORDER BY row\n"
        "  Drop rows with a missing value: SELECT %(cols)s FROM t WHERE %(where)s ORDER BY row\n"
        "  Several groups (top 3 and bottom 3 by %(num)s): write each group as its own SELECT "
        "and join them: SELECT * FROM (SELECT %(key)s, %(num)s FROM t ORDER BY %(num)s DESC "
        "LIMIT 3) UNION ALL SELECT * FROM (SELECT %(key)s, %(num)s FROM t ORDER BY %(num)s "
        "LIMIT 3). Keep each condition simple; do not nest window functions."
    ).replace("%s\n", "\n  ".join(value_rules(table)).replace("%", "%%") + "\n", 1) \
        % _example_names(table)


def _example_names(table):
    """Real column names for the worked examples, so the model adapts a
    pattern about its own table rather than copying placeholder letters."""
    names = column_names(table)
    key = names[table.label_col if table.label_col is not None else 0]
    cols = ", ".join(names) if len(names) <= 8 else ", ".join(names[:3]) + ", ..."
    checked = [n for n in names[1:3]] or names[:1]
    numeric = sorted(numeric_columns(table))
    num = names[numeric[0]] if numeric else names[-1]
    return {"key": key, "cols": cols, "num": num,
            "where": " AND ".join("%s IS NOT NULL" % n for n in checked)}


def instructions(table):
    return (
        "Questions that pick, find, count, list, rank or add up rows are answered with "
        "SQL, even when the rows are shown. First reply with one SQLite SELECT as JSON:\n"
        '{"sql_needed": true, "values": "<column>: <value>, <value> = <meaning>; empty = '
        '<meaning>", "sql": "SELECT ..."}\n'
        "The selection is the table t, described with the selection.\n"
        "- values: for each text column your WHERE tests, what each listed value and an "
        "empty cell mean for this question, read as a person would. If nothing makes "
        "the meaning of empty clear, write empty = unclear and give empty cells their "
        "own count or sum in the SQL instead of deciding. The SQL must match it.\n"
        "- %s\n"
        "- Return at most %d rows and include the name column so results are "
        "readable.\n"
        "Reply {\"sql_needed\": false} only for questions about what the table is or "
        "how it is laid out, or about formulas." % ("\n- ".join(value_rules(table)), MAX_ROWS)
    )


class SqlQuery(object):
    def __init__(self, sql, table, values=""):
        self.sql = sql.strip().rstrip(";").strip()
        self.table = table
        self.values = " ".join(str(values or "").split())
        if not self.sql:
            raise QueryError("Empty SQL.")
        if len(self.sql) > MAX_SQL_LEN:
            raise QueryError("SQL is too long.")

    def describe(self):
        """The SQL, and how the model read the values it tests, so a wrong
        reading of a value is visible next to the number it produced."""
        return self.sql + ("\nRead values as: %s" % self.values if self.values else "")

    def run(self):
        cols, rows, notes = execute(self.sql, self.table, MAX_ROWS + 1, TIME_LIMIT)
        return self._format(cols, rows, notes)

    def _format(self, cols, rows, notes=()):
        t = self.table
        snap = t.snapshot
        lines = ["SQL: %s" % self.sql]
        if snap.n_read < snap.total_rows:
            lines.append("Only the first %d of %d rows of the selection were read."
                         % (snap.n_read, snap.total_rows))
        if t.totals:
            lines.append("Total rows were left out.")
        lines.extend(notes)
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


def execute(sql, table, max_rows, time_limit, rules=None):
    """Run one read-only statement. Returns (column names, rows, notes),
    where notes say which values could not be read as numbers. rules, from
    a clean, rewrite values as they are loaded (see _load)."""
    _check_empty_text(sql, table)
    conn, notes = _load(table, rules)
    notes = _relevant(notes, sql)
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
    return cols, rows, notes


class SheetResult(object):
    def __init__(self, headers, rows, source_rows, kinds=None, left_out=None,
                 totals=None, notes=None, formats=None):
        self.headers = headers
        self.rows = rows
        self.source_rows = source_rows          # data rows offered to the SQL
        self.kinds = kinds or [None] * len(headers)  # "date", "percent", ...
        self.left_out = left_out                # labels of rows not copied, if known
        self.totals = totals or []              # total rows never offered
        self.notes = notes or []
        # Per column, the (column, row) of a source cell whose number format
        # the copy reuses, so dates read '02 Sep 2026' as in the source.
        self.formats = formats or [None] * len(headers)

    def values(self):
        """Header plus rows, as Calc wants them: floats and strings only.
        Date columns become day numbers so the new sheet can format them."""
        def cell(v, kind):
            if v is None:
                return ""
            if kind in ("date", "datetime") and is_iso_date(v):
                return iso_to_serial(v)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            return str(v)
        return [tuple(self.headers)] + [
            tuple(cell(v, self.kinds[i]) for i, v in enumerate(r)) for r in self.rows]


    def readable(self, limit=5):
        """The first rows as text, the way the new sheet will show them."""
        def cell(v, kind):
            if v is None:
                return ""
            if kind == "percent" and isinstance(v, (int, float)):
                return "%s%%" % format_cell(round(v * 100, 4))
            if kind == "logical" and v in (0, 1):
                return "TRUE" if v else "FALSE"
            return format_cell(v)
        return [[cell(v, self.kinds[i]) for i, v in enumerate(r)] for r in self.rows[:limit]]


def run_to_sheet(sql, table, rules=None):
    """Run sql over every data row, for writing to a new sheet. Total rows
    are left out: copied unchanged they would be stale and would distort any
    average the SQL computes. Output columns named after sheet letters get
    the original header and display kind."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise QueryError("Empty SQL.")
    cols, rows, notes = execute(sql, table, MAX_SHEET_ROWS + 1, SHEET_TIME_LIMIT, rules)
    if not cols:
        raise QueryError("The SQL returned no columns.")
    if len(rows) > MAX_SHEET_ROWS:
        raise QueryError("The result has more than %d rows." % MAX_SHEET_ROWS)
    names = column_names(table)
    index = {n: c for c, n in enumerate(names)}
    headers, kinds, formats = [], [], []
    for i, col in enumerate(cols):
        c = index.get(col.lower())
        formats.append(_format_cell(table, c) if c is not None else None)
        if c is not None:
            full = table.names[c]
            headers.append(full.split(" (", 1)[1][:-1] if " (" in full else col.upper())
            kinds.append(table.column_kind(c))
        else:
            headers.append(col)
            filled = [r[i] for r in rows if r[i] is not None]
            kinds.append("date" if filled and all(is_iso_date(v) for v in filled) else None)
    source = len(table.body) - len(table.skipped)
    return SheetResult(headers, rows, source, kinds, _left_out(table, cols, rows, index, rules),
                       [table.where(i) for i in sorted(table.totals)], notes, formats)


def _format_cell(table, c):
    """A data cell of column c holding a real value (not text typed as a date
    or number), as absolute (column, row), or None."""
    snap = table.snapshot
    offset = 1 if table.header else 0
    typed = set(snap.typed_dates.get(c, []))
    for i, row in enumerate(table.body):
        if i in table.skipped or c >= len(row) or is_empty(row[c]) or i + offset in typed:
            continue
        v = row[c]
        if isinstance(v, str) and not is_iso_date(v) and v not in ("TRUE", "FALSE"):
            continue
        return snap.start_col + c, snap.start_row + offset + i
    return None


def _left_out(table, cols, rows, index, rules=None):
    """Labels of source rows missing from the result, when every output column
    is an unchanged source column (filters and de-duplication). None when the
    result was transformed and rows cannot be matched back."""
    if not cols or any(c.lower() not in index for c in cols):
        return None
    picked = ", ".join('"%s"' % c.lower() for c in cols)
    _, source, _ = execute("SELECT row, %s FROM t ORDER BY row" % picked, table,
                           MAX_SHEET_ROWS + 1, SHEET_TIME_LIMIT, rules)
    waiting = {}
    for r in source:
        waiting.setdefault(tuple(r[1:]), []).append(r[0])
    for r in rows:
        bucket = waiting.get(tuple(r))
        if not bucket:
            return None
        bucket.pop(0)
    missing = sorted(n for bucket in waiting.values() for n in bucket)
    return [table.where(n - table.first_row) for n in missing]


def format_cell(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return format_number(v)
    s = str(v)
    return s if len(s) <= 40 else s[:39] + "~"


def numeric_columns(table):
    """Columns where most filled cells hold numbers (or text that reads as one)."""
    found = set()
    for c in range(table.width):
        nums = text = 0
        for i, row in enumerate(table.body):
            if i in table.skipped or c >= len(row) or is_empty(row[c]):
                continue
            if to_number(row[c]) is None:
                text += 1
            else:
                nums += 1
        if nums > text:
            found.add(c)
    return found


def _load(table, rules=None):
    """Table t from the data rows. With rules ({(column, value lowered):
    new value}, from a clean; value "" is an empty cell), each rule replaces
    its value, and text in a number column that no rule covers stays text
    instead of becoming missing, so cleaning never loses a value."""
    if not AVAILABLE:
        raise QueryError("SQLite is not available in this LibreOffice.")
    conn = sqlite3.connect(":memory:")
    names = column_names(table)
    conn.execute("CREATE TABLE t (row INTEGER, %s)" % ", ".join('"%s"' % n for n in names))
    numeric = numeric_columns(table)
    logical = {c for c in range(table.width) if table.column_kind(c) == "logical"}
    spelling = stored_spellings(table)
    merged = {}
    dropped = {}
    applied = {}
    trimmed = {}
    kept = {}
    data = []
    for i, row in enumerate(table.body):
        if i in table.skipped:
            continue
        values = [table.row_number(i)]
        for c in range(table.width):
            v = row[c] if c < len(row) else ""
            if isinstance(v, str) and v != v.strip():
                # Outer spaces never mean anything, and left in they make
                # paid = 'Yes' miss ' Yes '.
                trimmed[c] = trimmed.get(c, 0) + 1
                v = v.strip()
            if rules is not None and is_empty(v) and (c, "") in rules:
                new = rules[(c, "")]
                applied.setdefault((c, "", new), 0)
                applied[(c, "", new)] += 1
                values.append(new)
                continue
            if rules is not None and isinstance(v, str) and not is_empty(v):
                key = (c, fmt(v).lower())
                if key in rules:
                    new = rules[key]
                    applied.setdefault((c, fmt(v), new), 0)
                    applied[(c, fmt(v), new)] += 1
                    values.append(new)
                    continue
            if c in logical and v in ("TRUE", "FALSE"):
                # As in Calc, TRUE is 1 and FALSE is 0, so SUM counts TRUE.
                values.append(1 if v == "TRUE" else 0)
                continue
            n = None if is_empty(v) else to_number(v)
            if n is not None:
                values.append(n)
            elif is_empty(v):
                values.append(None)
            elif c in numeric and rules is not None:
                values.append(fmt(v))
                kept.setdefault(c, []).append(fmt(v))
            elif c in numeric:
                # Text in a number column ("-", "N/A", "TBC") is a missing value.
                # Left as text, SQLite would count it as 0 in SUM and AVG.
                values.append(None)
                dropped.setdefault(c, []).append(fmt(v))
            else:
                text = fmt(v)
                common = spelling.get((c, text.strip().lower()))
                if common is not None and text != common:
                    merged.setdefault((c, text, common), 0)
                    merged[(c, text, common)] += 1
                    text = common
                values.append(text)
        data.append(values)
    conn.executemany("INSERT INTO t VALUES (%s)" % ", ".join("?" * (table.width + 1)), data)
    conn.commit()
    notes = []
    for c in sorted({c for c, _, _ in applied}):
        changes = sorted(((old, new, n) for (k, old, new), n in applied.items() if k == c),
                         key=lambda x: (str(x[0]), str(x[1])))
        notes.append((names[c], "Changed in %s: %s." % (names[c], ", ".join(
            "'%s' to %s%s" % (old, "empty" if new is None else "'%s'" % fmt(new),
                              " (%d cells)" % n if n > 1 else "")
            for old, new, n in changes[:8]))))
    for c in sorted(trimmed) if rules is not None else ():
        notes.append((names[c], "Trimmed spaces in %s (%d cell%s)." % (
            names[c], trimmed[c], "" if trimmed[c] == 1 else "s")))
    for c in sorted(kept):
        vals = sorted(set(kept[c]))
        notes.append((names[c], "Kept as text in number column %s: %s." % (
            names[c], ", ".join("'%s'" % v for v in vals[:4]))))
    for c in sorted({c for c, _, _ in merged}):
        changes = sorted((old, new, n) for (k, old, new), n in merged.items() if k == c)
        notes.append((names[c], "Merged spellings in %s: %s." % (names[c], ", ".join(
            "'%s' as '%s'%s" % (old, new, " (%d cells)" % n if n > 1 else "")
            for old, new, n in changes[:6]))))
    for c in sorted(dropped):
        vals = dropped[c]
        shown = ", ".join("'%s'" % v for v in sorted(set(vals))[:4])
        notes.append((names[c], "Column %s: %d value%s could not be read as a number and %s "
                      "treated as missing (%s)." % (names[c], len(vals), "" if len(vals) == 1 else "s",
                                                    "was" if len(vals) == 1 else "were", shown)))
    return conn, notes


def _check_empty_text(sql, table):
    """Empty cells are NULL, so x = '' and x IN (..., '') never match them and
    the answer silently leaves blanks out. Such a query is refused with the
    fix, so the model gets a chance to correct it."""
    import re
    text = re.sub(r"(coalesce|ifnull)\s*\((?:[^()]|\([^()]*\))*?,\s*''\s*\)", "x",
                  sql, flags=re.I)
    compares = re.search(r"(?<![<>!])==?\s*''|''\s*==?(?!=)", text) or any(
        "''" in m.group(2) for m in re.finditer(r"(\bnot\s+)?\bin\s*\(([^()]*)\)", text, re.I)
        if not m.group(1))
    if not compares:
        return
    words = set(re.findall(r"[a-z_][a-z0-9_]*", text.lower()))
    numeric = numeric_columns(table)
    hit = [(name, table.profiles[c].missing) for c, name in enumerate(column_names(table))
           if name in words and c not in numeric and c < len(table.profiles)
           and table.profiles[c].missing]
    if hit:
        name = hit[0][0]
        raise QueryError(
            "Empty cells are NULL, so '' never matches them (%s). Use %s IS NULL, "
            "or COALESCE(%s, '') = ''." % (", ".join(
                "%s has %d empty cell%s" % (n, k, "" if k == 1 else "s") for n, k in hit),
                name, name))


def _relevant(notes, sql):
    """Notes about the columns a query uses; a note about an unused column
    is noise the model may try to explain."""
    import re
    words = set(re.findall(r"[a-z_][a-z0-9_]*", sql.lower()))
    every = re.search(r"(select|,)\s*\*|\.\*", sql.lower())
    return [text for name, text in notes if every or name in words]


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
    return SqlQuery(str(data.get("sql") or ""), Table(snapshot), data.get("values") or "")


def no_query_reply():
    return json.dumps({"sql_needed": False})
