"""Runs callables on LibreOffice's main thread from worker threads, via
com.sun.star.awt.AsyncCallback.

Callables and streamed text run in the order they were posted. Consecutive
text is merged, so a fast model does not cost one UI update per token.
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
        self._queue = []  # callables, or lists of text pieces
        self._scheduled = False
        self._on_text = on_text
        self.closed = False

    def post(self, fn):
        with self._lock:
            self._queue.append(fn)
            self._schedule_locked()

    def call(self, fn, timeout=120):
        """Run fn on the main thread and return its result to the worker
        that asked, raising whatever it raised."""
        done = threading.Event()
        box = {}

        def run():
            try:
                box["value"] = fn()
            except Exception as e:
                box["error"] = e
            finally:
                done.set()

        self.post(run)
        if not done.wait(timeout):
            raise RuntimeError("LibreOffice did not respond in time.")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def post_text(self, text):
        with self._lock:
            if self._queue and isinstance(self._queue[-1], list):
                self._queue[-1].append(text)
            else:
                self._queue.append([text])
            self._schedule_locked()

    def _schedule_locked(self):
        if self._scheduled or self.closed:
            return
        self._scheduled = True
        self._async.addCallback(self._wake, None)

    def _drain(self):
        with self._lock:
            queue, self._queue = self._queue, []
            self._scheduled = False
        if self.closed:
            return
        for item in queue:
            if isinstance(item, list):
                if self._on_text:
                    self._on_text("".join(item))
            else:
                item()
