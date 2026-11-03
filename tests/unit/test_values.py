import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))

from ciu_lo.core import context
from ciu_lo.core import values as V


class Dates(unittest.TestCase):
    def test_serial_round_trip(self):
        self.assertEqual(V.serial_to_iso(46297.0), "2026-10-02")
        self.assertEqual(V.serial_to_iso(46297.5, "datetime"), "2026-10-02 12:00")
        self.assertEqual(V.serial_to_iso(0.75, "time"), "18:00")
        self.assertEqual(V.serial_to_iso(46297.99999, "datetime"), "2026-10-03 00:00")
        for text in ("2026-10-02", "2017-09-01", "2026-10-02 12:30"):
            back = V.iso_to_serial(text)
            kind = "datetime" if " " in text else "date"
            self.assertEqual(V.serial_to_iso(back, kind), text)

    def test_named_formats(self):
        for text, want in [("2026-12-12", "2026-12-12"), ("Dec 24 2026", "2026-12-24"),
                           ("24 Dec 2026", "2026-12-24"), ("December 24, 2026", "2026-12-24"),
                           ("2026/12/24", "2026-12-24"), ("'2026-12-12", "2026-12-12")]:
            self.assertEqual(V.parse_date(text), want, text)

    def test_numeric_dates_need_a_decidable_order(self):
        self.assertEqual(V.parse_date("15/11/2026"), "2026-11-15")
        self.assertEqual(V.parse_date("11/15/2026"), "2026-11-15")
        self.assertIsNone(V.parse_date("05/06/2026"))            # 5 June or May 6?
        self.assertEqual(V.parse_date("05/06/2026", "dmy"), "2026-06-05")
        self.assertEqual(V.parse_date("05/06/2026", "mdy"), "2026-05-06")
        self.assertEqual(V.parse_date("15.11.2026"), "2026-11-15")
        for bad in ("31/02/2026", "hello", "", None, "2026", "13/13/2026"):
            self.assertIsNone(V.parse_date(bad), bad)

    def test_column_order(self):
        self.assertEqual(V.date_order(["01/02/2026", "15/02/2026", "x"]), "dmy")
        self.assertEqual(V.date_order(["01/02/2026", "02/15/2026"]), "mdy")
        self.assertIsNone(V.date_order(["01/02/2026", "03/04/2026"]))
        self.assertIsNone(V.date_order(["15/02/2026", "02/15/2026"]))   # contradicts itself

    def test_percent_text(self):
        self.assertTrue(V.is_percent_text("10%"))
        self.assertTrue(V.is_percent_text("'+4.5 %"))
        self.assertFalse(V.is_percent_text("10"))
        self.assertFalse(V.is_percent_text("ten%"))


class ApplyKinds(unittest.TestCase):
    def test_reader_kinds_convert_values(self):
        rows = [["Due", "Remote", "When"], [46297.0, 1.0, 46297.5], ["15/11/2026", 0.0, ""],
                ["Dec 24 2026", "", 46298.25], ["soon", 1.0, ""]]
        out, kinds = V.apply_kinds(rows, {0: "date", 1: "logical", 2: "datetime"})
        self.assertEqual([r[0] for r in out], ["Due", "2026-10-02", "2026-11-15", "2026-12-24", "soon"])
        self.assertEqual([r[1] for r in out], ["Remote", "TRUE", "FALSE", "", "TRUE"])
        self.assertEqual(out[1][2], "2026-10-02 12:00")
        self.assertEqual(kinds, {0: "date", 1: "logical", 2: "datetime"})
        self.assertEqual(rows[1][0], 46297.0)                 # input untouched

    def test_text_date_column_detected(self):
        rows = [["Name", "Joined"], ["a", "01/02/2026"], ["b", "15/02/2026"], ["c", "03/04/2026"]]
        out, kinds = V.apply_kinds(rows, {})
        self.assertEqual(kinds, {1: "date"})
        self.assertEqual([r[1] for r in out[1:]], ["2026-02-01", "2026-02-15", "2026-04-03"])

    def test_ambiguous_text_dates_left_alone(self):
        rows = [["Name", "Joined"], ["a", "01/02/2026"], ["b", "03/04/2026"], ["c", "05/06/2026"]]
        out, kinds = V.apply_kinds(rows, {})
        self.assertEqual(kinds, {})
        self.assertEqual(out, rows)

    def test_codes_and_numbers_not_mistaken_for_dates(self):
        rows = [["Code", "Qty"], ["00123", 4.0], ["2026", 5.0], ["A-1", 6.0]]
        out, kinds = V.apply_kinds(rows, {})
        self.assertEqual(kinds, {})


class Totals(unittest.TestCase):
    def table(self, rows):
        return context.Table(context.Snapshot("d", "S", 0, 0, rows))

    def test_subtotal_label_outside_label_column(self):
        rows = [["ID", "Name", "Dept", "Salary"],
                ["007", "Ann", "Sales", 48000.0], ["012", "Bo", "Sales", 52000.0],
                ["", "Sales subtotal", "", 100000.0],
                ["003", "Cy", "Eng", 72000.0], ["021", "Di", "Eng", 64000.0],
                ["", "Engineering subtotal", "", 136000.0]]
        t = self.table(rows)
        self.assertEqual(sorted(t.totals), [2, 5])
        out = context.render(t.snapshot)
        self.assertIn("max 72000 (row 5: 003)", out)
        self.assertIn("Total rows (left out of the statistics): row 4, row 7", out)

    def test_word_total_in_an_ordinary_row_is_not_a_total(self):
        rows = [["Item", "Note", "Cost"], ["Tea", "total paid by card", 2.0],
                ["Milk", "ok", 1.5], ["Jam", "ok", 3.0]]
        self.assertEqual(self.table(rows).totals, set())

    def test_labels(self):
        for label in ("Total", "Grand total", "Sales subtotal", "Sub-total:", "TOTALS", "World",
                      "Total Q4"):
            self.assertTrue(context._is_total_label(label), label)
        for label in ("Totally Fresh Ltd", "Subtotalia", "total paid by card at the till today",
                      "Acme"):
            self.assertFalse(context._is_total_label(label), label)


class Kinds(unittest.TestCase):
    def test_date_statistics(self):
        rows = [["Invoice", "Due"], ["A", "2026-10-02"], ["B", "2026-12-12"], ["C", "2026-09-01"]]
        snap = context.Snapshot("d", "S", 0, 0, rows)
        line = [l for l in context.render(snap).splitlines() if l.startswith("- B")][0]
        self.assertIn("date; earliest 2026-09-01 (row 4: C), latest 2026-12-12 (row 3: B)", line)
        self.assertEqual(context.Table(snap).column_kind(1), "date")

    def test_display_kinds_noted(self):
        rows = [["Item", "VAT", "Remote"], ["a", 0.2, "TRUE"], ["b", 0.05, "FALSE"]]
        snap = context.Snapshot("d", "S", 0, 0, rows, kinds={1: "percent", 2: "logical"})
        out = context.render(snap)
        self.assertIn("stored as fractions and shown as percentages (0.2 shows as 20%)", out)
        self.assertIn("TRUE/FALSE values", out)
        t = context.Table(snap)
        self.assertEqual([t.column_kind(c) for c in range(3)], [None, "percent", "logical"])


if __name__ == "__main__":
    unittest.main()
