"""Applies a validated plan to the sheet as one undo step. Main thread only.

After writing, each formula's error code is read back. Structural errors roll
the whole plan back; data errors such as #DIV/0! are kept and reported.
"""

from com.sun.star.sheet.FillDirection import TO_BOTTOM, TO_RIGHT

from ..core.plan import box_ref

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
        um.undo()
        return ApplyResult(False, "Could not write the change, nothing was kept: %s" % e)
    um.leaveUndoContext()

    bad, warn = _check_errors(sheet, plan)
    if bad:
        um.undo()
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
