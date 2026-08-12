#!/usr/bin/env python3
"""
A stand-in for an HID Amico VL35LF reader.

Why this exists
---------------
There is one reader on a desk and the plant has twenty. Everything about the agent that only
matters at scale — fan-out across doors, per-terminal ordering, a circuit breaker for a reader
that has stopped answering, whether a Raspberry Pi can keep up at a shift change — cannot be
exercised against one device. Those claims stay assertions until something can play twenty doors
at once, and install day is the wrong time to find out they were wrong.

It is deliberately faithful to the hardware's awkward parts rather than to its documentation,
because those are what broke things:

  - notification payloads quote their numerics ("id": "519"), while the same fields come back
    bare from load_objects
  - the change key is "type": "inserted", not "operation": "insert"
  - the timestamp field is "time", and the reader emits its LOCAL wall clock as though it were UTC
  - set_configuration refuses anything that is not a string
  - opening a door is execute_actions with a sec_box action whose parameters are a key=value
    string, not JSON
  - load_objects answers under "data" on some firmware and "users" on others
  - a stale session gets 401, and exactly one silent re-login is expected

Usage
-----
    python3 fake_reader.py --count 20 --base-port 9000

Readers then answer on 127.0.0.1:9000..9019. Register them in Havenz with ip_address set to
"127.0.0.1:9000" and so on — the agent interpolates the whole string, so a port comes along for
free.

Extra switches for the unhappy paths:
    --latency-ms 250        every call takes this long
    --fail-after N          reader N and above refuse everything, to exercise the breaker
    --scan-every SECONDS    each reader invents a badge-in on a timer, to generate event load

Testing the EVENT path at scale needs --distinct-addresses, which gives each reader its own
loopback address instead of sharing one and differing by port. The agent works out which reader
posted an event from its source address, so readers sharing one address all look like whichever
matched first and their events collapse into a single row by idempotency. Command fan-out does not
care — commands are addressed outbound — so plain ports are fine for that.

Distinct addresses are reliable on Linux, which is what Home Assistant OS runs. On Windows, binding
many of 127.0.0.0/8 at once is unreliable, so run event-load tests on the target rather than on a
development machine.

Pure standard library, like everything else here.
"""

import argparse
import json
import random
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # A real reader accepts several connections at once; a serialising stub would make the agent
    # look faster than it is by hiding its own concurrency behind the reader's queueing.
    allow_reuse_address = True


class FakeReader:
    """One reader's state: its users, its access log, its session and its monitor config."""

    def __init__(self, index, username="admin", password="admin", broken=False, latency_ms=0,
                 accept_any=False):
        self.index = index
        self.device_id = f"70136644145{index:05d}"
        self.username = username
        self.password = password
        self.broken = broken
        self.accept_any = accept_any
        self.latency = latency_ms / 1000.0

        self.sessions = set()
        self.users = {}          # terminal user_id -> {registration, name, ...}
        self.groups = set()      # (user_id, group_id)
        self.access_logs = []
        self.monitor = None
        self.next_user_id = 1
        self.next_log_id = 1
        self.lock = threading.Lock()

        # The reader keeps local time and reports it as epoch, so a simulator that reported real
        # UTC would quietly hide the conversion bug this whole system already tripped over once.
        self.utc_offset_seconds = -6 * 3600

    def now(self):
        return int(time.time() + self.utc_offset_seconds)

    # -- helpers ----------------------------------------------------------

    def find_by_registration(self, registration):
        for uid, user in self.users.items():
            if user.get("registration") == registration:
                return uid
        return None

    def record_scan(self, user_id=0, event=7):
        """Append an access-log row and notify, exactly as a real scan would."""
        with self.lock:
            log_id = self.next_log_id
            self.next_log_id += 1
            entry = {"id": log_id, "time": self.now(), "event": event, "user_id": user_id}
            self.access_logs.append(entry)
            monitor = self.monitor
        if monitor:
            threading.Thread(target=self._notify, args=(monitor, entry), daemon=True).start()
        return entry

    def _notify(self, monitor, entry):
        """POST the access-log insert, with the quoting the hardware actually uses."""
        url = (f"http://{monitor['hostname']}:{monitor['port']}/"
               f"{monitor['path'].strip('/')}/dao")
        body = json.dumps({
            "device_id": int(self.device_id[-6:]),
            "object_changes": [{
                "object": "access_logs",
                "type": "inserted",
                # Quoted, like the real thing. A simulator that sent bare numbers would have
                # agreed with the parser that was wrong.
                "values": {k: str(v) for k, v in entry.items()},
            }],
        }).encode()
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:  # noqa: BLE001 — a reader does not care whether we were listening
            pass

    # -- endpoints --------------------------------------------------------

    def handle(self, endpoint, payload, session):
        if self.latency:
            time.sleep(self.latency)

        if self.broken:
            # Chosen over refusing the connection because it is the nastier failure: the reader is
            # up, answers, and is useless. A breaker that only trips on connection errors misses it.
            return 500, {"error": "internal error", "code": 99}

        if endpoint == "hidlogin.fcgi":
            # accept_any exists so a load test can reuse whatever credentials the backend already
            # holds for a real terminal, rather than the plaintext being written into this tool.
            if not self.accept_any and (payload.get("login") != self.username
                                        or payload.get("password") != self.password):
                return 401, {"error": "Invalid login or password", "code": 1}
            token = f"sess-{self.index}-{random.randint(100000, 999999)}"
            self.sessions.add(token)
            return 200, {"session": token}

        if session not in self.sessions:
            return 401, {"error": "Invalid session", "code": 2}

        handler = getattr(self, f"_ep_{endpoint.replace('.fcgi', '')}", None)
        if handler is None:
            return 400, {"error": f"Invalid command: {endpoint.replace('.fcgi', '')}", "code": 1}
        return handler(payload)

    def _ep_system_information(self, payload):
        return 200, {
            "device_id": self.device_id,
            "time": self.now(),
            "uptime": {"days": 0, "hours": 1, "minutes": 2, "seconds": 3},
            "daylight_savings_time_active": False,
            # Empty on the real hardware, so empty here — a simulator that filled it in would let
            # code depend on something the device does not supply.
            "firmware_version": "",
        }

    def _ep_set_configuration(self, payload):
        # The real device rejects non-strings whatever its own documentation claims.
        for section in payload.values():
            if isinstance(section, dict):
                for value in section.values():
                    if not isinstance(value, str):
                        return 400, {"error": "Invalid data (string expected)", "code": 3}
        if "monitor" in payload:
            self.monitor = payload["monitor"]
        return 200, {}

    def _ep_set_system_time(self, payload):
        return 200, {}

    def _ep_execute_actions(self, payload):
        for action in payload.get("actions", []):
            if action.get("action") == "sec_box":
                params = action.get("parameters")
                # A key=value string, not JSON. Sending the wrong shape is a silent no-op on the
                # hardware, so it is an explicit error here — a simulator that shrugged would let
                # exactly that bug through.
                if not isinstance(params, str) or not params.startswith("door="):
                    return 400, {"error": "sec_box expects parameters like 'door=1'", "code": 4}
                self.record_scan(user_id=0, event=12)   # remote open
        return 200, {}

    def _ep_load_objects(self, payload):
        obj = payload.get("object")
        if obj == "users":
            rows = [dict(id=uid, **user) for uid, user in self.users.items()]
            for clause in payload.get("where", []) or []:
                if clause.get("field") == "registration":
                    rows = [r for r in rows if r.get("registration") == clause.get("value")]
            # Under "users" here; other firmware answers under "data". The agent accepts both, and
            # this alternates by reader index so a run exercises each.
            key = "users" if self.index % 2 == 0 else "data"
            return 200, {key: rows}
        if obj == "access_logs":
            return 200, {"access_logs": list(self.access_logs)}
        return 200, {obj: []}

    def _ep_create_objects(self, payload):
        obj = payload.get("object")
        values = payload.get("values") or []
        if obj == "users":
            for row in values:
                uid = self.next_user_id
                self.next_user_id += 1
                self.users[uid] = {k: v for k, v in row.items()}
            return 200, {}
        if obj == "user_groups":
            for row in values:
                pair = (row.get("user_id"), row.get("group_id"))
                if pair in self.groups:
                    # The device treats a repeat as an error rather than a no-op, which is why the
                    # agent swallows failures here.
                    return 400, {"error": "already a member", "code": 5}
                self.groups.add(pair)
            return 200, {}
        return 200, {}

    def _ep_modify_objects(self, payload):
        if payload.get("object") == "users":
            uid = (payload.get("where", {}).get("users", {}) or {}).get("id")
            if uid in self.users:
                self.users[uid].update(payload.get("values") or {})
                return 200, {}
            return 400, {"error": "no such user", "code": 6}
        return 200, {}

    def _ep_destroy_objects(self, payload):
        if payload.get("object") == "users":
            uid = (payload.get("where", {}).get("users", {}) or {}).get("id")
            self.users.pop(uid, None)
            self.groups = {g for g in self.groups if g[0] != uid}
        return 200, {}

    def _ep_remote_enroll(self, payload):
        uid = payload.get("user_id")
        if uid not in self.users:
            return 400, {"error": "no such user", "code": 6}
        time.sleep(0.3)   # a person stepping up to the camera
        # 1x1 JPEG, base64 — enough to prove the round trip carries bytes intact.
        return 200, {"image": "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
                              "DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
                              "2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
                              "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QA"
                              "HwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUF"
                              "BAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
                              "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1"
                              "dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
                              "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APn+"
                              "iiigD//Z"}

    def _ep_user_set_image(self, payload):
        return 200, {}


def build_handler(reader):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path, _, query = self.path.lstrip("/").partition("?")
            session = None
            for part in query.split("&"):
                if part.startswith("session="):
                    session = part[len("session="):]

            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length)

            # user_set_image sends raw image bytes, not JSON.
            if path.startswith("user_set_image"):
                payload = {"bytes": len(raw)}
            else:
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    return self._send(400, {"error": "bad json", "code": 7})

            code, body = reader.handle(path, payload, session)
            self._send(code, body)

        def do_GET(self):
            self._send(200, {"fake_reader": reader.index, "device_id": reader.device_id})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Simulated HID Amico readers")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--base-port", type=int, default=9000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--distinct-addresses", action="store_true",
                    help="give each reader its own loopback address (127.0.0.2, .3, ...) instead "
                         "of sharing one and differing only by port. Readers on a real LAN have "
                         "distinct addresses, and the agent identifies which one posted an event "
                         "by source address — so sharing one silently attributes every reader's "
                         "events to whichever matched first.")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--latency-ms", type=int, default=0,
                    help="delay every call, to imitate a slow reader or a congested LAN")
    ap.add_argument("--fail-after", type=int, default=None,
                    help="readers at or above this index answer 500 to everything")
    ap.add_argument("--accept-any", action="store_true",
                    help="accept any credentials, for load tests against existing terminal rows")
    ap.add_argument("--scan-every", type=float, default=None,
                    help="each reader invents a badge-in this often, in seconds")
    args = ap.parse_args()

    readers = []
    for i in range(args.count):
        broken = args.fail_after is not None and i >= args.fail_after
        reader = FakeReader(i, args.username, args.password, broken, args.latency_ms,
                            args.accept_any)
        if args.distinct_addresses:
            # 127.0.0.2 upwards: every one of 127.0.0.0/8 routes to loopback, so each reader gets
            # its own address on the standard port, exactly as it would on a real LAN.
            host, port = f"127.0.0.{i + 2}", args.base_port
        else:
            host, port = args.host, args.base_port + i
        reader.address = f"{host}:{port}"
        server = ThreadingHTTPServer((host, port), build_handler(reader))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        readers.append(reader)

    print(f"{args.count} reader(s): {readers[0].address} .. {readers[-1].address}"
          + (f", {args.count - args.fail_after} of them broken" if args.fail_after is not None else ""))
    print("register them in Havenz with ip_address set to exactly those values")

    if args.scan_every:
        def scanner():
            while True:
                time.sleep(args.scan_every)
                for reader in readers:
                    if reader.broken:
                        continue
                    uid = next(iter(reader.users), 0)
                    reader.record_scan(user_id=uid, event=7 if uid else 3)
        threading.Thread(target=scanner, daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
