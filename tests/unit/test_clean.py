import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))

from ciu_lo.core import clean as C
from ciu_lo.core import context

ROWS = [["Invoice", "Client", "Paid", "Amount", "Notes"],
        ["I1", "Acme", "Yes", 10.0, "first order"], ["I2", " acme ", "Y", "TBC", "rush"],
        ["I3", "Bright", "no", "£1,200.50", "late"], ["I4", "Bright", "No", 3.0, "ok"],
        ["I5", "Brite", "settled", "nil", "called twice"], ["I6", "Acme", "", 2.0, "?"]]


def table(rows=ROWS):
    return context.Table(context.Snapshot("d", "S", 0, 0, rows))


def reply(*columns):
    return json.dumps({"columns": [{"column": c, "rules": [{"from": a, "to": b} for a, b in r]}
                                   for c, r in columns]})


class Candidates(unittest.TestCase):
    def test_columns_offered(self):
        cands = {k.name: k for k in C.candidates(table())}
        self.assertEqual(sorted(cands), ["amount", "client", "paid"])
        self.assertEqual(cands["paid"].values,
                         [("No", 2), ("Y", 1), ("Yes", 1), ("settled", 1)])
        self.assertEqual(cands["paid"].empty, 1)
        self.assertTrue(cands["amount"].numeric)
        self.assertEqual(cands["amount"].values, [("TBC", 1), ("nil", 1)])

    def test_ids_free_text_and_dates_not_offered(self):
        rows = [["ID", "Due", "Comment"]] + [
            ["X%d" % i, "2026-10-%02d" % (i + 1), "note number %d" % i] for i in range(6)]
        snap = context.Snapshot("d", "S", 0, 0, rows, kinds={1: "date"})
        self.assertEqual(C.candidates(context.Table(snap)), [])
        rows = [["Account", "Grade"], ["00123", "A1"], ["00123", "B2"], ["00456", "A1"]]
        self.assertEqual(C.candidates(table(rows)), [])


class Messages(unittest.TestCase):
    def test_cell_text_only_in_user_message(self):
        msgs, kept, skipped = C.build_messages(C.candidates(table()), "TBC is unknown", 4096)
        self.assertEqual(msgs[0]["content"], C.SYSTEM)
        self.assertIn("- paid (Paid, text): 'No' x2, 'Y' x1, 'Yes' x1, 'settled' x1, empty x1",
                      msgs[1]["content"])
        self.assertIn("Instructions from the user: TBC is unknown", msgs[1]["content"])
        self.assertEqual(skipped, [])

    def test_columns_that_do_not_fit_are_reported(self):
        rows = [["Code%d" % c for c in range(12)]] + [
            ["value %d-%d" % (c, i % 25) for c in range(12)] for i in range(60)]
        cands = C.candidates(table(rows))
        msgs, kept, skipped = C.build_messages(cands, "", 2048)
        self.assertTrue(kept and skipped)
        used = sum(context.estimate_tokens(m["content"]) for m in msgs)
        self.assertLess(used + C.REPLY_TOKENS, 2048)


class Rules(unittest.TestCase):
    def test_valid_rules_kept_and_bad_ones_reported(self):
        raw = reply(("paid", [("Y", "Yes"), ("settled", "Yes"), ("maybe", "No"), ("No", "No")]),
                    ("Amount", [("TBC", ""), ("nil", "0"), ("TBC", "soon")]),
                    ("notes", [("rush", "urgent")]))
        rules, rejected = C.parse_rules(raw, C.candidates(table()))
        self.assertEqual(rules, {(2, "y"): "Yes", (2, "settled"): "Yes", (3, "tbc"): None,
                                 (3, "nil"): 0.0})
        self.assertIn(("paid", "maybe", "not a value in the column"), rejected)
        self.assertIn(("amount", "TBC", "'soon' is not a number"), rejected)

    def test_unreadable_reply(self):
        for raw in ("", "not json", "[1, 2]", '{"columns": "x"}'):
            self.assertEqual(C.parse_rules(raw, C.candidates(table())), ({}, []))


class Cleaning(unittest.TestCase):
    def test_copy_and_summary(self):
        cands = C.candidates(table())
        rules, rejected = C.parse_rules(reply(("paid", [("Y", "Yes"), ("settled", "Yes")]),
                                              ("client", [("Brite", "Bright")]),
                                              ("amount", [("TBC", "")])), cands)
        cl = C.Cleaning(table(), rules, rejected)
        self.assertEqual([r[1:4] for r in cl.result.rows], [
            ("Acme", "Yes", 10.0), ("Acme", "Yes", None), ("Bright", "No", 1200.5),
            ("Bright", "No", 3.0), ("Bright", "Yes", "nil"), ("Acme", None, 2.0)])
        text = cl.describe()
        self.assertIn("Changed in paid: 'Y' to 'Yes', 'settled' to 'Yes'.", text)
        self.assertIn("Changed in amount: 'TBC' to empty.", text)
        self.assertIn("Trimmed spaces in client (1 cell).", text)
        self.assertIn("Kept as text in number column amount: 'nil'.", text)
        self.assertIn("amount: 1 number stored as text becomes a number.", text)
        self.assertEqual(cl.result.headers, ["Invoice", "Client", "Paid", "Amount", "Notes"])

    def test_nothing_to_clean(self):
        rows = [["Item", "Cost"], ["Tea", 2.0], ["Milk", 1.5], ["Tea", 2.0]]
        self.assertFalse(C.Cleaning(table(rows), {}).changes)

    def test_totals_not_copied(self):
        rows = [["Item", "Cost"], ["Tea", 2.0], ["milk", 1.5], ["Milk", 1.0], ["Total", 4.5]]
        cl = C.Cleaning(table(rows), {})
        self.assertEqual(len(cl.result.rows), 3)
        self.assertIn("Total rows are not copied: row 5: Total.", cl.describe())


if __name__ == "__main__":
    unittest.main()
