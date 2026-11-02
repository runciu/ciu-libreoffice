"""Change plans proposed by the model: parsing, normalisation and validation.

LibreOffice behaviours this relies on:
  - setFormula needs ';' between arguments; ',' gives Err:508.
  - Unclosed brackets are silently auto-closed.
  - WEBSERVICE and external file references fetch the moment they are
    written, so they must be refused before writing, not rolled back after.
"""

import json
import re

from .context import col_letter

TOOLS = ("set_formula", "fill_formula", "set_values")
SHEET_TOOL = "sql_to_sheet"
DEFAULT_SHEET = "Cleaned"
MAX_OPS = 6
MAX_CELLS = 5000
MAX_FORMULA_LEN = 600

# Formulas may only refer to this document. INDIRECT is denied because it
# can build an external reference out of cell text.
DENIED_FUNCTIONS = ("WEBSERVICE", "FILTERXML", "DDE", "HYPERLINK", "INFO",
                    "INDIRECT")
_EXTERNAL_REF = re.compile(r"'[^']*'\s*#")
_LOCATION = re.compile(r"[a-z][a-z0-9+.\-]*://|\bfile:|\\\\|\bwww\.", re.I)

def plan_schema(sql=False):
    tools = list(TOOLS) + ([SHEET_TOOL] if sql else [])
    op = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": tools},
            "target": {"type": "string"},
            "formula": {"type": "string"},
            "values": {"type": "array",
                       "items": {"type": "array",
                                 "items": {"type": ["string", "number"]}}},
        },
        "required": ["tool"],
    }
    if sql:
        op["properties"]["sql"] = {"type": "string"}
        op["properties"]["sheet"] = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "operations": {"type": "array", "minItems": 0, "maxItems": MAX_OPS, "items": op},
        },
        "required": ["summary", "operations"],
    }


PLAN_SCHEMA = plan_schema()


class PlanError(Exception):
    pass


class Operation(object):
    def __init__(self, tool, target, formula=None, values=None, sql=None, sheet=None):
        self.tool = tool
        self.target = target
        self.formula = formula
        self.values = values
        self.sql = sql
        self.sheet = sheet
        # (c0, r0, c1, r1), 0-based; a new sheet has no target on this one.
        self.box = None if tool == SHEET_TOOL else parse_range(target)

    @property
    def n_cells(self):
        if self.box is None:
            return 0
        c0, r0, c1, r1 = self.box
        return (c1 - c0 + 1) * (r1 - r0 + 1)

    def describe(self):
        if self.tool == SHEET_TOOL:
            return "New sheet '%s' from: %s" % (self.sheet, self.sql)
        if self.tool == "set_formula":
            return "%s: %s" % (self.target, self.formula)
        if self.tool == "fill_formula":
            first = box_ref(self.box[0], self.box[1])
            return "%s: %s, filled across %s (%d cells)" % (
                first, self.formula, self.target, self.n_cells)
        if self.n_cells == 1:
            return "%s: set to %s" % (self.target, _short(self.values[0][0], 30))
        flat = [v for row in self.values for v in row]
        preview = ", ".join(_short(v) for v in flat[:6])
        return "%s: %d values (%s%s)" % (self.target, self.n_cells, preview,
                                        ", ..." if len(flat) > 6 else "")


class Plan(object):
    def __init__(self, summary, operations, sheet=None):
        self.summary = summary
        self.operations = operations
        self.sheet = sheet

    @property
    def n_cells(self):
        return sum(op.n_cells for op in self.operations)

    def describe(self):
        lines = [self.summary or "Proposed change:"]
        for i, op in enumerate(self.operations, 1):
            lines.append("  %d. %s" % (i, op.describe()))
        return "\n".join(lines)


_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$")


def parse_cell(ref):
    m = _CELL.match(ref.strip())
    if not m:
        raise PlanError("'%s' is not a cell address." % ref)
    letters, digits = m.group(1).upper(), int(m.group(2))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    col -= 1
    if col > 16383 or digits < 1 or digits > 1048576:
        raise PlanError("'%s' is outside the sheet." % ref)
    return col, digits - 1


def parse_range(ref):
    ref = (ref or "").strip()
    if "." in ref or "!" in ref:
        raise PlanError("'%s': targets must be on the current sheet (no sheet name)." % ref)
    parts = ref.split(":")
    if len(parts) == 1:
        c, r = parse_cell(parts[0])
        return c, r, c, r
    if len(parts) == 2:
        c0, r0 = parse_cell(parts[0])
        c1, r1 = parse_cell(parts[1])
        return min(c0, c1), min(r0, r1), max(c0, c1), max(r0, r1)
    raise PlanError("'%s' is not a range." % ref)


def box_ref(c, r):
    return "%s%d" % (col_letter(c), r + 1)


def normalize_formula(f):
    """Make a model-written formula acceptable to Calc, or raise PlanError."""
    if not isinstance(f, str) or not f.strip():
        raise PlanError("Missing formula.")
    f = f.strip()
    if not f.startswith("="):
        f = "=" + f
    if len(f) > MAX_FORMULA_LEN:
        raise PlanError("Formula is too long.")

    out, depth, brace, in_str = [], 0, 0, False
    for ch in f:
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    raise PlanError("Unbalanced ')' in %s" % f)
            elif ch == "{":
                brace += 1
            elif ch == "}":
                brace -= 1
            elif ch == "," and brace == 0:
                ch = ";"
        out.append(ch)
    if in_str:
        raise PlanError("Unclosed quote in %s" % f)
    if depth != 0 or brace != 0:
        raise PlanError("Unbalanced brackets in %s" % f)
    f = "".join(out)

    upper = _strip_strings(f).upper()
    for name in DENIED_FUNCTIONS:
        if re.search(r"(?<![A-Z0-9_.])" + name + r"\s*\(", upper):
            raise PlanError("%s is not allowed: it can reach outside the document." % name)
    if _EXTERNAL_REF.search(_strip_strings(f)):
        raise PlanError("References to other files are not allowed: %s" % f)
    if _LOCATION.search(f):
        raise PlanError("Formulas may not contain web addresses or file paths: %s" % f)
    return f


def _strip_strings(f):
    return re.sub(r'"[^"]*"', '""', f)


def extract_json(text):
    """The first top-level JSON object in text (tolerates prose or fences)."""
    if not text:
        raise PlanError("The model returned nothing.")
    start = text.find("{")
    if start < 0:
        raise PlanError("The model did not return a plan.")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError as e:
                    raise PlanError("The plan was not valid JSON (%s)." % e)
    raise PlanError("The plan was cut off before it finished.")


def parse_plan(text, sheet=None, allow_sql=False):
    data = extract_json(text)
    if not isinstance(data, dict):
        raise PlanError("The plan must be a JSON object.")
    ops_raw = data.get("operations")
    if not isinstance(ops_raw, list):
        raise PlanError("The plan has no operations list.")
    if len(ops_raw) > MAX_OPS:
        raise PlanError("The plan has too many operations (%d, max %d)." % (len(ops_raw), MAX_OPS))

    ops = []
    for i, raw in enumerate(ops_raw, 1):
        if not isinstance(raw, dict):
            raise PlanError("Operation %d is not an object." % i)
        tool = raw.get("tool")
        if tool not in TOOLS and not (allow_sql and tool == SHEET_TOOL):
            raise PlanError("Operation %d: unknown tool %r." % (i, tool))
        op = Operation(tool, str(raw.get("target", "")),
                       formula=raw.get("formula"), values=raw.get("values"),
                       sql=raw.get("sql"), sheet=raw.get("sheet"))
        _check_op(i, op)
        ops.append(op)
    if any(op.tool == SHEET_TOOL for op in ops) and len(ops) > 1:
        raise PlanError("A new-sheet operation must be the only operation in a plan.")

    plan = Plan(str(data.get("summary") or "").strip(), ops, sheet=sheet)
    if plan.n_cells > MAX_CELLS:
        raise PlanError("The plan would change %d cells (max %d)." % (plan.n_cells, MAX_CELLS))
    _check_overlaps(ops)
    return plan


def sheet_name(name):
    """A valid, readable sheet name from what the model suggested."""
    name = re.sub(r"[\[\]*?:/\\']", "", str(name or "")).strip()[:31].strip()
    return name or DEFAULT_SHEET


def _check_op(i, op):
    if op.tool == SHEET_TOOL:
        if not isinstance(op.sql, str) or not op.sql.strip():
            raise PlanError("Operation %d: sql_to_sheet needs SQL." % i)
        op.sql = op.sql.strip().rstrip(";").strip()
        op.sheet = sheet_name(op.sheet)
        return
    c0, r0, c1, r1 = op.box
    if op.tool == "set_formula":
        if op.n_cells != 1:
            raise PlanError("Operation %d: set_formula needs one cell, got %s." % (i, op.target))
        op.formula = normalize_formula(op.formula)
    elif op.tool == "fill_formula":
        if c0 != c1 and r0 != r1:
            raise PlanError("Operation %d: fill_formula needs a single row or column, got %s." % (i, op.target))
        if op.n_cells < 2:
            raise PlanError("Operation %d: fill_formula needs at least two cells; use set_formula." % i)
        op.formula = normalize_formula(op.formula)
    elif op.tool == "set_values":
        vals = op.values
        if not isinstance(vals, list) or not vals or not all(isinstance(r, list) for r in vals):
            raise PlanError("Operation %d: set_values needs a list of rows." % i)
        rows, cols = r1 - r0 + 1, c1 - c0 + 1
        if len(vals) != rows or any(len(r) != cols for r in vals):
            raise PlanError("Operation %d: %s is %dx%d but %d rows of values were given." % (
                i, op.target, rows, cols, len(vals)))
        for row in vals:
            for v in row:
                if not isinstance(v, (str, int, float)) or isinstance(v, bool):
                    raise PlanError("Operation %d: values must be text or numbers." % i)
                if isinstance(v, str) and v.lstrip().startswith("="):
                    raise PlanError("Operation %d: put formulas in set_formula or fill_formula, not set_values." % i)


def _check_overlaps(ops):
    boxed = [op for op in ops if op.box is not None]
    for a in range(len(boxed)):
        for b in range(a + 1, len(boxed)):
            x, y = boxed[a].box, boxed[b].box
            if x[0] <= y[2] and y[0] <= x[2] and x[1] <= y[3] and y[1] <= x[3]:
                raise PlanError("Operations %d and %d write to the same cells." % (a + 1, b + 1))


def _short(v, n=12):
    from .context import fmt
    s = fmt(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
    return s if len(s) <= n else s[:n - 1] + "~"
