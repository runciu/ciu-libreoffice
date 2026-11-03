import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))

from ciu_lo.core import context, prompts
from ciu_lo.core import query as Q

ROWS = [
    ["Country", "Pop 2022", "Pop 2023", "Change", "Region"],
    ["Algeria", 45477389.0, 46164219.0, "+1.51%", "Africa"],
    ["Brazil", 210306415.0, 211140729.0, "+0.40%", "Americas"],
    ["Bulgaria", 6825864.0, 6795803.0, "−0.44%", "Europe"],
    ["Chad", 18455316.0, 19319064.0, "'+4.68%", "Africa"],
    ["China", 1425179569.0, 1422584933.0, "−0.18%", "Asia"],
    ["Egypt", 110990103.0, 112716598.0, "+1.56%", "Africa"],
    ["India", 1417173173.0, 1428627663.0, "+0.81%", "Asia"],
    ["Nigeria", 218541212.0, 223804632.0, "+2.41%", "Africa"],
    ["Tuvalu", 11312.0, 11396.0, "+0.74%", "Oceania"],
]


def snapshot(rows=ROWS, start_col=0, start_row=0):
    rows = [list(r) for r in rows]
    return context.Snapshot("list.xlsx", "Countries", start_col, start_row, rows)


def with_world():
    rows = [list(r) for r in ROWS]
    rows.append(["World", sum(r[1] for r in ROWS[1:]), sum(r[2] for r in ROWS[1:]), "+0.8%", ""])
    return snapshot(rows)


def run(spec, snap=None):
    spec = dict(spec, query_needed=True)
    q = Q.parse(json.dumps(spec), snap or snapshot())
    return q.run()


class Numbers(unittest.TestCase):
    def test_to_number(self):
        cases = [("+4.68%", 0.0468), ("'+4.68%", 0.0468), ("\u22120.44%", -0.0044),
                 ("1,234", 1234.0), (" 7 ", 7.0), (3.0, 3.0), ("\u00a31,200.50", 1200.5),
                 ("(120.00)", -120.0), ("-\u00a35", -5.0), ("\u00a3-5", -5.0), ("$ 3.5", 3.5),
                 ("0", 0.0), ("0.25", 0.25), ("\u20ac1,000", 1000.0), ("1e3", 1000.0)]
        for text, want in cases:
            self.assertAlmostEqual(Q.to_number(text), want, msg=repr(text))
        for text in ("Africa", "", None, "1.2.3", "nan", "inf", "00123", "007", "1,5",
                     "\u00a3", "()", "%", "+", "12,34,5", "TBC", "1 000"):
            self.assertIsNone(Q.to_number(text), repr(text))


class Expressions(unittest.TestCase):
    def value(self, expr, row):
        return Q._Parser(expr, lambda name: ord(name) - 65).parse()(row)

    def test_precedence_and_brackets(self):
        row = [2.0, 3.0, 4.0]
        self.assertEqual(self.value("A + B * C", row), 14.0)
        self.assertEqual(self.value("(A + B) * C", row), 20.0)
        self.assertEqual(self.value("-A + 10", row), 8.0)
        self.assertEqual(self.value("(C - B) / B * 100", row), (4 - 3) / 3 * 100)

    def test_division_by_zero_and_text(self):
        self.assertIsNone(self.value("A / B", [1.0, 0.0]))
        self.assertIsNone(self.value("A + B", [1.0, "Africa"]))

    def test_rejects_anything_else(self):
        for bad in ("A ** B", "__import__('os')", "A;B", "(A + B", "A B", "", "A %"):
            with self.assertRaises(Q.QueryError, msg=bad):
                self.value(bad, [1.0, 2.0])


class Running(unittest.TestCase):
    def test_largest_increase_in_africa(self):
        out = run({"filters": [{"column": "E", "op": "=", "value": "africa"}],
                   "value": "C - B", "sort": "desc", "limit": 3})
        lines = out.splitlines()
        self.assertIn("Matched 4 of 9 rows.", out)
        self.assertTrue(lines[2].startswith("1. row 9: Nigeria"), out)
        self.assertIn("value 5263420", lines[2])
        self.assertTrue(lines[3].startswith("2. row 7: Egypt"))
        self.assertIn("(1 more not shown)", out)

    def test_percentage_text_column(self):
        out = run({"value": "D", "sort": "desc", "limit": 2})
        self.assertIn("1. row 5: Chad", out)
        out = run({"value": "D", "sort": "asc", "limit": 1})
        self.assertIn("1. row 4: Bulgaria", out)
        self.assertIn("value -0.0044", out)

    def test_numeric_filter_on_text_percentages(self):
        out = run({"filters": [{"column": "D", "op": "<", "value": 0}], "aggregate": "count"})
        self.assertIn("Count: 2", out)

    def test_aggregates(self):
        africa = [{"column": "E", "op": "=", "value": "Africa"}]
        self.assertIn("Sum: 393464020 (over 4 values)",
                      run({"filters": africa, "value": "B", "aggregate": "sum"}))
        self.assertIn("Max: 223804632 (row 9: Nigeria)",
                      run({"filters": africa, "value": "C", "aggregate": "max"}))
        self.assertIn("Median: ", run({"value": "B", "aggregate": "median"}))
        self.assertIn("Count: 4", run({"filters": africa, "aggregate": "count"}))

    def test_total_rows_excluded(self):
        out = run({"value": "B", "sort": "desc", "limit": 1}, with_world())
        self.assertIn("1. row 6: China", out)
        self.assertIn("Total rows were left out.", out)

    def test_contains_and_not_equal(self):
        out = run({"filters": [{"column": "A", "op": "contains", "value": "IN"},
                               {"column": "E", "op": "!=", "value": "Asia"}],
                   "value": "B", "sort": "desc"})
        self.assertIn("Matched 0 of 9 rows.", out)
        out = run({"filters": [{"column": "A", "op": "contains", "value": "ch"}]})
        self.assertIn("Matched 2 of 9 rows.", out)

    def test_offset_selection_uses_sheet_letters(self):
        out = run({"filters": [{"column": "G", "op": "=", "value": "Africa"}],
                   "value": "E - D", "sort": "desc", "limit": 1},
                  snapshot(start_col=2, start_row=4))
        self.assertIn("1. row 13: Nigeria", out)
        self.assertIn("D 218541212", out)

    def test_limit_capped(self):
        q = Q.parse(json.dumps({"query_needed": True, "value": "B", "limit": 500}), snapshot())
        self.assertEqual(q.limit, Q.MAX_LIMIT)


class Parsing(unittest.TestCase):
    def test_no_query(self):
        for raw in ('{"query_needed": false}', "", "no json", '{"summary": "x"}', "{broken"):
            self.assertIsNone(Q.parse(raw, snapshot()), raw)

    def test_bad_queries_raise(self):
        cases = [{"filters": [{"column": "Z", "op": "=", "value": 1}]},
                 {"filters": [{"column": "A", "op": "~", "value": 1}]},
                 {"value": "B +"},
                 {"aggregate": "sum"},
                 {"filters": [{"column": "Sales!A", "op": "=", "value": 1}]}]
        for spec in cases:
            with self.assertRaises(Q.QueryError, msg=spec):
                Q.parse(json.dumps(dict(spec, query_needed=True)), snapshot())

    def test_instruction_example_is_valid(self):
        example = Q.INSTRUCTIONS.split("as JSON:\n")[1].split("\n")[0]
        rows = [["A", "B", "C", "D", "E"], ["x", 1.0, 2.0, "1%", "Africa"]]
        Q.parse(example, snapshot(rows)).run()


class Prompting(unittest.TestCase):
    def test_query_turn_fits_window(self):
        rows = [ROWS[0]] + [["Country %d" % i, 1e8 + i * 7919.0, 1e8 + i * 8111.0, "+1.00%", "Africa"]
                            for i in range(300)]
        snap = snapshot(rows)
        for n_ctx in (2048, 4096, 8192):
            msgs, max_tokens = prompts.build_messages(snap, "largest increase in Africa?", n_ctx,
                                                      engine=Q)
            result = prompts.fit_result(run({"value": "C - B", "sort": "desc", "limit": 20}, snap))
            full = prompts.follow_up(msgs, '{"query_needed": true, "value": "C - B"}', result)
            used = sum(context.estimate_tokens(m["content"]) for m in full)
            self.assertLess(used + max_tokens, n_ctx, n_ctx)

    def test_follow_up_keeps_prefix(self):
        msgs, _ = prompts.build_messages(snapshot(), "q", 4096, engine=Q)
        full = prompts.follow_up(msgs, '{"query_needed": false}', None)
        self.assertEqual(full[:2], msgs)
        self.assertIn("No query needed", full[-1]["content"])


if __name__ == "__main__":
    unittest.main()
