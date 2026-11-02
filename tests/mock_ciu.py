"""A stand-in for CIU Core. Run directly to serve it on port 8674."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockState(object):
    def __init__(self):
        self.state = "ready"
        self.n_ctx = 4096
        self.reply = "The <think>hidden</think>selection has 3 rows. Total is =SUM(B2:B4)."
        self.chunk = 7
        self.delay = 0.0
        self.http_error = None
        self.max_prompt_chars = None
        self.plan_reply = '{"summary": "nothing", "operations": []}'
        self.reject_response_format = False
        self.last_request = None
        self.requests = []


STATE = MockState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/status":
            body = {"runner": {"state": STATE.state, "n_ctx": STATE.n_ctx,
                               "model_path": "/Users/x/.ciu/models/Qwen3.5-9B-Q4_K_M.gguf",
                               "error": None},
                    "base_url": "http://127.0.0.1:8674/v1"}
            self._json(200, body)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n).decode("utf-8"))
        STATE.last_request = req
        STATE.requests.append(req)
        if STATE.state != "ready":
            return self._json(503, {"error": {"message": "No model loaded (state: %s)." % STATE.state,
                                              "type": "ciu_not_ready"}})
        if STATE.http_error:
            code, msg = STATE.http_error
            STATE.http_error = None
            return self._json(code, {"error": {"message": msg}})
        prompt_chars = sum(len(m.get("content", "")) for m in req.get("messages", []))
        if STATE.max_prompt_chars and prompt_chars > STATE.max_prompt_chars:
            return self._json(400, {"error": {
                "message": "This conversation has filled the %d-token context." % STATE.n_ctx,
                "type": "ciu_context_full", "n_ctx": STATE.n_ctx}})
        if not req.get("stream"):
            if STATE.reject_response_format and "response_format" in req:
                return self._json(400, {"error": {"message": "unsupported response_format"}})
            return self._json(200, {"choices": [{"message": {"role": "assistant",
                                                             "content": STATE.plan_reply}}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        text = STATE.reply
        try:
            for i in range(0, len(text), STATE.chunk):
                ev = {"choices": [{"delta": {"content": text[i:i + STATE.chunk]}}]}
                self.wfile.write(("data: %s\n\n" % json.dumps(ev)).encode("utf-8"))
                self.wfile.flush()
                if STATE.delay:
                    time.sleep(STATE.delay)
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8674
    STATE.delay = 0.03
    STATE.reply = ("Mock CIU here. I can see your selection; a real model would "
                   "answer now. Try =SUM(B2:B10) for a total.")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("mock CIU on http://127.0.0.1:%d" % port)
    srv.serve_forever()
