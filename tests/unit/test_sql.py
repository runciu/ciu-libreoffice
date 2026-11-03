import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))
sys.path.insert(0, HERE)

from ciu_lo.core import context, engine, prompts
from ciu_lo.core import query as Q
from ciu_lo.core import sql as S
from test_query import ROWS, snapshot, with_world


def run(sql, snap=None):
    q = S.parse(json.dumps({"sql_needed": True, "sql": sql}), snap or snapshot())
    return q.run()


def fails(test, sql, needle="", snap=None):
    with test.assertRaises(Q.QueryError, msg=sql) as cm:
        run(sql, snap)
    test.assertIn(needle, str(cm.exception))


class Results(unittest.TestCase):
    def test_largest_increase_in_africa(self):
        out = run("SELECT a, c - b AS increase FROM t WHERE LOWER(e) = 'africa' "
                  "ORDER BY increase DESC LIMIT 2")
        lines = out.splitlines()
        self.assertEqual(lines[1], "2 rows:")
        self.assertEqual(lines[2], "a | increase")
        self.assertEqual(lines[3], "Nigeria | 5263420")
        self.assertEqual(lines[4], "Egypt | 1726495")

    def test_group_by(self):
        out = run("SELECT e, COUNT(*), SUM(b) FROM t GROUP BY e ORDER BY SUM(b) DESC")
        self.assertIn("Asia | 2 | 2842352742", out)
        self.assertIn("Africa | 4 | 393464020", out)

    def test_percent_text_is_numeric(self):
        out = run("SELECT a, d FROM t ORDER BY d LIMIT 1")
        self.assertIn("Bulgaria | -0.44", out)
        self.assertIn("COUNT(*)\n2", run("SELECT COUNT(*) FROM t WHERE d < 0").replace(" ", ""))

    def test_text_in_number_column_is_null(self):
        rows = [["Item", "Price", "Note"], ["a", 7.0, "ok"], ["b", 7.0, "ok"], ["c", 7.2, "-"],
                ["d", 6.9, "ok"], ["e", "-", "late"], ["f", "N/A", "ok"]]
        snap = snapshot(rows)
        self.assertIn("\n7.025", run("SELECT AVG(b) FROM t", snap))
        self.assertIn("\n2", run("SELECT COUNT(*) FROM t WHERE b IS NULL", snap))
        self.assertIn("\n1", run("SELECT COUNT(*) FROM t WHERE c = '-'", snap))

    def test_row_column_is_sheet_row(self):
        self.assertIn("9 | Nigeria", run("SELECT row, a FROM t WHERE a = 'Nigeria'"))

    def test_totals_excluded(self):
        out = run("SELECT a FROM t ORDER BY b DESC LIMIT 1", with_world())
        self.assertIn("\nChina", out)
        self.assertIn("Total rows were left out.", out)
        self.assertIn("\n0", run("SELECT COUNT(*) FROM t WHERE a = 'World'", with_world()))

    def test_offset_selection_uses_sheet_letters(self):
        snap = snapshot(start_col=2, start_row=4)
        self.assertIn("Nigeria | 5263420",
                      run("SELECT c, e - d FROM t WHERE g = 'Africa' ORDER BY 2 DESC LIMIT 1", snap))

    def test_keyword_letters_renamed(self):
        rows = [["H%d" % i for i in range(80)], [float(i) for i in range(80)]]
        names = S.column_names(context.Table(snapshot(rows)))
        self.assertIn("as_", names)
        self.assertIn("by_", names)
        self.assertEqual(run("SELECT as_, by_ FROM t", snapshot(rows)).splitlines()[-1], "44 | 76")

    def test_result_capped(self):
        rows = [["n", "v"]] + [["x%d" % i, float(i)] for i in range(100)]
        out = run("SELECT * FROM t", snapshot(rows))
        self.assertIn("20 rows:", out)
        self.assertIn("(more rows not shown)", out)

    def test_cte_allowed(self):
        out = run("WITH a AS (SELECT e, AVG(d) g FROM t GROUP BY e) SELECT e FROM a ORDER BY g DESC LIMIT 1")
        self.assertIn("\nAfrica", out)


class Sandbox(unittest.TestCase):
    def test_writes_denied(self):
        for sql in ("DELETE FROM t", "UPDATE t SET b = 0", "INSERT INTO t (a) VALUES ('x')",
                    "DROP TABLE t", "CREATE TABLE x (y)", "CREATE TEMP VIEW v AS SELECT 1",
                    "REPLACE INTO t (a) VALUES (1)"):
            fails(self, sql, "not authorized")

    def test_files_and_pragmas_denied(self):
        path = os.path.join(tempfile.mkdtemp(), "leak.db")
        fails(self, "ATTACH DATABASE '%s' AS x" % path)
        self.assertFalse(os.path.exists(path))
        for sql in ("PRAGMA table_info(t)", "PRAGMA writable_schema = 1", "VACUUM INTO '%s'" % path,
                    "DETACH DATABASE main", "BEGIN", "SAVEPOINT a"):
            fails(self, sql)
        self.assertFalse(os.path.exists(path))

    def test_extension_loading_denied(self):
        fails(self, "SELECT load_extension('/tmp/evil')", "not authorized")
        fails(self, "SELECT readfile('/etc/passwd')")

    def test_one_statement_only(self):
        fails(self, "SELECT 1; DELETE FROM t", "one statement")

    def test_runaway_queries_stopped(self):
        rows = [["n", "v"]] + [["x%d" % i, float(i)] for i in range(300)]
        start = time.monotonic()
        fails(self, "SELECT COUNT(*) FROM t a, t b, t c, t d", "longer than", snapshot(rows))
        fails(self, "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) "
                    "SELECT MAX(x) FROM r", "longer than")
        self.assertLess(time.monotonic() - start, 3 * S.TIME_LIMIT + 2)

    def test_schema_tables_hidden_from_writes(self):
        fails(self, "UPDATE sqlite_master SET sql = ''")

    def test_syntax_error_reported(self):
        fails(self, "SELEC a FROM t", "SQLite error")
        fails(self, "SELECT nope FROM t", "no such column")
        fails(self, "", "Empty SQL")


class Parsing(unittest.TestCase):
    def test_no_query(self):
        for raw in ('{"sql_needed": false}', "", "text", '{"query_needed": true}'):
            self.assertIsNone(S.parse(raw, snapshot()), raw)

    def test_instructions_list_columns(self):
        text = S.instructions(context.Table(snapshot()))
        self.assertIn("columns: row (sheet row number), a (Country), b (Pop 2022)", text)
        example = text.split("as JSON:\n")[1].split("\n")[0]
        self.assertEqual(json.loads(example)["sql_needed"], True)


class Routing(unittest.TestCase):
    def test_wants_change(self):
        self.assertTrue(prompts.wants_change('{"change_request": true}'))
        self.assertTrue(prompts.wants_change('ok {"change_request": true, "sql_needed": false}'))
        for reply in ('{"change_request": false}', '{"sql_needed": true, "sql": "SELECT 1"}',
                      '{"change_request": "yes"}', "", "not json", None):
            self.assertFalse(prompts.wants_change(reply), reply)

    def test_hint_and_schema(self):
        for eng in (S, Q):
            msgs, _ = prompts.build_messages(snapshot(), "add a column", 4096, engine=eng)
            self.assertIn('{"change_request": true}', msgs[0]["content"])
            self.assertIn("change_request", eng.SCHEMA["properties"])
        msgs, _ = prompts.build_messages(snapshot(), "q", 4096)
        self.assertNotIn("change_request", msgs[0]["content"])
        self.assertIn("Formulas listed under Formulas are exact", msgs[0]["content"])


class Fallback(unittest.TestCase):
    def test_engine_choice(self):
        self.assertIs(engine.current(), S)
        self.assertIn("SQL (SQLite", engine.describe())
        S.AVAILABLE = False
        try:
            self.assertIs(engine.current(), Q)
            self.assertIn("built-in queries", engine.describe())
        finally:
            S.AVAILABLE = True

    def test_prompt_fits_with_sql_engine(self):
        rows = [ROWS[0]] + [["Country %d" % i, 1e8 + i * 7919.0, 1e8 + i * 8111.0, "+1.00%", "Africa"]
                            for i in range(300)]
        snap = snapshot(rows)
        for n_ctx in (2048, 4096, 8192):
            msgs, max_tokens = prompts.build_messages(snap, "largest increase?", n_ctx, engine=S)
            bad = '{"sql_needed": true, "sql": "SELECT a, c - b FROM t ORDER BY 2 DESC LIMIT 20"}'
            msgs = prompts.retry_turn(msgs, bad, "SQLite error: no such column: zz")
            result = prompts.fit_result(run("SELECT a, c - b FROM t ORDER BY 2 DESC LIMIT 20", snap))
            full = prompts.follow_up(msgs, bad, result)
            used = sum(context.estimate_tokens(m["content"]) for m in full)
            self.assertLess(used + max_tokens, n_ctx, n_ctx)


if __name__ == "__main__":
    unittest.main()
