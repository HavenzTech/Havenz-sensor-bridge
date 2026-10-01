"""What every scenario has to hand: the plant's ids, signed-in clients, and ways to observe it."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, feeder as feeder_mod, readers, record, seed as seed_mod, stack
from .api import Account
from .signalr import SignalRClient
from .util import http, wait_until


class Context:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.world = seed_mod.load_world(run_dir)
        self.api = seed_mod.api_from_world(self.world)
        self.timeline = record.Timeline(run_dir)
        self.company_id = self.world["company"]["id"]
        self.property_id = self.world["property"]["id"]
        self.people = {p["key"]: p for p in self.world["people"]}
        self.terminals = {t["number"]: t for t in self.world["terminals"]}
        self.terminal_by_id = {t["id"]: t for t in self.world["terminals"]}
        self.areas = self.world["areas"]
        self.feeder = feeder_mod.Feeder(self.world)
        self.hub = None
        self.deferred = []            # (scenario name, due monotonic, callable(record))
        self._clock_offset = None
        self._clock_offset_at = 0.0
        self._accounts = {}
        self.options = {}

    # -- world ----------------------------------------------------------------

    def save_world(self):
        seed_mod.save_world(self.run_dir, self.world)

    def doors_for(self, person):
        return [t for t in self.world["terminals"] if t["areaKey"] in person["areas"]]

    def admin(self, key="admin-1"):
        email = self.people[key]["email"]
        return next(a for a in self.api.admins if a.email == email)

    def platform_operator(self):
        """The one account that may register and retire screens (super-admin of the plant's company)."""
        email = self.world["accounts"]["plantOperator"]["email"]
        return next(a for a in self.api.admins if a.email == email)

    def account_for(self, person_key):
        """Sign in as a staff member. Their first sign-in replaces the temporary password, as it must."""
        if person_key in self._accounts:
            return self._accounts[person_key]
        p = self.people[person_key]
        if p.get("password"):
            acct = Account(p["email"], p["password"], p["name"])
            self.api.login(acct)
        else:
            acct = Account(p["email"], p["temporaryPassword"], p["name"])
            self.api.login(acct)
            self.api.change_password(acct, seed_mod.STAFF_PASSWORD)
            p["password"] = seed_mod.STAFF_PASSWORD
            self.save_world()
        self._accounts[person_key] = acct
        return acct

    def new_person(self, name, email, area_keys, wait_seconds=240):
        """A new member of staff, added mid-run: account, photo, access, and on their readers."""
        _, created = self.api.post("/api/admin/users", {"email": email, "name": name, "role": "employee"}, expect=201)
        person = {"key": email.split("@")[0], "group": "added", "name": name, "email": email, "role": "employee",
                  "areas": list(area_keys), "id": created["userId"], "temporaryPassword": created["temporaryPassword"]}
        self.api.upload_photo(f"/api/havenzhub/facialrecognition/enroll/photo/{person['id']}",
                              seed_mod.photo_jpeg(name, int(time.time()) % 1000), expect=200)
        self.api.post("/api/admin/area-access/bulk",
                      {"userIds": [person["id"]], "areaIds": [self.areas[a]["id"] for a in area_keys],
                       "accessLevel": "standard"}, expect=200, timeout=300)
        doors = [t["number"] for t in self.world["terminals"] if t["areaKey"] in area_keys]
        ok, waited = wait_until(lambda: len(self.readers_holding(person["id"], doors)) == len(doors), wait_seconds, 2)
        person["onReadersAfterSeconds"] = round(waited, 1) if ok else None
        self.world["people"].append(person)
        self.people[person["key"]] = person
        self.save_world()
        return person

    # -- the device end --------------------------------------------------------

    def readers_holding(self, user_id, numbers=None, need_face=True):
        """Which readers hold this person right now (asked of the readers, not of the backend)."""
        numbers = numbers or range(1, config.READER_COUNT + 1)

        def holds(n):
            for u in readers.detail(n)["userList"]:
                if u["registration"] == user_id and (u["hasFace"] or not need_face):
                    return n
            return None
        with ThreadPoolExecutor(max_workers=10) as pool:
            return [n for n in pool.map(holds, numbers) if n]

    def user_counts(self):
        """How many people each reader holds - one call for all twenty."""
        return {r["number"]: r["users"] for r in readers.state()["readers"]}

    def wait_until_dropped(self, before, numbers, timeout):
        """
        The moment (time.monotonic) every one of `numbers` holds fewer people than in `before` - a
        removal has landed on each - or None. Polled five times a second with a single request, so
        the figure is the readers' timing rather than the harness's.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            now = self.user_counts()
            if all(now[n] < before[n] for n in numbers):
                return time.monotonic()
            time.sleep(0.2)
        return None

    def open_counts(self):
        return {r["number"]: {"face": r["opensByFace"], "remote": r["opensRemote"], "all": r["opens"]}
                for r in readers.state()["readers"]}

    def vm_now(self):
        """
        The containers' clock, as epoch seconds, right now.

        Rows the backend writes and mail the sink receives are stamped with the containers' clock;
        a timing taken as "row time minus the moment the harness acted" needs both on one clock.
        Measured on such stamps, a timing does not stretch when this machine is busy and the
        harness is slow to look.
        """
        if self._clock_offset is None or time.monotonic() - self._clock_offset_at > 120:
            self._clock_offset = self.vm_clock_offset()
            self._clock_offset_at = time.monotonic()
        return time.time() + self._clock_offset

    def vm_clock_offset(self):
        """Seconds to ADD to this machine's clock to get the containers' clock."""
        t0 = time.time()
        now = readers.state()["now"]
        t1 = time.time()
        return now - (t0 + t1) / 2

    # -- the backend's record ---------------------------------------------------

    def access_events(self, since_iso, terminal_id=None):
        where = f"created_at >= '{since_iso}'"
        if terminal_id:
            where += f" and terminal_id = '{terminal_id}'"
        rows = stack.psql(f"""
select id, terminal_id, terminal_access_log_id, coalesce(user_id::text, ''), event_code, event_type, source,
       extract(epoch from "timestamp"), extract(epoch from created_at), coalesce(denied_reason, '')
from iot.amico_access_events where {where} order by created_at, id""") or []
        return [{"id": r[0], "terminalId": r[1], "logId": int(r[2]), "userId": r[3] or None, "code": int(r[4]),
                 "type": r[5], "source": r[6], "occurred": float(r[7]), "recorded": float(r[8]),
                 "deniedReason": r[9]} for r in rows]

    def sink_messages(self, since_iso, to=None, subject=None):
        query = f"since={since_iso}"
        if to:
            query += f"&to={to}"
        _, rows, _ = http("GET", f"{config.SINK}/messages?{query}", timeout=15, expect=200)
        if subject:
            rows = [m for m in rows if subject.lower() in m["subject"].lower()]
        return rows

    def open_alerts(self, rule=None, status="open"):
        _, page = self.api.get(f"/api/havenzhub/alerts?propertyId={self.property_id}&status={status}&pageSize=100",
                               expect=200)
        rows = page["data"]
        return [a for a in rows if rule is None or a["rule"] == rule]

    def alert_rows(self, since_iso, rule=None, device_id=None, hub_id=None):
        """Incidents straight from the table - for counting, where a page of API results could hide one."""
        where = f"fired_at >= '{since_iso}'"
        if rule:
            where += f" and rule = '{rule}'"
        if device_id:
            where += f" and device_id = '{device_id}'"
        if hub_id:
            where += f" and site_hub_id = '{hub_id}'"
        rows = stack.psql(f"""
select id, rule, severity, coalesce(confirmation_state, ''), extract(epoch from fired_at),
       coalesce(extract(epoch from resolved_at)::text, ''), occurrences, extract(epoch from created_at)
from iot.alerts where {where} order by fired_at""") or []
        return [{"id": r[0], "rule": r[1], "severity": r[2], "confirmation": r[3], "firedAt": float(r[4]),
                 "resolvedAt": float(r[5]) if r[5] else None, "occurrences": int(r[6]),
                 "createdAt": float(r[7])} for r in rows]

    def deliveries(self, alert_id):
        rows = stack.psql(f"""
select event_kind, channel, status, coalesce(recipient_email, ''), attempts,
       coalesce(extract(epoch from sent_at)::text, '')
from iot.alert_deliveries where alert_id = '{alert_id}' order by created_at""") or []
        return [{"event": r[0], "channel": r[1], "status": r[2], "email": r[3], "attempts": int(r[4]),
                 "sentAt": float(r[5]) if r[5] else None} for r in rows]

    def health(self):
        _, body = self.api.get(f"/api/havenzhub/properties/{self.property_id}/health", expect=200)
        return body

    def agent_status(self):
        """The agent's own status page (pending events, last contact). None when it cannot be reached."""
        try:
            status, body, _ = http("GET", f"{config.AGENT_PAGE}/status.json", timeout=4)
            return body if status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    def agent_queue_on_disk(self):
        """Pending reader events in the agent's journal, read from its /data folder directly."""
        path = config.stack_dir() / "agent-data" / "events.jsonl"
        puts, closed = {}, set()
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("op") == "put":
                    puts[rec["id"]] = rec
                elif rec.get("op") in ("ack", "drop"):
                    closed.add(rec["id"])
        except FileNotFoundError:
            return []
        return [r for i, r in puts.items() if i not in closed]

    # -- what the screens hear ---------------------------------------------------

    def connect_hub(self):
        """Listen, as a signed-in administrator's page does, to every door's welcome events."""
        if self.hub:
            self.hub.close()
        account = self.admin("admin-1")
        self.api.login(account)                       # a full-length token: the hub closes when it expires
        hub = SignalRClient(config.API, lambda: account.token).connect()
        for t in self.world["terminals"]:
            hub.invoke("JoinTerminalGroup", t["id"])
        self.hub = hub
        return hub

    def welcomes(self, since_mono):
        out = []
        for mono, wall, target, args in self.hub.received("WelcomeEvent", since_mono):
            payload = args[0] if args else {}
            out.append({"mono": mono, "wall": wall, "eventType": payload.get("eventType"),
                        "terminalId": payload.get("terminalId"), "userId": (payload.get("user") or {}).get("id"),
                        "userName": (payload.get("user") or {}).get("name"), "timestamp": payload.get("timestamp")})
        return out

    # -- checks that can only be made later ---------------------------------------

    def defer(self, scenario, seconds_from_now, describe, fn):
        self.deferred.append({"scenario": scenario, "due": time.monotonic() + seconds_from_now,
                              "describe": describe, "fn": fn})

    def close(self):
        if self.hub:
            self.hub.close()
