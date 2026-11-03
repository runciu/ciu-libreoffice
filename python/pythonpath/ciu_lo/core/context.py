"""Turns a selection into bounded prompt text.

Statistics are computed here so the model does not do arithmetic; raw rows
fill whatever budget remains.
"""

import math
import re


class Snapshot(object):
    """Cells read from the sheet. rows may cover fewer rows than total_rows."""

    def __init__(self, doc_title, sheet, start_col, start_row, rows,
                 formulas=None, total_rows=None, total_cols=None,
                 scope_note=None, hints=None):
        self.doc_title = doc_title
        self.sheet = sheet
        self.start_col = start_col
        self.start_row = start_row
        self.rows = rows
        self.formulas = formulas or []
        self.total_rows = total_rows if total_rows is not None else len(rows)
        self.total_cols = total_cols if total_cols is not None else (len(rows[0]) if rows else 0)
        self.scope_note = scope_note
        self.hints = hints or []

    @property
    def n_read(self):
        return len(self.rows)

    def range_ref(self):
        if not self.total_rows or not self.total_cols:
            return cell_ref(self.start_col, self.start_row)
        a = cell_ref(self.start_col, self.start_row)
        b = cell_ref(self.start_col + self.total_cols - 1,
                     self.start_row + self.total_rows - 1)
        return a if a == b else "%s:%s" % (a, b)


def col_letter(idx):
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def cell_ref(col, row):
    return "%s%d" % (col_letter(col), row + 1)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (
        isinstance(v, float) and math.isnan(v))


def is_empty(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def detect_header(rows):
    """First row is a header if it is all text and some column below it is
    mostly numeric, or (all-text tables) its values do not repeat below.
    """
    if len(rows) < 2:
        return False
    first = rows[0]
    non_empty = [v for v in first if not is_empty(v)]
    if not non_empty or any(is_num(v) for v in non_empty):
        return False
    body = rows[1:]
    for c in range(len(first)):
        col = [r[c] for r in body if c < len(r) and not is_empty(r[c])]
        if col and sum(1 for v in col if is_num(v)) >= 0.6 * len(col):
            return True
    body_vals = set(str(v) for r in body for v in r if not is_empty(v))
    return not any(str(v) in body_vals for v in non_empty) and len(non_empty) == len(first)


_PERCENT = re.compile(r"^[+\-\u2212]?\d+(?:[.,]\d+)?\s*%$")
NA_MARKERS = ("n/a", "na", "#n/a", "-", "--", "null", "none", "nan", "?", "missing")
TOTAL_LABELS = ("total", "totals", "grand total", "sum", "world", "all", "overall")
TOTAL_TOLERANCE = 0.03


class ColumnProfile(object):
    def __init__(self, col_index, name, values, exclude=()):
        self.col_index = col_index
        self.name = name
        kept = [i for i in range(len(values)) if i not in exclude]
        self.count = len(kept)
        filled = [values[i] for i in kept if not is_empty(values[i])]
        self.missing = self.count - len(filled)
        nums = [float(v) for v in filled if is_num(v)]
        texts = [v for v in filled if not is_num(v)]
        if not filled:
            self.kind = "empty"
        elif not texts:
            self.kind = "number"
        elif not nums:
            self.kind = "text"
        else:
            self.kind = "mixed (%d numbers, %d text)" % (len(nums), len(texts))
        seen, dup_values = {}, 0
        for v in filled:
            key = fmt(v) if is_num(v) else str(v).strip().lower()
            seen[key] = seen.get(key, 0) + 1
        self.unique = len(seen)
        self.duplicates = sum(n - 1 for n in seen.values() if n > 1)
        self.top_repeats = sorted(((n, k) for k, n in seen.items() if n > 1),
                                  reverse=True)[:3]
        markers = {}
        for v in filled:
            if isinstance(v, str) and v.strip().lower() in NA_MARKERS:
                markers[v.strip()] = markers.get(v.strip(), 0) + 1
        self.na_markers = markers
        self.percent_text = sum(1 for v in filled if isinstance(v, str)
                                and _PERCENT.match(v.strip().lstrip("'")))
        self.nums = nums
        # Indexes into values, so callers can say which row holds an extreme.
        self.ranked = sorted((i for i in kept if is_num(values[i])),
                             key=lambda i: float(values[i]))
        if nums:
            self.min, self.max = min(nums), max(nums)
            self.sum = math.fsum(nums)
            self.mean = self.sum / len(nums)
        else:
            self.min = self.max = self.sum = self.mean = None
        self.where = None  # set by profile(): index -> "row N: label"

    def describe(self):
        bits = [self.kind]
        if self.missing:
            bits.append("%d missing" % self.missing)
        if self.na_markers:
            bits.append("%d look like missing-value markers (%s)" % (
                sum(self.na_markers.values()),
                ", ".join("%r x%d" % (k, n) for k, n in sorted(self.na_markers.items()))))
        if self.kind == "number" or self.nums:
            lo = hi = ""
            if self.where and self.ranked:
                lo = " (%s)" % self.where(self.ranked[0])
                hi = " (%s)" % self.where(self.ranked[-1])
            bits.append("min %s%s, max %s%s, sum %s, mean %s" % (
                fmt(self.min), lo, fmt(self.max), hi, fmt(self.sum), fmt(self.mean)))
            if self.where and len(self.ranked) >= 6:
                top = [self.where(i, label_only=True) for i in reversed(self.ranked[-3:])]
                low = [self.where(i, label_only=True) for i in self.ranked[:3]]
                bits.append("highest: %s; lowest: %s" % (", ".join(top), ", ".join(low)))
        if self.kind != "empty":
            bits.append("%d unique" % self.unique)
        if self.percent_text and self.percent_text * 2 >= len(self.nums) + self.percent_text:
            bits.append("percentages stored as text, like '10%'; in formulas Calc reads "
                        "'10%' as 0.1, so do not divide by 100")
        if self.duplicates:
            reps = ", ".join("%r x%d" % (k, n) for n, k in self.top_repeats)
            bits.append("%d duplicate values (%s)" % (self.duplicates, reps))
        return "; ".join(bits)


def _is_total_label(text):
    t = str(text).strip().lower().rstrip(":")
    return t in TOTAL_LABELS or t.startswith("total ") or t.endswith(" total")


def _total_rows(body, profiles, label_col):
    """Rows that aggregate the others: labelled as a total, or holding a
    value equal to the sum of every other value in its column."""
    found = set()
    if label_col is not None:
        for i, row in enumerate(body):
            if label_col < len(row) and _is_total_label(row[label_col]):
                found.add(i)
    for p in profiles:
        if len(p.ranked) < 3:
            continue
        i = p.ranked[-1]
        top = float(body[i][p.col_index])
        rest = p.sum - top
        if rest > 0 and abs(top - rest) <= TOTAL_TOLERANCE * rest:
            found.add(i)
    return found


class Table(object):
    """A selection split into header and body, with column profiles, the
    label column, and the indexes of total rows."""

    def __init__(self, snapshot):
        rows = snapshot.rows
        self.snapshot = snapshot
        self.header = detect_header(rows)
        self.body = rows[1:] if self.header else rows
        self.width = max((len(r) for r in rows), default=0)
        self.first_row = snapshot.start_row + (2 if self.header else 1)
        self.names = []
        for c in range(self.width):
            letter = col_letter(snapshot.start_col + c)
            if self.header and c < len(rows[0]) and not is_empty(rows[0][c]):
                self.names.append("%s (%s)" % (letter, str(rows[0][c]).strip()))
            else:
                self.names.append(letter)
        self.columns = [[r[c] if c < len(r) else "" for r in self.body]
                        for c in range(self.width)]

        profiles = self._profiles(())
        # Rows are named by the first text column, so the model can answer
        # "which row has the largest value" even when that row is not shown.
        self.label_col = next((p.col_index for p in profiles if p.kind == "text"), None)
        self.totals = _total_rows(self.body, profiles, self.label_col)
        self.profiles = self._profiles(self.totals) if self.totals else profiles
        for p in self.profiles:
            p.where = self.where

    def _profiles(self, exclude):
        return [ColumnProfile(c, self.names[c], self.columns[c], exclude)
                for c in range(self.width)]

    def row_number(self, i):
        return self.first_row + i

    def label(self, i):
        row = self.body[i]
        lc = self.label_col
        if lc is not None and lc < len(row) and not is_empty(row[lc]):
            return _cell_text(row[lc], 30)
        return ""

    def where(self, i, label_only=False):
        label = self.label(i)
        if label_only:
            return label or "row %d" % self.row_number(i)
        if label:
            return "row %d: %s" % (self.row_number(i), label)
        return "row %d" % self.row_number(i)


def profile(snapshot):
    """Returns (header, profiles, total_rows). Total rows are left out of the
    statistics, so "largest" means the largest item, not the grand total."""
    t = Table(snapshot)
    return t.header, t.profiles, [t.where(i) for i in sorted(t.totals)]


def fmt(v):
    """Numbers without float noise: 0.1+0.2 shows as 0.3, 3.0 as 3."""
    if v is None:
        return "-"
    if is_num(v):
        f = float(v)
        if f == int(f) and abs(f) < 1e15:
            return str(int(f))
        return ("%.10g" % f)
    return str(v)


def _cell_text(v, limit=40):
    s = fmt(v).replace("\t", " ").replace("\n", " ")
    return s if len(s) <= limit else s[:limit - 1] + "~"


def estimate_tokens(text):
    """Conservative token count for Qwen-style tokenizers.

    Digits are one token each, which is what made a character-based estimate
    undercount numeric sheets by two to three times. Punctuation, tabs and
    newlines count as one; letters as one per 3.5.
    """
    digits = letters = other = 0
    for ch in text:
        if ch.isdigit():
            digits += 1
        elif ch.isalpha():
            letters += 1
        elif ch != " ":
            other += 1
    return int(digits + other + letters / 3.5) + 1


def render(snapshot, budget=2000, max_formulas=25, measure=estimate_tokens):
    """Prompt text for the selection, at most budget as counted by measure."""
    header, profiles, totals = profile(snapshot)
    lines = []
    lines.append("Document: %s" % snapshot.doc_title)
    lines.append("Sheet: %s" % snapshot.sheet)
    lines.append("Range: %s (%d rows x %d columns)" % (
        snapshot.range_ref(), snapshot.total_rows, snapshot.total_cols))
    if snapshot.scope_note:
        lines.append("Scope: %s" % snapshot.scope_note)
    if snapshot.n_read < snapshot.total_rows:
        lines.append("Note: statistics cover the first %d of %d rows." % (
            snapshot.n_read, snapshot.total_rows))
    lines.append("Header row: %s" % (
        "yes (row %d)" % (snapshot.start_row + 1) if header else "not detected"))
    for h in snapshot.hints:
        lines.append(h)
    if totals:
        lines.append("Total rows (left out of the statistics): %s" % ", ".join(totals[:5]))

    if profiles:
        lines.append("")
        lines.append("Column statistics (computed exactly, use these numbers):")
        for p in profiles:
            lines.append("- %s: %s" % (p.name, p.describe()))

    formula_lines = []
    for r, frow in enumerate(snapshot.formulas):
        for c, f in enumerate(frow):
            if isinstance(f, str) and f.startswith("="):
                formula_lines.append("- %s: %s" % (
                    cell_ref(snapshot.start_col + c, snapshot.start_row + r), f))
    if formula_lines:
        lines.append("")
        lines.append("Formulas (%d):" % len(formula_lines))
        lines.extend(formula_lines[:max_formulas])
        if len(formula_lines) > max_formulas:
            lines.append("- ... %d more" % (len(formula_lines) - max_formulas))

    head = "\n".join(lines)
    if measure(head) > budget:
        marker = "[truncated]"
        while lines and measure("\n".join(lines + [marker])) > budget:
            lines.pop()
        return "\n".join(lines + [marker]) if lines else marker[:budget]

    footer = "[showing %d of %d rows]" % (snapshot.total_rows, snapshot.total_rows)
    remaining = budget - measure(head) - measure(footer) - 2
    cols = "\t".join(col_letter(snapshot.start_col + c)
                     for c in range(len(profiles)))
    table = ["", "Cells (tab-separated):", "row\t" + cols]
    used = sum(measure(t) + 1 for t in table)
    shown = 0
    for r, row in enumerate(snapshot.rows):
        line = "%d\t%s" % (snapshot.start_row + r + 1,
                           "\t".join(_cell_text(v) for v in row))
        cost = measure(line) + 1
        if used + cost > remaining:
            break
        table.append(line)
        used += cost
        shown += 1
    if shown < snapshot.total_rows:
        table.append("[showing %d of %d rows]" % (shown, snapshot.total_rows))
    return head + "\n" + "\n".join(table)


def summary_line(snapshot):
    """One line for the dialog: what CIU will see."""
    sheet = snapshot.sheet if len(snapshot.sheet) <= 24 else snapshot.sheet[:23] + "~"
    s = "%s!%s, %d x %d" % (sheet, snapshot.range_ref(),
                            snapshot.total_rows, snapshot.total_cols)
    if snapshot.scope_note:
        s += " (%s)" % snapshot.scope_note
    return s
