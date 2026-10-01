#!/usr/bin/env python3
"""
Twenty door readers on one plant LAN.

This wraps the repo's stand-in reader (tools/fake_reader.py) rather than re-implementing it, so
the awkward firmware behaviour that file was written to preserve - quoted numerics, local time
reported as UTC, sessions that expire, the key=value door command - is exactly what the site agent
meets here. What this file adds is the part a rehearsal needs and a bench test does not:

  * each reader on its OWN address, answering on port 80, and posting its events FROM that
    address - the agent tells doors apart by where an event came from
  * a person walking up to a door: `scan` decides the way the device decides (is this face on
    this reader, is the person in the door's group, is it inside their dated window) and, when
    the answer is yes, the reader opens the door itself and says so
  * a count of every time each door actually opened, taken at the device end - the only place
    "did the door open once?" can be answered honestly
  * faults on demand: a reader that accepts a connection and never answers, one that has lost
    power (its address disappears from the LAN), one that is slow
  * a control API on :9100 for the scenarios

What it is not: HID firmware. The decision rules below are the vendor's documented ones as this
system already relies on them; anything the real reader does differently is outside what this can
show. That limit is stated in the rehearsal report.

Pure standard library.
"""

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_reader import FakeReader  # noqa: E402

COUNT = int(os.environ.get("READER_COUNT", "20"))
SUBNET = os.environ.get("READER_SUBNET", "10.107.0")
FIRST = int(os.environ.get("READER_FIRST_HOST", "11"))
PORT = int(os.environ.get("READER_PORT", "80"))
CONTROL_PORT = int(os.environ.get("READER_CONTROL_PORT", "9100"))
INTERFACE = os.environ.get("READER_INTERFACE", "eth0")
USERNAME = os.environ.get("READER_USERNAME", "admin")
PASSWORD = os.environ.get("READER_PASSWORD", "admin")

# The reader's own event codes (access_logs.event), as the hardware reports them.
EVENT_NOT_IDENTIFIED = 3
EVENT_DENIED = 6
EVENT_GRANTED = 7
EVENT_REMOTE_OPEN = 12


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class PlantReader(FakeReader):
    """One door. The stand-in reader, plus an address, a door, and a way to break it."""

    def __init__(self, number, ip):
        super().__init__(number, USERNAME, PASSWORD)
        self.number = number
        self.ip = ip
        self.address = ip if PORT == 80 else f"{ip}:{PORT}"
        self.mode = "ok"                 # ok | hang | error | off
        self.faces = set()               # the reader's own user ids that have a face template
        self.opens = []                  # every time the door actually opened
        self.notify_ok = 0
        self.notify_failed = 0
        self.requests = 0
        self.logins = 0

    # -- the door -----------------------------------------------------------

    def _door_opened(self, cause, user_id=0, registration=None):
        self.opens.append({"t": time.time(), "cause": cause, "userId": user_id,
                           "registration": registration})

    def scan(self, registration):
        """
        A person presents their face at this door.

        The reader decides on its own - that is the design: doors keep working when the building
        has no internet. Known face, in the door's group, inside their dated window: the door
        opens and the log says granted. Known but outside the window, or not in the group:
        denied. No template on this reader for that face: not identified, and nobody's id is
        attached to the row.
        """
        started = time.time()
        if self.mode == "off":
            return {"powered": False, "opened": False, "event": None, "t": started}

        uid = self.find_by_registration(registration)
        if uid is None or uid not in self.faces:
            event, logged_uid = EVENT_NOT_IDENTIFIED, 0
        else:
            user = self.users[uid]
            now = self.now()
            begin = int(user.get("begin_time") or 0)
            end = int(user.get("end_time") or 0)
            in_group = (uid, 1) in self.groups
            in_window = (not begin or now >= begin) and (not end or now <= end)
            event = EVENT_GRANTED if (in_group and in_window) else EVENT_DENIED
            logged_uid = uid

        opened = event == EVENT_GRANTED
        if opened:
            self._door_opened("face", logged_uid, registration)
        entry = self.record_scan(user_id=logged_uid, event=event)
        return {"powered": True, "opened": opened, "event": event, "logId": entry["id"],
                "userId": logged_uid, "t": started}

    # -- what the base class does, with the device end made visible ----------

    def _notify(self, monitor, entry):
        """Post the access-log insert from THIS reader's address, as the hardware would."""
        if self.mode == "off":
            return
        body = json.dumps({
            "device_id": int(self.device_id[-6:]),
            "object_changes": [{
                "object": "access_logs",
                "type": "inserted",
                "values": {k: str(v) for k, v in entry.items()},
            }],
        }).encode()
        path = "/" + monitor["path"].strip("/") + "/dao"
        try:
            timeout = int(monitor.get("request_timeout") or 5000) / 1000.0
            conn = http.client.HTTPConnection(monitor["hostname"], int(monitor["port"]),
                                              timeout=timeout, source_address=(self.ip, 0))
            conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if 200 <= resp.status < 300:
                self.notify_ok += 1
            else:
                self.notify_failed += 1
        except Exception:  # noqa: BLE001 - a reader does not care whether anyone was listening
            self.notify_failed += 1

    def _ep_execute_actions(self, payload):
        before = self.next_log_id
        code, body = super()._ep_execute_actions(payload)
        if code == 200 and self.next_log_id > before:
            self._door_opened("remote")
        return code, body

    def _ep_remote_enroll(self, payload):
        code, body = super()._ep_remote_enroll(payload)
        if code == 200:
            self.faces.add(payload.get("user_id"))
        return code, body

    def _ep_destroy_objects(self, payload):
        code, body = super()._ep_destroy_objects(payload)
        self.faces &= set(self.users)
        return code, body

    def set_face(self, user_id):
        if user_id not in self.users:
            return 400, {"error": "no such user", "code": 6}
        self.faces.add(user_id)
        return 200, {}

    # -- faults --------------------------------------------------------------

    def power(self, on):
        """
        Cut or restore this reader's power.

        Off means its address leaves the LAN, so the agent's calls go unanswered the way they do
        when a PoE port drops. On again is a reboot: sessions are gone (the next call gets 401 and
        the agent logs in again), while users, faces, the access log and the monitor setting are
        on the device's flash and survive.
        """
        if on:
            subprocess.run(["ip", "addr", "add", f"{self.ip}/24", "dev", INTERFACE],
                           capture_output=True)
            self.sessions = set()
            self.mode = "ok"
        else:
            self.mode = "off"
            subprocess.run(["ip", "addr", "del", f"{self.ip}/24", "dev", INTERFACE],
                           capture_output=True)

    def summary(self):
        return {
            "number": self.number, "address": self.address, "deviceId": self.device_id,
            "mode": self.mode, "users": len(self.users), "faces": len(self.faces),
            "groupMembers": len(self.groups), "logs": len(self.access_logs),
            "opens": len(self.opens),
            "opensByFace": sum(1 for o in self.opens if o["cause"] == "face"),
            "opensRemote": sum(1 for o in self.opens if o["cause"] == "remote"),
            "monitor": self.monitor, "notifyOk": self.notify_ok,
            "notifyFailed": self.notify_failed, "requests": self.requests, "logins": self.logins,
        }

    def detail(self):
        users = []
        for uid, user in sorted(self.users.items()):
            users.append({
                "id": uid, "registration": user.get("registration"), "name": user.get("name"),
                "beginTime": user.get("begin_time"), "endTime": user.get("end_time"),
                "hasFace": uid in self.faces, "inGroup": (uid, 1) in self.groups,
            })
        return {**self.summary(), "userList": users, "openList": list(self.opens),
                "logTail": list(self.access_logs[-500:]), "readerNow": self.now()}


READERS = {}
BY_IP = {}
SCANNER = {"every": None, "stop": threading.Event(), "scans": 0}


def send_json(handler, code, obj):
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ReaderHttp(BaseHTTPRequestHandler):
    """
    All twenty readers' port 80. Which reader a request is for is the address it arrived on, so a
    reader whose address has been taken off the LAN (a power cut) needs no socket surgery.
    """
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        reader = BY_IP.get(self.connection.getsockname()[0])
        if reader is None:
            return send_json(self, 404, {"error": "no reader on this address"})
        send_json(self, 200, {"fake_reader": reader.number, "device_id": reader.device_id})

    def do_POST(self):
        reader = BY_IP.get(self.connection.getsockname()[0])
        if reader is None:
            return send_json(self, 404, {"error": "no reader on this address"})

        url = urlparse(self.path)
        endpoint = url.path.lstrip("/")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        reader.requests += 1

        if reader.mode == "hang":
            # Up, listening, and silent: the failure a breaker has to catch. The connection is
            # held until the caller gives up (or the reader is told to recover).
            deadline = time.time() + 600
            while reader.mode == "hang" and time.time() < deadline:
                time.sleep(0.25)
            self.close_connection = True
            return
        if reader.mode == "error":
            return send_json(self, 500, {"error": "internal error", "code": 99})

        if endpoint == "hidlogin.fcgi":
            reader.logins += 1

        if endpoint.startswith("user_set_image"):
            # The one call that is not JSON: raw image bytes, everything else in the query.
            if query.get("session") not in reader.sessions:
                return send_json(self, 401, {"error": "Invalid session", "code": 2})
            if not raw:
                return send_json(self, 400, {"error": "empty image", "code": 8})
            try:
                user_id = int(query.get("user_id") or 0)
            except ValueError:
                user_id = 0
            return send_json(self, *reader.set_face(user_id))

        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return send_json(self, 400, {"error": "bad json", "code": 7})
        code, body = reader.handle(endpoint, payload, query.get("session"))
        send_json(self, code, body)


def scan_loop(every, stop):
    """Soak load: every reader invents a badge-in on a timer, cycling through the people it holds."""
    turn = 0
    while not stop.wait(every):
        turn += 1
        for reader in list(READERS.values()):
            if reader.mode != "ok":
                continue
            regs = [u.get("registration") for uid, u in sorted(reader.users.items())
                    if uid in reader.faces]
            reader.scan(regs[turn % len(regs)] if regs else "00000000-0000-0000-0000-000000000000")
            SCANNER["scans"] += 1


class Control(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        return json.loads(raw) if raw else {}

    @staticmethod
    def _reader(parts):
        try:
            return READERS[int(parts[1])]
        except (KeyError, ValueError, IndexError):
            return None

    def do_GET(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts == ["state"]:
            return send_json(self, 200, {
                "now": time.time(), "scanEvery": SCANNER["every"], "soakScans": SCANNER["scans"],
                "readers": [r.summary() for r in READERS.values()]})
        if len(parts) == 2 and parts[0] == "readers":
            reader = self._reader(parts)
            if reader:
                return send_json(self, 200, reader.detail())
        send_json(self, 404, {"error": "not found"})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = self._body()

        if parts == ["scan-every"]:
            SCANNER["stop"].set()
            SCANNER["stop"] = threading.Event()
            SCANNER["every"] = body.get("seconds")
            if SCANNER["every"]:
                threading.Thread(target=scan_loop, args=(float(SCANNER["every"]), SCANNER["stop"]),
                                 daemon=True).start()
            return send_json(self, 200, {"scanEvery": SCANNER["every"]})

        if len(parts) == 3 and parts[0] == "readers":
            reader = self._reader(parts)
            if reader is None:
                return send_json(self, 404, {"error": "no such reader"})
            if parts[2] == "scan":
                return send_json(self, 200, reader.scan(body.get("registration") or ""))
            if parts[2] == "mode":
                mode = body.get("mode")
                if mode not in ("ok", "hang", "error"):
                    return send_json(self, 400, {"error": "mode is ok | hang | error"})
                reader.mode = mode
                reader.latency = float(body.get("latencyMs") or 0) / 1000.0
                return send_json(self, 200, reader.summary())
            if parts[2] == "power":
                reader.power(bool(body.get("on")))
                return send_json(self, 200, reader.summary())
        send_json(self, 404, {"error": "not found"})


def main():
    for n in range(1, COUNT + 1):
        ip = f"{SUBNET}.{FIRST + n - 1}"
        added = subprocess.run(["ip", "addr", "add", f"{ip}/24", "dev", INTERFACE],
                               capture_output=True, text=True)
        if added.returncode != 0 and "exists" not in (added.stderr or "").lower():
            raise SystemExit(f"could not add {ip} to {INTERFACE}: {added.stderr.strip()} "
                             "(the container needs NET_ADMIN)")
        reader = PlantReader(n, ip)
        READERS[n] = reader
        BY_IP[ip] = reader

    server = ThreadingHTTPServer(("0.0.0.0", PORT), ReaderHttp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    control = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), Control)
    print(f"{COUNT} readers on the plant LAN: {READERS[1].address} .. {READERS[COUNT].address}; "
          f"control API on :{CONTROL_PORT}", flush=True)
    try:
        control.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
