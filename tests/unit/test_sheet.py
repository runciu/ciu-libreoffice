import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))
sys.path.insert(0, HERE)

from ciu_lo.core import context, prompts
from ciu_lo.core import plan as P
from ciu_lo.core import sql as S
from ciu_lo.core.query import QueryError
from test_query import ROWS, snapshot, with_world

MESSY = [
    ["Name", "City", "Sales"],
    ["Ann", "Leeds", 10.0],
    ["Bob", "", 7.0],
    [" ann ", "Leeds", 10.0],
    ["Cat", "York", ""],
    ["Dan", "N/A", 4.0],
    ["Bob", "Hull", 7.0],
    ["Eve", "-", 2.0],
]

DEDUPE = ("SELECT name, city, sales FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY name "
          "ORDER BY row) AS n FROM t) WHERE n = 1 ORDER BY row")


def table(rows=MESSY):
    return context.Table(snapshot(rows))


class RunToSheet(unittest.TestCase):
    def test_remove_duplicates_keeps_first(self):
        r = S.run_to_sheet(DEDUPE, table())
        self.assertEqual(r.headers, ["Name", "City", "Sales"])
        self.assertEqual([row[0] for row in r.rows], ["Ann", "Bob", "Cat", "Dan", "Eve"])
        self.assertEqual(r.source_rows, 7)

    def test_drop_missing_including_markers(self):
        sql = ("SELECT name, city, sales FROM t WHERE city IS NOT NULL AND sales IS NOT NULL "
               "AND city NOT IN ('N/A', '-') ORDER BY row")
        # City is a text column, so its markers stay text and are matched by value.
        r = S.run_to_sheet(sql, table())
        self.assertEqual([row[0] for row in r.rows], ["Ann", "Ann", "Bob"])
        self.assertIn("Merged spellings in name: 'ann' as 'Ann'.", r.notes)

    def test_values_are_calc_ready(self):
        r = S.run_to_sheet("SELECT name, sales, COUNT(*) AS n FROM t GROUP BY name ORDER BY row", table())
        values = r.values()
        self.assertEqual(values[0], ("Name", "Sales", "n"))
        cat = [v for v in values if v[0] == "Cat"][0]
        self.assertEqual(cat, ("Cat", "", 1.0))
        self.assertTrue(all(isinstance(v, (str, float)) for row in values for v in row))

    def test_totals_left_out_of_copies(self):
        r = S.run_to_sheet("SELECT country FROM t ORDER BY row", context.Table(with_world()))
        self.assertEqual(r.rows[-1][0], "Tuvalu")
        self.assertEqual(r.totals, ["row 11: World"])
        self.assertEqual(r.left_out, [])
        self.assertIn("Copies leave total rows out",
                      S.sheet_instructions(context.Table(with_world())))

    def test_percent_text_becomes_number(self):
        r = S.run_to_sheet("SELECT country, change FROM t WHERE country = 'Chad'", table(ROWS))
        self.assertEqual(r.values()[1], ("Chad", 0.0468))
        self.assertEqual(r.kinds, [None, "percent"])

    def test_headerless_selection(self):
        r = S.run_to_sheet("SELECT a, b FROM t", table([[1.0, 2.0], [3.0, 4.0]]))
        self.assertEqual(r.headers, ["A", "B"])

    def test_sandbox_still_applies(self):
        for sql in ("DELETE FROM t", "ATTACH DATABASE '/tmp/x.db' AS x", ""):
            with self.assertRaises(QueryError, msg=sql):
                S.run_to_sheet(sql, table())

    def test_row_cap(self):
        old = S.MAX_SHEET_ROWS
        S.MAX_SHEET_ROWS = 3
        try:
            with self.assertRaises(QueryError) as cm:
                S.run_to_sheet("SELECT * FROM t", table())
            self.assertIn("more than 3 rows", str(cm.exception))
        finally:
            S.MAX_SHEET_ROWS = old

    def test_examples_in_instructions_run(self):
        text = S.sheet_instructions(table())
        ran = 0
        for line in text.splitlines():
            if "SELECT name, city, sales" in line:
                ran += 1
                S.run_to_sheet(line.split(": ", 1)[1], table())
        self.assertEqual(ran, 2)


class Plans(unittest.TestCase):
    def op(self, **kw):
        raw = dict({"tool": "sql_to_sheet", "sql": DEDUPE}, **kw)
        return json.dumps({"summary": "dedupe", "operations": [raw]})

    def test_needs_permission(self):
        with self.assertRaises(P.PlanError):
            P.parse_plan(self.op())
        plan = P.parse_plan(self.op(sheet="No dupes"), sheet="Data", allow_sql=True)
        self.assertEqual(plan.operations[0].sheet, "No dupes")
        self.assertEqual(plan.n_cells, 0)
        self.assertIn("New sheet 'No dupes' from: SELECT", plan.describe())

    def test_sheet_names_sanitised(self):
        for given, want in [("Clean/ed: [v2]?", "Cleaned v2"), ("", "Cleaned"), (None, "Cleaned"),
                            ("x" * 40, "x" * 31), ("'Q1'", "Q1")]:
            plan = P.parse_plan(self.op(sheet=given), allow_sql=True)
            self.assertEqual(plan.operations[0].sheet, want, given)

    def test_must_be_alone_and_have_sql(self):
        both = json.dumps({"summary": "x", "operations": [
            {"tool": "sql_to_sheet", "sql": "SELECT 1"},
            {"tool": "set_formula", "target": "A1", "formula": "=1"}]})
        with self.assertRaises(P.PlanError):
            P.parse_plan(both, allow_sql=True)
        with self.assertRaises(P.PlanError):
            P.parse_plan(self.op(sql="  "), allow_sql=True)

    def test_schema(self):
        self.assertNotIn("sql_to_sheet", json.dumps(P.plan_schema()))
        self.assertIn("sql_to_sheet", json.dumps(P.plan_schema(sql=True)))
        self.assertEqual(P.PLAN_SCHEMA, P.plan_schema())


class Prompting(unittest.TestCase):
    def test_tool_offered_only_with_sql(self):
        msgs, _ = prompts.build_action_messages(snapshot(MESSY), "remove duplicates", 4096)
        self.assertNotIn("sql_to_sheet", msgs[0]["content"])
        msgs, _ = prompts.build_action_messages(snapshot(MESSY), "remove duplicates", 4096, sql=S)
        system = msgs[0]["content"]
        self.assertIn("sql_to_sheet", system)
        self.assertLess(system.index("sql_to_sheet"), system.index("Rules:"))
        user = msgs[1]["content"]
        self.assertIn("name (Name; values: ", user)
        self.assertIn("sales (Sales)", user)
        self.assertIn("First rows of t, as stored: (row=2, name='Ann', city='Leeds', sales=10)", user)
        self.assertNotIn("Leeds", system)

    def test_fits_window(self):
        rows = [ROWS[0]] + [["Country %d" % i, 1e8 + i, 1e8 + i * 2, "+1.0%", "Africa"]
                            for i in range(400)]
        for n_ctx in (2048, 4096, 8192):
            msgs, max_tokens = prompts.build_action_messages(snapshot(rows), "drop NA", n_ctx, sql=S)
            used = sum(context.estimate_tokens(m["content"]) for m in msgs)
            self.assertLess(used + max_tokens, n_ctx, n_ctx)

    def test_text_percentages_noted(self):
        out = context.render(snapshot(ROWS))
        line = [l for l in out.splitlines() if l.startswith("- D (Change)")][0]
        self.assertIn("Calc reads '10%' as 0.1, so do not divide by 100", line)
        line = [l for l in out.splitlines() if l.startswith("- B (Pop 2022)")][0]
        self.assertNotIn("percentages", line)

    def test_missing_markers_in_stats(self):
        out = context.render(snapshot(MESSY))
        self.assertIn("B (City): text; 1 missing; 2 look like missing-value markers ('-' x1, 'N/A' x1)", out)


if __name__ == "__main__":
    unittest.main()
