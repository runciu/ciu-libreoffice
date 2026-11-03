import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))

from ciu_lo.core import clean as C
from ciu_lo.core import context

ROWS = [["Invoice", "Client", "Paid", "Amount", "Notes"],
        ["I1", "Acme", "Yes", 10.0, "first order"], ["I2", " acme ", "Y", "TBC", "awaiting quote"],
        ["I3", "Bright", "no", "£1,200.50", "late"], ["I4", "Bright", "No", 3.0, "ok"],
        ["I5", "Brite", "settled", "nil", "called twice"], ["I6", "Acme", "", 2.0, "?"],
        ["I7", "Acme", "yse", 4.0, "fine"], ["I8", "Bright", "maybe", 1.0, "check"]]

REPLIES = {
    "client": {"kind": "name", "rules": [{"from": "Brite", "to": "Bright"}]},
    "paid": {"kind": "yes/no", "standard": ["Yes", "No"], "blank_means": "No",
             "rules": [{"from": "Y", "to": "Yes"}, {"from": "yse", "to": "Yes"},
                       {"from": "settled", "to": "Yes"}]},
    "amount": {"kind": "amount", "rules": [{"from": "TBC", "to": ""}, {"from": "nil", "to": "0"}]},
}


def table(rows=ROWS):
    return context.Table(context.Snapshot("d", "S", 0, 0, rows))


def decide(t, replies=REPLIES):
    return [C.parse(json.dumps(replies.get(k.name, {"kind": "other", "rules": []})), k)
            for k in C.candidates(t)[0]]


class Candidates(unittest.TestCase):
    def test_columns_offered(self):
        cands = {k.name: k for k in C.candidates(table())[0]}
        self.assertEqual(sorted(cands), ["amount", "client", "paid"])
        self.assertEqual(cands["paid"].empty, 1)
        self.assertTrue(cands["amount"].numeric)
        self.assertEqual(cands["amount"].values, [("TBC", 1), ("nil", 1)])

    def test_ids_free_text_codes_and_dates_not_offered(self):
        rows = [["ID", "Due", "Comment"]] + [
            ["X%d" % i, "2026-10-%02d" % (i + 1), "note number %d" % i] for i in range(6)]
        snap = context.Snapshot("d", "S", 0, 0, rows, kinds={1: "date"})
        self.assertEqual(C.candidates(context.Table(snap))[0], [])
        rows = [["Account", "Grade"], ["00123", "A1"], ["00123", "B2"], ["00456", "A1"]]
        self.assertEqual(C.candidates(table(rows))[0], [])


class Choosing(unittest.TestCase):
    def test_consistent_columns_not_sent(self):
        rows = [["Client", "Country", "Paid"], ["Acme", "UK", "Yes"], ["Bright", "France", "No"],
                ["Acme", "UK", "settled"], ["Bright", "France", "No"]]
        cands, consistent = C.candidates(table(rows))
        self.assertEqual([k.name for k in cands], ["paid"])
        self.assertEqual(consistent, ["client", "country"])
        cands, consistent = C.candidates(table(rows), ask_all=True)
        self.assertEqual([k.name for k in cands], ["client", "country", "paid"])
        text = C.Cleaning(table(rows), consistent=["client", "country"]).describe()
        self.assertIn("Looked consistent, not sent to CIU: client, country.", text)

    def test_look_alikes_and_markers_are_sent(self):
        rows = [["City"], ["London"], ["Lodnon"], ["London"], ["Paris"], ["Paris"], ["Leeds"],
                ["Leeds"]]
        self.assertEqual([k.name for k in C.candidates(table(rows))[0]], ["city"])
        rows = [["Region"], ["North"], ["N/A"], ["North"], ["South"], ["N/A"], ["South"]]
        self.assertEqual([k.name for k in C.candidates(table(rows))[0]], ["region"])

    def test_long_column_sends_only_look_alikes(self):
        names = ["Town %s" % w for w in ("Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir",
                 "Ginkgo", "Hazel", "Ivy", "Juniper", "Kapok", "Larch", "Maple", "Nutmeg", "Oak",
                 "Pine", "Quince", "Rowan", "Spruce", "Teak", "Upas", "Vine", "Willow", "Yew",
                 "Zelkova", "Acacia", "Banyan", "Cypress", "Date", "Ebony", "Fig", "Gum")]
        rows = [["Town"]] + [[n] for n in names for _ in range(2)] + [["Town Mapel"]]
        cands, _ = C.candidates(table(rows))
        self.assertEqual(len(cands), 1)
        self.assertEqual(sorted(v for v, _ in cands[0].values), ["Town Mapel", "Town Maple"])
        self.assertEqual(cands[0].hidden, 31)
        user = C.messages(cands[0])[1]["content"]
        self.assertIn("Values that look alike or like missing markers (the column has 31 other "
                      "values that look distinct): 'Town Maple' x2, 'Town Mapel' x1", user)
        d = C.parse(json.dumps({"kind": "category", "standard": ["Town Maple"],
                                "rules": [{"from": "Town Mapel", "to": "Town Maple"}]}), cands[0])
        self.assertEqual(d.unresolved(), [])

    def test_look_alikes(self):
        groups = C.lookalikes([("London", 9), ("Lodnon", 1), ("Paris", 5), ("paris.", 1),
                               ("Leeds", 3), ("Lima", 2), ("Unit 4", 2), ("Unit 5", 2)])
        self.assertEqual(groups, [[("London", 9), ("Lodnon", 1)], [("Paris", 5), ("paris.", 1)]])


class Messages(unittest.TestCase):
    def test_one_column_per_request_with_rows_for_rare_values(self):
        cands = {k.name: k for k in C.candidates(table())[0]}
        msgs = C.messages(cands["amount"], "TBC is unknown")
        self.assertEqual(msgs[0]["content"], C.NUMBER_SYSTEM)
        user = msgs[1]["content"]
        self.assertIn("Column: Amount (a number column)\nText values: 'TBC' x1, 'nil' x1", user)
        self.assertIn("- 'TBC' in row 3: Invoice I2 | Client  acme  | Paid Y | Notes awaiting quote", user)
        self.assertIn("Instructions from the user: TBC is unknown", user)
        msgs = C.messages(cands["paid"])
        self.assertEqual(msgs[0]["content"], C.TEXT_SYSTEM)
        self.assertIn("'yse' x1", msgs[1]["content"])
        self.assertIn("empty x1", msgs[1]["content"])
        self.assertTrue(C.fits(cands["paid"], 4096))
        self.assertFalse(C.fits(cands["paid"], 300))


class Parsing(unittest.TestCase):
    def cand(self, name):
        return {k.name: k for k in C.candidates(table())[0]}[name]

    def test_rules_checked_against_the_column(self):
        reply = {"kind": "yes/no", "standard": ["Yes", "No", "yes"], "blank_means": "Unpaid",
                 "rules": [{"from": "Y", "to": "yes"}, {"from": "perhaps", "to": "No"},
                           {"from": "maybe", "to": "Unsure"}, {"from": "No", "to": "No"}]}
        d = C.parse(json.dumps(reply), self.cand("paid"))
        self.assertEqual(d.kind, "yes/no")
        self.assertEqual(d.standard, ["Yes", "No"])
        self.assertEqual(d.rules, {"y": "Yes"})
        self.assertIsNone(d.blank)
        self.assertIn(("perhaps", "not a value in the column"), d.rejected)
        self.assertIn(("maybe", "'Unsure' is not one of the column's values"), d.rejected)
        self.assertIn(("empty", "'Unpaid' is not one of the column's values"), d.rejected)
        self.assertEqual([v for v, _ in d.unresolved()], ["maybe", "settled", "yse"])

    def test_number_column(self):
        reply = {"kind": "category", "rules": [{"from": "TBC", "to": ""}, {"from": "nil", "to": "zero"}]}
        d = C.parse(json.dumps(reply), self.cand("amount"))
        self.assertEqual(d.kind, "amount")
        self.assertEqual(d.rules, {"tbc": None})
        self.assertIn(("nil", "'zero' is not a number"), d.rejected)

    def test_names_map_only_to_listed_values(self):
        reply = {"kind": "name", "rules": [{"from": "Brite", "to": "Bright"},
                                           {"from": "Acme", "to": "Acme Ltd"}]}
        d = C.parse(json.dumps(reply), self.cand("client"))
        self.assertEqual(d.rules, {"brite": "Bright"})

    def test_unreadable_reply(self):
        for raw in ("", "not json", "[1, 2]"):
            d = C.parse(raw, self.cand("paid"))
            self.assertEqual((d.rules, d.blank), ({}, None))


class Spelling(unittest.TestCase):
    def test_typos_and_abbreviations(self):
        for old, new in [("yse", "Yes"), ("Y", "Yes"), ("Lodnon", "London"), ("compl.", "Complete"),
                         ("done.", "Done"), ("Engneering", "Engineering"), ("N", "No")]:
            self.assertTrue(C.is_spelling(old, new), (old, new))

    def test_changes_of_meaning(self):
        for old, new in [("settled", "Yes"), ("TBC", None), ("nil", 0.0), ("Brite", "Bright"),
                         ("Paris", "London"), ("no", "Yes"), ("Unit 4", "Unit 5")]:
            self.assertFalse(C.is_spelling(old, new), (old, new))


class Cleaning(unittest.TestCase):
    def test_copy_and_grouped_summary(self):
        t = table()
        cl = C.Cleaning(t, decide(t))
        self.assertEqual([r[1:4] for r in cl.result.rows], [
            ("Acme", "Yes", 10.0), ("Acme", "Yes", None), ("Bright", "No", 1200.5),
            ("Bright", "No", 3.0), ("Bright", "Yes", 0.0), ("Acme", "No", 2.0),
            ("Acme", "Yes", 4.0), ("Bright", "maybe", 1.0)])
        text = cl.describe()
        self.assertIn("How CIU read the columns: client: name; paid: yes/no (Yes, No); "
                      "amount: amount.", text)
        self.assertIn("Check these (CIU's reading of what values mean):\n"
                      "  client: 'Brite' to 'Bright'\n  paid: 'settled' to 'Yes'\n"
                      "  paid: empty cells read as 'No' (1 cell)\n"
                      "  amount: 'TBC' to empty, 'nil' to '0'", text)
        self.assertIn("Spelling and abbreviations:\n  paid: 'Y' to 'Yes', 'yse' to 'Yes'", text)
        self.assertIn("Unresolved, left as they are:\n  paid: 'maybe' x1 (not one of 'Yes', 'No')", text)
        self.assertIn("Trimmed spaces in client (1 cell).", text)
        self.assertIn("amount: 1 number stored as text becomes a number.", text)
        self.assertNotIn("Changed in", text)
        self.assertEqual(cl.result.headers, ["Invoice", "Client", "Paid", "Amount", "Notes"])

    def test_rejected_and_skipped_reported(self):
        t = table()
        cands = C.candidates(t)[0]
        d = C.parse(json.dumps({"kind": "yes/no", "standard": ["Yes", "No"],
                                "rules": [{"from": "perhaps", "to": "No"}]}), cands[1])
        text = C.Cleaning(t, [d], skipped=[cands[2]]).describe()
        self.assertIn("Rules not used:\n  paid 'perhaps': not a value in the column", text)
        self.assertIn("Not checked: amount", text)

    def test_nothing_to_clean(self):
        rows = [["Item", "Cost"], ["Tea", 2.0], ["Milk", 1.5], ["Tea", 2.0]]
        self.assertFalse(C.Cleaning(table(rows)).changes)

    def test_totals_not_copied(self):
        rows = [["Item", "Cost"], ["Tea", 2.0], ["milk", 1.5], ["Milk", 1.0], ["Total", 4.5]]
        cl = C.Cleaning(table(rows))
        self.assertEqual(len(cl.result.rows), 3)
        self.assertIn("Total rows are not copied: row 5: Total.", cl.describe())


if __name__ == "__main__":
    unittest.main()
