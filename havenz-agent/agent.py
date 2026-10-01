#!/usr/bin/env python3
"""
Havenz Site Agent
=================

Why this exists
---------------
Havenz runs on Cloud Run. The door readers it manages sit on a private network inside a customer's
building. There is no route from one to the other, and the previous answer — a VPN tunnel from our
cloud into the customer's LAN — is both an operational burden and a sales blocker: asking a power
plant's IT department for a tunnel into their internal network is a conversation that ends badly.

So the direction is inverted. Nothing dials in. This agent runs on the site's own Raspberry Pi,
holds an outbound connection to Havenz, and performs terminal work locally on behalf of the
backend. The only network requirement at any site is what every site already has: the ability to
make an outbound HTTPS request.

What it does
------------
  1. PAIR      — once, with a code an admin generates in the Havenz app. Exchanges it for this
                 site's own key, stored in /data so it survives restarts and add-on updates.
  2. HEARTBEAT — proof of life on a fixed interval, whether or not there is anything to say. Havenz
                 alerts on this going quiet, so an agent that only spoke when it had news would be
                 indistinguishable from a dead one.

Later slices add the command channel, the LAN webhook listener for access events, and reader
discovery. Each arrives only once the one before it has been proven on real hardware.

What it deliberately is not
---------------------------
It is not a proxy. Havenz sends *intent* — "open door 1", "sync this user" — and the agent decides
how to achieve it against the reader. A dumb HTTP proxy would put a login round trip through the
cloud on every unlock and could never meet the latency budget a person standing at a door implies.

Pure standard library (no pip installs). Run:  python3 agent.py config.json [--register CODE]
"""

import base64
import http.client
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from socketserver import ThreadingMixIn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("havenz-agent")


def _prefer_ipv4():
    """
    Try IPv4 addresses before IPv6 for every outbound call.

    The backend resolves to eight IPv6 addresses and eight IPv4 ones. Python tries them in the
    order the resolver returns, giving each the full socket timeout, and does not do Happy
    Eyeballs. On a network whose IPv6 is advertised but does not carry traffic — a VirtualBox
    guest, plenty of sites — every call therefore waits out eight dead routes before it reaches a
    working one. Measured here: 8 x 40s = 320s to fetch a reader list, 8 x 20s = 166s to pair.

    That is not slowness, it is a broken agent. Unlock commands expire after ten seconds and
    command leases after forty-five, so a door opens only if the whole round trip beats a deadline
    the agent is already minutes past. It looks exactly like an agent that is offline, which is how
    it was first misread.

    Sorting rather than filtering: an IPv6-only site still works, it just tries IPv4 first and
    falls through. The cost there is a few failed connects on a network where IPv4 genuinely does
    not exist; the cost of the reverse is every door in the building.
    """
    original = socket.getaddrinfo

    def ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
        results = original(host, port, family, type, proto, flags)
        return sorted(results, key=lambda entry: 0 if entry[0] == socket.AF_INET else 1)

    socket.getaddrinfo = ipv4_first


_prefer_ipv4()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Serves requests concurrently. Twenty readers can post at the same moment."""
    daemon_threads = True
    allow_reuse_address = True

AGENT_VERSION = "0.6.0"

# How far our clock may differ from the backend's before we say so.
#
# A Pi has no real-time clock. One that boots before the network is up believes it is 1970 until NTP
# catches up, and command expiry is judged against wall time — so a badly wrong clock silently
# discards perfectly valid work. Better to name it in the log than to debug it at a plant.
CLOCK_SKEW_WARN_SECONDS = 30

# Backoff when the backend is unreachable. Capped so a site that was offline overnight comes back
# within a minute of the link returning, rather than sleeping through the morning.
BACKOFF_START_SECONDS = 5
BACKOFF_MAX_SECONDS = 60

# Where a reader posts its events, relative to the agent's address.
#
# A BASE, not a complete path: the reader builds hostname:port/{path}/{kind} and uses this one
# value for /dao, /door, /operation_mode and /user_image alike. No leading slash, no kind suffix —
# getting that wrong 404s every notification, which looks exactly like a reader that has gone
# quiet. Kept in step with the backend's AmicoApiService.MonitorBasePath.
MONITOR_BASE_PATH = "api/amico/notifications"


# Addresses that are ours but useless to a reader.
#
# Home Assistant runs add-ons on an internal Docker network at 172.30.32.0/23. An agent behind that
# bridge asks the routing table for its source address and is truthfully told 172.30.x.x — an
# address no reader on the site's LAN can route to. Configuring readers with it produces the worst
# possible failure: every call succeeds, every event vanishes.
#
# The add-on declares host_network so this should not arise, but the check stays: if the answer is
# ever one of these, something is wrong in a way that is silent otherwise, and it should be loud.
UNREACHABLE_PREFIXES = ("172.30.", "172.17.", "127.")


def local_address_for(reader_ip):
    """
    Our own address as this reader would see it.

    Asked of the routing table rather than assumed, because a Home Assistant box commonly has
    several interfaces — Docker bridges, a VPN, wired and wireless — and the reader must be given
    the one that actually reaches back here. No packet is sent; connect() on a UDP socket only
    picks a route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((reader_ip, 80))
        address = probe.getsockname()[0]
    except Exception:  # noqa: BLE001
        address = socket.gethostbyname(socket.gethostname())
    finally:
        probe.close()

    if address.startswith(UNREACHABLE_PREFIXES):
        raise ReaderError(
            f"the agent's own address is {address}, which is a container network the reader cannot "
            "reach. The add-on needs host_network enabled — without it the reader would be pointed "
            "at an address that silently swallows every event.")

    return address


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if "api_url" not in cfg:
        raise SystemExit("config error: missing 'api_url'")
    cfg.setdefault("register_path", "/api/bridge/register")
    cfg.setdefault("heartbeat_path", "/api/agent/heartbeat")
    cfg.setdefault("heartbeat_interval_seconds", 30)
    cfg.setdefault("setup_port", 8099)
    cfg.setdefault("discovery_enabled", False)
    cfg.setdefault("discovery_interval_seconds", 900)
    # Which doors this agent has already opened, kept where a restart cannot lose it. /data is the
    # add-on's own persistent volume — the same place the hub key lives. (A box paired under 0.5.0
    # has "/data/executed.json" saved here; executed_store_path() reads that as the journal beside it.)
    cfg.setdefault("executed_store_path", "/data/executed.jsonl")
    # Command results wait here until Havenz has them, so a refused or undeliverable result is
    # sent again instead of being thrown away.
    cfg.setdefault("result_outbox_path", "/data/results.jsonl")
    # Reader events wait here, on disk, until Havenz has them. Written before the reader is
    # answered, so neither a restart nor an uplink outage can lose one.
    cfg.setdefault("event_queue_path", "/data/events.jsonl")
    # Which address is which door - no credentials - so an event arriving while Havenz is
    # unreachable can still be attributed after a restart.
    cfg.setdefault("roster_path", "/data/roster.json")
    return cfg


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

def backend_headers(cfg):
    """
    A paired agent authenticates with its own key.

    No token exchange, unlike the browser panels. A screen needs a short-lived credential because a
    browser cannot keep a secret; this agent has a filesystem and lives in a locked enclosure, so a
    refresh cycle would buy nothing but one more thing to expire at three in the morning.
    """
    return {
        "Content-Type": "application/json",
        "X-Hub-Key": cfg.get("hub_key", ""),
        "X-Agent-Version": AGENT_VERSION,
    }


def backend_post(cfg, path, payload, timeout=20):
    """POST JSON to the backend as this agent; return the parsed response."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    url = f"{cfg['api_url'].rstrip('/')}{path}"
    req = urllib.request.Request(url, data=body, method="POST", headers=backend_headers(cfg))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def backend_get(cfg, path, timeout=40):
    """GET from the backend as this agent; return the parsed response."""
    url = f"{cfg['api_url'].rstrip('/')}{path}"
    req = urllib.request.Request(url, method="GET", headers=backend_headers(cfg))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

class ReaderError(Exception):
    """The reader refused, or could not be reached. Carries the reader's own words."""


class ReaderOutcomeUnknown(ReaderError):
    """
    The request went to the reader and no answer came back.

    Not the same thing as a failure, and for a door the difference is the whole point. A reader
    that accepts "open" and is then slow to say so - or drops the connection - has very possibly
    opened the door. Reporting that as "failed" tells the person at the door to tap again, and
    tapping again is how a door opens twice. Seen on the bench: the reader opened, answered after
    the ten-second timeout, and the tap was recorded as "could not reach terminal".

    A subclass, so every caller that only cares that the work did not complete still catches it
    as a ReaderError; only an unlock treats it differently.
    """


def _may_have_reached_the_reader(error):
    """
    True when a transport error leaves it open whether the reader acted.

    A refused connection, an unroutable address or a name that does not resolve never reached the
    reader: that is a failure. A timeout, or a connection that was accepted and then dropped or
    reset, may well have - the command was on the wire. urllib does not say whether a timeout hit
    while connecting or while waiting for the reply, so every timeout is treated as "may have": for
    a door, wrongly saying "check the door" costs a glance; wrongly saying "failed" costs a second
    opening.
    """
    reason = getattr(error, "reason", error)
    for e in (error, reason):
        if isinstance(e, (ConnectionRefusedError, socket.gaierror)):
            return False
        if isinstance(e, (socket.timeout, TimeoutError, http.client.RemoteDisconnected,
                          http.client.IncompleteRead, ConnectionResetError, ConnectionAbortedError,
                          BrokenPipeError)):
            return True
    return "timed out" in str(error).lower()


class Reader:
    """
    A session-holding client for one HID Amico terminal on this LAN.

    Holding the session here is the whole reason the agent keeps credentials. The alternative —
    relaying a login handshake through the cloud before every call — turns a 50ms unlock into
    several hundred milliseconds of round trips, on the one operation where somebody is standing
    at a door waiting.

    Firmware quirks belong in this class rather than in the backend, so they get fixed once:
      - set_configuration rejects non-strings whatever the documentation says
      - the monitor 'path' is a base, not a full path
      - numbers come back quoted
    """

    def __init__(self, terminal_id, name, ip, username, password, timeout=10):
        self.terminal_id = terminal_id
        self.name = name
        # May carry a port ("10.0.0.250:8080"). Real readers answer on 80, but a site behind a
        # port-forward or a bank of simulated readers on one host will not, and every URL below
        # interpolates this whole string so both work without a special case.
        self.ip = ip
        self.host = ip.rsplit(":", 1)[0] if ":" in ip else ip
        self._username = username
        self._password = password
        self._timeout = timeout
        self._session = None

    def _login(self):
        body = json.dumps({"login": self._username, "password": self._password}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.ip}/hidlogin.fcgi", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ReaderError(
                f"login rejected by {self.ip}: HTTP {e.code} "
                f"{e.read().decode('utf-8', 'replace')[:160]}") from None
        except Exception as e:  # noqa: BLE001
            raise ReaderError(f"cannot reach {self.ip}: {e}") from None

        token = data.get("session")
        if not token:
            raise ReaderError(f"{self.ip} returned no session token")
        self._session = token
        return token

    def call(self, endpoint, payload=None):
        """
        POST to a .fcgi endpoint, acquiring or refreshing the session as needed.

        Retries exactly once on 401. A session expires on its own schedule and the first call
        after that is the one that discovers it, so a single silent re-login is the difference
        between working and failing every few hours for no visible reason.
        """
        for attempt in (1, 2):
            session = self._session or self._login()
            url = f"http://{self.ip}/{endpoint}?session={session}"
            body = json.dumps(payload or {}).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, method="POST", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self._session = None      # expired; re-login and try once more
                    continue
                raise ReaderError(
                    f"{endpoint} on {self.ip}: HTTP {e.code} "
                    f"{e.read().decode('utf-8', 'replace')[:160]}") from None
            except Exception as e:  # noqa: BLE001
                if _may_have_reached_the_reader(e):
                    raise ReaderOutcomeUnknown(f"{endpoint} on {self.ip}: {e}") from None
                raise ReaderError(f"{endpoint} on {self.ip}: {e}") from None

    # -- operations -------------------------------------------------------

    def system_info(self):
        data = self.call("system_information.fcgi")
        return {
            "deviceId": str(data.get("device_id") or ""),
            # Observed empty on this firmware. Reported as-is rather than invented, so the gap
            # stays visible instead of being papered over with a plausible-looking string.
            "firmwareVersion": str(data.get("firmware_version") or ""),
            "ipAddress": self.ip,
        }

    def open_door(self, door=1):
        # Not an "open door" endpoint — the reader models this as triggering its security box.
        # The parameters field is a string of key=value, not JSON, which is easy to get wrong and
        # fails silently as a no-op rather than an error.
        self.call("execute_actions.fcgi", {
            "actions": [{"action": "sec_box", "parameters": f"door={int(door)}"}]
        })
        return {}

    def configure_monitor(self, host, port):
        """
        Point this reader at the agent, so its access events stay on the LAN.

        `path` is a BASE, not a complete endpoint: the reader builds every URL as
        hostname:port/{path}/{kind} and uses this one value for /dao, /door, /operation_mode and
        /user_image alike. A leading slash or a kind suffix here produces 404s that look exactly
        like a reader that has stopped reporting.

        Everything is a string. set_configuration.fcgi answers non-strings with
        {"error":"Invalid data (string expected)"} whatever the guide says a field's type is.
        """
        self.call("set_configuration.fcgi", {
            "monitor": {
                "request_timeout": "5000",
                "hostname": str(host),
                "port": str(port),
                "path": MONITOR_BASE_PATH,
            }
        })
        return {"hostname": str(host), "port": str(port), "path": MONITOR_BASE_PATH}

    @staticmethod
    def _require(payload, command, *fields):
        """
        Fail a malformed command with a message someone can act on.

        Without this a missing field surfaces as `agent error: 'day'` — a bare KeyError, with no
        indication of which command was malformed or what it wanted. That is the difference between
        a five-second diagnosis and an afternoon of guessing, at a site nobody can walk into.
        """
        missing = [f for f in fields if payload.get(f) is None]
        if missing:
            raise ReaderError(
                f"{command} was sent without {', '.join(missing)} — the agent cannot carry it out")

    def sync_clock(self, payload):
        self._require(payload, "SyncClock", "day", "month", "year", "hour", "minute", "second")
        """
        Set the clock from values the backend worked out.

        The wall-clock components and the NTP offset arrive ready-made because the daylight-saving
        reasoning behind them is subtle and already correct on the backend. Re-deriving it here
        would give us two versions to keep in step and timestamps an hour apart twice a year.
        """
        self.call("set_system_time.fcgi", {
            "day": int(payload["day"]), "month": int(payload["month"]), "year": int(payload["year"]),
            "hour": int(payload["hour"]), "minute": int(payload["minute"]),
            "second": int(payload["second"]),
        })
        self.call("set_configuration.fcgi", {
            "ntp": {"enabled": "1", "timezone": str(payload.get("ntpTimezone", "UTC+0"))}
        })
        return {}

    # -- users ------------------------------------------------------------

    def find_user_id(self, registration):
        """The reader's own numeric id for a Havenz user, or None."""
        data = self.call("load_objects.fcgi", {
            "object": "users",
            "where": [{"field": "registration", "op": "=", "value": registration}],
        })
        # Firmware variation: results come back under "data" on some builds and "users" on others.
        rows = data.get("data") or data.get("users") or []
        return int(rows[0]["id"]) if rows else None

    def _add_to_default_group(self, terminal_user_id):
        """
        Put the user in group 1, which is what actually authorises them at the door.

        Creating the user is not enough on its own — without this they exist on the reader and are
        refused entry. Failure is ignored because the usual cause is that they are already a
        member, which the reader reports as an error rather than a no-op.
        """
        try:
            self.call("create_objects.fcgi", {
                "object": "user_groups",
                "values": [{"user_id": terminal_user_id, "group_id": 1}],
            })
        except ReaderError:
            pass

    def create_user(self, payload):
        self._require(payload, "CreateUser", "registration")
        registration = payload["registration"]
        existing = self.find_user_id(registration)

        if existing is None:
            self.call("create_objects.fcgi", {
                "object": "users",
                "values": [{
                    "registration": registration,
                    "name": payload.get("name"),
                    "begin_time": payload.get("beginTime"),
                    "end_time": payload.get("endTime"),
                }],
            })
            existing = self.find_user_id(registration)

        if existing is not None:
            self._add_to_default_group(existing)
        return {"terminalUserId": existing or 0}

    def update_user(self, payload):
        self._require(payload, "UpdateUser", "registration")
        registration = payload["registration"]
        terminal_user_id = self.find_user_id(registration)
        if terminal_user_id is None:
            return self.create_user(payload)   # not there yet; creating is the correct update

        self.call("modify_objects.fcgi", {
            "object": "users",
            "where": {"users": {"id": terminal_user_id}},
            "values": {
                "registration": registration,
                "name": payload.get("name"),
                "begin_time": payload.get("beginTime"),
                "end_time": payload.get("endTime"),
            },
        })
        self._add_to_default_group(terminal_user_id)
        return {"terminalUserId": terminal_user_id}

    def delete_user(self, payload):
        self._require(payload, "DeleteUser", "registration")
        terminal_user_id = self.find_user_id(payload["registration"])
        if terminal_user_id is None:
            return {}      # already gone; deleting is idempotent by intent
        self.call("destroy_objects.fcgi", {
            "object": "users",
            "where": {"users": {"id": terminal_user_id}},
        })
        return {}

    def upload_face_photo(self, payload):
        """
        Push a face image for an existing user.

        The only call that is not JSON: the image goes as a raw octet-stream body with everything
        else in the query string.
        """
        self._require(payload, "UploadFacePhoto", "registration", "jpegBase64")
        terminal_user_id = self.find_user_id(payload["registration"])
        if terminal_user_id is None:
            raise ReaderError(
                f"user {payload['registration']} is not on {self.ip} yet, so there is nobody to "
                "attach a face to")

        jpeg = base64.b64decode(payload["jpegBase64"])
        stamp = int(time.time())

        for attempt in (1, 2):
            session = self._session or self._login()
            url = (f"http://{self.ip}/user_set_image.fcgi?session={session}"
                   f"&user_id={terminal_user_id}&timestamp={stamp}&match=1")
            req = urllib.request.Request(
                url, data=jpeg, method="POST",
                headers={"Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=max(self._timeout, 30)) as resp:
                    resp.read()
                    return {"terminalUserId": terminal_user_id}
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self._session = None
                    continue
                raise ReaderError(
                    f"face upload to {self.ip}: HTTP {e.code} "
                    f"{e.read().decode('utf-8','replace')[:160]}") from None
            except Exception as e:  # noqa: BLE001
                raise ReaderError(f"face upload to {self.ip}: {e}") from None

    def remote_enroll(self, payload):
        """
        Capture a face at the reader, with someone standing in front of it.

        Synchronous: the reader holds the request open until it has an image or gives up, so the
        timeout here is a person's patience rather than the network's.
        """
        self._require(payload, "StartRemoteEnrollment", "registration")
        terminal_user_id = self.find_user_id(payload["registration"])
        if terminal_user_id is None:
            raise ReaderError(
                f"user {payload['registration']} must be synced to {self.ip} before enrolment")

        previous, self._timeout = self._timeout, 100
        try:
            data = self.call("remote_enroll.fcgi", {
                "type": "face", "user_id": terminal_user_id, "save": True, "sync": True,
            })
        finally:
            self._timeout = previous

        image = data.get("image")
        if not image:
            raise ReaderError("the reader finished enrolment but sent back no image")
        return {"imageBase64": image}

    # -- events -----------------------------------------------------------

    def access_logs(self, after_log_id=None):
        """
        The reader's access-log rows - all of them, or only those after a mark.

        Timestamps are passed up untouched. The reader runs on local time and reports that wall
        clock as if it were UTC; undoing that is the backend's job, in the one place that already
        does it correctly.

        `after_log_id` is the backend's high-water mark: the highest row it already holds (less an
        overlap it chooses). Havenz reads this log every thirty seconds as the safety net under the
        live events, and without a mark every one of those reads hauled the reader's entire history
        across the site's uplink to be thrown away row by row.

        The mark is the backend's, not ours, on purpose: only the backend knows what it has
        durably stored. This agent keeps no polling state, so restarting it can neither lose rows
        nor replay them.

        One trap. A reader that has been factory reset or replaced starts its log again at 1. Its
        highest id is then BELOW the mark, and "nothing newer than 5120" would hide everything it
        records until its counter climbed past 5120 - weeks, silently. So when the reader's highest
        id is below the mark, everything is returned and the result says so.
        """
        data = self.call("load_objects.fcgi", {"object": "access_logs"})
        entries = []
        for row in data.get("access_logs") or []:
            try:
                entries.append({
                    "id": int(row["id"]),
                    # 0 means nobody was identified, which is a real and common outcome.
                    "userId": int(row.get("user_id") or 0),
                    "event": int(row.get("event") or 0),
                    "time": int(row.get("time") or 0),
                })
            except (TypeError, ValueError):
                continue   # one malformed row must not cost us the rest of the log
        return apply_log_cursor(entries, after_log_id)

    def registration_map(self):
        """
        The reader's numeric user ids mapped to Havenz user GUIDs.

        Fetched whole rather than per event: a shift change is dozens of scans in a few minutes,
        and a round trip each would not keep up.
        """
        data = self.call("load_objects.fcgi", {"object": "users"})
        rows = data.get("data") or data.get("users") or []
        mapping = {}
        for row in rows:
            registration = row.get("registration")
            if row.get("id") is not None and registration:
                mapping[str(row["id"])] = str(registration)
        return {"map": mapping}


def apply_log_cursor(entries, after_log_id):
    """Rows above the mark - or all of them, flagged, when the reader's log has started again."""
    reader_max = max((e["id"] for e in entries), default=None)
    try:
        mark = int(after_log_id) if after_log_id is not None else 0
    except (TypeError, ValueError):
        mark = 0

    restarted = False
    if mark > 0 and reader_max is not None:
        if reader_max < mark:
            restarted = True
            log.warning("the reader's access log has started again (its highest row is %d, the "
                        "backend holds %d) - returning all of it", reader_max, mark)
        else:
            entries = [e for e in entries if e["id"] > mark]

    return {"entries": entries, "readerMaxLogId": reader_max, "logRestarted": restarted}


def register(cfg_path, cfg, code):
    """
    Exchange a one-time pairing code for this site's key.

    The same endpoint the sensor gateway uses: an agent and a sensor bridge are the same kind of
    thing to Havenz — a container bound to one property, holding its own key — so they share
    pairing, revocation and liveness rather than having two implementations of each.
    """
    code = code.strip().upper()
    # Declaring what we are lets the backend refuse a sensor-gateway code without consuming it, so
    # the installer can retype the same code on the right add-on. Without this the code is spent on
    # the mistake and they have to go back to the app for a new one.
    body = json.dumps({
        "pairingCode": code,
        "agentVersion": AGENT_VERSION,
        "expectedKind": "agent",
    }).encode("utf-8")
    url = f"{cfg['api_url'].rstrip('/')}{cfg['register_path']}"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"pairing failed: HTTP {e.code} — {e.read().decode('utf-8', 'replace')}")

    # Belt and braces. A backend that predates `expectedKind` will happily pair us to a sensor hub,
    # and the failure that produces — every later call rejected, for a reason nobody standing in a
    # plant room can see — is bad enough to be worth catching on both sides.
    kind = (data.get("kind") or "").lower()
    if kind and kind != "agent":
        raise SystemExit(
            f"that code is for a '{kind}' hub, not a site agent. In the Havenz app, add a Site "
            f"Agent for this property and use the code it gives you.")

    cfg["hub_key"] = data["apiKey"]
    cfg["hub_id"] = data.get("hubId")
    cfg["property_id"] = data.get("propertyId")
    cfg["company_id"] = data.get("companyId")
    cfg["site_name"] = data.get("name")
    save_config(cfg_path, cfg)
    log.info("paired as '%s' to property %s", data.get("name"), data.get("propertyId"))
    return data


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

# What the status page shows. Held in memory only — it is a view of the last cycle, not state we
# would ever act on, so there is nothing here worth persisting or worth trusting after a restart.
STATE = {
    "last_heartbeat_at": None,
    "last_error": None,
    "terminals": [],
    "clock_skew_seconds": None,
    "readers": {},
}


def parse_server_time(value):
    """
    Parse the backend's UTC timestamp into epoch seconds. Returns None if unparseable.

    Fiddlier than it looks, for two reasons that both produce silence rather than an error:

    1. .NET emits up to 7 fractional digits ("...05.1234567Z"); Python's fromisoformat accepts at
       most 6 and rejects the rest outright. The fraction must be split off by scanning the digit
       run only — sweeping up every digit in the tail also swallows the timezone offset.
    2. A timestamp with no offset parses as naive and .timestamp() then reads it as local time,
       which on a machine outside UTC yields a skew warning of exactly the offset. Assume UTC,
       because that is the only thing this endpoint ever sends.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")

    if "." in text:
        head, _, tail = text.partition(".")
        i = 0
        while i < len(tail) and tail[i].isdigit():
            i += 1
        fraction, offset = tail[:i], tail[i:]
        text = f"{head}.{fraction[:6]}{offset}" if fraction else f"{head}{offset}"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def heartbeat(cfg):
    """One heartbeat: report we are alive, learn what we are responsible for."""
    data = backend_post(cfg, cfg["heartbeat_path"], {"agentVersion": AGENT_VERSION})

    terminals = data.get("terminals") or []
    STATE["terminals"] = terminals
    STATE["last_heartbeat_at"] = time.time()
    STATE["last_error"] = None

    # A reader whose address has moved.
    #
    # Readers are on DHCP by default and leases do change — a router reboot, a lease expiry, a
    # reader plugged into a different segment. Holding the address we were first told, forever,
    # means that door silently stops working until somebody restarts the add-on, and the logs say
    # only "cannot reach", naming an address nothing has answered on for hours.
    #
    # The heartbeat already carries each terminal's current address, so the correction is free.
    held = STATE.get("readers") or {}
    for terminal in terminals:
        reader = held.get(terminal.get("id"))
        if reader and terminal.get("ipAddress") and reader.ip != terminal["ipAddress"]:
            log.info("reader %s has moved from %s to %s — reloading it",
                     reader.name, reader.ip, terminal["ipAddress"])
            held.pop(terminal["id"], None)
            breaker_record(terminal["id"], True, reader.name)   # its old address failing is not its fault
            STATE["readers"] = {}                               # refetched, with credentials, on next use

    server_epoch = parse_server_time(data.get("serverTimeUtc"))
    if server_epoch is not None:
        skew = time.time() - server_epoch
        STATE["clock_skew_seconds"] = round(skew, 1)
        if abs(skew) > CLOCK_SKEW_WARN_SECONDS:
            # Loud, because the symptom this causes — commands quietly expiring — looks like a
            # network fault and would otherwise be diagnosed as one.
            log.warning(
                "clock is %.0fs %s the backend. Command expiry is judged against wall time, so "
                "this will cause work to be discarded. Check NTP on this device.",
                abs(skew), "ahead of" if skew > 0 else "behind")

    return terminals


def heartbeat_loop(cfg):
    """Heartbeat forever, backing off while the backend is unreachable."""
    interval = int(cfg["heartbeat_interval_seconds"])
    backoff = BACKOFF_START_SECONDS
    known = None

    while True:
        try:
            terminals = heartbeat(cfg)
            backoff = BACKOFF_START_SECONDS

            # Log the roster only when it changes. A line every thirty seconds forever would bury
            # the one line that matters on the day something is wrong.
            names = sorted(t.get("name", "?") for t in terminals)
            if names != known:
                known = names
                log.info("serving %d terminal(s): %s",
                         len(names), ", ".join(names) if names else "none yet")

            time.sleep(interval)

        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            STATE["last_error"] = f"HTTP {e.code}: {detail}"
            if e.code in (401, 403):
                # Not retryable by waiting. The key was revoked, or this agent was re-paired
                # elsewhere. Say so plainly rather than emitting an auth failure every 30s forever.
                log.error("this agent is no longer authorised (HTTP %d). Re-pair it from the "
                          "Havenz app: %s", e.code, detail)
                time.sleep(BACKOFF_MAX_SECONDS)
            elif e.code == 429:
                # Told how long to stay away. A heartbeat that waits exactly that long is back
                # sooner than one that guesses, and Havenz calls an agent offline after ninety
                # seconds of silence.
                delay = retry_after_seconds(e, backoff)
                log.warning("heartbeat failed (HTTP %d): %s — retrying in %ds", e.code, detail, delay)
                time.sleep(delay)
            else:
                log.warning("heartbeat failed (HTTP %d): %s — retrying in %ds", e.code, detail, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)

        except Exception as e:  # noqa: BLE001 — the loop must outlive every transport failure
            STATE["last_error"] = str(e)
            log.warning("heartbeat failed (%s) — retrying in %ds", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)


# ---------------------------------------------------------------------------
# Finding readers on this network
# ---------------------------------------------------------------------------

# HID's block of MAC addresses. Every Amico reader's address begins with this, so it is what
# distinguishes one from the printers, phones and thermostats sharing the network.
HID_OUI = "fc:52:ce"

# How long to wait for each address to answer. Deliberately short: this is a sweep of every host on
# a /24, and a reader that is up answers in single-digit milliseconds on its own LAN.
PROBE_TIMEOUT = 0.35


def own_address():
    """This machine's address on the network it reaches the world through."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))       # no packet is sent; this only selects a route
        return probe.getsockname()[0]
    except Exception:  # noqa: BLE001
        return socket.gethostbyname(socket.gethostname())
    finally:
        probe.close()


def arp_table():
    """
    The kernel's address -> MAC table, as {ip: mac}.

    Read from /proc/net/arp rather than shelling out to `arp`, which is not present in the add-on's
    Alpine base. Only entries the kernel has actually resolved appear here, which is precisely what
    we want: it is evidence something answered, not a guess.
    """
    entries = {}
    try:
        with open("/proc/net/arp", encoding="utf-8") as f:
            next(f, None)                     # header
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    entries[parts[0]] = parts[3].lower()
    except OSError:
        pass                                  # not Linux, or no permission; discovery simply finds nothing
    return entries


def sweep(subnet_prefix, workers=32):
    """
    Touch every address on the /24 so the kernel learns their MAC addresses.

    A TCP connect to port 80 rather than an ICMP ping: raw sockets need privileges the add-on does
    not have, and a refused connection populates the ARP table just as well as an accepted one.
    """
    targets = [f"{subnet_prefix}.{n}" for n in range(1, 255)]
    lock = threading.Lock()

    def worker():
        while True:
            with lock:
                if not targets:
                    return
                ip = targets.pop()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(PROBE_TIMEOUT)
            try:
                s.connect((ip, 80))
            except Exception:  # noqa: BLE001 — refused still teaches the kernel the MAC
                pass
            finally:
                s.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def discover_readers(cfg):
    """
    Look for HID readers on this network and report what was found.

    Finding a reader does nothing to it. Nothing is configured, nothing is adopted, no credentials
    are tried — the candidate is reported and sits inert until a person claims it in Zhub. Scanning
    identifies hardware; it does not confer trust, and configuring a device found on somebody's
    network without being asked is how you lose an account.

    Off unless a site enables it, for the same reason.
    """
    if not cfg.get("discovery_enabled"):
        return []

    address = own_address()
    prefix = address.rsplit(".", 1)[0]
    log.info("scanning %s.0/24 for readers", prefix)

    sweep(prefix)
    candidates = [
        {"mac": mac, "ipAddress": ip}
        for ip, mac in sorted(arp_table().items())
        if mac.startswith(HID_OUI)
    ]

    if not candidates:
        log.info("no readers found on %s.0/24", prefix)
        return []

    log.info("found %d reader(s): %s", len(candidates),
             ", ".join(c["ipAddress"] for c in candidates))
    try:
        backend_post(cfg, "/api/agent/discovered", {"readers": candidates})
    except Exception as e:  # noqa: BLE001
        log.warning("could not report discovered readers (%s)", e)
    return candidates


def discovery_loop(cfg):
    """
    Sweep on a slow cycle.

    Slow because the point is not to notice a reader within seconds — it is to notice one that has
    been plugged in since yesterday, and to re-find one whose address has moved. A sweep every few
    minutes across a customer's network would be rude and pointless in equal measure.
    """
    interval = int(cfg.get("discovery_interval_seconds", 900))
    while True:
        try:
            discover_readers(cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("discovery sweep failed (%s)", e)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

# What we have already carried out.
#
# Delivery is at-least-once by design: a lease whose result never reached the backend is retried,
# and the retry is indistinguishable from a first delivery. Doing the work twice is harmless for a
# user sync and unacceptable for an unlock, so the guarantee has to live here, at the only place
# that knows whether the reader was actually touched.
#
# Two things are remembered per piece of work:
#
#   the command id — this particular delivery, and
#   the intent id  — what the person actually asked for, one tap on an unlock button.
#
# The intent is the one that matters. A command id changes when the backend re-mints a command for
# the same tap, so deduplicating on it alone leaves the exact hole this is meant to close: a retry
# arrives under a new id and the door opens a second time.
#
# And it is kept on disk. The set used to be memory-only, so an agent restarted between opening a
# door and reporting it came back with no memory of the door it had just opened — the backend
# retried, and the door opened again. A Pi restarts: on an update, on a power blip, on a crash. The
# add-on already owns /data, so persisting is cheap and the gap is not.
#
# On disk it is an append-only journal, one line per piece of work:
#
#   {"v":2,"keys":["<command id>","<intent id>"],"at":<epoch>,"result":<what the reader said>}
#
# Until 0.6.0 it was one JSON file, written out in full, fsynced and swapped in after every
# command. With a day's enrolment in it that was 0.2-0.4 s per command (measured at the first plant
# rehearsal, ~3,000 entries), paid on the one path every door's work passes through: a remote
# unlock opened the door in a tenth of a second and the app waited half a second more for this
# file. Appending a line costs the same whether the store holds ten entries or five thousand, and
# a power cut can tear at most the last line, which is ignored on the way back in.
#
# Bounded, because this runs for months on a small box: the newest EXECUTED_MAX entries, and
# nothing older than EXECUTED_TTL_SECONDS. A week is far longer than any command's own lifetime
# (the longest is two minutes), so the bound can never discard something still in play. The journal
# is rewritten as just what is still remembered when the agent starts and whenever it has grown to
# twice that - on a background thread, never on a command's path.
_executed = {}
_executed_lock = threading.Lock()

EXECUTED_MAX = 5000
EXECUTED_TTL_SECONDS = 7 * 24 * 3600
EXECUTED_STORE_VERSION = 2           # the journal. 1 was the whole-file store of 0.5.0.
EXECUTED_LEGACY_VERSION = 1
EXECUTED_COMPACT_MIN_LINES = 1000

# Work whose record must be ON THE DISK before anyone is told it was done.
#
# The record exists so that a door is not opened twice, so for a door nothing changes from 0.5.0:
# by the time the result is reported, the line saying "this tap has been carried out" has been
# fsynced. (An enrolment is in the list because repeating it means asking a person to stand at the
# reader again.) Everything else that changes a reader - a user push, a photo, a removal - is safe
# to repeat, so its line is appended and handed to the operating system, and the background thread
# fsyncs it within a second. A process that dies keeps those lines; only a power cut inside that
# second can lose one, and what it loses is the memory of work that is harmless to do again.
EXECUTED_DURABLE_FIRST = frozenset({"OpenDoor", "StartRemoteEnrollment"})

# How often the background thread syncs what was appended without an fsync (this journal's lines
# and the result outbox's), and looks at whether either has grown enough to be rewritten.
STORAGE_SYNC_SECONDS = 1.0

# The file itself: which path the counters below describe, how many lines it holds, whether
# anything has been appended since the last fsync, and whether it ends in a torn line that the next
# append must not be glued onto. `tail` collects lines appended while a compaction is writing its
# copy, so they can be carried over before the copy is swapped in.
_executed_file_lock = threading.Lock()
_executed_file = {"path": None, "lines": 0, "dirty": False, "needs_newline": False, "tail": None}


def executed_store_path(cfg):
    """
    Where the executed journal lives. /data survives restarts and add-on updates; /tmp does not.

    A box paired under 0.5.0 has "/data/executed.json" saved in its config.json. That name now
    means "the journal beside it": the .json file is the old whole-file store, read once on the
    first start and removed (see executed_store_load).
    """
    path = cfg.get("executed_store_path") or "/data/executed.jsonl"
    return path + "l" if path.endswith(".json") else path


def _executed_legacy_path(path):
    """The 0.5.0 store that belongs to this journal, if the name says there could be one."""
    return path[:-1] if path.endswith(".jsonl") else None


def _executed_file_state(path):
    """The counters for `path`, started afresh if they were describing another file. Caller holds the file lock."""
    state = _executed_file
    if state["path"] != path:
        lines, torn = 0, False
        try:
            with open(path, "rb") as f:
                raw = f.read()
            lines = raw.count(b"\n")
            torn = bool(raw) and not raw.endswith(b"\n")
        except OSError:
            pass
        state.update(path=path, lines=lines, dirty=False, needs_newline=torn, tail=None)
    return state


def _executed_read_journal(path):
    """Every readable line of the journal as {key: entry}. Returns (entries, lines, corrupt, torn). Never raises."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, 0, 0, False
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the executed-command journal at %s (%s); starting empty - a "
                    "command in flight across this restart may run twice", path, e)
        return {}, 0, 0, False

    loaded, corrupt, torn = {}, 0, False
    lines = raw.split(b"\n")
    ends_whole = raw.endswith(b"\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line.decode("utf-8"))
            if rec.get("v") != EXECUTED_STORE_VERSION:
                raise ValueError(f"version {rec.get('v')}")
            entry = {"at": float(rec.get("at") or 0), "result": rec.get("result")}
            for key in rec["keys"]:
                if key:
                    loaded[key] = entry
        except Exception:  # noqa: BLE001
            if index == len(lines) - 1 and not ends_whole:
                torn = True              # the last line, cut short by a power loss
            else:
                corrupt += 1
    return loaded, sum(1 for line in lines if line.strip()), corrupt, torn


def _executed_read_legacy(path):
    """
    The entries of a 0.5.0 store, or None when there is nothing usable there.

    Unreadable, or written by a version this agent does not know: left where it is and ignored,
    loudly - exactly what 0.5.0 did with a store it could not read.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the 0.5.0 executed-command store at %s (%s); ignoring it - a "
                    "command in flight across this update may run twice", path, e)
        return None

    if not isinstance(stored, dict) or stored.get("version") != EXECUTED_LEGACY_VERSION:
        log.warning("executed-command store at %s is version %s, not %s; ignoring it",
                    path, stored.get("version") if isinstance(stored, dict) else "?",
                    EXECUTED_LEGACY_VERSION)
        return None

    loaded = {}
    for entry in stored.get("entries") or []:
        try:
            key = entry["key"]
            at = float(entry.get("at") or 0)
        except Exception:  # noqa: BLE001
            continue
        if key:
            loaded[key] = {"at": at, "result": entry.get("result")}
    return loaded


def executed_store_load(cfg):
    """
    Re-read what this agent did before it restarted. Returns how many keys it remembers.

    Never raises. A journal that is missing, torn by a power cut, or holding lines this version
    cannot read leaves the agent with whatever could be read - at worst deduplicating in memory
    only, exactly where it was before this existed - and that is strictly better than refusing to
    start. It is logged loudly, because silently forgetting which doors you opened is the failure
    this whole file is about.

    The first start after an update from 0.5.0 finds that version's whole-file store, takes its
    entries into the journal and removes it.
    """
    path = executed_store_path(cfg)
    loaded, lines, corrupt, torn = _executed_read_journal(path)

    legacy_path = _executed_legacy_path(path)
    legacy = _executed_read_legacy(legacy_path) if legacy_path else None
    if legacy:
        for key, entry in legacy.items():
            loaded.setdefault(key, entry)        # the journal, being newer, wins

    cutoff = time.time() - EXECUTED_TTL_SECONDS
    loaded = {key: entry for key, entry in loaded.items() if entry["at"] >= cutoff}

    with _executed_lock:
        _executed.clear()
        _executed.update(loaded)
        _executed_prune_locked()
        remembered = len(_executed)

    with _executed_file_lock:
        _executed_file.update(path=path, lines=lines, dirty=False, needs_newline=torn, tail=None)

    if torn:
        log.warning("the executed-command journal ended in a half-written line (power lost "
                    "mid-write); ignored")
    if corrupt:
        log.warning("%d unreadable line(s) in the executed-command journal at %s were skipped",
                    corrupt, path)

    if lines or legacy is not None:
        # Start from a file that holds exactly what is remembered: no torn line, nothing expired.
        written = _executed_compact(path)
        if legacy is not None and written:
            try:
                os.unlink(legacy_path)
                log.info("took %d entr%s over from the 0.5.0 store at %s and removed it",
                         len(legacy), "y" if len(legacy) == 1 else "ies", legacy_path)
            except OSError as e:
                log.warning("could not remove the 0.5.0 store at %s (%s); it will be read again "
                            "at the next start, which is harmless", legacy_path, e)
        log.info("recovered %d executed command(s) from %s", remembered, path)
    else:
        log.info("no executed-command journal at %s yet; starting with an empty one", path)
    return remembered


def _executed_prune_locked():
    """Drop anything past its week, then anything past the count. Caller holds the lock."""
    cutoff = time.time() - EXECUTED_TTL_SECONDS
    for key in [k for k, v in _executed.items() if v["at"] < cutoff]:
        _executed.pop(key, None)

    if len(_executed) > EXECUTED_MAX:
        oldest = sorted(_executed.items(), key=lambda kv: kv[1]["at"])[:len(_executed) - EXECUTED_MAX]
        for key, _ in oldest:
            _executed.pop(key, None)


def _executed_append(cfg, keys, at, result, durable):
    """
    Add one line to the journal.

    `durable` means the line is fsynced before this returns. Otherwise it is written and flushed to
    the operating system, and the background thread fsyncs it within a second.

    A failure to write is logged, never raised. The work is already done; losing the record of it
    costs a possible repeat after a restart, while refusing to acknowledge a door we just opened
    costs a retry immediately.
    """
    path = executed_store_path(cfg)
    try:
        line = json.dumps({"v": EXECUTED_STORE_VERSION, "keys": keys, "at": at, "result": result},
                          separators=(",", ":")).encode("utf-8") + b"\n"
        with _executed_file_lock:
            state = _executed_file_state(path)
            f = open(path, "ab")
            try:
                if state["needs_newline"]:
                    f.write(b"\n")           # never glue a record onto a torn one
                    state["needs_newline"] = False
                f.write(line)
                f.flush()
            except Exception:
                f.close()
                raise
            state["lines"] += 1
            if state["tail"] is not None:
                state["tail"].append(line)
            if not durable:
                state["dirty"] = True
                f.close()
        # The fsync is outside the lock: it is the slow part, and another door's line must not
        # wait for it. It covers everything written to the file so far, this line included.
        if durable:
            try:
                os.fsync(f.fileno())
            finally:
                f.close()
    except Exception as e:  # noqa: BLE001
        log.warning("could not persist the executed-command journal to %s (%s)", path, e)
        return
    if not durable:
        _flusher_start()


def _executed_compact(path):
    """
    Rewrite the journal as just what is still remembered. Returns True if it was replaced.

    Temp file plus replace, never truncated in place: truncating the real file leaves a window
    where a power cut produces an empty store, which reads as "this agent has never opened a door".

    The copy is written without holding the file lock, so a command that finishes meanwhile is not
    kept waiting; whatever was appended in that time is carried over under the lock just before the
    copy is swapped in.
    """
    tmp = path + ".tmp"
    try:
        with _executed_file_lock:
            state = _executed_file_state(path)
            if state["tail"] is not None:
                return False                 # another compaction is already writing its copy
            with _executed_lock:
                groups = {}
                for key, entry in _executed.items():
                    groups.setdefault(id(entry), (entry, []))[1].append(key)
                snapshot = sorted(((entry["at"], keys, entry["result"]) for entry, keys in groups.values()),
                                  key=lambda item: item[0])
            state["tail"] = []

        try:
            with open(tmp, "wb") as f:
                for at, keys, result in snapshot:
                    f.write(json.dumps({"v": EXECUTED_STORE_VERSION, "keys": keys, "at": at,
                                        "result": result}, separators=(",", ":")).encode("utf-8") + b"\n")
                f.flush()
                os.fsync(f.fileno())

            with _executed_file_lock:
                state = _executed_file
                if state["path"] != path:
                    raise RuntimeError("the journal moved while it was being compacted")
                tail = state["tail"] or []
                if tail:
                    with open(tmp, "ab") as f:
                        f.writelines(tail)
                        f.flush()
                        os.fsync(f.fileno())
                os.replace(tmp, path)
                state.update(lines=len(snapshot) + len(tail), dirty=False, needs_newline=False, tail=None)
            return True
        except Exception:
            with _executed_file_lock:
                if _executed_file["path"] == path:
                    _executed_file["tail"] = None
            raise
    except Exception as e:  # noqa: BLE001
        log.warning("could not compact the executed-command journal at %s (%s); carrying on with "
                    "the journal as it is", path, e)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def executed_store_sync(cfg=None):
    """
    What the background thread does for this journal every second: fsync whatever was appended
    without one, and rewrite the journal if it has grown to twice what is still remembered.

    One caller at a time (the sync lock), and never under the file lock while the disk is being
    waited on - a command appending its line must not queue behind this.
    """
    with _executed_sync_lock:
        with _executed_file_lock:
            path = executed_store_path(cfg) if cfg else _executed_file["path"]
            if not path:
                return
            state = _executed_file_state(path)
            dirty, lines = state["dirty"], state["lines"]
            state["dirty"] = False

        if dirty:
            try:
                with open(path, "ab") as f:
                    os.fsync(f.fileno())
            except Exception as e:  # noqa: BLE001
                log.warning("could not sync the executed-command journal at %s (%s)", path, e)

        if lines > EXECUTED_COMPACT_MIN_LINES:
            with _executed_lock:
                live = len({id(entry) for entry in _executed.values()})
            if lines > max(EXECUTED_COMPACT_MIN_LINES, 2 * live):
                _executed_compact(path)


_executed_sync_lock = threading.Lock()


_flusher = {"thread": None}
_flusher_lock = threading.Lock()


def _flusher_start():
    """Start the background sync thread if it is not running. Safe to call from anywhere, often."""
    with _flusher_lock:
        thread = _flusher["thread"]
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=storage_sync_loop, daemon=True, name="storage-sync")
            _flusher["thread"] = thread
            thread.start()


def storage_sync_loop():
    """
    The one thread that pays for the disk so that no door has to.

    Every second: fsync the executed journal and the result outbox if anything was appended to them
    without an fsync, and compact either one that has outgrown what it holds.
    """
    while True:
        time.sleep(STORAGE_SYNC_SECONDS)
        try:
            executed_store_sync()
        except Exception:  # noqa: BLE001 - this loop must outlive everything
            log.exception("syncing the executed-command journal hit an unexpected error; carrying on")
        outbox = STATE.get("result_outbox")
        if outbox is not None:
            try:
                outbox.sync()
            except Exception:  # noqa: BLE001
                log.exception("syncing the result outbox hit an unexpected error; carrying on")


def _remember(cfg, command_id, intent_id, result, kind=None):
    """
    Record that this command — and the intent behind it — has been carried out.

    One line appended to the journal. For a door (see EXECUTED_DURABLE_FIRST) the line is on the
    disk before this returns; for anything else it is with the operating system and synced within
    a second.
    """
    now = time.time()
    keys = [key for key in (command_id, intent_id) if key]
    if not keys:
        return
    entry = {"at": now, "result": result}
    with _executed_lock:
        for key in keys:
            _executed[key] = entry
        _executed_prune_locked()
    _executed_append(cfg, keys, now, result, durable=kind in EXECUTED_DURABLE_FIRST)


def _already_executed(command_id, intent_id):
    """
    The stored result if this work is already done, else None.

    The intent is checked as well as the command id, so a second command minted for the same tap is
    recognised as the repeat it is. Returns a two-tuple so the caller can say WHICH matched — the
    log line "already executed" is useless when a door opens twice and nobody can tell whether the
    protection was even consulted.
    """
    with _executed_lock:
        for key, kind in ((command_id, "command"), (intent_id, "intent")):
            if key and key in _executed:
                return _executed[key]["result"], kind
    return None, None


# Commands that only READ the reader. Running one twice is harmless, so they are not remembered.
#
# They used to be, results and all - and the access log is read every thirty seconds per door, so
# the executed store filled with thousands of copies of reader logs and was rewritten in full after
# every command. On a Pi's SD card that is wear and latency spent protecting nothing: the set exists
# so that a DOOR is not opened twice. Their results are not journalled in the result outbox either
# (see report): a read that could not be reported is simply read again.
READ_ONLY_COMMANDS = frozenset({
    "GetSystemInfo", "GetAccessLogs", "FindTerminalUserId", "GetUserRegistrationMap",
})


# Guards the roster refresh. With one worker per reader, twenty of them can meet a terminal they do
# not know in the same moment (a reader just claimed, or the first batch after a start); one fetch
# answers all of them.
_readers_lock = threading.Lock()
_readers_fetched = {"at": 0.0}


def readers_for(cfg, force=False):
    """
    This site's readers, with their credentials, cached between calls.

    Refetched when the backend mentions a terminal we do not know about, so a reader claimed in
    Zhub becomes usable without restarting the add-on.
    """
    held = STATE.get("readers")
    if held and not force:
        return held

    asked = time.monotonic()
    with _readers_lock:
        held = STATE.get("readers")
        # Somebody else fetched it while we were waiting for the lock: that answer is newer than
        # our question, so it is the one we wanted.
        if held and (not force or _readers_fetched["at"] >= asked):
            return held
        rows = backend_get(cfg, "/api/agent/terminals")
        held = {
            r["id"]: Reader(r["id"], r.get("name", "?"), r["ipAddress"], r["username"], r["password"])
            for r in rows
        }
        STATE["readers"] = held
        _readers_fetched["at"] = time.monotonic()
        log.info("hold credentials for %d reader(s)", len(held))
        roster_save(cfg, {r.host: tid for tid, r in held.items()})
        return held


def execute(cfg, command):
    """Carry out one command against its reader. Returns the JSON-serialisable result."""
    kind = command.get("type")
    terminal_id = command.get("terminalId")
    payload = json.loads(command["payload"]) if command.get("payload") else {}

    readers = readers_for(cfg)
    reader = readers.get(terminal_id)
    if reader is None:
        reader = readers_for(cfg, force=True).get(terminal_id)   # newly claimed?
    if reader is None:
        raise ReaderError(f"no credentials held for terminal {terminal_id}")

    if kind == "GetSystemInfo":
        return reader.system_info()
    if kind == "OpenDoor":
        return reader.open_door(payload.get("door", 1))
    if kind == "ConfigureMonitor":
        # The reader posts to us, not to the internet. Resolved fresh each time rather than cached,
        # because a DHCP lease that moves the agent silently points all twenty readers at a dead
        # address and every event quietly falls back to polling.
        return reader.configure_monitor(local_address_for(reader.host), cfg.get("webhook_port", 8100))
    if kind == "SyncClock":
        return reader.sync_clock(payload)
    if kind == "CreateUser":
        return reader.create_user(payload)
    if kind == "UpdateUser":
        return reader.update_user(payload)
    if kind == "DeleteUser":
        return reader.delete_user(payload)
    if kind == "UploadFacePhoto":
        return reader.upload_face_photo(payload)
    if kind == "StartRemoteEnrollment":
        return reader.remote_enroll(payload)
    if kind == "GetAccessLogs":
        return reader.access_logs(payload.get("afterLogId"))
    if kind == "FindTerminalUserId":
        Reader._require(payload, "FindTerminalUserId", "registration")
        return {"terminalUserId": reader.find_user_id(payload["registration"]) or 0}
    if kind == "GetUserRegistrationMap":
        return reader.registration_map()

    # An older agent meeting a newer backend. Say which version refused, so the fix is obvious.
    raise ReaderError(f"this agent does not know how to '{kind}' (agent v{AGENT_VERSION})")


def expired(command):
    """True if the command's deadline has passed."""
    deadline = parse_server_time(command.get("notValidAfter"))
    return deadline is not None and time.time() > deadline


def handle(cfg, command):
    """Execute one command and report the outcome. Never raises."""
    # Noted for report(): the result of a read is kept in memory, the result of work on disk.
    _in_hand.kind = command.get("type")
    try:
        _handle(cfg, command)
    finally:
        _in_hand.kind = None


def _handle(cfg, command):
    command_id = command.get("id")
    intent_id = command.get("intentId")

    # A repeat of something already done. Acknowledge with the original result rather than doing it
    # again — the point of remembering.
    #
    # Matching on the intent as well as the command id is what closes the real hole: the backend
    # can re-mint a command for the same tap, and a repeat under a new id used to be
    # indistinguishable from someone asking a second time.
    done, matched = _already_executed(command_id, intent_id)
    if matched:
        log.info("%s %s already executed (matched by %s); acknowledging without repeating",
                 command.get("type"), command_id, matched)
        report(cfg, command_id, True, done, None, 0)
        return

    # Too late to act on.
    #
    # Discarded rather than run, and this is the important half of the design: a door that opens by
    # itself two minutes after someone asked is a security incident, not a late success. The
    # decision is made here, on site, because a round trip to ask would itself be the delay.
    if expired(command):
        log.warning("discarding %s %s — it expired before we collected it",
                    command.get("type"), command_id)
        report(cfg, command_id, False, None, "expired before the agent collected it", 0)
        return

    terminal_id = command.get("terminalId")

    # A reader we have already given up on for now. Failed immediately rather than after a full
    # timeout, so a dead door cannot occupy a worker that the working doors need.
    if breaker_is_open(terminal_id):
        log.info("%s for terminal %s refused: that reader is marked down",
                 command.get("type"), terminal_id)
        report(cfg, command_id, False, None,
               "the agent has marked this reader as unreachable; it will be retried shortly", 0)
        return

    started = time.time()
    try:
        result = execute(cfg, command)
        elapsed = int((time.time() - started) * 1000)
        if command.get("type") not in READ_ONLY_COMMANDS:
            _remember(cfg, command_id, intent_id, result, command.get("type"))
        breaker_record(terminal_id, True, command.get("type"))
        log.info("%s on terminal %s in %dms", command.get("type"), terminal_id, elapsed)
        report(cfg, command_id, True, result, None, elapsed)
    except ReaderOutcomeUnknown as e:
        elapsed = int((time.time() - started) * 1000)
        breaker_record(terminal_id, False, terminal_id)
        if command.get("type") == "OpenDoor":
            # The unlock reached the reader and the reader never answered. It may well have opened
            # the door, so this is reported as UNKNOWN, never as failed: "failed" invites a second
            # tap. Not remembered as executed either - we do not know that it was.
            log.warning("OpenDoor on terminal %s: no answer from the reader after %dms (%s) - "
                        "reporting the outcome as UNKNOWN; the door may have opened",
                        terminal_id, elapsed, e)
            report(cfg, command_id, False, None,
                   f"{e} - the unlock was sent and the reader did not answer, so the door may have opened",
                   elapsed, outcome="unknown")
        else:
            log.warning("%s failed after %dms: %s", command.get("type"), elapsed, e)
            report(cfg, command_id, False, None, str(e), elapsed)
    except ReaderError as e:
        elapsed = int((time.time() - started) * 1000)
        breaker_record(terminal_id, False, terminal_id)
        log.warning("%s failed after %dms: %s", command.get("type"), elapsed, e)
        report(cfg, command_id, False, None, str(e), elapsed)
    except Exception as e:  # noqa: BLE001
        elapsed = int((time.time() - started) * 1000)
        log.exception("%s raised unexpectedly", command.get("type"))
        report(cfg, command_id, False, None, f"agent error: {e}", elapsed)


# ---------------------------------------------------------------------------
# Reporting results
# ---------------------------------------------------------------------------
#
# A result is the only way Havenz learns that a reader did what it was asked. It used to be sent
# once: if the backend refused it (HTTP 429 - twenty doors' results arriving together against a
# limit sized for one gateway) or the uplink blinked, the agent logged a line and moved on, and the
# backend, hearing nothing, recorded the command as "unknown" although the reader had done the
# work. At the first plant rehearsal seventeen results of finished work were thrown away like that
# during one enrolment.
#
# Now a result is held in an outbox until Havenz has it, with the same discipline as reader
# events: written to an append-only journal in /data, sent, and only then marked done; retried
# until it is delivered; picked up again after a restart.
#
#   {"v":1,"op":"put","rid":...,"id":"<command id>","at":...,"body":{...}}
#   {"v":1,"op":"ack","rid":...,"at":...}                  Havenz has it
#   {"v":1,"op":"drop","rid":...,"at":...,"reason":...}     given up on, and why
#
# Two deliberate differences from the event queue:
#
#   - The journal line is not fsynced before the first attempt to send. A result is sent the moment
#     the reader has answered - somebody may be standing at a door waiting for it - so the line is
#     handed to the operating system and the background thread fsyncs it within a second. A process
#     that dies keeps it; a power cut in that second loses a result Havenz then records as unknown,
#     which is the honest answer and exactly what happened to every result before this existed.
#   - The result of a READ (the access log, the user list) is held in memory only. Reads are
#     repeatable and the log read alone runs every thirty seconds per door; journalling them would
#     put copies of reader logs on the disk to protect nothing. They are still retried, for as long
#     as the agent is running and for at most ten minutes.

RESULT_OUTBOX_VERSION = 1
RESULT_MAX_PENDING = 5000
RESULT_MAX_BYTES = 32 * 1024 * 1024
RESULT_MAX_AGE_SECONDS = 24 * 3600
RESULT_READ_MAX_AGE_SECONDS = 600
RESULT_COMPACT_AFTER = 500
RESULT_DELIVERY_WORKERS = 2

# How long to wait after a 429 that did not say. Short: the backend's window is a minute at most
# and it normally does say.
RESULT_RETRY_AFTER_DEFAULT_SECONDS = 2
# The key was revoked or the agent re-paired. Waiting does not fix that, but pairing again does,
# and the results are still true - so they are kept and tried once a minute.
RESULT_UNAUTHORISED_RETRY_SECONDS = 60
# Havenz says the result itself is unacceptable (400, 413, 422). Retrying every second is pointless;
# it is tried every five minutes until it is a day old, like a refused reader event.
RESULT_REFUSAL_CODES = frozenset({400, 413, 422})
RESULT_REFUSED_RETRY_SECONDS = 300

# The longest any Retry-After is believed. A backend that asks for an hour has a problem of its
# own; a door's result should not sit out an hour on its say-so.
RETRY_AFTER_MAX_SECONDS = 60


def retry_after_seconds(http_error, default):
    """
    How long a refusal asked us to wait, in seconds: its Retry-After header, capped, or `default`
    when there is none or it cannot be read. Only the seconds form is understood - it is the only
    one Havenz sends.
    """
    try:
        value = (http_error.headers or {}).get("Retry-After")
        seconds = int(str(value).strip())
    except Exception:  # noqa: BLE001
        return default
    if seconds < 0:
        return default
    return min(seconds, RETRY_AFTER_MAX_SECONDS)


class ResultOutbox:
    """Command results waiting to reach Havenz. Thread-safe; one instance per agent."""

    def __init__(self, path, clock=time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Condition()
        self._pending = []            # records, oldest first
        self._blocked_until = 0.0     # a 429 closes the door for everyone until then
        self._file_lock = threading.Lock()
        self._dirty = False
        self._needs_newline = False
        self._tail = None             # lines appended while a compaction writes its copy
        self._closed_since_compact = 0
        self.stats = {"delivered": 0, "dropped": 0, "refused_429": 0, "unpersisted": 0,
                      "corrupt_lines": 0, "redelivered_after_restart": 0, "last_error": None}

    # -- loading ----------------------------------------------------------

    def load(self):
        """Pick up the results the last process had not delivered. Never raises."""
        puts, closed, corrupt, torn = {}, set(), 0, False
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("could not read the result outbox at %s (%s); starting empty - results that "
                      "were waiting in it are NOT being delivered", self.path, e)
            return 0

        lines = raw.split(b"\n")
        self._needs_newline = bool(raw) and not raw.endswith(b"\n")
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
                if rec.get("v") != RESULT_OUTBOX_VERSION:
                    raise ValueError(f"version {rec.get('v')}")
                op = rec["op"]
                if op == "put":
                    if not isinstance(rec["body"], dict):
                        raise ValueError("body")
                    puts[rec["rid"]] = self._record(rec["rid"], rec["id"], rec["body"], float(rec["at"]),
                                                    durable=True, persisted=True, size=len(line))
                elif op in ("ack", "drop"):
                    closed.add(rec["rid"])
            except Exception:  # noqa: BLE001
                if index == len(lines) - 1 and self._needs_newline:
                    torn = True          # the last line, cut short by a power loss
                else:
                    corrupt += 1

        pending = [r for rid, r in puts.items() if rid not in closed]
        pending.sort(key=lambda r: r["at"])
        with self._lock:
            self._pending = pending
            self.stats["corrupt_lines"] += corrupt
            self.stats["redelivered_after_restart"] = len(pending)

        if torn:
            log.warning("the result outbox ended in a half-written line (power lost mid-write); ignored")
        if corrupt:
            log.error("%d unreadable line(s) in the result outbox at %s were skipped", corrupt, self.path)
        if pending:
            log.warning("recovered %d command result(s) that had not reached Havenz before the "
                        "restart (oldest %ds ago); sending them now",
                        len(pending), int(self._clock() - pending[0]["at"]))
        self._compact()
        return len(pending)

    @staticmethod
    def _record(rid, command_id, body, at, durable, persisted, size):
        return {"rid": rid, "id": command_id, "body": body, "at": at, "durable": durable,
                "persisted": persisted, "size": size, "attempts": 0, "next_try": 0.0, "in_flight": False}

    # -- writing ----------------------------------------------------------

    def _append(self, entry):
        """One journal line, handed to the operating system. The background sync fsyncs it."""
        line = json.dumps(entry, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._file_lock:
            with open(self.path, "ab") as f:
                if self._needs_newline:
                    f.write(b"\n")           # never glue a record onto a torn one
                    self._needs_newline = False
                f.write(line)
                f.flush()
            self._dirty = True
            if self._tail is not None:
                self._tail.append(line)      # a compaction is writing its copy; carry this over
        return len(line)

    def put(self, command_id, body, durable=True, hold=False):
        """
        Take custody of one result. Returns the record.

        `durable=False` is for the result of a read: held in memory, never written. `hold=True`
        hands the record to the caller already marked as being sent, so the caller can make the
        first attempt itself (see report) without a retry thread racing it; the caller then calls
        deliver() or release().

        If the disk refuses the line the result is still held in memory and delivered, which is
        what happened to every result before this outbox existed; it is logged and counted.
        """
        now = self._clock()
        rec = self._record(str(uuid.uuid4()), command_id, body, now, durable, persisted=False, size=0)
        rec["in_flight"] = bool(hold)
        if durable:
            try:
                rec["size"] = self._append({"v": RESULT_OUTBOX_VERSION, "op": "put", "rid": rec["rid"],
                                            "id": command_id, "at": now, "body": body})
                rec["persisted"] = True
            except Exception as e:  # noqa: BLE001
                self.stats["unpersisted"] += 1
                log.error("could NOT write the result of %s to the result outbox (%s); holding it "
                          "in memory - it will be lost if the agent restarts before Havenz has it",
                          command_id, e)
        with self._lock:
            self._pending.append(rec)
            self._enforce_bounds_locked()
            self._lock.notify_all()
        return rec

    def release(self, rec):
        """Give a held record back without having tried it: a retry thread sends it when it may."""
        with self._lock:
            rec["in_flight"] = False
            self._lock.notify_all()

    def _close(self, rec, op, reason=None):
        with self._lock:
            if rec in self._pending:
                self._pending.remove(rec)
            rec["in_flight"] = False
            self._closed_since_compact += 1
            self._lock.notify_all()
        if rec["persisted"]:
            entry = {"v": RESULT_OUTBOX_VERSION, "op": op, "rid": rec["rid"], "at": self._clock()}
            if reason:
                entry["reason"] = reason
            try:
                # Losing an ack to a power cut costs one re-delivery, which Havenz answers
                # "already recorded".
                self._append(entry)
            except Exception as e:  # noqa: BLE001
                log.warning("could not journal the %s of the result of %s (%s); it may be sent "
                            "again after a restart", op, rec["id"], e)

    def ack(self, rec):
        self.stats["delivered"] += 1
        self._close(rec, "ack")

    def drop(self, rec, reason):
        """Give up on a result - journalled, logged and counted; never silent."""
        self.stats["dropped"] += 1
        log.warning("DROPPED the result of command %s, held for %ds: %s (%d dropped since start). "
                    "Havenz will record that command as unknown.",
                    rec["id"], int(self._clock() - rec["at"]), reason, self.stats["dropped"])
        self._close(rec, "drop", reason)

    def _enforce_bounds_locked(self):
        """Oldest out first when the outbox outgrows a small box. Caller holds the lock."""
        def too_big():
            return (len(self._pending) > RESULT_MAX_PENDING
                    or sum(r["size"] for r in self._pending) > RESULT_MAX_BYTES)

        while len(self._pending) > 1 and too_big():
            victim = next((r for r in self._pending if not r["in_flight"]), None)
            if victim is None:
                return
            self._lock.release()
            try:
                self.drop(victim, "the outbox is full (uplink down too long) - oldest result discarded")
            finally:
                self._lock.acquire()

    def _compact(self):
        """
        Rewrite the journal as just what is still waiting. Temp file + replace, never in place.

        The copy is written and fsynced without holding the file lock, so a reader's worker
        appending its result meanwhile is not kept waiting on the disk; whatever was appended in
        that time is carried over under the lock just before the copy is swapped in. Called at
        load and from the background thread, never from a command's path.
        """
        tmp = self.path + ".tmp"
        with self._file_lock:
            if self._tail is not None:
                return                       # another compaction is already writing its copy
            if not os.path.exists(self.path):
                return
            with self._lock:
                waiting = [{"v": RESULT_OUTBOX_VERSION, "op": "put", "rid": rec["rid"], "id": rec["id"],
                            "at": rec["at"], "body": rec["body"]}
                           for rec in self._pending if rec["persisted"]]
                closed = self._closed_since_compact
            self._tail = []
        try:
            with open(tmp, "wb") as f:
                for entry in waiting:
                    f.write(json.dumps(entry, separators=(",", ":")).encode("utf-8") + b"\n")
                f.flush()
                os.fsync(f.fileno())
            with self._file_lock:
                tail, self._tail = self._tail, None
                if tail:
                    # Lines written while the copy was being made. A put that is also in the copy
                    # is then there twice under one record id, which loading reads as one result.
                    with open(tmp, "ab") as f:
                        f.writelines(tail)
                        f.flush()
                os.replace(tmp, self.path)
                self._needs_newline = False
                self._dirty = bool(tail)     # the carried-over lines still want their fsync
            with self._lock:
                self._closed_since_compact = max(0, self._closed_since_compact - closed)
        except Exception as e:  # noqa: BLE001
            with self._file_lock:
                self._tail = None
            log.warning("could not compact the result outbox at %s (%s); carrying on with the "
                        "journal as it is", self.path, e)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def sync(self):
        """
        What the background thread does every second: fsync what was appended, compact if due.
        The disk is never waited on under the file lock - a worker appending a result must not
        queue behind this.
        """
        if self._closed_since_compact >= RESULT_COMPACT_AFTER:
            self._compact()
        with self._file_lock:
            dirty, self._dirty = self._dirty, False
        if not dirty:
            return
        try:
            with open(self.path, "ab") as f:
                os.fsync(f.fileno())
        except Exception as e:  # noqa: BLE001
            log.warning("could not sync the result outbox at %s (%s)", self.path, e)

    # -- sending ----------------------------------------------------------

    def blocked(self):
        """True while a 429's Retry-After is still running. Nothing is sent until it has passed."""
        with self._lock:
            return self._clock() < self._blocked_until

    def take(self, timeout=None):
        """The next result that may be sent now, marked as being sent - or None after `timeout`."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while True:
                now = self._clock()
                expired = [r for r in self._pending if not r["in_flight"] and now - r["at"] >
                           (RESULT_MAX_AGE_SECONDS if r["durable"] else RESULT_READ_MAX_AGE_SECONDS)]
                if expired:
                    self._lock.release()
                    try:
                        for rec in expired:
                            self.drop(rec, "undelivered for a day" if rec["durable"]
                                      else "the result of a read, undelivered for ten minutes")
                    finally:
                        self._lock.acquire()
                    continue

                wake_in = None
                if now < self._blocked_until:
                    wake_in = self._blocked_until - now
                else:
                    for rec in self._pending:
                        if rec["in_flight"]:
                            continue
                        if rec["next_try"] <= now:
                            rec["in_flight"] = True
                            return rec
                        wait = rec["next_try"] - now
                        wake_in = wait if wake_in is None else min(wake_in, wait)

                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                waits = [w for w in (wake_in, remaining) if w is not None]
                self._lock.wait(min(waits) if waits else None)

    def _retry_later(self, rec, delay, error, block_everyone=False):
        with self._lock:
            now = self._clock()
            rec["next_try"] = now + delay
            rec["in_flight"] = False
            self.stats["last_error"] = error
            if block_everyone:
                self._blocked_until = max(self._blocked_until, now + delay)
            self._lock.notify_all()

    def deliver(self, cfg, rec, post=None):
        """
        One attempt to hand a result to Havenz, settled in the outbox. Returns the outcome:
        "ok", "dropped", or what stopped it ("rate_limited", "retry", "unauthorised", "refused").
        """
        post = post or post_result
        try:
            answer = post(cfg, rec)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, the result is still ours to deliver
            answer = ("retry", f"agent error: {e}", None)
        outcome, detail = answer[0], answer[1]
        asked = answer[2] if len(answer) > 2 else None
        attempts = rec["attempts"]
        rec["attempts"] = attempts + 1

        if outcome == "ok":
            self.ack(rec)
            if attempts:
                log.info("reported the result of %s on attempt %d, %ds after the reader answered",
                         rec["id"], attempts + 1, int(self._clock() - rec["at"]))
            return outcome

        if outcome == "gone":
            # 404: Havenz has no such command for this agent (the agent was re-paired, or the
            # backend's database was replaced). Nobody is waiting for this and nobody ever will.
            log.warning("could not report the result of %s (%s) - Havenz has no such command for "
                        "this agent, so it is not kept", rec["id"], detail)
            self.drop(rec, f"Havenz has no such command for this agent ({detail})")
            return "dropped"

        if outcome == "rate_limited":
            self.stats["refused_429"] += 1
            delay = asked if asked is not None else RESULT_RETRY_AFTER_DEFAULT_SECONDS
        elif outcome == "unauthorised":
            delay = RESULT_UNAUTHORISED_RETRY_SECONDS
        elif outcome == "refused":
            delay = RESULT_REFUSED_RETRY_SECONDS
        else:
            delay = min(BACKOFF_MAX_SECONDS, 2 ** min(attempts, 6))

        # Worded for the people who read this log and for the rehearsal that counts it: the result
        # was refused or could not be sent, it has NOT been thrown away, and when it goes again.
        log.warning("could not report the result of %s (%s) - kept, retrying in %ds",
                    rec["id"], detail, delay)
        self._retry_later(rec, delay, detail, block_everyone=(outcome == "rate_limited"))
        return outcome

    def snapshot(self):
        with self._lock:
            now = self._clock()
            oldest = self._pending[0]["at"] if self._pending else None
            return {
                "pending": len(self._pending),
                "oldest_age_seconds": None if oldest is None else int(now - oldest),
                "blocked_for_seconds": max(0, int(self._blocked_until - now + 0.999)),
                **self.stats,
            }


def post_result(cfg, rec):
    """
    One attempt to POST a result. Returns (outcome, detail, retry_after):
    "ok" | "rate_limited" | "gone" | "unauthorised" | "refused" | "retry".

    A 200 is success whatever its body says: `accepted: false` means Havenz already had this
    result, which is exactly what a retry after a lost answer should be told.
    """
    try:
        backend_post(cfg, f"/api/agent/commands/{rec['id']}/result", rec["body"], timeout=15)
        return "ok", None, None
    except urllib.error.HTTPError as e:
        detail = str(e)                      # "HTTP Error 429: Too Many Requests"
        if e.code == 429:
            return "rate_limited", detail, retry_after_seconds(e, RESULT_RETRY_AFTER_DEFAULT_SECONDS)
        if e.code == 404:
            return "gone", detail, None
        if e.code in (401, 403):
            return "unauthorised", detail, None
        if e.code in RESULT_REFUSAL_CODES:
            return "refused", detail, None
        return "retry", detail, None
    except Exception as e:  # noqa: BLE001
        return "retry", str(e), None


def result_delivery_loop(cfg, outbox):
    """One retry worker. Runs for as long as `outbox` is the agent's outbox."""
    while STATE.get("result_outbox") is outbox:
        try:
            rec = outbox.take(timeout=5)
            if rec is not None:
                outbox.deliver(cfg, rec)
        except Exception:  # noqa: BLE001 - this loop must outlive everything
            log.exception("result delivery worker hit an unexpected error; carrying on")
            time.sleep(1)


_result_outbox_lock = threading.Lock()


def result_outbox(cfg):
    """
    The agent's result outbox, opened (and whatever was waiting in it recovered) on first use.

    main() opens it before any command can be leased. It is also opened on demand so that a result
    can never be reported into nothing, whichever way the agent was started.
    """
    path = cfg.get("result_outbox_path") or "/data/results.jsonl"
    with _result_outbox_lock:
        outbox = STATE.get("result_outbox")
        if outbox is None or outbox.path != path:
            outbox = ResultOutbox(path)
            outbox.load()
            STATE["result_outbox"] = outbox
            for _ in range(RESULT_DELIVERY_WORKERS):
                threading.Thread(target=result_delivery_loop, args=(cfg, outbox), daemon=True).start()
            _flusher_start()
        return outbox


# Which kind of command the current thread is carrying out, so report() can tell the result of a
# read (kept in memory) from the result of work (journalled) without every caller having to say.
_in_hand = threading.local()


def report(cfg, command_id, success, result, error, duration_ms, outcome=None):
    """
    Send the outcome back, and keep it until Havenz has it. Never raises.

    The first attempt is made here, at once, on the reader's own worker thread - the common case is
    one POST and nothing else. If Havenz refuses it or cannot be reached, the result stays in the
    outbox and the retry threads send it; if a 429 has already closed the door, it goes straight to
    them rather than knocking again.

    `outcome="unknown"` says the command reached the reader and nothing came back. A backend that
    does not know the field ignores it and records a failure, exactly as before.
    """
    body = {
        "success": success,
        "result": None if result is None else json.dumps(result),
        "error": error,
        "durationMs": duration_ms,
    }
    if outcome:
        body["outcome"] = outcome
    try:
        outbox = result_outbox(cfg)
        durable = getattr(_in_hand, "kind", None) not in READ_ONLY_COMMANDS
        rec = outbox.put(command_id, body, durable=durable, hold=True)
        if outbox.blocked():
            outbox.release(rec)
            return
        outbox.deliver(cfg, rec)
    except Exception:  # noqa: BLE001 - the work is already done; reporting must not undo it
        log.exception("could not report the result of %s", command_id)


# Per-terminal health, so one dead reader does not spoil the site.
#
# The failure that matters is not a reader that refuses a connection — that fails in milliseconds.
# It is a reader that accepts the connection and then says nothing, costing a full timeout every
# time it is asked. Twenty of those at a shift change is twenty workers blocked on a device that is
# not going to answer, while the doors that do work wait behind them.
#
# So a reader that has failed repeatedly is marked down and its commands are failed at once, with a
# message saying so. The backend keeps queueing for it, and one probe after the cooldown is enough
# to bring it back — nothing has to be reset by hand.
_breakers = {}
BREAKER_TRIPS_AFTER = 3
BREAKER_COOLDOWN_SECONDS = 60


def breaker_is_open(terminal_id):
    state = _breakers.get(terminal_id)
    return bool(state and state["open_until"] > time.time())


def breaker_record(terminal_id, ok, name=""):
    state = _breakers.setdefault(terminal_id, {"failures": 0, "open_until": 0.0})
    if ok:
        if state["failures"] or state["open_until"]:
            log.info("reader %s is answering again", name or terminal_id)
        state["failures"] = 0
        state["open_until"] = 0.0
        return
    state["failures"] += 1
    if state["failures"] >= BREAKER_TRIPS_AFTER and not breaker_is_open(terminal_id):
        state["open_until"] = time.time() + BREAKER_COOLDOWN_SECONDS
        log.warning("reader %s has failed %d times — marking it down for %ds. Its work stays "
                    "queued and the other doors carry on.",
                    name or terminal_id, state["failures"], BREAKER_COOLDOWN_SECONDS)


# Polls START at least this far apart.
#
# An idle agent holds one long poll open and this never applies: the poll that brings an unlock
# was already waiting, and the next one starts the moment it returns. Under load it is what keeps
# the agent from asking once per command - a poll that returns at once is followed by a short
# pause, so the next one collects whatever several doors have become ready for in the meantime.
# The cost is at most this much added to a command that arrives in the gap, during a busy spell.
POLL_MIN_INTERVAL_SECONDS = 0.2

# A reader's worker that has had nothing to do for this long goes away; the next command for that
# door starts another. Keeps a terminal that was removed from holding a thread for ever.
READER_WORKER_IDLE_SECONDS = 600


class ReaderWorkers:
    """
    One worker per reader, each with its own queue.

    The agent used to ask for work, do ALL of it, and only then ask again. While it waited out a
    reader that had accepted a connection and gone silent - ten seconds - it was not asking, so a
    remote unlock for any other door sat in Havenz's queue for the whole of that timeout, and an
    unlock only lives ten seconds. Measured at the first plant rehearsal: a healthy door opened in
    9.2 s beside a hanging one, against 0.27 s normally.

    Now the poller only hands commands out. Each reader's commands are carried out by that
    reader's own thread, in the order they arrived, so a reader that hangs holds up nothing but
    itself - and one reader is still never sent two commands at once, which is the ordering the
    backend's dispatch relies on ("grant Mike" then "revoke Mike" must not invert).

    One thread per reader is the bound on parallel work. They are idle almost all the time; twenty
    readers are twenty parked threads.
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._queues = {}

    def submit(self, command):
        """Hand a command to its reader's worker, starting one if that reader has none."""
        terminal_id = command.get("terminalId")
        with self._lock:
            queue = self._queues.get(terminal_id)
            if queue is None:
                queue = self._queues[terminal_id] = Queue()
                threading.Thread(target=self._run, args=(terminal_id, queue), daemon=True,
                                 name=f"reader-{terminal_id}").start()
            # Put under the lock, so a worker deciding it is idle cannot retire between our
            # finding its queue and using it.
            queue.put(command)

    def _run(self, terminal_id, queue):
        while True:
            try:
                command = queue.get(timeout=READER_WORKER_IDLE_SECONDS)
            except Empty:
                with self._lock:
                    if queue.empty():
                        self._queues.pop(terminal_id, None)
                        return
                continue
            try:
                handle(self._cfg, command)
            except Exception:  # noqa: BLE001 - handle() never raises; this worker must outlive it if it does
                log.exception("the worker for terminal %s hit an unexpected error; carrying on", terminal_id)


def command_loop(cfg, workers=None):
    """Hold a long poll open, hand whatever arrives to the readers' workers, and ask again at once."""
    wait = int(cfg.get("command_wait_seconds", 25))
    workers = workers or ReaderWorkers(cfg)
    backoff = BACKOFF_START_SECONDS
    last_started = None

    while True:
        if last_started is not None:
            gap = POLL_MIN_INTERVAL_SECONDS - (time.monotonic() - last_started)
            if gap > 0:
                time.sleep(gap)
        last_started = time.monotonic()
        try:
            batch = backend_get(cfg, f"/api/agent/commands?wait={wait}", timeout=wait + 15)
            backoff = BACKOFF_START_SECONDS
            # Never executed here. The backend hands out at most one command per terminal at a
            # time, and each goes to that terminal's own worker, so nothing can reorder a door's
            # work and nothing one door does can delay the next poll.
            for command in batch.get("commands") or []:
                workers.submit(command)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log.error("no longer authorised to collect commands (HTTP %d)", e.code)
                time.sleep(BACKOFF_MAX_SECONDS)
            elif e.code == 429:
                # Told how long to stay away, so stay away exactly that long instead of guessing.
                delay = retry_after_seconds(e, backoff)
                log.warning("command poll failed (HTTP %d) — retrying in %ds", e.code, delay)
                time.sleep(delay)
            else:
                log.warning("command poll failed (HTTP %d) — retrying in %ds", e.code, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
        except Exception as e:  # noqa: BLE001 — this loop must outlive every transport failure
            log.warning("command poll failed (%s) — retrying in %ds", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)


# ---------------------------------------------------------------------------
# Access events from the readers on this LAN
# ---------------------------------------------------------------------------

# Which address is which door, kept on disk.
#
# The listener works out which reader posted an event from its source address, and it learns the
# addresses from Havenz. If the agent restarts while Havenz is unreachable it has no roster, and
# every event in that window used to be refused as "not a reader we manage" - lost at exactly the
# moment the queue below exists for. Addresses and terminal ids only; never a credential.
def roster_path(cfg):
    return cfg.get("roster_path") or "/data/roster.json"


def roster_save(cfg, hosts):
    path = roster_path(cfg)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "hosts": hosts}, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("could not save the reader roster to %s (%s)", path, e)
    STATE["roster_hosts"] = dict(hosts)


def roster_load(cfg):
    try:
        with open(roster_path(cfg), "r", encoding="utf-8") as f:
            stored = json.load(f)
        hosts = stored.get("hosts") if isinstance(stored, dict) else None
        STATE["roster_hosts"] = dict(hosts) if isinstance(hosts, dict) else {}
    except FileNotFoundError:
        STATE["roster_hosts"] = {}
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the saved reader roster (%s); events cannot be attributed "
                    "until Havenz is reachable", e)
        STATE["roster_hosts"] = {}
    return STATE["roster_hosts"]


def terminal_for_source(source):
    """The terminal id a LAN address belongs to, from the live roster or the saved one."""
    for tid, reader in (STATE.get("readers") or {}).items():
        if reader.host == source:
            return tid
    return (STATE.get("roster_hosts") or {}).get(source)


# ---------------------------------------------------------------------------
# The event queue
# ---------------------------------------------------------------------------
#
# A reader posts an event once. It used to be answered 200 and the event then lived in a thread
# that tried Havenz three times over three seconds and gave up - so a restart, or an uplink that
# blinked for ten seconds, lost it. The reader's own log and the backend's poller recover SOME of
# what is missed, depending on how long the reader keeps its log and what kind of event it was;
# "nothing is lost" was a hope, not a property.
#
# Now the order is: write it to disk, THEN answer the reader, then deliver from the disk, and keep
# trying until Havenz has it. The agent has custody of the event from the moment it says 200, so it
# must be able to prove it still has it after a power cut.
#
# The file is an append-only journal, one JSON object per line:
#
#   {"v":1,"op":"put","id":...,"terminalId":...,"kind":"dao","receivedAt":...,"body":"<base64>"}
#   {"v":1,"op":"ack","id":...,"at":...}                 Havenz has it
#   {"v":1,"op":"drop","id":...,"at":...,"reason":...}    given up on, and why
#
# Append-only because appending is the one write a power cut cannot turn into a lost file: the worst
# case is a torn last line, which is ignored. Pending = every put with no ack or drop after it.
# Rewritten (temp file + replace, never truncated in place) on start and every so often, so it
# stays the size of what is actually waiting.

EVENT_QUEUE_VERSION = 1
EVENT_MAX_PENDING = 10000
EVENT_MAX_BYTES = 64 * 1024 * 1024
EVENT_MAX_AGE_SECONDS = 7 * 24 * 3600
EVENT_MAX_BODY_BYTES = 4 * 1024 * 1024
EVENT_COMPACT_AFTER = 500
EVENT_DELIVERY_WORKERS = 4

# How long a reader's post waits for its event to be confirmed on disk before it is answered anyway.
#
# The reader allows five seconds (Reader.configure_monitor sets request_timeout to 5000 ms). In
# every normal case the order is still: on disk first, reader answered second - the journal's
# writer confirms a batch in milliseconds. This bound is for the disk that stalls. Past three
# seconds we choose answering over waiting, and the trade is deliberate:
#
#   - A reader that times out has behaviour nobody has observed. It may retry, it may give up, it
#     may do something worse; a simulation cannot say and the bench has one reader.
#   - An event answered before its line is on disk is not lost. It is in memory, it is being
#     delivered to Havenz, and its line is still queued for the disk. It is lost only if the agent
#     dies in that same moment - and even then it is still in the reader's own log, which Havenz
#     reads every thirty seconds.
#
# Every time it happens it is counted (answered_before_durable on the status page) and logged.
EVENT_ACK_WAIT_SECONDS = 3.0

# The journal's writer thread goes away after this long with nothing to write.
EVENT_WRITER_IDLE_SECONDS = 30.0

# The reader's keepalive is never queued. Delivered an hour late it would tell Havenz "this reader
# spoke just now" about a reader that may since have died - the opposite of what it is for.
EVENT_UNQUEUED_KINDS = frozenset({"device_is_alive"})

# Havenz will never accept these as they stand (the terminal was removed, the payload is
# malformed), so retrying every second is pointless - but the cause may be put right (a terminal
# re-activated), so they are retried slowly for a day before being given up on. Everything else -
# no network, 5xx, 429, even 401 - is an outage, and is retried until it ends.
EVENT_REFUSAL_CODES = frozenset({400, 403, 404, 413, 422})
EVENT_REFUSED_RETRY_SECONDS = 300
EVENT_REFUSED_GIVE_UP_SECONDS = 24 * 3600
EVENT_DEAD_LETTER_KEEP = 200


class EventQueue:
    """The on-disk queue of reader events. Thread-safe; one instance per agent."""

    def __init__(self, path, clock=time.time):
        self.path = path
        self.dead_path = path + ".dead"
        self._clock = clock
        self._lock = threading.Condition()
        self._pending = []            # records, oldest first
        self._in_flight = set()       # terminal ids with a delivery in progress
        self._closed_since_compact = 0
        self._compact_queued = False
        self._needs_newline = False
        # The journal's writer: one thread, one queue of lines waiting for the disk. See _enqueue.
        self._file_lock = threading.Lock()
        self._wcond = threading.Condition()
        self._wqueue = []
        self._writing = False
        self._writer = None
        self._slow_disk_warned_at = 0.0
        self.stats = {"delivered": 0, "dropped": 0, "unpersisted": 0, "corrupt_lines": 0,
                      "redelivered_after_restart": 0, "answered_before_durable": 0,
                      "last_error": None}

    # -- loading ----------------------------------------------------------

    def load(self):
        """
        Pick up where the last process stopped. Never raises.

        A journal that is missing is an empty queue. One that ends in a torn line - the power went
        mid-append - loses at most that line, which is an event the reader was never answered for
        and will still hold in its own log. Anything else unreadable is skipped, counted, and
        logged, because refusing to start would turn one bad line into a site with no events.
        """
        puts, closed, corrupt, torn = {}, set(), 0, False
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            log.info("no event queue at %s yet; starting with an empty one", self.path)
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("could not read the event queue at %s (%s); starting empty - events that "
                      "were waiting in it are NOT being delivered", self.path, e)
            return 0

        lines = raw.split(b"\n")
        self._needs_newline = bool(raw) and not raw.endswith(b"\n")
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
                if rec.get("v") != EVENT_QUEUE_VERSION:
                    raise ValueError(f"version {rec.get('v')}")
                op = rec["op"]
                if op == "put":
                    base64.b64decode(rec["body"], validate=True)
                    puts[rec["id"]] = {
                        "id": rec["id"], "terminalId": rec["terminalId"], "kind": rec["kind"],
                        "receivedAt": float(rec["receivedAt"]), "body": rec["body"],
                        "persisted": True, "attempts": 0, "next_try": 0.0, "refused_since": None,
                        "mono": None, "size": len(rec["body"]),
                    }
                elif op in ("ack", "drop"):
                    closed.add(rec["id"])
            except Exception:  # noqa: BLE001
                if index == len(lines) - 1 and self._needs_newline:
                    torn = True          # the last line, cut short by a power loss
                else:
                    corrupt += 1

        pending = [r for i, r in puts.items() if i not in closed]
        pending.sort(key=lambda r: r["receivedAt"])
        with self._lock:
            self._pending = pending
            self.stats["corrupt_lines"] += corrupt
            self.stats["redelivered_after_restart"] = len(pending)

        if torn:
            log.warning("the event queue ended in a half-written line (power lost mid-write); "
                        "ignored - that event was never acknowledged to its reader")
        if corrupt:
            log.error("%d unreadable line(s) in the event queue at %s were skipped", corrupt, self.path)
        if pending:
            oldest = int(self._clock() - pending[0]["receivedAt"])
            log.warning("recovered %d reader event(s) that had not reached Havenz before the "
                        "restart (oldest %ds ago); delivering them now", len(pending), oldest)
        else:
            log.info("event queue at %s is empty", self.path)

        self._compact()
        return len(pending)

    # -- writing ----------------------------------------------------------
    #
    # One thread writes the journal, and it writes in batches.
    #
    # Every event used to be appended and fsynced by the thread answering its reader, under the
    # queue's one lock. Twenty doors posting in the same second therefore waited for twenty writes
    # in a row; at the quarter of a second a write the first plant rehearsal measured on slow
    # storage, the last reader had waited out its five seconds and hung up before it was answered
    # (316 of 1,760 posts in a thirty-minute soak). Nothing was lost - every one of those events
    # was kept and delivered - but what a real reader does after such a timeout is not known.
    #
    # Now the answering thread only queues its line. The writer takes everything that is queued,
    # writes it in one go and fsyncs ONCE for the lot, then releases every thread waiting on it.
    # Twenty doors in one second wait for one or two fsyncs, not twenty. Acks ride the same writer
    # and nobody waits for them. Compaction runs here too, so there is only ever one writer.

    def _enqueue(self, entry=None, rec=None, durable=False, wait=False, compact=False):
        """
        Queue one journal line (or a compaction) for the writer. Returns the queued item; with
        `wait`, its "done" event is set once the line has been written (or has failed to be).
        """
        item = {
            "line": None if entry is None else json.dumps(entry, separators=(",", ":")).encode("utf-8") + b"\n",
            "op": None if entry is None else entry["op"],
            "rec": rec, "durable": durable, "compact": compact,
            "done": threading.Event() if wait else None,
        }
        with self._wcond:
            self._wqueue.append(item)
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(target=self._write_loop, daemon=True, name="event-journal")
                self._writer.start()
            self._wcond.notify_all()
        return item

    def _write_loop(self):
        """The writer. Goes away when it has been idle a while; the next line starts another."""
        while True:
            with self._wcond:
                idle_since = time.monotonic()
                while not self._wqueue:
                    self._wcond.wait(EVENT_WRITER_IDLE_SECONDS)
                    if not self._wqueue and time.monotonic() - idle_since >= EVENT_WRITER_IDLE_SECONDS:
                        self._writer = None
                        return
                batch, self._wqueue = self._wqueue, []
                self._writing = True
            try:
                lines = []
                for item in batch:
                    if item["compact"]:
                        self._write_lines(lines)
                        lines = []
                        self._compact()
                    else:
                        lines.append(item)
                self._write_lines(lines)
            except Exception:  # noqa: BLE001 - the writer must outlive everything
                log.exception("the event journal's writer hit an unexpected error; carrying on")
            finally:
                for item in batch:
                    if item["done"] is not None:
                        item["done"].set()
                with self._wcond:
                    self._writing = False
                    self._wcond.notify_all()

    def _write_lines(self, items):
        """Append a batch of lines with one write and - if any of them needs it - one fsync."""
        if not items:
            return
        try:
            with self._file_lock:
                with open(self.path, "ab") as f:
                    if self._needs_newline:
                        f.write(b"\n")           # never glue a record onto a torn one
                        self._needs_newline = False
                    f.write(b"".join(item["line"] for item in items))
                    f.flush()
                    # Puts and drops are durable. A batch of nothing but acks is not fsynced:
                    # losing an ack to a power cut costs one re-delivery, which Havenz
                    # de-duplicates, and fsyncing every one would double the SD-card writes.
                    if any(item["durable"] for item in items):
                        os.fsync(f.fileno())
        except Exception as e:  # noqa: BLE001
            for item in items:
                rec = item["rec"]
                if item["op"] == "put":
                    rec["persisted"] = False
                    self.stats["unpersisted"] += 1
                    log.error("could NOT write a %s event from terminal %s to the event queue (%s); "
                              "holding it in memory - it will be lost if the agent restarts before "
                              "Havenz has it", rec["kind"], rec["terminalId"], e)
                else:
                    log.warning("could not journal the %s of event %s (%s); it may be delivered "
                                "again after a restart", item["op"], rec["id"], e)
        finally:
            for item in items:
                if item["done"] is not None:
                    item["done"].set()

    def flush(self, timeout=30.0):
        """
        Block until everything queued for the journal has been written. For shutdown and for tests;
        nothing on a reader's path calls it. Returns False if the disk did not get there in time.
        """
        deadline = time.monotonic() + timeout
        with self._wcond:
            while self._wqueue or self._writing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._wcond.wait(remaining)
        return True

    def put(self, terminal_id, kind, body):
        """
        Take custody of one event. The caller answers the reader when this returns.

        It returns once the event's line is on disk - write-before-answer, as in 0.5.0 - for as
        long as the disk answers within EVENT_ACK_WAIT_SECONDS. Past that it returns anyway (see
        the comment on that constant for the trade). Either way the event is in the in-memory queue
        from the first moment, so its delivery to Havenz does not wait for the disk.

        If the disk write fails the event is kept in memory and delivered anyway, which is exactly
        what happened to every event before this queue existed; it is logged and counted, because
        an agent that can no longer write its disk is about to have bigger problems.
        """
        now = self._clock()
        encoded = base64.b64encode(body).decode("ascii")
        rec = {
            "id": str(uuid.uuid4()), "terminalId": str(terminal_id), "kind": kind,
            "receivedAt": now, "body": encoded, "persisted": True, "attempts": 0, "next_try": 0.0,
            "refused_since": None, "mono": time.monotonic(), "size": len(encoded),
        }
        with self._lock:
            self._pending.append(rec)
            # Queued under the same lock that put it in the pending list, so the journal holds a
            # door's events in the order its reader sent them and a put always precedes its ack.
            item = self._enqueue({"v": EVENT_QUEUE_VERSION, "op": "put", "id": rec["id"],
                                  "terminalId": rec["terminalId"], "kind": kind,
                                  "receivedAt": now, "body": encoded},
                                 rec=rec, durable=True, wait=True)
            self._enforce_bounds_locked()
            self._lock.notify_all()

        if not item["done"].wait(EVENT_ACK_WAIT_SECONDS):
            self.stats["answered_before_durable"] += 1
            mono = time.monotonic()
            if mono - self._slow_disk_warned_at >= 10:
                self._slow_disk_warned_at = mono
                log.warning("the disk took more than %.0fs to confirm a %s event from terminal %s; "
                            "answering the reader now so it does not time out. The event is held in "
                            "memory and is being delivered; its line is still queued for the disk "
                            "(%d answered this way since start)",
                            EVENT_ACK_WAIT_SECONDS, kind, terminal_id,
                            self.stats["answered_before_durable"])
        return rec

    def _close(self, rec, op, reason=None):
        with self._lock:
            if rec in self._pending:
                self._pending.remove(rec)
            self._in_flight.discard(rec["terminalId"])
            if rec["persisted"]:
                entry = {"v": EVENT_QUEUE_VERSION, "op": op, "id": rec["id"], "at": self._clock()}
                if reason:
                    entry["reason"] = reason
                # Written by the journal's writer, and never waited for. A drop is fsynced with
                # its batch; an ack is not (see _write_lines).
                self._enqueue(entry, rec=rec, durable=(op == "drop"))
            self._closed_since_compact += 1
            if self._closed_since_compact >= EVENT_COMPACT_AFTER and not self._compact_queued:
                self._compact_queued = True
                self._enqueue(compact=True)
            self._lock.notify_all()

    def ack(self, rec):
        self.stats["delivered"] += 1
        self._close(rec, "ack")

    def drop(self, rec, reason):
        """Give up on an event - journalled, logged, counted and dead-lettered; never silent."""
        self.stats["dropped"] += 1
        log.warning("DROPPED a %s event from terminal %s received %ds ago: %s (%d dropped since "
                    "start). The reader's own log still holds it if it was an access event.",
                    rec["kind"], rec["terminalId"], int(self._clock() - rec["receivedAt"]),
                    reason, self.stats["dropped"])
        self._dead_letter(rec, reason)
        self._close(rec, "drop", reason)

    def _dead_letter(self, rec, reason):
        try:
            kept = []
            try:
                with open(self.dead_path, "r", encoding="utf-8") as f:
                    kept = f.read().splitlines()[-(EVENT_DEAD_LETTER_KEEP - 1):]
            except FileNotFoundError:
                pass
            kept.append(json.dumps({
                "id": rec["id"], "terminalId": rec["terminalId"], "kind": rec["kind"],
                "receivedAt": rec["receivedAt"], "droppedAt": self._clock(), "reason": reason,
                "body": rec["body"]}, separators=(",", ":")))
            tmp = self.dead_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(kept) + "\n")
            os.replace(tmp, self.dead_path)
        except Exception as e:  # noqa: BLE001
            log.warning("could not keep a copy of the dropped event (%s)", e)

    def _enforce_bounds_locked(self):
        """Oldest out first when the queue outgrows a small box's disk. Caller holds the lock."""
        def too_big():
            return (len(self._pending) > EVENT_MAX_PENDING
                    or sum(r["size"] for r in self._pending) > EVENT_MAX_BYTES)

        while len(self._pending) > 1 and too_big():
            victim = next((r for r in self._pending if r["terminalId"] not in self._in_flight), None)
            if victim is None:
                return
            self._lock.release()
            try:
                self.drop(victim, "the queue is full (uplink down too long) - oldest event discarded")
            finally:
                self._lock.acquire()

    def _compact(self):
        """
        Rewrite the journal as just what is still waiting. Temp file + replace, never in place.

        Runs at load, and afterwards only on the writer's thread, so it never competes with an
        append. Lines still queued behind it are appended to the new file afterwards: a put that is
        also in the copy is then there twice under one id, which loading reads as one event.
        """
        with self._file_lock:
            with self._lock:
                waiting = [{"v": EVENT_QUEUE_VERSION, "op": "put", "id": rec["id"],
                            "terminalId": rec["terminalId"], "kind": rec["kind"],
                            "receivedAt": rec["receivedAt"], "body": rec["body"]}
                           for rec in self._pending if rec["persisted"]]
                closed = self._closed_since_compact
                self._compact_queued = False
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "wb") as f:
                    for entry in waiting:
                        f.write(json.dumps(entry, separators=(",", ":")).encode("utf-8") + b"\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                with self._lock:
                    self._closed_since_compact = max(0, self._closed_since_compact - closed)
                self._needs_newline = False
            except Exception as e:  # noqa: BLE001
                log.warning("could not compact the event queue at %s (%s); carrying on with the "
                            "journal as it is", self.path, e)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- reading ----------------------------------------------------------

    def age_ms(self, rec):
        """
        How long this event has been in our custody.

        Sent with every delivery so Havenz can tell an arrival from history: an event that waited
        out an afternoon's outage must be recorded, and must NOT flash "Welcome" on a door panel
        for someone who walked through hours ago. Measured on the monotonic clock while the
        process that received it is still running, so a Pi whose wall clock jumps when NTP
        arrives does not invent or hide an hour; across a restart only the wall clock survives.
        """
        if rec.get("mono") is not None:
            return max(0, int((time.monotonic() - rec["mono"]) * 1000))
        return max(0, int((self._clock() - rec["receivedAt"]) * 1000))

    def take(self, timeout=None):
        """
        The next event ready to be delivered, or None after `timeout`.

        In order per terminal: a door's events go up in the order its reader sent them, so a
        terminal with a delivery in flight, or whose oldest event is waiting out a retry, is
        skipped whole. Other doors carry on - one removed terminal must not dam the site.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while True:
                now = self._clock()
                blocked = set(self._in_flight)
                expired, wake_in = [], None
                for rec in self._pending:
                    tid = rec["terminalId"]
                    if tid in blocked:
                        continue
                    if now - rec["receivedAt"] > EVENT_MAX_AGE_SECONDS:
                        expired.append(rec)
                        continue
                    if rec["next_try"] > now:
                        blocked.add(tid)
                        wait = rec["next_try"] - now
                        wake_in = wait if wake_in is None else min(wake_in, wait)
                        continue
                    if not expired:
                        self._in_flight.add(tid)
                        return rec
                    break

                if expired:
                    self._lock.release()
                    try:
                        for rec in expired:
                            self.drop(rec, "undelivered for a week")
                    finally:
                        self._lock.acquire()
                    continue

                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                waits = [w for w in (wake_in, remaining) if w is not None]
                self._lock.wait(min(waits) if waits else None)

    def retry_later(self, rec, delay, error, refused=False):
        """Put an event back, to be tried again after `delay` seconds."""
        with self._lock:
            rec["attempts"] += 1
            rec["next_try"] = self._clock() + delay
            if refused and rec["refused_since"] is None:
                rec["refused_since"] = self._clock()
            self.stats["last_error"] = error
            self._in_flight.discard(rec["terminalId"])
            self._lock.notify_all()

    def delivered_attempt(self, rec):
        with self._lock:
            rec["attempts"] += 1

    def snapshot(self):
        with self._lock:
            oldest = self._pending[0]["receivedAt"] if self._pending else None
            return {
                "pending": len(self._pending),
                "oldest_age_seconds": None if oldest is None else int(self._clock() - oldest),
                **self.stats,
            }


def event_headers(cfg, rec, age_ms, attempt):
    """
    X-Terminal-Id names the reader; our hub key proves we are entitled to speak for it. The
    backend checks the terminal belongs to this agent's property, so a compromised agent in one
    building cannot invent access events against doors in another.
    """
    return {
        "Content-Type": "application/json",
        "X-Hub-Key": cfg.get("hub_key", ""),
        "X-Agent-Version": AGENT_VERSION,
        "X-Terminal-Id": str(rec["terminalId"]),
        "X-Event-Id": rec["id"],
        "X-Event-Age-Ms": str(age_ms),
        "X-Event-Attempt": str(attempt),
    }


def post_event(cfg, rec, age_ms, attempt):
    """
    One attempt to hand an event to Havenz. Returns ("ok" | "retry" | "refused", detail), with a
    third element - seconds to stay away - when Havenz refused it with 429 and said how long.

    The payload goes up byte for byte as the reader sent it - parsing, idempotency and
    broadcasting all stay in the one implementation on the backend that already gets them right.
    """
    url = f"{cfg['api_url'].rstrip('/')}/api/amico/notifications/{rec['kind']}"
    body = base64.b64decode(rec["body"])
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=event_headers(cfg, rec, age_ms, attempt))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return "ok", None
    except urllib.error.HTTPError as e:
        detail = f"HTTP {e.code}"
        if e.code == 429:
            return "retry", detail, retry_after_seconds(e, None)
        return ("refused" if e.code in EVENT_REFUSAL_CODES else "retry"), detail
    except Exception as e:  # noqa: BLE001
        return "retry", str(e)


def deliver_one(cfg, queue, rec, post=None):
    """Deliver one taken event and settle it in the queue. Returns the outcome."""
    post = post or post_event
    attempt = rec["attempts"] + 1
    answer = post(cfg, rec, queue.age_ms(rec), attempt)
    outcome, detail = answer[0], answer[1]
    asked = answer[2] if len(answer) > 2 else None

    if outcome == "ok":
        queue.delivered_attempt(rec)
        queue.ack(rec)
        if attempt > 1 or queue.age_ms(rec) > 5000:
            log.info("relayed %s event from terminal %s on attempt %d, %ds after the reader sent it",
                     rec["kind"], rec["terminalId"], attempt, queue.age_ms(rec) // 1000)
        else:
            log.info("relayed %s event from terminal %s", rec["kind"], rec["terminalId"])
        return outcome

    if outcome == "refused":
        since = rec["refused_since"]
        if since is not None and queue._clock() - since > EVENT_REFUSED_GIVE_UP_SECONDS:
            queue.drop(rec, f"refused by Havenz for a day ({detail})")
            return "dropped"
        if since is None:
            log.warning("Havenz refused a %s event from terminal %s (%s) - is that door still "
                        "assigned to this agent? Keeping it; trying again every %d minutes for a day",
                        rec["kind"], rec["terminalId"], detail, EVENT_REFUSED_RETRY_SECONDS // 60)
        queue.retry_later(rec, EVENT_REFUSED_RETRY_SECONDS, detail, refused=True)
        return outcome

    # An outage, not a refusal: keep it, back off, keep trying until the uplink is back.
    delay = min(BACKOFF_MAX_SECONDS, 2 ** min(rec["attempts"], 6))
    if asked:
        delay = max(delay, asked)            # a 429 said how long; do not come back sooner
    if rec["attempts"] in (0, 3) or rec["attempts"] % 20 == 0:
        log.warning("could not relay %s from terminal %s (%s) - it is safe on disk; trying again "
                    "in %ds", rec["kind"], rec["terminalId"], detail, delay)
    queue.retry_later(rec, delay, detail)
    return outcome


def event_delivery_loop(cfg, queue):
    """One delivery worker. Several run; the queue keeps each door's events in order."""
    while True:
        try:
            rec = queue.take(timeout=30)
            if rec is not None:
                deliver_one(cfg, queue, rec)
        except Exception:  # noqa: BLE001 - this loop must outlive everything
            log.exception("event delivery worker hit an unexpected error; carrying on")
            time.sleep(1)


def relay_unqueued(cfg, terminal_id, kind, raw):
    """
    Best-effort relay for what is deliberately not queued: keepalives, and the rare event too big
    to journal. One try, now. A keepalive that cannot be delivered now is worthless later.
    """
    rec = {"id": str(uuid.uuid4()), "terminalId": str(terminal_id), "kind": kind,
           "body": base64.b64encode(raw).decode("ascii")}
    outcome, detail = post_event(cfg, rec, 0, 1)
    if outcome != "ok" and kind not in EVENT_UNQUEUED_KINDS:
        log.warning("could not relay an oversized %s event from terminal %s (%s); it was not "
                    "queued", kind, terminal_id, detail)


def start_event_listener(cfg, port, queue=None):
    """
    Receive access events straight from the readers, keep them safe, and pass them upstream.

    Once this is running there is no internet-facing webhook for an agent-served site at all.
    That dissolves a real problem rather than mitigating it: the public endpoint identified a
    reader by source IP, with device_id as a fallback, and device_id is printed on the outside of
    the device. Here the reader is on our own network and we authenticate the relay ourselves.

    The reader posts to hostname:port/{path}/{kind}, so this serves /api/amico/notifications/dao
    and its siblings.

    The order is the point: the event is written to the on-disk queue FIRST, the reader is
    answered SECOND, and delivery to Havenz happens from the queue. Answering first and hoping
    to deliver - what this used to do - means a restart or a ten-second uplink blip loses an event
    the reader believes we have.
    """
    if queue is None:
        queue = EventQueue(cfg.get("event_queue_path") or "/data/events.jsonl")
        queue.load()
    STATE["event_queue"] = queue

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, body=b"{}"):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            kind = self.path.rstrip("/").rsplit("/", 1)[-1] or "dao"
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            source = self.client_address[0]

            # Which reader this is. On our own LAN the source address is trustworthy in a way it
            # never is across the internet — we are on the same broadcast domain as the device.
            terminal_id = terminal_for_source(source)

            if terminal_id is None:
                log.warning("event from %s on this LAN, which is not a reader we manage — ignored", source)
                return self._reply(404)

            if kind in EVENT_UNQUEUED_KINDS or len(raw) > EVENT_MAX_BODY_BYTES:
                self._reply(200)
                threading.Thread(target=relay_unqueued, args=(cfg, terminal_id, kind, raw),
                                 daemon=True).start()
                return

            # Disk first, reader second. put() returns once the event is fsynced (or, if the disk
            # refused, once it is at least held in memory - no worse than before there was a queue).
            try:
                queue.put(terminal_id, kind, raw)
            except Exception:  # noqa: BLE001 - a reader must never see our bug as its problem
                log.exception("the event queue refused a %s event; relaying it directly", kind)
                threading.Thread(target=relay_unqueued, args=(cfg, terminal_id, kind, raw),
                                 daemon=True).start()
            self._reply(200)

        def do_GET(self):
            self._reply(200, b'{"havenz":"site agent event listener"}')

    # Threaded, because a plain HTTPServer accepts connections one at a time. With one reader that
    # is invisible; with twenty posting at a shift change the twentieth waits behind the other
    # nineteen, and a reader kept waiting may give up on an event it has already recorded.
    server = ThreadedHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(EVENT_DELIVERY_WORKERS):
        threading.Thread(target=event_delivery_loop, args=(cfg, queue), daemon=True).start()
    log.info("listening for reader events on port %d (queue: %s)", port, queue.path)
    return server


# ---------------------------------------------------------------------------
# On-site UI (Home Assistant ingress)
# ---------------------------------------------------------------------------

SETUP_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect your Havenz site agent</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
         min-height: 100vh; display: grid; place-items: center; background: #0b0f14; color: #e7edf3; }
  .card { width: min(92vw, 440px); background: #121821; border: 1px solid #223; border-radius: 16px;
          padding: 28px; box-shadow: 0 10px 40px rgba(0,0,0,.4); }
  h1 { font-size: 20px; margin: 0 0 4px; }
  p  { color: #93a1b0; font-size: 14px; margin: 0 0 20px; }
  input { width: 100%; box-sizing: border-box; font-size: 22px; letter-spacing: .12em; text-align: center;
          padding: 14px; border-radius: 10px; border: 1px solid #2a3a4a; background: #0d131a; color: #fff;
          text-transform: uppercase; }
  button { width: 100%; margin-top: 14px; padding: 14px; font-size: 16px; font-weight: 600; border: 0;
           border-radius: 10px; background: #06b6d4; color: #012; cursor: pointer; }
  button:disabled { opacity: .6; cursor: default; }
  .msg { margin-top: 14px; font-size: 14px; text-align: center; min-height: 20px; }
  .ok { color: #34d399; } .err { color: #f87171; }
</style></head>
<body><div class="card">
  <h1>Connect this site agent</h1>
  <p>Enter the pairing code from the Havenz app (Property &rarr; Site agents &rarr; Add agent).</p>
  <input id="code" placeholder="HVNZ-XXXX-XXXX" autocomplete="off" autofocus>
  <button id="go">Connect</button>
  <div class="msg" id="msg"></div>
</div>
<script>
  const btn = document.getElementById('go'), inp = document.getElementById('code'), msg = document.getElementById('msg');
  btn.onclick = async () => {
    const code = inp.value.trim().toUpperCase();
    if (!code) { msg.className='msg err'; msg.textContent='Enter your pairing code'; return; }
    btn.disabled = true; msg.className='msg'; msg.textContent='Connecting\\u2026';
    try {
      // relative URL so it works both standalone and behind Home Assistant ingress
      const r = await fetch('register', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pairingCode: code})});
      const d = await r.json();
      if (r.ok) { msg.className='msg ok'; msg.textContent='Connected. This site\\u2019s doors are now reachable from Havenz.'; inp.disabled=true; setTimeout(()=>location.reload(), 1500); }
      else { msg.className='msg err'; msg.textContent = d.error || 'That code did not work.'; btn.disabled=false; }
    } catch (e) { msg.className='msg err'; msg.textContent='Could not reach the agent.'; btn.disabled=false; }
  };
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') btn.click(); });
</script></body></html>"""


# The installer's page. Deliberately shows the reader list and the time since last contact rather
# than a green tick: the lesson of the monitor-path bug is that silent success is worse than loud
# failure, and someone standing in a plant room needs to see what is actually working.
STATUS_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Havenz site agent</title>
<style>:root{color-scheme:light dark}body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
min-height:100vh;display:grid;place-items:center;background:#0b0f14;color:#e7edf3}
.card{width:min(92vw,460px);background:#121821;border:1px solid #223;border-radius:16px;padding:28px}
h1{font-size:20px;margin:0 0 6px;text-align:center}.ok{color:#34d399;font-size:15px;text-align:center}
.bad{color:#f87171;font-size:15px;text-align:center}p{color:#93a1b0;font-size:14px}
ul{list-style:none;padding:0;margin:18px 0 0}li{display:flex;justify-content:space-between;gap:12px;
padding:9px 0;border-top:1px solid #223;font-size:14px}li span:last-child{color:#93a1b0;font-size:13px}
details{margin-top:18px}summary{color:#93a1b0;font-size:13px;cursor:pointer}
input{width:100%;box-sizing:border-box;font-size:18px;letter-spacing:.12em;text-align:center;margin-top:12px;
padding:12px;border-radius:10px;border:1px solid #2a3a4a;background:#0d131a;color:#fff;text-transform:uppercase}
button{width:100%;margin-top:12px;padding:12px;font-size:15px;font-weight:600;border:0;border-radius:10px;
background:#06b6d4;color:#012;cursor:pointer}button:disabled{opacity:.6;cursor:default}
.msg{margin-top:12px;font-size:14px;min-height:20px}.err{color:#f87171}</style></head>
<body><div class="card"><h1>Havenz site agent</h1>
<div id="conn" class="ok">Connecting\\u2026</div>
<p id="detail" style="text-align:center"></p>
<ul id="terms"></ul>
<details><summary>Move this agent to a different property</summary>
<p style="margin-top:10px">Enter a new pairing code (Property &rarr; Site agents &rarr; Add agent in
the Havenz app). This agent will stop serving the current property immediately.</p>
<input id="code" placeholder="HVNZ-XXXX-XXXX" autocomplete="off">
<button id="go">Move agent</button>
<div class="msg" id="msg"></div></details></div>
<script>
  async function refresh() {
    try {
      const s = await (await fetch('status.json')).json();
      const conn = document.getElementById('conn'), detail = document.getElementById('detail');
      if (s.last_error) { conn.className='bad'; conn.textContent='\\u26a0 ' + s.last_error; }
      else if (s.seconds_since_heartbeat === null) { conn.className='ok'; conn.textContent='Connecting\\u2026'; }
      else { conn.className='ok'; conn.textContent='\\u2713 Connected to Havenz'; }
      const ev = s.events || {};
      const rs = s.results || {};
      detail.textContent = (s.seconds_since_heartbeat === null ? ''
        : 'Last contact ' + s.seconds_since_heartbeat + 's ago' +
          (s.site_name ? ' \\u2014 ' + s.site_name : '')) +
        (ev.pending ? ' \\u2014 ' + ev.pending + ' door event(s) waiting to be sent' : '') +
        (ev.dropped ? ' \\u2014 ' + ev.dropped + ' dropped' : '') +
        (rs.pending ? ' \\u2014 ' + rs.pending + ' result(s) waiting to be sent' : '') +
        (rs.dropped ? ' \\u2014 ' + rs.dropped + ' result(s) dropped' : '');
      document.getElementById('terms').innerHTML = (s.terminals||[]).length
        ? s.terminals.map(t => '<li><span>' + t.name + '</span><span>' + (t.ipAddress||'') + '</span></li>').join('')
        : '<li><span>No doors assigned to this agent yet</span><span></span></li>';
    } catch (e) { /* page stays as it was; the next tick will correct it */ }
  }
  refresh(); setInterval(refresh, 5000);
  const btn = document.getElementById('go'), inp = document.getElementById('code'), msg = document.getElementById('msg');
  btn.onclick = async () => {
    const code = inp.value.trim().toUpperCase();
    if (!code) { msg.className='msg err'; msg.textContent='Enter your pairing code'; return; }
    btn.disabled = true; msg.className='msg'; msg.textContent='Moving\\u2026';
    try {
      const r = await fetch('register', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pairingCode: code})});
      const d = await r.json();
      if (r.ok) { msg.className='msg'; msg.textContent='Moved.'; inp.disabled=true; setTimeout(()=>location.reload(), 1200); }
      else { msg.className='msg err'; msg.textContent = d.error || 'That code did not work.'; btn.disabled=false; }
    } catch (e) { msg.className='msg err'; msg.textContent='Could not reach the agent.'; btn.disabled=false; }
  };
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') btn.click(); });
</script></body></html>"""


def start_web_server(cfg_path, cfg, port, paired_event):
    """Non-blocking local web server: pairing form until paired, status page after."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.rstrip("/").endswith("status.json"):
                last = STATE["last_heartbeat_at"]
                return self._send(200, json.dumps({
                    "site_name": cfg.get("site_name"),
                    "seconds_since_heartbeat": None if last is None else int(time.time() - last),
                    "last_error": STATE["last_error"],
                    "clock_skew_seconds": STATE["clock_skew_seconds"],
                    "terminals": STATE["terminals"],
                    # Reader events still waiting to reach Havenz, and anything given up on.
                    "events": STATE["event_queue"].snapshot() if STATE.get("event_queue") else None,
                    # Command results still waiting to reach Havenz, and anything given up on.
                    "results": STATE["result_outbox"].snapshot() if STATE.get("result_outbox") else None,
                }))
            html = STATUS_HTML if cfg.get("hub_key") else SETUP_HTML
            self._send(200, html, "text/html; charset=utf-8")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                code = (payload.get("pairingCode") or "").strip()
                if not code:
                    return self._send(400, json.dumps({"error": "Enter your pairing code"}))
                register(cfg_path, cfg, code)
                self._send(200, json.dumps({"ok": True}))
                paired_event.set()
            except SystemExit as e:
                self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(e)}))

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 agent.py <config.json> [--register HVNZ-XXXX-XXXX]")
    cfg_path = sys.argv[1]
    cfg = load_config(cfg_path)
    args = sys.argv[2:]

    if "--register" in args:
        i = args.index("--register")
        if i + 1 >= len(args):
            raise SystemExit("usage: python3 agent.py <config.json> --register HVNZ-XXXX-XXXX")
        register(cfg_path, cfg, args[i + 1])
        return

    paired = threading.Event()
    if cfg.get("hub_key"):
        paired.set()

    # --once is a diagnostic: do one cycle and report. Waiting indefinitely for someone to pair
    # would make it look like a hang, which is exactly what it did the first time it was used.
    if "--once" in args and not cfg.get("hub_key"):
        raise SystemExit("not paired — run with --register HVNZ-XXXX-XXXX first")
    if cfg.get("setup_server") or "--setup" in args or not cfg.get("hub_key"):
        start_web_server(cfg_path, cfg, int(cfg["setup_port"]), paired)
        if not paired.is_set():
            log.info("not paired yet — open the Havenz Agent panel in Home Assistant and enter "
                     "your pairing code")
            paired.wait()
            log.info("paired — starting up")

    log.info("Havenz site agent v%s -> %s (heartbeat every %ss)",
             AGENT_VERSION, cfg["api_url"], cfg["heartbeat_interval_seconds"])

    # Before anything can be leased. An agent that starts taking commands before it remembers what
    # it did last time is exactly the agent that opens a door twice after a restart.
    executed_store_load(cfg)

    if "--once" in args:
        heartbeat(cfg)
        print(json.dumps(STATE["terminals"], indent=2))
        return

    # Which address is which door, as of the last time Havenz told us - so an event that arrives
    # while Havenz is unreachable is attributed and queued rather than refused.
    roster_load(cfg)

    # Readers post their events to us directly, so this has to be up before we tell any of them to.
    # It also recovers whatever was still waiting in the on-disk queue when the agent last stopped.
    start_event_listener(cfg, int(cfg.get("webhook_port", 8100)))

    # Learn the roster once at startup so an event arriving in the first few seconds can be
    # attributed. Failure is not fatal — the command loop refreshes it.
    try:
        readers_for(cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("could not load the reader list at startup (%s); will retry", e)

    # Before any command can be leased: whatever results the last process had not delivered go up
    # now, and the thread that syncs the journals is running.
    result_outbox(cfg)
    _flusher_start()

    # Commands are collected on their own thread and carried out on one thread per reader. The
    # heartbeat must keep reporting while a reader is being slow, an unlock must not wait behind a
    # heartbeat, and one door must not wait behind another — separate concerns, separate threads.
    threading.Thread(target=command_loop, args=(cfg,), daemon=True).start()

    # Discovery gets its own thread too: a sweep of 254 addresses takes seconds, and no door should
    # wait on it.
    if cfg.get("discovery_enabled"):
        threading.Thread(target=discovery_loop, args=(cfg,), daemon=True).start()
    heartbeat_loop(cfg)


if __name__ == "__main__":
    main()
