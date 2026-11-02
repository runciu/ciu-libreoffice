"""Reads real Calc selections."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))

from lo_harness import Office
from ciu_lo.calc import reader
from ciu_lo.core import context

OFFICE = None


def setUpModule():
    global OFFICE
    OFFICE = Office().start()


def tearDownModule():
    OFFICE.stop()


class Reader(unittest.TestCase):
    def setUp(self):
        self.doc = OFFICE.new_calc()
        self.ctrl = self.doc.getCurrentController()
        self.sheet = self.doc.getSheets().getByIndex(0)
        self.sheet.setName("Sales")
        data = (("Region", "Units", "Price", "Total"),
                ("North", 3.0, 2.5, ""),
                ("South", 5.0, 4.0, ""),
                ("North", "", 1.0, ""),
                ("East", 2.0, 9.5, ""))
        self.sheet.getCellRangeByName("B2:E6").setDataArray(data)
        for r in range(3, 7):
            self.sheet.getCellRangeByName("E%d" % r).setFormula("=C%d*D%d" % (r, r))

    def tearDown(self):
        self.doc.close(True)

    def select(self, ref):
        self.ctrl.select(self.sheet.getCellRangeByName(ref))

    def test_range_selection(self):
        self.select("B2:E6")
        s = reader.read_selection(self.doc, self.ctrl)
        self.assertEqual(s.sheet, "Sales")
        self.assertEqual(s.range_ref(), "B2:E6")
        self.assertEqual(s.rows[1][:3], ["North", 3.0, 2.5])
        self.assertEqual(s.rows[1][3], 7.5)
        self.assertEqual(s.formulas[1][3], "=C3*D3")
        self.assertIsNone(s.scope_note)

    def test_single_cell_expands_to_block(self):
        self.select("C4")
        s = reader.read_selection(self.doc, self.ctrl)
        self.assertEqual(s.range_ref(), "B2:E6")
        self.assertIn("data block around C4", s.scope_note)

    def test_whole_columns_trimmed_to_used_area(self):
        self.ctrl.select(self.sheet.getColumns().getByIndex(2))
        s = reader.read_selection(self.doc, self.ctrl)
        self.assertEqual(s.total_rows, 6)
        self.assertEqual(s.range_ref(), "C1:C6")

    def test_rendered_context_end_to_end(self):
        self.select("B2:E6")
        out = context.render(reader.read_selection(self.doc, self.ctrl))
        self.assertIn("Range: B2:E6 (5 rows x 4 columns)", out)
        self.assertIn("C (Units): number; 1 missing", out)
        self.assertIn("E3: =C3*D3", out)
        self.assertIn("\n3\tNorth\t3\t2.5\t7.5", out)

    def test_empty_area_rejected(self):
        self.select("H20:J30")
        with self.assertRaises(reader.SelectionError):
            reader.read_selection(self.doc, self.ctrl)

    def test_multi_range_takes_first(self):
        ranges = self.doc.createInstance("com.sun.star.sheet.SheetCellRanges")
        ranges.addRangeAddress(self.sheet.getCellRangeByName("B2:C6").getRangeAddress(), False)
        ranges.addRangeAddress(self.sheet.getCellRangeByName("E2:E6").getRangeAddress(), False)
        self.ctrl.select(ranges)
        s = reader.read_selection(self.doc, self.ctrl)
        self.assertEqual(s.range_ref(), "B2:C6")
        self.assertIn("first of several", s.scope_note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
