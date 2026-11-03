import math
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "python", "pythonpath"))
sys.path.insert(0, os.path.join(HERE, ".."))

from ciu_lo.core import context, prompts
from ciu_lo.core.ciu_client import CIUClient, CIUError, ThinkFilter
import mock_ciu


def sales_snapshot(n=4):
    rows = [["Region", "Revenue", "Customer"]]
    for i in range(n):
        rows.append(["North" if i % 2 else "South", float(100 + i * 10), "C%d" % (i % 3)])
    formulas = [[str(v) for v in r] for r in rows]
    return context.Snapshot("sales.ods", "Sales", 0, 0, rows, formulas)


class Addressing(unittest.TestCase):
    def test_letters(self):
        for idx, want in [(0, "A"), (25, "Z"), (26, "AA"), (51, "AZ"), (52, "BA"), (701, "ZZ"), (702, "AAA")]:
            self.assertEqual(context.col_letter(idx), want)

    def test_range_ref(self):
        snap = context.Snapshot("d", "S", 1, 4, [[1.0, 2.0]] * 3)
        self.assertEqual(snap.range_ref(), "B5:C7")
        self.assertEqual(context.Snapshot("d", "S", 2, 0, [[1.0]]).range_ref(), "C1")


class Analysis(unittest.TestCase):
    def test_header_detected_over_numbers(self):
        self.assertTrue(context.detect_header(sales_snapshot().rows))

    def test_no_header_for_numeric_first_row(self):
        self.assertFalse(context.detect_header([[1.0, 2.0], [3.0, 4.0]]))

    def test_all_text_table_header(self):
        rows = [["Name", "City"], ["Ann", "Leeds"], ["Bo", "York"]]
        self.assertTrue(context.detect_header(rows))
        rows = [["Ann", "Leeds"], ["Bo", "Leeds"]]
        self.assertFalse(context.detect_header(rows))

    def test_profile_stats_missing_and_duplicates(self):
        rows = [["ID", "Amount"], ["a", 1.5], ["b", ""], ["a", 2.5], ["c", 0.1], ["d", 0.2]]
        header, profs, _ = context.profile(context.Snapshot("d", "S", 0, 0, rows))
        self.assertTrue(header)
        ids, amt = profs
        self.assertEqual(ids.duplicates, 1)
        self.assertEqual(amt.missing, 1)
        self.assertEqual(amt.kind, "number")
        self.assertEqual(context.fmt(amt.sum), "4.3")
        self.assertIn("A (ID)", ids.name)

    def test_mixed_column(self):
        p = context.ColumnProfile(0, "A", [1.0, "x", 2.0])
        self.assertTrue(p.kind.startswith("mixed"))

    def test_fmt(self):
        self.assertEqual(context.fmt(3.0), "3")
        self.assertEqual(context.fmt(0.1 + 0.2), "0.3")
        self.assertEqual(context.fmt(None), "-")


class Rendering(unittest.TestCase):
    def test_contains_key_parts(self):
        out = context.render(sales_snapshot(), budget=4000)
        self.assertIn("Sheet: Sales", out)
        self.assertIn("Range: A1:C5", out)
        self.assertIn("B (Revenue)", out)
        self.assertIn("sum 460", out)
        self.assertIn("\n2\tSouth\t100\tC0", out)

    def test_budget_is_respected_for_many_sizes(self):
        big = sales_snapshot(1500)
        for budget in (100, 300, 800, 2000, 6000):
            self.assertLessEqual(context.estimate_tokens(context.render(big, budget=budget)), budget)
            self.assertLessEqual(len(context.render(big, budget=budget, measure=len)), budget)

    def test_truncation_reported(self):
        out = context.render(sales_snapshot(1500), budget=600)
        self.assertRegex(out, r"\[showing \d+ of 1501 rows\]")

    def test_partial_read_noted(self):
        snap = sales_snapshot(10)
        snap.total_rows = 5000
        self.assertIn("first 11 of 5000 rows", context.render(snap))

    def test_formulas_listed(self):
        rows = [[1.0, 2.0, 3.0]]
        formulas = [["1", "2", "=A1+B1"]]
        out = context.render(context.Snapshot("d", "S", 0, 9, rows, formulas))
        self.assertIn("C10: =A1+B1", out)

    def test_wide_cells_clipped(self):
        rows = [["x" * 500]]
        out = context.render(context.Snapshot("d", "S", 0, 0, rows))
        self.assertNotIn("x" * 100, out)


def countries_snapshot(n=238):
    rows = [["Country", "Population 2022", "Population 2023", "Change", "Region"]]
    for i in range(n):
        pop = 1000.0 + (i * 7919) % 50000000
        rows.append(["Country %03d" % i, pop, pop * 1.01, "+1.00%", "Europe"])
    rows[100] = ["China", 1425887337.0, 1425671352.0, "-0.02%", "Asia"]
    rows[90] = ["India", 1417173173.0, 1428627663.0, "+0.81%", "Asia"]
    rows[200] = ["Tuvalu", 11312.0, 11396.0, "+0.74%", "Oceania"]
    rows[201] = ["Tokelau", 900.0, 910.0, "+1.11%", "Oceania"]
    return context.Snapshot("list.xlsx", "Countries", 0, 0, rows)


class Tokens(unittest.TestCase):
    def test_digits_count_one_each(self):
        self.assertGreaterEqual(context.estimate_tokens("1425887337"), 10)
        self.assertGreaterEqual(context.estimate_tokens("40578842\t2827608"), 16)

    def test_never_below_digit_and_symbol_count(self):
        text = "row\tA\tB\n2\t40578842\t+1.65%\n3\t2827608\t-0.44%"
        floor = sum(1 for ch in text if not ch.isalpha() and ch != " ")
        self.assertGreaterEqual(context.estimate_tokens(text), floor)

    def test_numeric_sheet_fits_4k(self):
        """The case that overflowed: a sheet of large integers at 4096."""
        msgs, max_tokens = prompts.build_messages(countries_snapshot(), "most populated?", 4096)
        text = "".join(m["content"] for m in msgs)
        digits = sum(ch.isdigit() for ch in text)
        self.assertLess(digits + max_tokens, 4096)


class Extremes(unittest.TestCase):
    def test_min_max_name_their_rows(self):
        out = context.render(countries_snapshot(), budget=1500)
        self.assertIn("max 1425887337 (row 101: China)", out)
        self.assertIn("highest: China, India,", out)
        self.assertIn("min 900 (row 202: Tokelau)", out)
        self.assertIn("lowest: Tokelau,", out)

    def test_extremes_survive_when_rows_do_not(self):
        out = context.render(countries_snapshot(), budget=700)
        self.assertNotIn("\tChina\t", out)
        self.assertIn("China", out)

    def test_total_row_by_sum_is_excluded(self):
        snap = countries_snapshot()
        body = snap.rows[1:]
        world = [math.fsum(r[c] for r in body) if c in (1, 2) else v
                 for c, v in enumerate(["Planet", 0, 0, "+0.9%", "-"])]
        snap.rows.insert(150, world)
        out = context.render(snap, budget=1500)
        self.assertIn("Total rows (left out of the statistics): row 151: Planet", out)
        self.assertIn("max 1425887337 (row 101: China)", out)
        self.assertIn("highest: China, India,", out)

    def test_total_row_by_label_is_excluded(self):
        rows = [["Item", "Cost"], ["Tea", 2.0], ["Milk", 1.5], ["Grand total", 99.0]]
        out = context.render(context.Snapshot("d", "S", 0, 0, rows))
        self.assertIn("Total rows (left out of the statistics): row 4: Grand total", out)
        self.assertIn("max 2 (row 2: Tea)", out)
        self.assertIn("sum 3.5", out)

    def test_no_false_total_on_ordinary_data(self):
        out = context.render(countries_snapshot(), budget=1500)
        self.assertNotIn("Total rows", out)

    def test_no_label_column_falls_back_to_row(self):
        rows = [["A", "B"]] + [[float(i), float(i * 2)] for i in range(10)]
        out = context.render(context.Snapshot("d", "S", 0, 0, rows))
        self.assertIn("max 9 (row 11)", out)
        self.assertIn("highest: row 11, row 10, row 9", out)


class Prompts(unittest.TestCase):
    def test_fits_context_window(self):
        big = sales_snapshot(3000)
        for n_ctx in (2048, 4096, 8192, 32768):
            msgs, max_tokens = prompts.build_messages(big, "Summarise", n_ctx)
            used = sum(context.estimate_tokens(m["content"]) for m in msgs)
            self.assertLess(used + max_tokens, n_ctx, n_ctx)

    def test_missing_n_ctx_uses_default(self):
        msgs, max_tokens = prompts.build_messages(sales_snapshot(), "q", None)
        self.assertEqual(max_tokens, prompts.answer_tokens(prompts.DEFAULT_N_CTX))
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("Request: q", msgs[1]["content"])


class Think(unittest.TestCase):
    def run_chunks(self, text, size):
        f = ThinkFilter()
        out = "".join(f.feed(text[i:i + size]) for i in range(0, len(text), size))
        return out + f.flush()

    def test_every_split(self):
        text = "Hello <think>secret plans</think>world <think>more</think>!"
        for size in range(1, len(text) + 1):
            self.assertEqual(self.run_chunks(text, size), "Hello world !", size)

    def test_lone_angle_bracket_kept(self):
        self.assertEqual(self.run_chunks("a < b and x<thin", 3), "a < b and x<thin")

    def test_unclosed_think_dropped(self):
        self.assertEqual(self.run_chunks("ok<think>never closed", 4), "ok")


LOOPING = (
    "Inconsistent text:\n- Column C (Client): row 4 (cell C4) \"Acme Ltd\" vs row 7. "
    "Let me re-check the data provided. Row 4: \"Acme Ltd\", Row 7: \"Acme Ltd\", Row 15: \"Acme Ltd\". "
    "They look consistent. Wait, row 4 is \"Acme Ltd\", row 7 is \"Acme Ltd\", row 15 is \"Acme Ltd\". "
    "Let me check the data again. Row 4: \"Acme Ltd\", Row 7: \"Acme Ltd\", Row 15: \"Acme Ltd\". "
    "They are the same. Row 4: \"Acme Ltd\", Row 7: \"Acme Ltd\", Row 15: \"Acme Ltd\". ")


class Repetition(unittest.TestCase):
    def test_real_loop_detected(self):
        from ciu_lo.core.ciu_client import RepetitionGuard
        g = RepetitionGuard()
        for i in range(0, len(LOOPING), 9):
            g.feed(LOOPING[i:i + 9])
        self.assertTrue(g.looping)

    def test_normal_lists_are_fine(self):
        from ciu_lo.core.ciu_client import RepetitionGuard
        g = RepetitionGuard()
        g.feed("".join("- Column J (Notes) is empty in row %d.\n" % r for r in range(4, 30)))
        g.feed("Tea | 2\nTea | 2\nTea | 2\nTea | 2\n")   # short lines may repeat
        self.assertFalse(g.looping)


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mock_ciu.start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        mock_ciu.STATE.__init__()
        self.client = CIUClient(self.base)

    def test_status_ready(self):
        st = self.client.status()
        self.assertTrue(st.ready)
        self.assertEqual(st.model, "Qwen3.5-9B-Q4_K_M.gguf")
        self.assertEqual(st.n_ctx, 4096)
        self.assertIn("Connected", st.describe())

    def test_status_unreachable(self):
        st = CIUClient("http://127.0.0.1:9").status(timeout=0.5)
        self.assertFalse(st.reachable)
        self.assertIn("not running", st.describe())

    def test_stream_strips_thinking_and_sends_options(self):
        got = []
        text = self.client.chat_stream([{"role": "user", "content": "hi"}], got.append, max_tokens=50)
        self.assertEqual(text, "The selection has 3 rows. Total is =SUM(B2:B4).")
        self.assertEqual("".join(got), text)
        req = mock_ciu.STATE.last_request
        self.assertTrue(req["stream"])
        self.assertEqual(req["max_tokens"], 50)
        self.assertFalse(req["chat_template_kwargs"]["enable_thinking"])

    def test_finish_reasons(self):
        mock_ciu.STATE.finish_reason = "length"
        self.client.chat_stream([], lambda t: None)
        self.assertEqual(self.client.last_finish, "length")
        mock_ciu.STATE.reply = LOOPING * 3
        mock_ciu.STATE.finish_reason = "stop"
        text = self.client.chat_stream([], lambda t: None)
        self.assertEqual(self.client.last_finish, "repetition")
        self.assertLess(len(text), len(LOOPING) * 3)

    def test_not_loaded_message(self):
        mock_ciu.STATE.state = "stopped"
        with self.assertRaises(CIUError) as cm:
            self.client.chat_stream([], lambda t: None)
        self.assertIn("No model loaded", str(cm.exception))

    def test_context_full_message_passed_through(self):
        mock_ciu.STATE.http_error = (400, "This conversation has filled the 4096-token context.")
        with self.assertRaises(CIUError) as cm:
            self.client.chat_stream([], lambda t: None)
        self.assertIn("4096-token context", str(cm.exception))

    def test_stop_midway(self):
        mock_ciu.STATE.reply = "x" * 400
        mock_ciu.STATE.chunk = 4
        mock_ciu.STATE.delay = 0.005
        stop = threading.Event()
        got = []

        def on_delta(t):
            got.append(t)
            if len(got) == 5:
                stop.set()
        text = self.client.chat_stream([], on_delta, should_stop=stop.is_set)
        self.assertLess(len(text), 400)


if __name__ == "__main__":
    unittest.main()
