"""Clean data into a new sheet.

The model reads what values mean, from each column's distinct values only:
'Y' is Yes, 'settled' is Paid, 'TBC' is unknown. It replies with rules, and
code applies them to every row, so the work is the same for 20 rows or
20,000 and no value is retyped by the model. Fixes that need no judgement
(spacing, case, numbers and dates stored as text) are made by code alone.
"""

import json
import re

from . import sql as S
from .context import estimate_tokens, fmt, is_empty
from .values import DATE_KINDS, to_number

_CODE = re.compile(r"^(?=.*\d)[A-Za-z0-9._/#-]+$")

SHEET = "Cleaned"
MAX_VALUES = 30         # a column with more distinct values is free text or IDs
MAX_VALUE_LENGTH = 40
MAX_NEW_LENGTH = 60
REPLY_TOKENS = 600

SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "rules": {
                        "type": "array",
                        "maxItems": MAX_VALUES,
                        "items": {
                            "type": "object",
                            "properties": {"from": {"type": "string"},
                                           "to": {"type": "string"}},
                            "required": ["from", "to"],
                        },
                    },
                },
                "required": ["column", "rules"],
            },
        },
    },
    "required": ["columns"],
}

SYSTEM = (
    "You standardise messy spreadsheet values so they can be counted and filtered. "
    "For each column you get its distinct values with how often each occurs. Reply "
    "with rules that rewrite values to a standard form:\n"
    "- Values that mean the same thing become one value, the clearest spelling already "
    "in the column: abbreviations (Y to Yes), synonyms (settled to Paid), typos "
    "(Lodnon to London).\n"
    "- Markers of a missing or unknown value ('N/A', '-', 'TBC', 'unknown') become "
    "empty: write \"\" as to.\n"
    "- In a number column, a word becomes a number only when its meaning is certain "
    "('nil' or 'none' is 0, 'twelve' is 12).\n"
    "- Leave out values that are already standard and any value you are unsure about. "
    "Never change a value into something with a different meaning.\n"
    "- Use the column names and values exactly as listed. Leave out columns with "
    "nothing to change.\n"
    "Reply as JSON, for example:\n"
    '{"columns": [{"column": "status", "rules": [{"from": "done.", "to": "Done"}, '
    '{"from": "complete", "to": "Done"}, {"from": "?", "to": ""}]}]}'
)


class Candidate(object):
    """A column whose values the model is asked about."""

    def __init__(self, col, name, label, numeric, values, empty=0):
        self.col = col
        self.name = name
        self.label = label
        self.numeric = numeric
        self.values = values      # [(text, count)], most common first
        self.empty = empty

    def line(self):
        listed = ", ".join("'%s' x%d" % (v, n) for v, n in self.values)
        if self.empty:
            listed += ", empty x%d" % self.empty
        what = "number column; these cells are not numbers" if self.numeric else "text"
        label = "%s, " % self.label if self.label else ""
        return "- %s (%s%s): %s" % (self.name, label, what, listed)


def _label(table, c):
    full = table.names[c]
    return full.split(" (", 1)[1][:-1] if " (" in full else ""


def candidates(table):
    names = S.column_names(table)
    numeric = S.numeric_columns(table)
    out = []
    for c in range(min(table.width, len(table.profiles))):
        if table.column_kind(c) in DATE_KINDS + ("logical",):
            continue
        if c in numeric:
            counts = {}
            for i, row in enumerate(table.body):
                if i in table.skipped or c >= len(row):
                    continue
                v = row[c]
                if isinstance(v, str) and not is_empty(v) and to_number(v) is None:
                    counts[v.strip()] = counts.get(v.strip(), 0) + 1
            if counts:
                values = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:MAX_VALUES]
                out.append(Candidate(c, names[c], _label(table, c), True, values))
            continue
        p = table.profiles[c]
        filled = p.count - p.missing
        if not p.kind.startswith(("text", "mixed")) or p.unique > MAX_VALUES or \
                p.unique >= filled or \
                any(len(fmt(shown)) > MAX_VALUE_LENGTH for _, shown, _ in p.groups):
            continue  # free text, identifiers, or nothing repeated
        values = [(fmt(shown).strip(), n) for n, shown, _ in p.groups if isinstance(shown, str)]
        if values and all(_CODE.match(v) for v, _ in values):
            continue  # codes such as 00123 or INV-7: nothing to interpret
        if values:
            out.append(Candidate(c, names[c], _label(table, c), False, values, p.missing))
    return out


def build_messages(cands, guidance, n_ctx, scale=1.0):
    """System rules, and the values in the user message: cell text never goes
    in the system prompt. Columns that do not fit the window are left out
    and returned, so the preview can say they were not checked."""
    room = (n_ctx - REPLY_TOKENS - estimate_tokens(SYSTEM) - 120) * scale
    kept, skipped, used = [], [], 0
    for cand in sorted(cands, key=lambda k: len(k.values)):
        size = estimate_tokens(cand.line())
        if used + size <= room:
            kept.append(cand)
            used += size
        else:
            skipped.append(cand)
    kept.sort(key=lambda k: k.col)
    user = "Columns:\n" + "\n".join(k.line() for k in kept)
    if guidance:
        user += "\n\nInstructions from the user: " + guidance
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}], kept, skipped


def parse_rules(raw, cands):
    """({(column, value lowered): new value}, [(column name, text, why)] of
    rules that were not used). A new value is None for empty, a float in a
    number column, else text."""
    from .plan import extract_json
    try:
        data = extract_json(raw or "")
    except Exception:
        data = None
    if not isinstance(data, dict):
        return {}, []
    by_name = {}
    for k in cands:
        by_name[k.name.lower()] = k
        if k.label:
            by_name.setdefault(k.label.lower(), k)
    rules, rejected = {}, []
    for col in data.get("columns") or []:
        if not isinstance(col, dict):
            continue
        cand = by_name.get(str(col.get("column") or "").strip().lower())
        if cand is None:
            continue
        listed = {v.lower(): v for v, _ in cand.values}
        for rule in col.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            old = str(rule.get("from") or "").strip()
            new = str(rule.get("to") if rule.get("to") is not None else "").strip()
            if old.lower() not in listed:
                rejected.append((cand.name, old, "not a value in the column"))
                continue
            if new == "":
                value = None
            elif cand.numeric:
                value = to_number(new)
                if value is None:
                    rejected.append((cand.name, old, "'%s' is not a number" % new))
                    continue
            elif len(new) > MAX_NEW_LENGTH:
                rejected.append((cand.name, old, "replacement too long"))
                continue
            else:
                value = new
            if value == listed[old.lower()]:
                continue
            rules[(cand.col, old.lower())] = value
    return rules, rejected


class Cleaning(object):
    """A proposed clean: the copy, and what was changed to make it."""

    def __init__(self, table, rules, rejected=(), skipped=()):
        self.table = table
        self.rules = rules
        names = S.column_names(table)
        self.sql = "SELECT %s FROM t ORDER BY row" % ", ".join('"%s"' % n for n in names)
        self.result = S.run_to_sheet(self.sql, table, rules)
        self.meaning = [n for n in self.result.notes if n.startswith("Changed in ")]
        self.fixes = [n for n in self.result.notes if not n.startswith("Changed in ")]
        self.fixes += _typed_fixes(table, names)
        self.rejected = list(rejected)
        self.skipped = [k.name for k in skipped]

    @property
    def changes(self):
        return bool(self.meaning or self.fixes)

    def describe(self, sheet=SHEET):
        r = self.result
        lines = ["Proposed clean (not applied yet): a cleaned copy in a new sheet '%s', "
                 "%d rows x %d columns." % (sheet, len(r.rows), len(r.headers))]
        if self.meaning:
            lines += ["", "Values read by CIU (check these):"] + ["  " + m for m in self.meaning]
        if self.fixes:
            lines += ["", "Standard fixes:"] + ["  " + f for f in self.fixes]
        if self.rejected:
            lines += ["", "Rules not used:"] + ["  %s '%s': %s" % x for x in self.rejected[:6]]
        if self.skipped:
            lines += ["", "Not checked (did not fit the context): %s" % ", ".join(self.skipped)]
        if r.totals:
            lines += ["", "Total rows are not copied: %s." % ", ".join(r.totals[:4])]
        return "\n".join(lines)


def _typed_fixes(table, names):
    """Values stored as text that the copy writes as real numbers and dates."""
    out = []
    numeric = S.numeric_columns(table)
    for c in sorted(numeric):
        n = sum(1 for i, row in enumerate(table.body)
                if i not in table.skipped and c < len(row) and isinstance(row[c], str)
                and not is_empty(row[c]) and to_number(row[c]) is not None)
        if n:
            out.append("%s: %s stored as text become%s." % (
                names[c], "1 number" if n == 1 else "%d numbers" % n,
                "s a number" if n == 1 else " numbers"))
    for c, rows in sorted(table.snapshot.typed_dates.items()):
        if c < len(names) and rows:
            n = len(rows)
            out.append("%s: %s typed as text become%s." % (
                names[c], "1 date" if n == 1 else "%d dates" % n,
                "s a date" if n == 1 else " dates"))
    return out


def no_reply():
    return json.dumps({"columns": []})
