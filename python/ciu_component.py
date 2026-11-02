"""UNO entry point. Menu commands from registry/Addons.xcu arrive in trigger()."""

import traceback

import uno
import unohelper
from com.sun.star.task import XJobExecutor

IMPL_NAME = "com.runciu.ciu.Assistant"

# Keeps dialogs alive after trigger() returns; one per document frame.
_open = []


class Assistant(unohelper.Base, XJobExecutor):
    def __init__(self, ctx):
        self.ctx = ctx

    def trigger(self, args):
        try:
            if args == "status":
                self._status()
            else:
                self._open()
        except Exception:
            self._message("CIU error", traceback.format_exc())

    def _frame(self):
        desktop = self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", self.ctx)
        return desktop.getCurrentFrame()

    def _open(self):
        from ciu_lo.ui.assistant import AssistantDialog

        frame = self._frame()
        for f, dlg in _open:
            if f == frame:
                dlg.to_front()
                return
        dlg = AssistantDialog(self.ctx, frame)
        entry = (frame, dlg)
        _open.append(entry)
        dlg.on_closed = lambda d: _open.remove(entry) if entry in _open else None
        dlg.show()

    def _status(self):
        from ciu_lo.core.ciu_client import CIUClient

        client = CIUClient()
        st = client.status()
        text = st.describe() + "\n\nAddress: %s" % client.base
        if st.error and not st.reachable:
            text += "\nDetail: %s" % st.error
        self._message("CIU connection", text)

    def _message(self, title, text):
        smgr = self.ctx.ServiceManager
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        frame = self._frame()
        parent = frame.getContainerWindow() if frame else None
        box = toolkit.createMessageBox(
            parent, uno.Enum("com.sun.star.awt.MessageBoxType", "INFOBOX"),
            1, title, text)
        box.execute()


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    Assistant, IMPL_NAME, ("com.sun.star.task.Job",))
