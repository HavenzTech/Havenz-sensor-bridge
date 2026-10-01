"""
Scenario 8 - the backend restarts in the middle of a shift.

A deploy, a crash, a platform restart: the backend process dies with work in flight. People are
badging in, a remote unlock has been handed to the agent and the reader is in the middle of
opening. Afterwards every piece of work must be in a state somebody can read; nothing may have
been done twice; and everything that polls must be polling again without anyone touching it.
"""

import threading
import time
import uuid

from .. import config, readers, stack
from ..util import iso, wait_until

SIMULATED = ("The backend container is killed (not shut down) while 16 people are badging in and while one "
             "remote unlock is in flight on a reader made slow for the purpose. It is started again at once.")


def run(ctx, rec):
    account = ctx.account_for("shift-a-2")
    door = ctx.terminals[4]                               # Door 103-B
    panel_doors = [t for t in ctx.world["terminals"] if t["hasPanel"] and t["areaKey"] in ("101", "102", "103", "107")]
    people = [p for p in ctx.world["people"] if p["group"] in ("shift-a", "shift-b") and not p.get("deactivated")][18:34]
    taps = [{"person": p, "terminal": panel_doors[i % len(panel_doors)], "offset": 1.0 + i * 1.6}
            for i, p in enumerate(people)]
    since_iso = iso()
    before = ctx.open_counts()

    # A grant made just before the restart: its push to the readers is work in flight too.
    on_door_17 = {u["registration"] for u in readers.detail(17)["userList"]}
    newcomer = next(p for p in ctx.world["people"] if p["group"] == "office" and not p.get("deactivated")
                    and p["id"] not in on_door_17)
    status, _ = ctx.api.post("/api/admin/area-access",
                             {"userId": newcomer["id"], "areaId": ctx.areas["111"]["id"], "accessLevel": "standard"})
    rec.step("restart-begin", f"{len(taps)} people are badging in; {newcomer['name']} has just been given a new door; "
             "a remote unlock is on its way to a slow reader",
             expect={"doors": {"state": "welcome", "note": "greetings continue; a few may be late or missing across the restart"}})

    t0 = time.monotonic()

    def fire(tap):
        delay = tap["offset"] - (time.monotonic() - t0)
        if delay > 0:
            time.sleep(delay)
        tap["result"] = readers.scan(tap["terminal"]["number"], tap["person"]["id"])
    threads = [threading.Thread(target=fire, args=(t,), daemon=True) for t in taps]
    for t in threads:
        t.start()

    time.sleep(6)
    # The reader is made slow only now, so that it is this unlock - not some earlier routine
    # command - that is in the reader's hands when the backend dies. If the agent does not collect
    # it in time (it is busy with the shift), that unlock simply ends "failed" and another is sent:
    # the case being rehearsed needs one in flight.
    readers.set_mode(4, "ok", latency_ms=5000)
    in_flight = False
    for attempt in range(3):
        request_id = str(uuid.uuid4())
        box = {}

        def unlock(rid=request_id, out=box):
            try:
                out["answer"] = ctx.api.post(f"/api/amico/terminals/{door['id']}/open", {"requestId": rid},
                                             account=account, timeout=30)
            except Exception as e:  # noqa: BLE001 - the connection dying is the point
                out["error"] = f"{type(e).__name__}: {e}"
        unlocker = threading.Thread(target=unlock, daemon=True)
        unlocker.start()

        def taken(rid=request_id):
            return stack.psql_scalar(f"select count(*) from iot.agent_commands where intent_id = '{rid}' "
                                     "and status = 'leased'") == "1"
        in_flight, _ = wait_until(taken, 8, 0.15)
        if in_flight:
            break
        unlocker.join(15)
    time.sleep(0.5)                                        # the agent has the command; the reader is mid-open
    rec.note("the unlock was " + ("with the agent, the reader mid-open," if in_flight else "still waiting to be collected")
             + f" when the backend was killed (attempt {attempt + 1})")

    t_kill = time.monotonic()
    stack.docker("kill", config.CONTAINER["api"], check=False)
    rec.step("backend-killed", "The backend process is killed",
             expect={"walls": {"connected": False, "note": "walls should say they are reconnecting"},
                     "doors": {"connected": False}}, within=20)
    time.sleep(2)
    stack.docker("start", config.CONTAINER["api"], check=False)
    waited = stack.wait_for_api(180)
    downtime = time.monotonic() - t_kill
    rec.step("backend-back", f"The backend is serving again after {downtime:.0f}s",
             expect={"walls": {"connected": True, "live": True}, "doors": {"connected": True}}, within=60)
    rec.metric("backend down for", round(downtime, 1), "s")
    unlocker.join(40)
    for t in threads:
        t.join(60)
    readers.set_mode(4, "ok", latency_ms=0)
    try:
        ctx.connect_hub()
    except Exception as e:  # noqa: BLE001
        rec.note(f"the harness's own listener could not reconnect straight away: {e}")

    # ---- the in-flight unlock ---------------------------------------------------------------------
    if "error" in box:
        rec.note(f"the app's own request died with the backend: {box['error']}"[:200])
    else:
        rec.note(f"the app's request was answered before the backend died: {box.get('answer')}"[:200])
    rec.check("the unlock was in the reader's hands when the backend was killed (the case being rehearsed)",
              bool(in_flight) and "error" in box,
              "the agent had collected it and the reader was mid-open; the app's connection was cut" if in_flight and "error" in box
              else "the agent had not collected the unlock in time, so this run exercised the easier case; run the scenario again")

    def settled():
        row = stack.psql(f"select status, coalesce(message, '') from iot.remote_unlock_requests where request_id = '{request_id}'",
                         check=False)
        return row[0] if row and row[0][0] != "pending" else None
    ledger, waited = wait_until(settled, 90, 2.0)
    opened = ctx.open_counts()[4]["remote"] - before[4]["remote"]
    rec.check("the in-flight unlock ends in a defined state that matches what the door did",
              bool(ledger) and ((ledger[0] in ("opened", "unknown") and opened == 1) or (ledger[0] == "failed" and opened == 0)),
              f"recorded outcome '{ledger[0] if ledger else 'still pending'}' {waited:.0f}s after the restart; the reader opened "
              f"{opened} time(s)" + (f" - \"{ledger[1][:120]}\"" if ledger else ""))
    retry_status, retry = ctx.api.post(f"/api/amico/terminals/{door['id']}/open", {"requestId": request_id},
                                       account=account, timeout=40)
    time.sleep(6)
    opened_after = ctx.open_counts()[4]["remote"] - before[4]["remote"]
    rec.check("retrying the same request after the restart does not open the door again",
              opened_after == opened and isinstance(retry, dict) and retry.get("replayed") is True,
              f"retry: HTTP {retry_status}, outcome {retry.get('outcome') if isinstance(retry, dict) else retry}, "
              f"replayed {retry.get('replayed') if isinstance(retry, dict) else '-'}; openings still {opened_after}")

    # ---- the badge-ins ----------------------------------------------------------------------------
    after = ctx.open_counts()
    face_opens = sum(after[n]["face"] - before[n]["face"] for n in after)
    rec.check("every badge-in opened its door, restart or not", face_opens == len(taps),
              f"{face_opens} openings for {len(taps)} badge-ins")
    expected = {(t["terminal"]["id"], t["result"]["logId"]): t for t in taps if "result" in t}

    def arrived():
        rows = ctx.access_events(since_iso)
        seen = {(r["terminalId"], r["logId"]) for r in rows}
        return rows if all(k in seen for k in expected) else None
    rows, waited = wait_until(arrived, 150, 2.0)
    rows = rows or ctx.access_events(since_iso)
    counts = {}
    for r in rows:
        key = (r["terminalId"], r["logId"])
        if key in expected:
            counts[key] = counts.get(key, 0) + 1
    rec.check("every badge-in is in the access log exactly once, including those made while the backend was down",
              len(counts) == len(expected) and all(v == 1 for v in counts.values()),
              f"{len(counts)} of {len(expected)} recorded {waited:.0f}s after the restart; recorded twice: "
              f"{sum(1 for v in counts.values() if v > 1)}")

    # ---- things that poll are polling again ----------------------------------------------------------
    def agent_talking():
        _, hubs = ctx.api.get(f"/api/havenzhub/hubs?propertyId={ctx.property_id}", expect=200)
        me = next((h for h in hubs if h["id"] == ctx.world["agentHub"]["id"]), None)
        return me if me and me.get("agentState") == "online" else None
    hub, waited = wait_until(agent_talking, 90, 3.0)
    rec.check("the site agent is talking to the backend again without being touched", bool(hub),
              f"online {waited:.0f}s after the backend came back" if hub else "not online after 90 s")

    def polled():
        now = stack.psql_scalar("select count(*) from iot.agent_commands where type = 'GetAccessLogs' and status = 'done' "
                                f"and created_at >= '{since_iso}'::timestamptz + interval '{int(downtime) + 10} seconds'")
        return int(now or 0) >= 20
    ok, waited = wait_until(polled, 120, 3.0)
    rec.check("the reader log poller resumes on all 20 doors", bool(ok),
              f"20 or more reader-log reads completed within {waited:.0f}s of the restart" if ok
              else "fewer than 20 reader-log reads completed in 120 s")

    def feeding():
        status, body = ctx.feeder.tick()
        return status == 200
    ok, waited = wait_until(feeding, 60, 3.0)
    rec.check("sensor readings are being accepted again", bool(ok), "ingest answering 200" if ok else "ingest still failing")

    def pushed():
        return ctx.readers_holding(newcomer["id"], [17])
    held, waited = wait_until(pushed, 240, 3.0)
    rec.check("the door access granted just before the restart still reaches its reader",
              bool(held), f"on the reader {waited:.0f}s after the restart" if held
              else f"not on the reader {waited:.0f}s after the restart")
    stuck = stack.psql_scalar("select count(*) from iot.agent_commands where status in ('queued', 'leased') "
                              "and not_valid_after < now() - interval '30 seconds'")
    rec.check("no command is left in limbo", stuck == "0", f"{stuck} command(s) still queued or leased past their deadline")


def cleanup(ctx, rec):
    readers.set_mode(4, "ok", latency_ms=0)
    if not stack.container_running(config.CONTAINER["api"]):
        stack.docker("start", config.CONTAINER["api"], check=False)
        stack.wait_for_api(180)
