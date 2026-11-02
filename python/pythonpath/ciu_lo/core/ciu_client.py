"""HTTP client for CIU Core. Standard library only, since LibreOffice's
bundled Python has no pip.
"""

import json
import os
import socket
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8674"


CONTEXT_FULL_TYPES = ("ciu_context_full", "exceed_context_size_error")


class CIUError(Exception):
    """A failure worth showing to the user as-is."""

    def __init__(self, message, kind=None):
        Exception.__init__(self, message)
        self.kind = kind

    @property
    def context_full(self):
        return self.kind in CONTEXT_FULL_TYPES


class Status(object):
    def __init__(self, reachable, state=None, model=None, n_ctx=None, error=None):
        self.reachable = reachable
        self.state = state
        self.model = model
        self.n_ctx = n_ctx
        self.error = error

    @property
    def ready(self):
        return self.reachable and self.state == "ready"

    def describe(self):
        if not self.reachable:
            return "CIU not running. Start it, then try again."
        if self.state != "ready":
            return "CIU is running but no model is loaded (state: %s)." % self.state
        ctx = (" | %d-token context" % self.n_ctx) if self.n_ctx else ""
        return "Connected | %s%s" % (self.model or "model loaded", ctx)


class CIUClient(object):
    def __init__(self, base_url=None):
        self.base = (base_url or os.environ.get("CIU_BASE_URL") or DEFAULT_BASE).rstrip("/")

    def status(self, timeout=2.0):
        try:
            with urllib.request.urlopen(self.base + "/api/status", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            return Status(False, error=str(e))
        except ValueError as e:
            return Status(False, error="Unexpected reply from CIU: %s" % e)

        runner = data.get("runner") or {}
        path = runner.get("model_path") or ""
        model = os.path.basename(path.replace("\\", "/")) if path else None
        return Status(True, state=runner.get("state"), model=model,
                      n_ctx=runner.get("n_ctx"), error=runner.get("error"))

    def chat_stream(self, messages, on_delta, should_stop=lambda: False,
                    max_tokens=700, temperature=0.2, timeout=300.0):
        """Stream a chat completion, calling on_delta for each piece of visible
        text. Returns the full answer. Worker thread only.
        """
        body = {
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Thinking tokens cost more latency than they are worth here.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise _http_error(e)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            raise CIUError("Could not reach CIU at %s (%s)." % (self.base, e))

        thinker = ThinkFilter()
        parts = []
        try:
            for raw in resp:
                if should_stop():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if "error" in event:
                    err = event["error"]
                    raise CIUError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = thinker.feed(delta.get("content") or "")
                    if text:
                        parts.append(text)
                        on_delta(text)
        except socket.timeout:
            raise CIUError("CIU stopped responding (timed out).")
        finally:
            resp.close()

        tail = thinker.flush()
        if tail:
            parts.append(tail)
            on_delta(tail)
        return "".join(parts)


    def chat_json(self, messages, schema, max_tokens=700, timeout=300.0):
        """A completion constrained to a JSON schema. Retries once without
        response_format if the server rejects it. Worker thread only.
        """
        body = {
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "plan", "schema": schema}},
        }
        try:
            data = self._post_json("/v1/chat/completions", body, timeout)
        except CIUError as e:
            if "response_format" not in str(e) and "grammar" not in str(e).lower():
                raise
            body.pop("response_format")
            data = self._post_json("/v1/chat/completions", body, timeout)
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise CIUError("Unexpected reply from CIU.")
        f = ThinkFilter()
        return f.feed(content) + f.flush()

    def _post_json(self, path, body, timeout):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise _http_error(e)
        except socket.timeout:
            raise CIUError("CIU stopped responding (timed out).")
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            raise CIUError("Could not reach CIU at %s (%s)." % (self.base, e))
        except ValueError:
            raise CIUError("Unexpected reply from CIU.")


def _http_error(http_error):
    try:
        data = json.loads(http_error.read().decode("utf-8", "replace"))
    except Exception:
        return CIUError("CIU returned HTTP %d." % http_error.code)
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        return CIUError(err.get("message") or json.dumps(err), err.get("type"))
    return CIUError(str(err))


class ThinkFilter(object):
    """Removes <think>...</think> blocks from a stream, even when the tags are
    split across chunks. A fallback for templates that ignore enable_thinking.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, text):
        self.buf += text
        out = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                if not self.inside:
                    out.append(self.buf[:i])
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            # Hold back anything that could be the start of a split tag.
            keep = _partial_suffix(self.buf, tag)
            if not self.inside:
                out.append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(out)

    def flush(self):
        text = "" if self.inside else self.buf
        self.buf = ""
        return text


def _partial_suffix(s, tag):
    """Length of the longest suffix of s that is a proper prefix of tag."""
    for n in range(min(len(s), len(tag) - 1), 0, -1):
        if tag.startswith(s[-n:]):
            return n
    return 0
