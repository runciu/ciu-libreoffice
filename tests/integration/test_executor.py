"""Applies plans to a real Calc document."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))

from lo_harness import Office
from ciu_lo.calc import executor, reader
from ciu_lo.core.plan import parse_plan

OFFICE = None


def setUpModule():
    global OFFICE
    OFFICE = Office(port=2130).start()


def tearDownModule():
    OFFICE.stop()


def plan(*ops, summary="test change"):
    return parse_plan(json.dumps({"summary": summary, "operations": list(ops)}), sheet="Sales")


class Executor(unittest.TestCase):
    def setUp(self):
        self.doc = OFFICE.new_calc()
        self.sheet = self.doc.getSheets().getByIndex(0)
        self.sheet.setName("Sales")
        self.sheet.getCellRangeByName("A1:C4").setDataArray((
            ("Item", "Price", "Qty"), ("Tea", 2.0, 3.0), ("Milk", 1.5, 2.0), ("Jam", 4.0, 0.0)))
        self.um = self.doc.getUndoManager()

    def tearDown(self):
        self.doc.close(True)

    def cell(self, ref):
        return self.sheet.getCellRangeByName(ref)

    def run_plan(self, p):
        occ = executor.occupied_cells(self.sheet, p)
        return executor.apply(self.doc, p, occ), occ

    def test_fill_column_with_header_is_one_undo_step(self):
        p = plan({"tool": "set_values", "target": "D1", "values": [["Total"]]},
                 {"tool": "fill_formula", "target": "D2:D4", "formula": "=B2*C2"},
                 summary="Add Total column")
        res, occ = self.run_plan(p)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(occ, [])
        self.assertEqual([self.cell("D%d" % r).getFormula() for r in range(1, 5)],
                         ["Total", "=B2*C2", "=B3*C3", "=B4*C4"])
        self.assertEqual([self.cell("D%d" % r).getValue() for r in range(2, 5)], [6.0, 3.0, 0.0])
        self.assertEqual(self.um.getCurrentUndoActionTitle(), "CIU: Add Total column")
        self.um.undo()
        self.assertEqual([self.cell("D%d" % r).getFormula() for r in range(1, 5)], ["", "", "", ""])

    def test_model_commas_work_after_normalisation(self):
        p = plan({"tool": "set_formula", "target": "E1", "formula": "=SUMPRODUCT(B2:B4,C2:C4)"})
        res, _ = self.run_plan(p)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.cell("E1").getValue(), 9.0)

    def test_fill_right(self):
        p = plan({"tool": "fill_formula", "target": "B5:C5", "formula": "=SUM(B2:B4)"})
        res, _ = self.run_plan(p)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.cell("C5").getFormula(), "=SUM(C2:C4)")

    def test_unknown_function_rolls_back_everything(self):
        p = plan({"tool": "set_values", "target": "E1", "values": [["Avg"]]},
                 {"tool": "fill_formula", "target": "E2:E4", "formula": "=AVERAGEE(B2:C2)"})
        res, _ = self.run_plan(p)
        self.assertFalse(res.ok)
        self.assertIn("E2: Err:525 unknown name", res.message)
        self.assertEqual(self.cell("E1").getFormula(), "")
        self.assertEqual(self.cell("E2").getFormula(), "")

    def test_circular_reference_rolls_back(self):
        res, _ = self.run_plan(plan({"tool": "set_formula", "target": "E2", "formula": "=E2+B2"}))
        self.assertFalse(res.ok)
        self.assertIn("circular", res.message)
        self.assertEqual(self.cell("E2").getFormula(), "")

    def test_div_zero_kept_with_warning(self):
        res, _ = self.run_plan(plan({"tool": "fill_formula", "target": "D2:D4", "formula": "=B2/C2"}))
        self.assertTrue(res.ok)
        self.assertEqual(res.warnings, ["D4 shows #DIV/0!"])
        self.assertEqual(self.cell("D4").getFormula(), "=B4/C4")

    def test_overwrite_detected_in_preview(self):
        p = plan({"tool": "fill_formula", "target": "C2:C4", "formula": "=B2*2"})
        occ = executor.occupied_cells(self.sheet, p)
        self.assertEqual([ref for ref, _ in occ], ["C2", "C3", "C4"])

    def test_sheet_changed_after_preview_blocks_apply(self):
        p = plan({"tool": "set_formula", "target": "D2", "formula": "=B2*C2"})
        occ = executor.occupied_cells(self.sheet, p)
        self.cell("D2").setString("typed by user meanwhile")
        res = executor.apply(self.doc, p, occ)
        self.assertFalse(res.ok)
        self.assertIn("changed since the preview", res.message)
        self.assertEqual(self.cell("D2").getString(), "typed by user meanwhile")

    def test_renamed_sheet(self):
        p = plan({"tool": "set_formula", "target": "D2", "formula": "=1"})
        self.sheet.setName("Renamed")
        res = executor.apply(self.doc, p, [])
        self.assertFalse(res.ok)
        self.assertIn("no longer exists", res.message)

    def test_values_numbers_and_text(self):
        res, _ = self.run_plan(plan({"tool": "set_values", "target": "E1:F2",
                                     "values": [["Code", 7], ["x", 2.5]]}))
        self.assertTrue(res.ok)
        self.assertEqual(self.cell("F1").getValue(), 7.0)
        self.assertEqual(self.cell("E2").getString(), "x")

    def test_every_tool_is_undoable_in_one_step(self):
        """Guards against any write path that bypasses Calc's undo manager."""
        before = self.sheet.getCellRangeByName("A1:H8").getFormulaArray()
        p = plan({"tool": "set_values", "target": "E1:F2", "values": [["a", 1], ["b", 2.5]]},
                 {"tool": "set_formula", "target": "G1", "formula": "=SUM(B2:B4)"},
                 {"tool": "fill_formula", "target": "D2:D4", "formula": "=B2*C2"},
                 {"tool": "fill_formula", "target": "B6:C6", "formula": "=MAX(B2:B4)"})
        res, _ = self.run_plan(p)
        self.assertTrue(res.ok, res.message)
        self.assertNotEqual(self.sheet.getCellRangeByName("A1:H8").getFormulaArray(), before)
        self.um.undo()
        self.assertEqual(self.sheet.getCellRangeByName("A1:H8").getFormulaArray(), before)

    def test_external_reference_never_reaches_network(self):
        """Calc fetches external references at write time, so the only safe
        place to stop them is the validator. Prove no request is made.
        """
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from ciu_lo.core.plan import PlanError
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                self.send_response(404)
                self.end_headers()
            do_HEAD = do_GET
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            for f in ("='%s/a.ods'#$Sheet1.A1" % url,
                      '=INDIRECT("\'%s/b.ods\'#$Sheet1.A1")' % url,
                      '=INDIRECT("\'"&A2&"\'#$Sheet1.A1")'):
                with self.assertRaises(PlanError):
                    plan({"tool": "set_formula", "target": "E1", "formula": f})
            import time
            time.sleep(1.0)
            self.assertEqual(hits, [])
            self.assertEqual(self.cell("E1").getFormula(), "")
        finally:
            srv.shutdown()

    def test_new_sheet_from_sql(self):
        from ciu_lo.core import sql as S
        from ciu_lo.core.context import Table
        self.doc.getCurrentController().select(self.cell("A1:C4"))
        snap = reader.read_full(self.doc, reader.read_selection(self.doc))
        result = S.run_to_sheet("SELECT a, b * c AS total FROM t ORDER BY total DESC", Table(snap))
        before = self.sheet.getCellRangeByName("A1:C4").getFormulaArray()
        self.doc.getSheets().insertNewByName("Cleaned", 1)
        res = executor.apply_sheet(self.doc, "Cleaned", result, "Sales")
        self.assertTrue(res.ok, res.message)
        self.assertIn("Created sheet 'Cleaned 2' with 3 rows", res.message)
        names = self.doc.getSheets().getElementNames()
        self.assertEqual(names, ("Sales", "Cleaned 2", "Cleaned"))
        new = self.doc.getSheets().getByName("Cleaned 2")
        self.assertEqual(new.getCellRangeByName("A1:B4").getDataArray(),
                         (("Item", "total"), ("Tea", 6.0), ("Milk", 3.0), ("Jam", 0.0)))
        self.assertEqual(self.doc.getCurrentController().getActiveSheet().getName(), "Cleaned 2")
        self.assertEqual(self.sheet.getCellRangeByName("A1:C4").getFormulaArray(), before)
        self.um.undo()
        self.assertEqual(self.doc.getSheets().getElementNames(), ("Sales", "Cleaned"))

    def test_failed_apply_never_undoes_user_edit(self):
        """An undo context with no changes does not reach the stack, so a
        blind undo() after an early failure would revert the user's edit."""
        self.cell("H1").setString("user edit")
        p = plan({"tool": "set_formula", "target": "E1", "formula": "=1"})
        original = executor._write

        def boom(sheet, op):
            raise RuntimeError("fail before writing")
        executor._write = boom
        try:
            res = executor.apply(self.doc, p, [])
        finally:
            executor._write = original
        self.assertFalse(res.ok)
        self.assertEqual(self.cell("H1").getString(), "user edit")

    def test_read_full_reads_every_row(self):
        rows = [("n", "v")] + [("r%d" % i, float(i)) for i in range(2500)]
        self.sheet.getCellRangeByName("K1:L2501").setDataArray(rows)
        self.doc.getCurrentController().select(self.cell("K1:L2501"))
        snap = reader.read_selection(self.doc)
        self.assertLess(snap.n_read, snap.total_rows)
        full = reader.read_full(self.doc, snap)
        self.assertEqual(full.n_read, 2501)
        with self.assertRaises(reader.SelectionError):
            reader.read_full(self.doc, snap, max_cells=100)

    def test_free_space_hints(self):
        self.doc.getCurrentController().select(self.cell("A1:C4"))
        snap = reader.read_selection(self.doc)
        self.assertIn("First empty column to the right: D (rows 1-4 are empty)", snap.hints)
        self.assertIn("Row 5 below the range is empty.", snap.hints)


if __name__ == "__main__":
    unittest.main(verbosity=2)
