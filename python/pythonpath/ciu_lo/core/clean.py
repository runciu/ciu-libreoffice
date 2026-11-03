"""Clean data into a new sheet.

The model interprets, one column at a time: what the column holds (yes/no,
a category, names, identifiers), its standard values, what each other
value means ('yse' and 'Y' are Yes, 'TBC' is unknown) and what an empty
cell means. Code checks every answer, applies the rules to every row, and
sorts the changes for the preview: spelling fixes, changes of meaning to
check, and values left unresolved. Fixes that need no judgement (spacing,
case, numbers and dates stored as text) are made by code alone.
"""

import re

from . import sql as S
from .context import estimate_tokens, fmt, is_empty
from .values import DATE_KINDS, to_number

_CODE = re.compile(r"^(?=.*\d)[A-Za-z0-9._/#-]+$")

SHEET = "Cleaned"
MAX_VALUES = 30         # a column with more distinct values is free text or IDs
MAX_VALUE_LENGTH = 40
MAX_NEW_LENGTH = 60
MAX_EXAMPLES = 4        # rows shown for rare values
REPLY_TOKENS = 400

KINDS = ("yes/no", "category", "name", "identifier", "free text", "amount", "other")
_STANDARDISED = ("yes/no", "category")

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "standard": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        "rules": {
            "type": "array",
            "maxItems": MAX_VALUES,
            "items": {
                "type": "object",
                "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                "required": ["from", "to"],
            },
        },
        "blank_means": {"type": "string"},
    },
    "required": ["kind", "rules"],
}

TEXT_SYSTEM = (
    "You help clean one column of a spreadsheet. You get its header, each distinct "
    "value with how often it occurs, and rows where rare values occur. Data is often "
    "messy: read every value as a person would. Reply as JSON:\n"
    "- kind: yes/no; category (a few repeated labels); name (people, companies, "
    "places); identifier (codes, IDs); free text; or other.\n"
    "- standard: for yes/no and category, the correct value for each real meaning, "
    "spelled as most cells already spell it.\n"
    "- rules: every other value mapped to the value it means: typos (yse to Yes), "
    "abbreviations (Y to Yes), synonyms (settled to Paid). A marker of a missing or "
    "unknown value ('N/A', '-', 'TBC', '?') maps to \"\". For names, map only clear "
    "misspellings to another value in the list. Leave out values you cannot place.\n"
    "- blank_means: \"\" when an empty cell is just missing, or the standard value an "
    "empty cell stands for when the column makes that clear.\n"
    "Example: {\"kind\": \"category\", \"standard\": [\"Done\", \"Open\"], \"rules\": "
    "[{\"from\": \"done.\", \"to\": \"Done\"}, {\"from\": \"opne\", \"to\": \"Open\"}, "
    "{\"from\": \"?\", \"to\": \"\"}], \"blank_means\": \"\"}"
)

NUMBER_SYSTEM = (
    "You help clean one number column of a spreadsheet. Some of its cells hold text "
    "instead of a number. For each text value, say the number it means, or \"\" when "
    "the value is unknown or missing ('TBC', 'N/A', '-'). Give a number only when the "
    "meaning is certain ('nil' or 'none' is 0, 'twelve' is 12). Reply as JSON with "
    "kind \"amount\", for example: {\"kind\": \"amount\", \"rules\": [{\"from\": \"nil\", "
    "\"to\": \"0\"}, {\"from\": \"TBC\", \"to\": \"\"}]}"
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
        self.examples = []        # (value, row text) for rare values

    def count(self, value):
        key = value.lower()
        return sum(n for v, n in self.values if v.lower() == key)


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
    for cand in out:
        cand.examples = _examples(table, cand)
    return out


def _examples(table, cand):
    """A row for each of the rarest values, so a value such as 'TBC' is read
    beside the rest of its row ('awaiting quote' in Notes)."""
    rare = [v for v, n in sorted(cand.values, key=lambda x: x[1]) if n <= 2][:MAX_EXAMPLES]
    out = []
    for value in rare:
        for i, row in enumerate(table.body):
            if i in table.skipped or cand.col >= len(row):
                continue
            if isinstance(row[cand.col], str) and row[cand.col].strip().lower() == value.lower():
                # Words say most about a value (a note such as 'awaiting
                # quote'), so text cells come before numbers, dates and codes.
                others = [c for c, v in enumerate(row) if c != cand.col and not is_empty(v)]
                others.sort(key=lambda c: (not _wordy(row[c]), c))
                cells = []
                for c in sorted(others[:6]):
                    text = fmt(row[c])
                    cells.append("%s %s" % (_label(table, c) or table.names[c],
                                            text if len(text) <= 25 else text[:24] + "\u2026"))
                out.append((value, "row %d: %s" % (table.row_number(i), " | ".join(cells))))
                break
    return out


def _wordy(v):
    return isinstance(v, str) and to_number(v) is None and not _CODE.match(v.strip()) \
        and any(ch.isalpha() for ch in v)


def messages(cand, guidance=""):
    """One small request per column. Cell text goes in the user message only."""
    lines = ["Column: %s%s" % (cand.label or cand.name,
                               " (a number column)" if cand.numeric else "")]
    listed = ", ".join("'%s' x%d" % (v, n) for v, n in cand.values)
    if cand.empty:
        listed += ", empty x%d" % cand.empty
    lines.append(("Text values: " if cand.numeric else "Values: ") + listed)
    if cand.examples:
        lines.append("Rows with rare values:")
        lines += ["- '%s' in %s" % ex for ex in cand.examples]
    if guidance:
        lines.append("Instructions from the user: " + guidance)
    system = NUMBER_SYSTEM if cand.numeric else TEXT_SYSTEM
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)}]


def fits(cand, n_ctx, guidance=""):
    n_ctx = n_ctx or 4096
    used = sum(estimate_tokens(m["content"]) for m in messages(cand, guidance))
    return used + REPLY_TOKENS + 50 < n_ctx


class Decision(object):
    """What the model said about one column, after checking."""

    def __init__(self, cand, kind="other", standard=(), rules=None, blank=None,
                 rejected=()):
        self.cand = cand
        self.kind = kind
        self.standard = list(standard)
        self.rules = rules or {}          # value lowered -> new value (None is empty)
        self.blank = blank                # value an empty cell becomes, or None
        self.rejected = list(rejected)    # (value, why)

    def unresolved(self):
        """Values still outside the standard ones after the rules."""
        if self.kind not in _STANDARDISED or not self.standard:
            return []
        known = {s.lower() for s in self.standard}
        return [(v, n) for v, n in self.cand.values
                if v.lower() not in known and v.lower() not in self.rules]


def parse(raw, cand):
    from .plan import extract_json
    try:
        data = extract_json(raw or "")
    except Exception:
        data = None
    if not isinstance(data, dict):
        return Decision(cand, rejected=[("", "the reply could not be read")])
    kind = data.get("kind") if data.get("kind") in KINDS else "other"
    if cand.numeric:
        kind = "amount"
    listed = {v.lower(): v for v, _ in cand.values}
    standard = []
    if kind in _STANDARDISED:
        for s in data.get("standard") or []:
            s = str(s).strip()
            if s and len(s) <= MAX_NEW_LENGTH and s.lower() not in {x.lower() for x in standard}:
                standard.append(s)
    allowed = {s.lower(): s for s in standard} or dict(listed)

    def target(new):
        """The checked replacement, or a reason it cannot be used."""
        if new == "":
            return None, None
        if cand.numeric:
            n = to_number(new)
            return (n, None) if n is not None else (None, "'%s' is not a number" % new)
        if new.lower() in allowed:
            return allowed[new.lower()], None
        return None, "'%s' is not one of the column's values" % new

    rules, rejected = {}, []
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        old = str(rule.get("from") or "").strip()
        new = str(rule.get("to") if rule.get("to") is not None else "").strip()
        if old.lower() not in listed:
            rejected.append((old, "not a value in the column"))
            continue
        value, why = target(new)
        if why:
            rejected.append((old, why))
            continue
        if value is not None and fmt(value) == listed[old.lower()]:
            continue
        rules[old.lower()] = value
    blank = None
    said = str(data.get("blank_means") or "").strip()
    if said and cand.empty and not cand.numeric:
        value, why = target(said)
        if why:
            rejected.append(("empty", why))
        else:
            blank = value
    return Decision(cand, kind, standard, rules, blank, rejected)


def _distance(a, b):
    """Edits between two strings, a swap of neighbours counting as one."""
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def is_spelling(old, new):
    """True when old reads as a typo or abbreviation of new: 'yse' and 'Y'
    for 'Yes', 'Lodnon' for 'London', 'compl.' for 'Complete'. Anything else
    is a change of meaning, which the preview asks the user to check."""
    if not isinstance(new, str):
        return False
    o = re.sub(r"[^0-9a-z]", "", old.lower())
    n = re.sub(r"[^0-9a-z]", "", new.lower())
    if not o or not n:
        return False
    if n.startswith(o) and (len(o) <= 3 or old.rstrip().endswith(".")):
        return True
    return _distance(o, n) <= (1 if len(n) <= 4 else 2)


class Cleaning(object):
    """A proposed clean: the copy, and what was changed to make it."""

    def __init__(self, table, decisions=(), skipped=()):
        self.table = table
        self.decisions = list(decisions)
        rules = {}
        for d in self.decisions:
            for old, new in d.rules.items():
                rules[(d.cand.col, old)] = new
            if d.blank is not None:
                rules[(d.cand.col, "")] = d.blank
        self.rules = rules
        names = S.column_names(table)
        self.sql = "SELECT %s FROM t ORDER BY row" % ", ".join('"%s"' % n for n in names)
        self.result = S.run_to_sheet(self.sql, table, rules)
        self.fixes = [n for n in self.result.notes if not n.startswith("Changed in ")]
        self.fixes += _typed_fixes(table, names)
        self.skipped = [k.name for k in skipped]
        self.spelling, self.check, self.left = [], [], []
        for d in self.decisions:
            name = d.cand.name
            shown = {v.lower(): v for v, _ in d.cand.values}
            spell = [(old, new) for old, new in d.rules.items() if is_spelling(shown[old], new)]
            meaning = [(old, new) for old, new in d.rules.items() if (old, new) not in spell]
            if spell:
                self.spelling.append("%s: %s" % (name, _changes(d, spell)))
            if meaning:
                self.check.append("%s: %s" % (name, _changes(d, meaning)))
            if d.blank is not None:
                self.check.append("%s: empty cells read as '%s' (%d cell%s)" % (
                    name, fmt(d.blank), d.cand.empty, "" if d.cand.empty == 1 else "s"))
            left = d.unresolved()
            if left:
                self.left.append("%s: %s (not one of %s)" % (
                    name, ", ".join("'%s' x%d" % x for x in left[:6]),
                    ", ".join("'%s'" % s for s in d.standard)))

    @property
    def changes(self):
        return bool(self.rules or self.fixes)

    def describe(self, sheet=SHEET):
        r = self.result
        lines = ["Proposed clean (not applied yet): a cleaned copy in a new sheet '%s', "
                 "%d rows x %d columns." % (sheet, len(r.rows), len(r.headers))]
        read = ["%s: %s%s" % (d.cand.name, d.kind,
                              " (%s)" % ", ".join(d.standard) if d.standard else "")
                for d in self.decisions]
        if read:
            lines += ["", "How CIU read the columns: " + "; ".join(read) + "."]
        if self.check:
            lines += ["", "Check these (CIU's reading of what values mean):"]
            lines += ["  " + x for x in self.check]
        if self.spelling:
            lines += ["", "Spelling and abbreviations:"] + ["  " + x for x in self.spelling]
        if self.left:
            lines += ["", "Unresolved, left as they are:"] + ["  " + x for x in self.left]
        if self.fixes:
            lines += ["", "Standard fixes:"] + ["  " + f for f in self.fixes]
        rejected = [(d.cand.name, v, why) for d in self.decisions for v, why in d.rejected]
        if rejected:
            lines += ["", "Rules not used:"] + ["  %s '%s': %s" % x for x in rejected[:6]]
        if self.skipped:
            lines += ["", "Not checked: %s" % ", ".join(self.skipped)]
        if r.totals:
            lines += ["", "Total rows are not copied: %s." % ", ".join(r.totals[:4])]
        return "\n".join(lines)


def _changes(decision, pairs):
    out = []
    for old, new in sorted(pairs, key=lambda p: -decision.cand.count(p[0])):
        n = decision.cand.count(old)
        shown = next(v for v, _ in decision.cand.values if v.lower() == old)
        out.append("'%s' to %s%s" % (shown, "empty" if new is None else "'%s'" % fmt(new),
                                     " (%d cells)" % n if n > 1 else ""))
    return ", ".join(out)


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
