"""Runs callables on LibreOffice's main thread from worker threads, via
com.sun.star.awt.AsyncCallback. Streamed text is coalesced per wake-up.
"""

import threading

import unohelper
from com.sun.star.awt import XCallback


class _Wake(unohelper.Base, XCallback):
    def __init__(self, owner):
        self.owner = owner

    def notify(self, data):
        self.owner._drain()


class MainThread(object):
    def __init__(self, ctx, on_text=None):
        self._async = ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.awt.AsyncCallback", ctx)
        self._wake = _Wake(self)
        self._lock = threading.Lock()
        self._queue = []
        self._text = []
        self._scheduled = False
        self._on_text = on_text
        self.closed = False

    def post(self, fn):
        with self._lock:
            self._queue.append(fn)
            self._schedule_locked()

    def post_text(self, text):
        with self._lock:
            self._text.append(text)
            self._schedule_locked()

    def _schedule_locked(self):
        if self._scheduled or self.closed:
            return
        self._scheduled = True
        self._async.addCallback(self._wake, None)

    def _drain(self):
        with self._lock:
            queue, self._queue = self._queue, []
            text, self._text = "".join(self._text), []
            self._scheduled = False
        if self.closed:
            return
        # Text first, so a callback queued after the last token sees it on screen.
        if text and self._on_text:
            self._on_text(text)
        for fn in queue:
            fn()
