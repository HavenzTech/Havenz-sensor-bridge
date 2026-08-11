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
import json
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("havenz-agent")

AGENT_VERSION = "0.1.0"

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
        return probe.getsockname()[0]
    except Exception:  # noqa: BLE001
        return socket.gethostbyname(socket.gethostname())
    finally:
        probe.close()


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
        self.ip = ip
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

    def access_logs(self):
        """
        Every access-log row the reader is holding.

        Timestamps are passed up untouched. The reader runs on local time and reports that wall
        clock as if it were UTC; undoing that is the backend's job, in the one place that already
        does it correctly.
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
        return {"entries": entries}

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
# Commands
# ---------------------------------------------------------------------------

# Command ids we have already carried out.
#
# Delivery is at-least-once by design: a lease whose result never reached the backend is retried,
# and the retry is indistinguishable from a first delivery. Doing the work twice is harmless for a
# user sync and unacceptable for an unlock, so the guarantee has to live here, at the only place
# that knows whether the reader was actually touched.
#
# Bounded because this runs for months on a Pi and an unbounded set is a slow memory leak.
_executed = {}
EXECUTED_MEMORY = 500


def _remember(command_id, result):
    _executed[command_id] = result
    if len(_executed) > EXECUTED_MEMORY:
        for stale in list(_executed)[:len(_executed) - EXECUTED_MEMORY]:
            _executed.pop(stale, None)


def readers_for(cfg, force=False):
    """
    This site's readers, with their credentials, cached between calls.

    Refetched when the backend mentions a terminal we do not know about, so a reader claimed in
    Zhub becomes usable without restarting the add-on.
    """
    if force or not STATE.get("readers"):
        rows = backend_get(cfg, "/api/agent/terminals")
        STATE["readers"] = {
            r["id"]: Reader(r["id"], r.get("name", "?"), r["ipAddress"], r["username"], r["password"])
            for r in rows
        }
        log.info("hold credentials for %d reader(s)", len(STATE["readers"]))
    return STATE["readers"]


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
        return reader.configure_monitor(local_address_for(reader.ip), cfg.get("webhook_port", 8100))
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
        return reader.access_logs()
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
    command_id = command.get("id")

    # A repeat of something already done. Acknowledge with the original result rather than doing
    # it again — the point of remembering.
    if command_id in _executed:
        log.info("command %s already executed; acknowledging without repeating", command_id)
        report(cfg, command_id, True, _executed[command_id], None, 0)
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

    started = time.time()
    try:
        result = execute(cfg, command)
        elapsed = int((time.time() - started) * 1000)
        _remember(command_id, result)
        log.info("%s on terminal %s in %dms", command.get("type"), command.get("terminalId"), elapsed)
        report(cfg, command_id, True, result, None, elapsed)
    except ReaderError as e:
        elapsed = int((time.time() - started) * 1000)
        log.warning("%s failed after %dms: %s", command.get("type"), elapsed, e)
        report(cfg, command_id, False, None, str(e), elapsed)
    except Exception as e:  # noqa: BLE001
        elapsed = int((time.time() - started) * 1000)
        log.exception("%s raised unexpectedly", command.get("type"))
        report(cfg, command_id, False, None, f"agent error: {e}", elapsed)


def report(cfg, command_id, success, result, error, duration_ms):
    """Send the outcome back. A failure to report is logged, not raised — the work is already done."""
    try:
        backend_post(cfg, f"/api/agent/commands/{command_id}/result", {
            "success": success,
            "result": None if result is None else json.dumps(result),
            "error": error,
            "durationMs": duration_ms,
        }, timeout=15)
    except Exception as e:  # noqa: BLE001
        # The backend will reap this as 'unknown', which is the honest outcome: the reader may
        # well have acted and we could not say so.
        log.warning("could not report the result of %s (%s)", command_id, e)


def command_loop(cfg):
    """Hold a long poll open, execute whatever arrives, repeat."""
    wait = int(cfg.get("command_wait_seconds", 25))
    backoff = BACKOFF_START_SECONDS

    while True:
        try:
            batch = backend_get(cfg, f"/api/agent/commands?wait={wait}", timeout=wait + 15)
            backoff = BACKOFF_START_SECONDS
            for command in batch.get("commands") or []:
                handle(cfg, command)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log.error("no longer authorised to collect commands (HTTP %d)", e.code)
                time.sleep(BACKOFF_MAX_SECONDS)
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

def start_event_listener(cfg, port):
    """
    Receive access events straight from the readers and pass them upstream.

    Once this is running there is no internet-facing webhook for an agent-served site at all.
    That dissolves a real problem rather than mitigating it: the public endpoint identified a
    reader by source IP, with device_id as a fallback, and device_id is printed on the outside of
    the device. Here the reader is on our own network and we authenticate the relay ourselves.

    The reader posts to hostname:port/{path}/{kind}, so this serves /api/amico/notifications/dao
    and its siblings. The payload is passed through untouched — parsing, idempotency and
    broadcasting all stay in the one implementation on the backend that already gets them right.
    """
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
            terminal_id = None
            for tid, reader in (STATE.get("readers") or {}).items():
                if reader.ip == source:
                    terminal_id = tid
                    break

            if terminal_id is None:
                log.warning("event from %s on this LAN, which is not a reader we manage — ignored", source)
                return self._reply(404)

            # Answer the reader immediately, then forward. A reader kept waiting on our uplink is
            # a reader that may give up and drop the event, and it has already done its job.
            self._reply(200)
            threading.Thread(target=forward_event, args=(cfg, terminal_id, kind, raw), daemon=True).start()

        def do_GET(self):
            self._reply(200, b'{"havenz":"site agent event listener"}')

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("listening for reader events on port %d", port)
    return server


def forward_event(cfg, terminal_id, kind, raw):
    """
    Relay one reader event upstream, retrying briefly before giving up.

    X-Terminal-Id names the reader; our hub key proves we are entitled to speak for it. The
    backend checks the terminal belongs to this agent's property, so a compromised agent in one
    building cannot invent access events against doors in another.
    """
    url = f"{cfg['api_url'].rstrip('/')}/api/amico/notifications/{kind}"
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Key": cfg.get("hub_key", ""),
        "X-Agent-Version": AGENT_VERSION,
        "X-Terminal-Id": str(terminal_id),
    }

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=raw, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            log.info("relayed %s event from terminal %s", kind, terminal_id)
            return
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                # Not fatal, and deliberately not buffered here: the reader keeps its own access
                # log and the backend's poller collects anything the webhook missed. The webhook
                # makes events instant; the poller is what makes them survive.
                log.warning("could not relay %s from terminal %s (%s) — the poller will "
                            "collect it later", kind, terminal_id, e)
                return
            time.sleep(2 ** attempt)


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
      detail.textContent = s.seconds_since_heartbeat === null ? ''
        : 'Last contact ' + s.seconds_since_heartbeat + 's ago' +
          (s.site_name ? ' \\u2014 ' + s.site_name : '');
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

    if "--once" in args:
        heartbeat(cfg)
        print(json.dumps(STATE["terminals"], indent=2))
        return

    # Readers post their events to us directly, so this has to be up before we tell any of them to.
    start_event_listener(cfg, int(cfg.get("webhook_port", 8100)))

    # Learn the roster once at startup so an event arriving in the first few seconds can be
    # attributed. Failure is not fatal — the command loop refreshes it.
    try:
        readers_for(cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("could not load the reader list at startup (%s); will retry", e)

    # Commands run on their own thread. The heartbeat must keep reporting while a reader is being
    # slow, and an unlock must not wait behind a heartbeat — separate concerns, separate threads.
    threading.Thread(target=command_loop, args=(cfg,), daemon=True).start()
    heartbeat_loop(cfg)


if __name__ == "__main__":
    main()
