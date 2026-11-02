"""Reads the current Calc selection into a Snapshot. Main thread only."""

from ..core.context import Snapshot

MAX_CELLS = 6000
MAX_ROWS = 2000


class SelectionError(Exception):
    pass


def is_spreadsheet(model):
    try:
        return model is not None and model.supportsService(
            "com.sun.star.sheet.SpreadsheetDocument")
    except Exception:
        return False


def doc_title(controller):
    try:
        return controller.getFrame().getTitle()
    except Exception:
        return "Untitled"


def read_selection(model, controller=None):
    """Snapshot of the selection.

    A range is trimmed to the used area, a single cell expands to its data
    block, and of several ranges only the first is used.
    """
    if not is_spreadsheet(model):
        raise SelectionError("The active document is not a spreadsheet.")
    controller = controller or model.getCurrentController()
    sel = controller.getSelection()
    scope_note = None

    if sel is not None and sel.supportsService("com.sun.star.sheet.SheetCellRanges"):
        if sel.getCount() == 0:
            raise SelectionError("Select some cells first.")
        sel = sel.getByIndex(0)
        scope_note = "first of several selected ranges"

    if sel is None or not hasattr(sel, "getRangeAddress"):
        raise SelectionError("Select some cells first.")

    addr = sel.getRangeAddress()
    sheet = model.getSheets().getByIndex(addr.Sheet)

    single = addr.StartColumn == addr.EndColumn and addr.StartRow == addr.EndRow
    if single:
        cursor = sheet.createCursorByRange(sel)
        cursor.collapseToCurrentRegion()
        region = cursor.getRangeAddress()
        if not (region.StartColumn == region.EndColumn and region.StartRow == region.EndRow):
            scope_note = "data block around %s" % _ref(addr.StartColumn, addr.StartRow)
            addr = region
    else:
        addr = _trim_to_used_area(sheet, addr)

    total_rows = addr.EndRow - addr.StartRow + 1
    total_cols = addr.EndColumn - addr.StartColumn + 1
    read_rows = max(1, min(total_rows, MAX_ROWS, MAX_CELLS // max(1, total_cols)))

    block = sheet.getCellRangeByPosition(
        addr.StartColumn, addr.StartRow,
        addr.EndColumn, addr.StartRow + read_rows - 1)
    rows = [list(r) for r in block.getDataArray()]
    formulas = [list(r) for r in block.getFormulaArray()]

    return Snapshot(
        doc_title=doc_title(controller),
        sheet=sheet.getName(),
        start_col=addr.StartColumn,
        start_row=addr.StartRow,
        rows=rows,
        formulas=formulas,
        total_rows=total_rows,
        total_cols=total_cols,
        scope_note=scope_note,
        hints=_free_space_hints(sheet, addr),
    )


FULL_MAX_CELLS = 500000


def read_full(model, snapshot, max_cells=FULL_MAX_CELLS):
    """Every row of the range a snapshot came from, for operations that must
    not silently drop rows, such as writing a cleaned copy."""
    if snapshot.total_rows * snapshot.total_cols > max_cells:
        raise SelectionError("The selection has more than %d cells; select fewer rows."
                             % max_cells)
    sheets = model.getSheets()
    if not sheets.hasByName(snapshot.sheet):
        raise SelectionError("Sheet '%s' no longer exists." % snapshot.sheet)
    block = sheets.getByName(snapshot.sheet).getCellRangeByPosition(
        snapshot.start_col, snapshot.start_row,
        snapshot.start_col + snapshot.total_cols - 1,
        snapshot.start_row + snapshot.total_rows - 1)
    return Snapshot(
        doc_title=snapshot.doc_title,
        sheet=snapshot.sheet,
        start_col=snapshot.start_col,
        start_row=snapshot.start_row,
        rows=[list(r) for r in block.getDataArray()],
        total_rows=snapshot.total_rows,
        total_cols=snapshot.total_cols,
    )


def _free_space_hints(sheet, addr):
    """Where new results can go without overwriting anything: the first
    empty column to the right and the first empty row below the range.
    """
    from ..core.context import col_letter
    hints = []
    for c in range(addr.EndColumn + 1, min(addr.EndColumn + 6, 16384)):
        col = sheet.getCellRangeByPosition(c, addr.StartRow, c, addr.EndRow)
        if all(v == "" for row in col.getFormulaArray() for v in row):
            hints.append("First empty column to the right: %s (rows %d-%d are empty)" % (
                col_letter(c), addr.StartRow + 1, addr.EndRow + 1))
            break
    below = addr.EndRow + 1
    if below < 1048576:
        row = sheet.getCellRangeByPosition(addr.StartColumn, below, addr.EndColumn, below)
        if all(v == "" for r in row.getFormulaArray() for v in r):
            hints.append("Row %d below the range is empty." % (below + 1))
    return hints


def _trim_to_used_area(sheet, addr):
    cursor = sheet.createCursor()
    cursor.gotoEndOfUsedArea(False)
    used = cursor.getRangeAddress()
    end_col = min(addr.EndColumn, used.EndColumn)
    end_row = min(addr.EndRow, used.EndRow)
    if end_col < addr.StartColumn or end_row < addr.StartRow:
        raise SelectionError("The selection is empty.")
    addr.EndColumn = end_col
    addr.EndRow = end_row
    return addr


def _ref(col, row):
    from ..core.context import cell_ref
    return cell_ref(col, row)
