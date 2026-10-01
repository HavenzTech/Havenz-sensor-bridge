"""
A small SignalR client, enough to hear what the screens hear.

The scenarios need to know that a welcome was broadcast for a door and how long after the tap,
without being a browser. This speaks SignalR's JSON protocol over the Server-Sent-Events
transport: one long GET carries frames down, plain POSTs carry invocations up. Standard library
only - no websocket package to install.

It listens the way a signed-in admin's page does (joining `terminal_<id>` groups); the screen
half of the rehearsal is what proves a paired panel actually shows it.
"""

import http.client as http_client
import json
import threading
import time
from urllib.parse import quote, urlparse

from .util import http as http_call

RS = "\x1e"


class SignalRClient:
    def __init__(self, base_url, token_provider, hub_path="/hubs/notifications"):
        self.base = base_url.rstrip("/")
        self.hub_path = hub_path
        self._token_provider = token_provider
        self._conn_token = None
        self._stream = None
        self._reader = None
        self._pinger = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._invocation = 0
        self._completions = {}
        self._handshake = threading.Event()
        self.messages = []            # (monotonic, wall, target, arguments)
        self.connected = False
        self.closed_reason = None

    # -- connection -----------------------------------------------------------

    def _auth(self):
        return {"Authorization": f"Bearer {self._token_provider()}"}

    def connect(self, timeout=15):
        status, body, _ = http_call("POST", f"{self.base}{self.hub_path}/negotiate?negotiateVersion=1",
                               body={}, headers=self._auth(), timeout=timeout)
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"SignalR negotiate failed: HTTP {status} {body}")
        self._conn_token = body.get("connectionToken") or body.get("connectionId")

        url = urlparse(self.base)
        conn = http_client.HTTPConnection(url.hostname, url.port or 80, timeout=None)
        conn.request("GET", f"{self.hub_path}?id={quote(self._conn_token)}",
                     headers={**self._auth(), "Accept": "text/event-stream", "Cache-Control": "no-cache"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"SignalR stream refused: HTTP {resp.status}")
        self._stream = (conn, resp)
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        self._post(json.dumps({"protocol": "json", "version": 1}) + RS)
        if not self._handshake.wait(timeout):
            raise RuntimeError("SignalR handshake was not answered")
        self.connected = True
        self._pinger = threading.Thread(target=self._ping_loop, daemon=True)
        self._pinger.start()
        return self

    def close(self):
        self._stop.set()
        self.connected = False
        try:
            if self._stream:
                self._stream[0].close()
        except Exception:  # noqa: BLE001
            pass

    def _post(self, text):
        status, body, _ = http_call("POST", f"{self.base}{self.hub_path}?id={quote(self._conn_token)}",
                               raw=text.encode("utf-8"),
                               headers={**self._auth(), "Content-Type": "text/plain;charset=UTF-8"},
                               timeout=15)
        if status not in (200, 202):
            raise RuntimeError(f"SignalR send failed: HTTP {status} {body}")

    def _ping_loop(self):
        while not self._stop.wait(10):
            try:
                self._post(json.dumps({"type": 6}) + RS)
            except Exception as e:  # noqa: BLE001
                self.closed_reason = f"ping failed: {e}"
                self.connected = False
                return

    def _read_loop(self):
        _, resp = self._stream
        buffer = ""
        try:
            while not self._stop.is_set():
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text.startswith("data:"):
                    continue
                buffer += text[5:].lstrip()
                while RS in buffer:
                    frame, _, buffer = buffer.partition(RS)
                    if frame:
                        self._on_frame(frame)
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.closed_reason = f"stream error: {e}"
        self.connected = False

    def _on_frame(self, frame):
        try:
            msg = json.loads(frame)
        except json.JSONDecodeError:
            return
        if not self._handshake.is_set():
            # The handshake answer is an empty object (or {"error": ...}).
            if "error" in msg:
                self.closed_reason = f"handshake refused: {msg['error']}"
            self._handshake.set()
            if "type" not in msg:
                return
        kind = msg.get("type")
        if kind == 1:
            with self._lock:
                self.messages.append((time.monotonic(), time.time(), msg.get("target"),
                                      msg.get("arguments") or []))
        elif kind == 3:
            with self._lock:
                self._completions[msg.get("invocationId")] = msg
        elif kind == 7:
            self.closed_reason = msg.get("error") or "closed by server"
            self.connected = False

    # -- use ------------------------------------------------------------------

    def invoke(self, target, *arguments, timeout=10):
        """Call a hub method and wait for its completion. Returns the completion message."""
        with self._lock:
            self._invocation += 1
            invocation_id = str(self._invocation)
        self._post(json.dumps({"type": 1, "invocationId": invocation_id, "target": target,
                               "arguments": list(arguments)}) + RS)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                done = self._completions.pop(invocation_id, None)
            if done is not None:
                if done.get("error"):
                    raise RuntimeError(f"{target} failed: {done['error']}")
                return done
            time.sleep(0.02)
        raise TimeoutError(f"{target} was not completed in {timeout}s")

    def received(self, target=None, since_mono=None):
        with self._lock:
            rows = list(self.messages)
        return [m for m in rows
                if (target is None or m[2] == target) and (since_mono is None or m[0] >= since_mono)]
