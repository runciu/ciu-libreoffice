"""Turns a selection into bounded prompt text.

Statistics are computed here so the model does not do arithmetic; raw rows
fill whatever budget remains.
"""

import math


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


class ColumnProfile(object):
    def __init__(self, col_index, name, values):
        self.col_index = col_index
        self.name = name
        self.count = len(values)
        filled = [v for v in values if not is_empty(v)]
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
        self.nums = nums
        if nums:
            self.min, self.max = min(nums), max(nums)
            self.sum = math.fsum(nums)
            self.mean = self.sum / len(nums)
        else:
            self.min = self.max = self.sum = self.mean = None

    def describe(self):
        bits = [self.kind]
        if self.missing:
            bits.append("%d missing" % self.missing)
        if self.kind == "number" or self.nums:
            bits.append("min %s, max %s, sum %s, mean %s" % (
                fmt(self.min), fmt(self.max), fmt(self.sum), fmt(self.mean)))
        if self.kind != "empty":
            bits.append("%d unique" % self.unique)
        if self.duplicates:
            reps = ", ".join("%r x%d" % (k, n) for n, k in self.top_repeats)
            bits.append("%d duplicate values (%s)" % (self.duplicates, reps))
        return "; ".join(bits)


def profile(snapshot):
    rows = snapshot.rows
    header = detect_header(rows)
    body = rows[1:] if header else rows
    width = max((len(r) for r in rows), default=0)
    profiles = []
    for c in range(width):
        letter = col_letter(snapshot.start_col + c)
        if header and c < len(rows[0]) and not is_empty(rows[0][c]):
            name = "%s (%s)" % (letter, str(rows[0][c]).strip())
        else:
            name = letter
        vals = [r[c] if c < len(r) else "" for r in body]
        profiles.append(ColumnProfile(c, name, vals))
    return header, profiles


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


def render(snapshot, budget_chars=6000, max_formulas=25):
    """Prompt text for the selection, at most budget_chars long."""
    header, profiles = profile(snapshot)
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
    if len(head) > budget_chars:
        return head[:budget_chars - 20].rstrip() + "\n[truncated]"

    remaining = budget_chars - len(head) - 80
    cols = "\t".join(col_letter(snapshot.start_col + c)
                     for c in range(len(profiles)))
    table = ["", "Cells (tab-separated):", "row\t" + cols]
    used = sum(len(t) + 1 for t in table)
    shown = 0
    for r, row in enumerate(snapshot.rows):
        line = "%d\t%s" % (snapshot.start_row + r + 1,
                           "\t".join(_cell_text(v) for v in row))
        if used + len(line) + 1 > remaining:
            break
        table.append(line)
        used += len(line) + 1
        shown += 1
    if shown < snapshot.total_rows:
        table.append("[showing %d of %d rows]" % (shown, snapshot.total_rows))
    return head + "\n" + "\n".join(table)


def summary_line(snapshot):
    """One line for the dialog: what CIU will see."""
    s = "%s!%s, %d x %d" % (snapshot.sheet, snapshot.range_ref(),
                            snapshot.total_rows, snapshot.total_cols)
    if snapshot.scope_note:
        s += " (%s)" % snapshot.scope_note
    return s
