"""In-process test hook, packaged only in the test build.

Driven through XNameAccess.getByName("<command>[:<arg>]").
"""

import json
import traceback

import unohelper
from com.sun.star.container import XNameAccess

_state = {}


class SelfTest(unohelper.Base, XNameAccess):
    def __init__(self, ctx):
        self.ctx = ctx

    def getByName(self, name):
        try:
            cmd, _, arg = name.partition(":")
            return getattr(self, "_" + cmd)(arg)
        except Exception:
            return "ERROR\n" + traceback.format_exc()

    def _open_and_ask(self, question):
        from ciu_lo.ui.assistant import AssistantDialog
        desktop = self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", self.ctx)
        dlg = AssistantDialog(self.ctx, desktop.getCurrentFrame())
        dlg.show()
        _state["dlg"] = dlg
        dlg.dm.getByName("txtPrompt").Text = question
        dlg.on_command("ask")
        return "ok"

    def _press(self, command):
        _state["dlg"].on_command(command)
        return "ok"

    def _poll(self, _):
        d = _state["dlg"]
        m = d.dm
        return json.dumps({
            "busy": d.busy,
            "answer": m.getByName("txtOut").Text,
            "status": m.getByName("lblStatus").Label,
            "scope": m.getByName("lblScope").Label,
            "ask_enabled": m.getByName("btnAsk").Enabled,
            "stop_enabled": m.getByName("btnStop").Enabled,
            "apply_enabled": m.getByName("btnApply").Enabled,
            "apply_label": m.getByName("btnApply").Label,
            "pending": d.pending is not None,
        })

    def _prompt(self, text):
        _state["dlg"].dm.getByName("txtPrompt").Text = text
        return "ok"

    def _close(self, _):
        _state["dlg"].close()
        return "closed"

    def _trigger(self, command):
        job = self.ctx.ServiceManager.createInstanceWithContext(
            "com.runciu.ciu.Assistant", self.ctx)
        if job is None:
            return "component missing"
        job.trigger(command)
        return "ok"

    def getElementNames(self):
        return ("poll",)

    def hasByName(self, name):
        return True

    def getElementType(self):
        import uno
        return uno.getTypeByName("string")

    def hasElements(self):
        return True


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    SelfTest, "com.runciu.ciu.SelfTest", ("com.runciu.ciu.SelfTest",))
