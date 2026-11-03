"""Exact queries over a selection: filter, compute, sort, limit, aggregate.

The model sees only part of a large table, so questions like "which African
country grew most" cannot be answered from the prompt. Instead the model
writes a small query (schema constrained), this module runs it over every
row read, and the result goes back to the model before it answers.
"""

import json
import math
import re

from .context import Table, col_letter, fmt, is_num

MAX_LIMIT = 20
DEFAULT_LIMIT = 10
MAX_SHOW = 6
OPS = ("=", "!=", ">", ">=", "<", "<=", "contains")
AGGREGATES = ("none", "count", "sum", "mean", "median", "min", "max")

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "change_request": {"type": "boolean"},
        "query_needed": {"type": "boolean"},
        "filters": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "op": {"type": "string", "enum": list(OPS)},
                    "value": {"type": ["string", "number"]},
                },
                "required": ["column", "op", "value"],
            },
        },
        "value": {"type": "string"},
        "sort": {"type": "string", "enum": ["desc", "asc", "none"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        "aggregate": {"type": "string", "enum": list(AGGREGATES)},
        "show": {"type": "array", "maxItems": MAX_SHOW, "items": {"type": "string"}},
    },
    "required": ["query_needed"],
}

INSTRUCTIONS = (
    "Some questions need every row, not just the rows shown: filtering, "
    "ranking, or totals over a subset. For those, first reply with a query "
    "as JSON:\n"
    '{"query_needed": true, "filters": [{"column": "E", "op": "=", "value": '
    '"Africa"}], "value": "C - B", "sort": "desc", "limit": 5}\n'
    "- filters: column letter, op (= != > >= < <= contains), value. Text "
    "matching ignores case.\n"
    "- value: arithmetic on column letters and numbers with + - * / and "
    "brackets, e.g. \"C\", \"C - B\", \"(C - B) / B * 100\". Text like "
    "\"+4.68%\" is read as the number 4.68.\n"
    "- sort: desc or asc by value; limit: rows to return (max 20).\n"
    "- aggregate instead of rows: count, sum, mean, median, min or max.\n"
    "If the shown cells and statistics already answer the question, reply "
    '{"query_needed": false}.'
)


class QueryError(Exception):
    pass


NAME = "built-in queries"
AVAILABLE = True
RESERVE = 500
SCHEMA = QUERY_SCHEMA


def instructions(table):
    return INSTRUCTIONS


def no_query_reply():
    return json.dumps({"query_needed": False})


def to_number(v):
    """Numbers, and text that plainly holds one: "1,234", "+4.68%", "-0.4"."""
    if is_num(v):
        return float(v)
    if not isinstance(v, str):
        return None
    t = v.strip().lstrip("'").replace("−", "-").replace(",", "")
    if t.endswith("%"):
        t = t[:-1].strip()
    if t.startswith("+"):
        t = t[1:]
    try:
        f = float(t)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z]{1,3})|(.))")


def _tokens(text):
    out = []
    for num, name, sym in _TOKEN.findall(text):
        if num:
            out.append(("num", float(num)))
        elif name:
            out.append(("col", name.upper()))
        elif sym.strip():
            if sym not in "+-*/()":
                raise QueryError("Unexpected %r in value expression." % sym)
            out.append(("sym", sym))
    return out


class _Parser(object):
    """expr := term (('+'|'-') term)*; term := factor (('*'|'/') factor)*;
    factor := number | column | '-' factor | '(' expr ')'"""

    def __init__(self, text, column_index):
        self.toks = _tokens(text)
        self.pos = 0
        self.column_index = column_index
        self.columns = []

    def parse(self):
        if not self.toks:
            raise QueryError("Empty value expression.")
        fn = self.expr()
        if self.pos != len(self.toks):
            raise QueryError("Could not read the value expression.")
        return fn

    def peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def take(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def expr(self):
        left = self.term()
        while self.peek() in (("sym", "+"), ("sym", "-")):
            op = self.take()[1]
            right = self.term()
            left = _binary(op, left, right)
        return left

    def term(self):
        left = self.factor()
        while self.peek() in (("sym", "*"), ("sym", "/")):
            op = self.take()[1]
            right = self.factor()
            left = _binary(op, left, right)
        return left

    def factor(self):
        kind, val = self.take()
        if kind == "num":
            return lambda row: val
        if kind == "col":
            c = self.column_index(val)
            self.columns.append(c)
            return lambda row: to_number(row[c]) if c < len(row) else None
        if (kind, val) == ("sym", "-"):
            inner = self.factor()
            return lambda row: None if inner(row) is None else -inner(row)
        if (kind, val) == ("sym", "("):
            inner = self.expr()
            if self.take() != ("sym", ")"):
                raise QueryError("Missing ')' in value expression.")
            return inner
        raise QueryError("Could not read the value expression.")


def _binary(op, a, b):
    def run(row):
        x, y = a(row), b(row)
        if x is None or y is None:
            return None
        if op == "+":
            return x + y
        if op == "-":
            return x - y
        if op == "*":
            return x * y
        return None if y == 0 else x / y
    return run


class Query(object):
    def __init__(self, data, table):
        self.table = table
        snap = table.snapshot
        self.filters = []
        for f in data.get("filters") or []:
            if not isinstance(f, dict) or f.get("op") not in OPS:
                raise QueryError("Bad filter: %r" % (f,))
            self.filters.append((self._col(f.get("column")), f["op"], f.get("value")))

        expr = str(data.get("value") or "").strip()
        self.expr_text = expr
        parser = _Parser(expr, self._col) if expr else None
        self.value = parser.parse() if parser else None
        expr_cols = parser.columns if parser else []

        self.sort = data.get("sort") if data.get("sort") in ("asc", "desc") else "none"
        self.aggregate = data.get("aggregate") if data.get("aggregate") in AGGREGATES else "none"
        if self.aggregate not in ("none", "count") and self.value is None:
            raise QueryError("An aggregate needs a value expression.")
        try:
            self.limit = max(1, min(MAX_LIMIT, int(data.get("limit") or DEFAULT_LIMIT)))
        except (TypeError, ValueError):
            self.limit = DEFAULT_LIMIT

        show = []
        for name in data.get("show") or []:
            try:
                show.append(self._col(name))
            except QueryError:
                pass
        if not show:
            if table.label_col is not None:
                show.append(table.label_col)
            # A bare column would just repeat the value; show inputs otherwise.
            if not re.match(r"^[A-Za-z]{1,3}$", expr):
                show.extend(expr_cols)
        seen = set()
        self.show = [c for c in show if not (c in seen or seen.add(c))][:MAX_SHOW]
        self.letters = lambda c: col_letter(snap.start_col + c)

    def _col(self, name):
        name = str(name or "").strip().upper()
        if not re.match(r"^[A-Z]{1,3}$", name):
            raise QueryError("%r is not a column letter." % name)
        idx = 0
        for ch in name:
            idx = idx * 26 + (ord(ch) - 64)
        c = idx - 1 - self.table.snapshot.start_col
        if c < 0 or c >= self.table.width:
            raise QueryError("Column %s is outside the selection." % name)
        return c

    def _match(self, row):
        for c, op, want in self.filters:
            have = row[c] if c < len(row) else ""
            if op == "contains":
                if str(want).strip().lower() not in fmt(have).lower():
                    return False
                continue
            hn, wn = to_number(have), to_number(want)
            if hn is not None and wn is not None:
                a, b = hn, wn
            else:
                if op not in ("=", "!="):
                    return False
                a, b = fmt(have).strip().lower(), str(want).strip().lower()
            if not {"=": a == b, "!=": a != b, ">": a > b, ">=": a >= b,
                    "<": a < b, "<=": a <= b}[op]:
                return False
        return True

    def describe(self):
        parts = []
        if self.filters:
            parts.append("rows where " + " and ".join(
                "%s %s %s" % (self.letters(c), op, json.dumps(v)) for c, op, v in self.filters))
        else:
            parts.append("all rows")
        if self.value is not None:
            parts.append("value = %s" % self.expr_text)
        if self.aggregate != "none":
            parts.append(self.aggregate)
        elif self.sort != "none":
            parts.append("%s first, top %d" % ("highest" if self.sort == "desc" else "lowest",
                                               self.limit))
        return ", ".join(parts)

    def run(self):
        t = self.table
        candidates = [i for i in range(len(t.body)) if i not in t.totals]
        matched = [i for i in candidates if self._match(t.body[i])]
        snap = t.snapshot
        lines = ["Query: %s" % self.describe(),
                 "Matched %d of %d rows%s." % (
                     len(matched), len(candidates),
                     " (only the first %d rows of the selection were read)" % snap.n_read
                     if snap.n_read < snap.total_rows else "")]
        if t.totals:
            lines.append("Total rows were left out.")

        values = {}
        if self.value is not None:
            for i in matched:
                v = self.value(t.body[i])
                if v is not None:
                    values[i] = v
            if len(values) < len(matched):
                lines.append("%d matched rows had no numeric value." % (len(matched) - len(values)))

        if self.aggregate == "count":
            lines.append("Count: %d" % (len(values) if self.value is not None else len(matched)))
            return "\n".join(lines)
        if self.aggregate != "none":
            nums = sorted(values.values())
            if not nums:
                lines.append("No numeric values to aggregate.")
                return "\n".join(lines)
            if self.aggregate == "sum":
                result = math.fsum(nums)
            elif self.aggregate == "mean":
                result = math.fsum(nums) / len(nums)
            elif self.aggregate == "median":
                mid = len(nums) // 2
                result = nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2
            else:
                pick = min if self.aggregate == "min" else max
                i = pick(values, key=values.get)
                lines.append("%s: %s (%s)" % (self.aggregate.capitalize(),
                                              _num(values[i]), t.where(i)))
                return "\n".join(lines)
            lines.append("%s: %s (over %d values)" % (self.aggregate.capitalize(),
                                                      _num(result), len(nums)))
            return "\n".join(lines)

        order = list(values) if self.value is not None else matched
        if self.sort != "none" and self.value is not None:
            order.sort(key=values.get, reverse=self.sort == "desc")
        shown = order[:self.limit]
        for rank, i in enumerate(shown, 1):
            row = t.body[i]
            cells = ["%s %s" % (self.letters(c), _short(row[c] if c < len(row) else ""))
                     for c in self.show if c != t.label_col]
            parts = [t.where(i)] + cells
            if self.value is not None:
                parts.append("value %s" % _num(values[i]))
            lines.append("%d. %s" % (rank, " | ".join(parts)))
        if len(order) > len(shown):
            lines.append("(%d more not shown)" % (len(order) - len(shown)))
        return "\n".join(lines)


def _num(v):
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return "%.6g" % v


def _short(v):
    s = fmt(v)
    return s if len(s) <= 30 else s[:29] + "~"


def parse(raw, snapshot):
    """Returns a Query, or None if the model said no query is needed.
    Raises QueryError for a query that cannot be run."""
    if not raw or "{" not in raw:
        return None
    try:
        from .plan import extract_json
        data = extract_json(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("query_needed"):
        return None
    return Query(data, Table(snapshot))
