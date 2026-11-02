"""The non-modal CIU window. Network calls run on a worker thread; results
return to the main thread through MainThread.
"""

import threading
import traceback

import uno
import unohelper
from com.sun.star.awt import XActionListener, XTopWindowListener

from ..calc import executor, reader
from ..core import prompts
from ..core import engine as enginemod
from ..core.query import QueryError
from ..core.plan import PLAN_SCHEMA, PlanError, parse_plan
from ..core.ciu_client import CIUClient, CIUError
from ..core.context import summary_line
from .mainthread import MainThread

W, H = 290, 300  # app-font units
PAD = 6


class AssistantDialog(object):
    def __init__(self, ctx, frame):
        self.ctx = ctx
        self.frame = frame
        self.client = CIUClient()
        self.status = None
        self.busy = False
        self.stop_event = threading.Event()
        self.answer = ""
        self.ui = MainThread(ctx, on_text=self._append_answer)
        self.on_closed = None
        self.pending = None  # (plan, cells it would overwrite)
        self._commands = {}
        self._build()

    def _build(self):
        smgr = self.ctx.ServiceManager
        dm = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", self.ctx)
        dm.Title = "CIU"
        dm.Width, dm.Height = W, H
        dm.PositionX, dm.PositionY = 60, 40
        self.dm = dm

        inner = W - 2 * PAD
        y = PAD
        self._label("lblStatus", PAD, y, inner, 10, "Checking CIU...")
        y += 13
        self._label("lblScope", PAD, y + 2, inner - 70, 10, "Selection: -")
        self._button("btnScope", W - PAD - 66, y, 66, 13, "Use selection", "scope")
        y += 18

        bw = (inner - 2 * 4) // 3
        for i, (title, _) in enumerate(prompts.QUICK_ACTIONS):
            self._button("btnQuick%d" % i, PAD + i * (bw + 4), y, bw, 14, title, "quick:%d" % i)
        y += 19

        self._label("lblAsk", PAD, y, inner, 9, "Ask about the selected cells, or describe a change:")
        y += 10
        p = self._model("Edit", "txtPrompt", PAD, y, inner, 34)
        p.MultiLine = True
        p.VScroll = True
        y += 38

        self._button("btnAsk", PAD, y, 52, 14, "Ask", "ask")
        self._button("btnChange", PAD + 56, y, 84, 14, "Propose change", "change")
        self._button("btnStop", W - PAD - 52, y, 52, 14, "Stop", "stop")
        y += 19

        out = self._model("Edit", "txtOut", PAD, y, inner, H - y - 22)
        out.MultiLine = True
        out.ReadOnly = True
        out.VScroll = True
        self._label("lblFoot", PAD, H - 15, inner - 124, 9,
                    "Nothing changes until you click Apply.")
        self._button("btnApply", W - PAD - 120, H - 18, 64, 14, "Apply", "apply")
        self._button("btnDiscard", W - PAD - 52, H - 18, 52, 14, "Discard", "discard")

        dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", self.ctx)
        dlg.setModel(dm)
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        parent = self.frame.getContainerWindow() if self.frame else None
        dlg.createPeer(toolkit, parent)
        self.dlg = dlg

        for name, command in self._commands.items():
            dlg.getControl(name).addActionListener(_Actions(self, command))
        dlg.getPeer().addTopWindowListener(_Closer(self))
        self._set_busy(False)

    def _model(self, kind, name, x, y, w, h):
        m = self.dm.createInstance("com.sun.star.awt.UnoControl%sModel" % kind)
        m.PositionX, m.PositionY, m.Width, m.Height = x, y, w, h
        self.dm.insertByName(name, m)
        return m

    def _label(self, name, x, y, w, h, text):
        m = self._model("FixedText", name, x, y, w, h)
        m.Label = text
        return m

    def _button(self, name, x, y, w, h, label, command):
        m = self._model("Button", name, x, y, w, h)
        m.Label = label
        self._commands[name] = command
        return m

    def show(self):
        self._dock_right()
        self.dlg.setVisible(True)
        self.refresh_scope()
        self._check_status()

    def _dock_right(self):
        """Place the window against the right edge of the Calc window."""
        try:
            from com.sun.star.awt.PosSize import POS
            parent = self.frame.getContainerWindow().getPosSize()
            me = self.dlg.getPosSize()
            x = max(0, parent.Width - me.Width - 24)
            y = max(0, min(120, parent.Height - me.Height - 24))
            self.dlg.setPosSize(x, y, 0, 0, POS)
        except Exception:
            pass

    def to_front(self):
        try:
            self.dlg.getPeer().toFront()
        except Exception:
            pass
        self.refresh_scope()

    def close(self):
        self.stop_event.set()
        self.ui.closed = True
        try:
            self.dlg.dispose()
        except Exception:
            pass
        if self.on_closed:
            self.on_closed(self)

    def on_command(self, command):
        if command == "ask":
            self.ask(self._get("txtPrompt"))
        elif command == "change":
            self.propose(self._get("txtPrompt"))
        elif command == "apply":
            self.apply_pending()
        elif command == "discard":
            self._clear_pending()
            self._show_answer("Discarded. Nothing was changed.")
        elif command == "stop":
            self.stop_event.set()
        elif command == "scope":
            self.refresh_scope()
        elif command.startswith("quick:"):
            title, question = prompts.QUICK_ACTIONS[int(command.split(":")[1])]
            self._set("txtPrompt", question)
            self.ask(question, with_query=False)

    def refresh_scope(self):
        try:
            snap = reader.read_selection(self._model_doc(), self._controller())
            self._label_set("lblScope", "Selection: " + summary_line(snap))
            return snap
        except reader.SelectionError as e:
            self._label_set("lblScope", "Selection: %s" % e)
        except Exception as e:
            self._label_set("lblScope", "Selection: could not read (%s)" % e)
        return None

    def ask(self, question, with_query=True):
        self._start(question, lambda snap, text: self._work(snap, text, with_query))

    def propose(self, request):
        self._start(request, self._work_plan)

    def _start(self, text, worker):
        if self.busy:
            return
        text = (text or "").strip()
        if not text:
            self._show_answer("Type a request first, or use one of the buttons.")
            return
        # UNO is main-thread only, so read the sheet before starting the worker.
        snap = self.refresh_scope()
        if snap is None:
            self._show_answer("Select some cells in the sheet, then try again.")
            return
        self._clear_pending()
        self._show_answer("")
        self.stop_event.clear()
        self._set_busy(True)
        threading.Thread(target=worker, args=(snap, text), daemon=True).start()

    def apply_pending(self):
        if not self.pending or self.busy:
            return
        plan, occupied = self.pending
        self._clear_pending()
        result = executor.apply(self._model_doc(), plan, occupied)
        text = result.message
        if result.warnings:
            text += "\n\nCheck these cells:\n" + "\n".join("  " + w for w in result.warnings)
        self._show_answer(text)
        self.refresh_scope()

    def _work_plan(self, snap, request):
        try:
            status = self.client.status()
            self.ui.post(lambda: self._apply_status(status))
            if not status.ready:
                self.ui.post(lambda: self._show_answer(status.describe()))
                return
            self.ui.post(lambda: self._show_answer("Working out the change..."))
            raw = self._fit_and_send(
                lambda scale: prompts.build_action_messages(snap, request, status.n_ctx, scale),
                lambda messages, max_tokens: self.client.chat_json(
                    messages, PLAN_SCHEMA, max_tokens=max_tokens))
            if self.stop_event.is_set():
                self.ui.post(lambda: self._show_answer("Stopped. Nothing was changed."))
                return
            try:
                plan = parse_plan(raw, sheet=snap.sheet)
            except PlanError as e:
                msg = str(e)
                self.ui.post(lambda: self._show_answer(
                    "CIU's proposal could not be used, so nothing was changed:\n" + msg))
                return
            self.ui.post(lambda: self._show_plan(plan))
        except CIUError as e:
            msg = str(e)
            self.ui.post(lambda: self._show_answer(msg))
        except Exception:
            tb = traceback.format_exc()
            self.ui.post(lambda: self._show_answer("Unexpected error:\n" + tb))
        finally:
            self.ui.post(lambda: self._set_busy(False))

    def _work(self, snap, question, with_query=True):
        try:
            status = self.client.status()
            self.ui.post(lambda: self._apply_status(status))
            if not status.ready:
                self.ui.post(lambda: self._show_answer(status.describe()))
                return
            engine = enginemod.current() if with_query else None
            self._fit_and_send(
                lambda scale: prompts.build_messages(snap, question, status.n_ctx, scale,
                                                     engine=engine),
                lambda messages, max_tokens: self._answer(
                    snap, messages, max_tokens, engine))
            if self.stop_event.is_set():
                self.ui.post(lambda: self._append_answer("\n[stopped]"))
        except CIUError as e:
            msg = str(e)
            self.ui.post(lambda: self._append_answer("\n" + msg))
        except Exception:
            tb = traceback.format_exc()
            self.ui.post(lambda: self._append_answer("\nUnexpected error:\n" + tb))
        finally:
            self.ui.post(lambda: self._set_busy(False))

    def _answer(self, snap, messages, max_tokens, engine):
        """Optional query step, then the streamed answer. Worker thread."""
        if engine is not None:
            self.ui.post(lambda: self._show_answer("Thinking..."))
            messages, result, shown = self._run_query(snap, messages, engine)
            if self.stop_event.is_set():
                return None
            self.ui.post(lambda: self._show_answer(shown))
            messages = prompts.follow_up(messages[:-1], messages[-1]["content"], result)
        return self.client.chat_stream(
            messages, on_delta=self.ui.post_text,
            should_stop=self.stop_event.is_set, max_tokens=max_tokens)

    def _run_query(self, snap, messages, engine, attempts=2):
        """Ask for a query and run it, letting the model fix one failure.
        Returns (messages ending with the model's last reply, result text or
        None, text to show above the answer)."""
        error = None
        for attempt in range(attempts):
            reply = self.client.chat_json(messages, engine.SCHEMA,
                                          max_tokens=prompts.QUERY_REPLY_TOKENS)
            reply = reply or engine.no_query_reply()
            if self.stop_event.is_set():
                break
            try:
                q = engine.parse(reply, snap)
                if q is None:
                    return messages + [{"role": "assistant", "content": reply}], None, ""
                result = prompts.fit_result(q.run())
                shown = "Computed with %s: %s\n\n" % (engine.NAME, q.describe())
                return messages + [{"role": "assistant", "content": reply}], result, shown
            except QueryError as e:
                error = str(e)
                if attempt + 1 < attempts:
                    messages = prompts.retry_turn(messages, reply, error)
        failed = "The query could not be run: %s" % error if error else None
        return messages + [{"role": "assistant", "content": reply}], failed, ""

    def _fit_and_send(self, build, send):
        """Send with a smaller selection each time the server reports the
        context as full. The request fails before any text is streamed, so a
        retry never shows a partial answer twice."""
        for i, scale in enumerate(prompts.RETRY_SCALES):
            messages, max_tokens = build(scale)
            try:
                return send(messages, max_tokens)
            except CIUError as e:
                if not e.context_full or i == len(prompts.RETRY_SCALES) - 1:
                    raise

    def _check_status(self):
        def work():
            status = self.client.status()
            self.ui.post(lambda: self._apply_status(status))
        threading.Thread(target=work, daemon=True).start()

    def _apply_status(self, status):
        self.status = status
        self._label_set("lblStatus", status.describe())

    def _show_answer(self, text):
        self.answer = text
        self._set("txtOut", text)

    def _append_answer(self, text):
        self.answer += text
        self._set("txtOut", self.answer)
        try:
            n = len(self.answer)
            sel = uno.createUnoStruct("com.sun.star.awt.Selection", n, n)
            self.dlg.getControl("txtOut").setSelection(sel)
        except Exception:
            pass

    def _show_plan(self, plan):
        """Preview. Runs on the main thread, so it can look at the sheet."""
        if not plan.operations:
            self._show_answer("CIU did not propose a change:\n" + (plan.summary or "no reason given"))
            return
        try:
            occupied = executor.occupied_cells(executor.get_sheet(self._model_doc(), plan.sheet), plan)
        except Exception as e:
            self._show_answer("Could not check the target cells: %s" % e)
            return
        lines = ["Proposed change (not applied yet):", plan.describe(), ""]
        if occupied:
            shown = ", ".join("%s (%s)" % (ref, _clip(text)) for ref, text in occupied[:6])
            more = ", ..." if len(occupied) > 6 else ""
            lines.append("WARNING: overwrites %d filled cell%s: %s%s" % (
                len(occupied), "" if len(occupied) == 1 else "s", shown, more))
        else:
            lines.append("Overwrites: nothing, all target cells are empty.")
        lines.append("")
        lines.append("Click Apply to make the change, or Discard.")
        self._show_answer("\n".join(lines))
        self.pending = (plan, occupied)
        self.dm.getByName("btnApply").Label = (
            "Apply (overwrite %d)" % len(occupied) if occupied else "Apply")
        self._refresh_buttons()

    def _clear_pending(self):
        self.pending = None
        self.dm.getByName("btnApply").Label = "Apply"
        self._refresh_buttons()

    def _set_busy(self, busy):
        self.busy = busy
        self._refresh_buttons()

    def _refresh_buttons(self):
        for name, cmd in self._commands.items():
            if self.busy:
                enabled = cmd == "stop"
            elif cmd in ("apply", "discard"):
                enabled = self.pending is not None
            else:
                enabled = cmd != "stop"
            self.dm.getByName(name).Enabled = enabled

    def _get(self, name):
        return self.dm.getByName(name).Text

    def _set(self, name, text):
        self.dm.getByName(name).Text = text

    def _label_set(self, name, text):
        self.dm.getByName(name).Label = text

    def _model_doc(self):
        return self.frame.getController().getModel()

    def _controller(self):
        return self.frame.getController()


def _clip(text, n=18):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "~"


class _Actions(unohelper.Base, XActionListener):
    def __init__(self, owner, command):
        self.owner = owner
        self.command = command

    def actionPerformed(self, event):
        try:
            self.owner.on_command(self.command)
        except Exception:
            self.owner._show_answer("Unexpected error:\n" + traceback.format_exc())

    def disposing(self, event):
        pass


class _Closer(unohelper.Base, XTopWindowListener):
    def __init__(self, owner):
        self.owner = owner

    def windowClosing(self, event):
        self.owner.close()

    def windowOpened(self, e): pass
    def windowClosed(self, e): pass
    def windowMinimized(self, e): pass
    def windowNormalized(self, e): pass
    def windowActivated(self, e): pass
    def windowDeactivated(self, e): pass
    def disposing(self, e): pass
