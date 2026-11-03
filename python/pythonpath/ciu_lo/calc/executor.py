"""Applies a validated plan to the sheet as one undo step. Main thread only.

After writing, each formula's error code is read back. Structural errors roll
the whole plan back; data errors such as #DIV/0! are kept and reported.
"""

from com.sun.star.sheet.FillDirection import TO_BOTTOM, TO_RIGHT

import re

from ..core.plan import box_ref, parse_cell

# Calc error codes that mean "this formula is wrong", not "the data is odd".
ROLLBACK_ERRORS = {
    501: "invalid character", 503: "invalid numeric value", 504: "invalid parameter",
    507: "missing pair", 508: "bracket or separator error", 509: "missing operator",
    510: "missing variable", 511: "missing variable", 512: "formula overflow",
    513: "string overflow", 514: "internal overflow", 516: "internal syntax error",
    517: "internal syntax error", 518: "internal syntax error",
    520: "internal syntax error", 521: "internal syntax error",
    522: "circular reference", 524: "invalid reference (#REF!)",
    525: "unknown name (#NAME?)",
}
WARN_ERRORS = {502: "invalid argument", 519: "#VALUE!", 523: "no convergence",
               532: "#DIV/0!", 32767: "#N/A"}


class ApplyResult(object):
    def __init__(self, ok, message, warnings=None):
        self.ok = ok
        self.message = message
        self.warnings = warnings or []


def occupied_cells(sheet, plan):
    """Non-empty cells the plan would overwrite: list of (ref, current text)."""
    hits = []
    for op in plan.operations:
        c0, r0, c1, r1 = op.box
        rng = sheet.getCellRangeByPosition(c0, r0, c1, r1)
        for dr, row in enumerate(rng.getFormulaArray()):
            for dc, text in enumerate(row):
                if text != "":
                    hits.append((box_ref(c0 + dc, r0 + dr), text))
    return hits


def get_sheet(doc, name):
    sheets = doc.getSheets()
    if not sheets.hasByName(name):
        raise KeyError("Sheet '%s' no longer exists." % name)
    return sheets.getByName(name)


def apply(doc, plan, expected_occupied):
    """Write the plan. expected_occupied is the overwrite list shown in the
    preview; if the sheet now has more filled target cells, stop.
    """
    try:
        sheet = get_sheet(doc, plan.sheet)
    except KeyError as e:
        return ApplyResult(False, str(e))

    now = occupied_cells(sheet, plan)
    if len(now) > len(expected_occupied):
        return ApplyResult(False, "The target cells changed since the preview "
                                  "(%d filled now, %d before). Propose again." % (
                                      len(now), len(expected_occupied)))

    um = doc.getUndoManager()
    title = "CIU: " + (plan.summary or "change")[:60]
    um.enterUndoContext(title)
    try:
        for op in plan.operations:
            _write(sheet, op)
    except Exception as e:
        um.leaveUndoContext()
        _rollback(um, title)
        return ApplyResult(False, "Could not write the change, nothing was kept: %s" % e)
    um.leaveUndoContext()

    bad, warn = _check_errors(sheet, plan)
    if bad:
        _rollback(um, title)
        lines = ["Calc rejected the formula, so nothing was changed:"]
        lines += ["  %s: %s" % (ref, why) for ref, why in bad[:8]]
        return ApplyResult(False, "\n".join(lines))

    msg = "Applied %d change%s to %d cell%s on %s. Ctrl+Z (Cmd+Z) undoes all of it." % (
        len(plan.operations), "" if len(plan.operations) == 1 else "s",
        plan.n_cells, "" if plan.n_cells == 1 else "s", plan.sheet)
    warnings = ["%s shows %s" % (ref, why) for ref, why in warn[:8]]
    if len(warn) > 8:
        warnings.append("... and %d more" % (len(warn) - 8))
    return ApplyResult(True, msg, warnings)


def _rollback(um, title):
    """Undo our own step only. An undo context with no changes never reaches
    the undo stack, so a blind undo() would revert the user's previous edit."""
    if um.isUndoPossible() and um.getCurrentUndoActionTitle() == title:
        um.undo()


def unique_sheet_name(doc, wanted):
    sheets = doc.getSheets()
    if not sheets.hasByName(wanted):
        return wanted
    n = 2
    while sheets.hasByName("%s %d" % (wanted, n)):
        n += 1
    return "%s %d" % (wanted, n)


def apply_sheet(doc, wanted_name, result, source_sheet):
    """Write a SheetResult to a new sheet after the source sheet. Undoing
    removes the new sheet in one step; the source sheet is never written."""
    values = result.values()
    width = len(values[0])
    sheets = doc.getSheets()
    name = unique_sheet_name(doc, wanted_name)
    position = sheets.getCount()
    for i, existing in enumerate(sheets.getElementNames()):
        if existing == source_sheet:
            position = i + 1
    um = doc.getUndoManager()
    title = "CIU: new sheet " + name
    um.enterUndoContext(title)
    try:
        sheets.insertNewByName(name, position)
        new = sheets.getByName(name)
        # Bulk write: fast, and undo stays correct because undoing the sheet
        # insertion removes the whole sheet, contents included.
        new.getCellRangeByPosition(0, 0, width - 1, len(values) - 1).setDataArray(values)
        _format_columns(doc, new, result.kinds, len(values))
        new.getCellRangeByPosition(0, 0, width - 1, 0).CharWeight = 150.0
    except Exception as e:
        um.leaveUndoContext()
        _rollback(um, title)
        return ApplyResult(False, "Could not create the new sheet, nothing was kept: %s" % e)
    um.leaveUndoContext()
    try:
        doc.getCurrentController().setActiveSheet(new)
    except Exception:
        pass
    n = len(values) - 1
    return ApplyResult(True, "Created sheet '%s' with %d row%s. Your original sheet is "
                             "unchanged. Ctrl+Z (Cmd+Z) removes the new sheet."
                       % (name, n, "" if n == 1 else "s"))


FORMAT_CODES = {"date": "YYYY-MM-DD", "datetime": "YYYY-MM-DD HH:MM", "time": "HH:MM",
                "percent": "0.00%", "logical": "BOOLEAN", "currency": "#,##0.00"}


def _format_columns(doc, sheet, kinds, height):
    """Give date, percentage and TRUE/FALSE columns of a new sheet the
    matching number format, so they read as they did in the source."""
    import uno
    formats = doc.getNumberFormats()
    locale = uno.createUnoStruct("com.sun.star.lang.Locale")
    for c, kind in enumerate(kinds):
        code = FORMAT_CODES.get(kind)
        if not code or height < 2:
            continue
        key = formats.queryKey(code, locale, False)
        if key == -1:
            key = formats.addNew(code, locale)
        sheet.getCellRangeByPosition(c, 1, c, height - 1).NumberFormat = key


PREVIEW_TITLE = "CIU: preview"
_REF = re.compile(r"(?<![A-Za-z0-9_.$])\$?([A-Z]{1,3})\$?([0-9]{1,7})(?![0-9(])")


class Preview(object):
    def __init__(self, samples, rejected):
        self.samples = samples      # [(ref, shown value, [(input ref, shown value)])]
        self.rejected = rejected    # [(ref, reason)] Calc would roll back on apply


def preview(doc, plan, per_op=3):
    """Calculate a few result cells by applying the plan and undoing it at
    once, so the user can check real numbers before applying. The document's
    modified flag and redo history are left as they were before."""
    sheet = get_sheet(doc, plan.sheet)
    um = doc.getUndoManager()
    was_modified = doc.isModified()
    targets = []
    for op in plan.operations:
        if op.tool == "set_values":
            continue
        c0, r0, c1, r1 = op.box
        cells = [(c, r) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
        targets.extend(cells[:per_op])
    um.enterUndoContext(PREVIEW_TITLE)
    try:
        for op in plan.operations:
            _write(sheet, op)
        samples = []
        for c, r in targets:
            cell = sheet.getCellByPosition(c, r)
            inputs = []
            for col, row in _REF.findall(cell.getFormula())[:4]:
                ic, ir = parse_cell(col + row)
                inputs.append((col + row, sheet.getCellByPosition(ic, ir).getString() or "(empty)"))
            samples.append((box_ref(c, r), cell.getString() or "(empty)", inputs))
        rejected, _ = _check_errors(sheet, plan)
    finally:
        um.leaveUndoContext()
        _rollback(um, PREVIEW_TITLE)
        try:
            um.clearRedo()
        except Exception:
            pass
        doc.setModified(was_modified)
    return Preview(samples, rejected)


def _write(sheet, op):
    c0, r0, c1, r1 = op.box
    if op.tool == "set_formula":
        sheet.getCellByPosition(c0, r0).setFormula(op.formula)
    elif op.tool == "fill_formula":
        sheet.getCellByPosition(c0, r0).setFormula(op.formula)
        rng = sheet.getCellRangeByPosition(c0, r0, c1, r1)
        if c0 == c1:
            rng.fillAuto(TO_BOTTOM, 1)
        else:
            rng.fillAuto(TO_RIGHT, 1)
    elif op.tool == "set_values":
        # Cell by cell: setDataArray and setFormulaArray bypass the undo manager.
        for dr, row in enumerate(op.values):
            for dc, v in enumerate(row):
                cell = sheet.getCellByPosition(c0 + dc, r0 + dr)
                if isinstance(v, (int, float)):
                    cell.setValue(float(v))
                else:
                    cell.setString(v)


def _check_errors(sheet, plan):
    bad, warn = [], []
    for op in plan.operations:
        if op.tool == "set_values":
            continue
        c0, r0, c1, r1 = op.box
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                code = sheet.getCellByPosition(c, r).getError()
                if not code:
                    continue
                ref = box_ref(c, r)
                if code in ROLLBACK_ERRORS:
                    bad.append((ref, "Err:%d %s" % (code, ROLLBACK_ERRORS[code])))
                else:
                    warn.append((ref, WARN_ERRORS.get(code, "Err:%d" % code)))
    return bad, warn
