"""
Scenario 9 - one reader stops answering; another loses power.

The nasty failure is not a reader that is off. It is one that accepts the connection and then says
nothing, so every call to it costs a full timeout. Twenty doors share one agent: a single silent
reader must be set aside quickly, the other nineteen must not notice, and somebody looking at the
admin pages must be able to tell which door it is.
"""

import threading
import time
import uuid

from .. import readers
from ..util import iso, percentile, wait_until

SIMULATED = ("Reader 7 (Door 104-A) is made to accept connections and never answer. Remote unlocks are sent "
             "to it and to a healthy door at the same time while people badge in elsewhere. Later reader 13 "
             "(Door 106-A) has its power cut for 40 s (its address leaves the LAN) and comes back rebooted.")

HUNG = 7
HEALTHY = 3
POWERED = 13


def unlock(ctx, account, terminal_id, timeout=45):
    t0 = time.monotonic()
    status, body = ctx.api.post(f"/api/amico/terminals/{terminal_id}/open", {"requestId": str(uuid.uuid4())},
                                account=account, timeout=timeout)
    return status, (body if isinstance(body, dict) else {}), time.monotonic() - t0


def terminal_row(ctx, terminal_id):
    _, rows = ctx.api.get("/api/amico/terminals", expect=200)
    return next(t for t in rows if t["id"] == terminal_id), rows


def failing(row):
    return bool(row.get("lastErrorAt")) and (not row.get("lastOkAt") or row["lastErrorAt"] > row["lastOkAt"])


def run(ctx, rec):
    admin = ctx.admin("admin-3")                    # an administrator has access to every door
    hung_door, healthy_door = ctx.terminals[HUNG], ctx.terminals[HEALTHY]
    since_iso = iso()

    baseline = [unlock(ctx, admin, healthy_door["id"]) for _ in range(3)]
    base_ms = percentile([b[2] * 1000 for b in baseline if b[0] == 200], 50)
    rec.metric("remote unlock on a healthy door, before", round(base_ms, 0) if base_ms else None, "ms")

    readers.set_mode(HUNG, "hang")
    rec.step("reader-hangs", f"The reader at {hung_door['name']} stops answering (it still accepts connections)",
             expect={"doors": {"note": f"{hung_door['name']} panel keeps showing idle; other panels greet as normal"}})

    # ---- three calls to the silent reader, while the rest of the plant carries on -------------------
    slow, during = [], []

    def hit_hung():
        slow.append(unlock(ctx, admin, hung_door["id"]))

    people = [p for p in ctx.world["people"] if p["group"] == "shift-b" and not p.get("deactivated")][:8]
    panel_doors = [t for t in ctx.world["terminals"] if t["hasPanel"] and t["number"] != HUNG
                   and t["areaKey"] in ("101", "102", "103", "107")]
    welcome_lat = []
    for round_no in range(3):
        worker = threading.Thread(target=hit_hung, daemon=True)
        worker.start()
        time.sleep(1.0)                                              # the agent is now stuck on the silent reader
        during.append(unlock(ctx, admin, healthy_door["id"]))
        for i in range(2):
            person = people[(round_no * 2 + i) % len(people)]
            door = panel_doors[(round_no * 2 + i) % len(panel_doors)]
            mono = time.monotonic()
            result = readers.scan(door["number"], person["id"])
            got, waited = wait_until(lambda: [w for w in ctx.welcomes(mono) if w["userId"] == person["id"]
                                              and w["terminalId"] == door["id"]], 8, 0.05)
            if got and result["opened"]:
                welcome_lat.append((got[0]["mono"] - mono) * 1000)
        worker.join(60)

    rec.check("a call to the silent reader ends as 'unknown' or 'failed', never as a success",
              len(slow) == 3 and all(s[0] in (409, 502) for s in slow),
              f"three unlocks at {hung_door['name']}: {[(s[0], s[1].get('outcome'), f'{s[2]:.0f}s') for s in slow]}")
    during_ms = [d[2] * 1000 for d in during if d[0] == 200]
    rec.metric("remote unlock on a healthy door, while another reader hangs", round(percentile(during_ms, 50), 0)
               if during_ms else None, "ms")
    rec.check("the other doors do not wait behind the silent one",
              len(during_ms) == 3 and max(during_ms) < 3000, finding="F-AGENT-HEAD-OF-LINE", detail=
              f"healthy door opened in {[round(d) for d in during_ms]} ms while {hung_door['name']} was hanging "
              f"(before: {base_ms:.0f} ms)" if during_ms else f"healthy door answers: {[(d[0], d[1].get('outcome')) for d in during]}")
    rec.check("badge-ins at other doors are greeted as fast as ever",
              len(welcome_lat) == 6 and max(welcome_lat) < 1500,
              f"{len(welcome_lat)} of 6 greeted, slowest {max(welcome_lat):.0f} ms (the doors themselves opened at once; a "
              "greeting waits when the backend has to ask the agent whose face it was)" if welcome_lat
              else "no greetings measured", finding="F-AGENT-HEAD-OF-LINE")

    # ---- the breaker ------------------------------------------------------------------------------
    status, body, took = unlock(ctx, admin, hung_door["id"])
    rec.step("reader-isolated", f"After three failures the agent sets {hung_door['name']} aside",
             expect={"doors": {}})
    rec.check("after three failures the agent stops waiting on that reader and says so",
              status == 502 and took < 3 and "unreachable" in str(body.get("message")).lower(),
              f"fourth unlock: HTTP {status} in {took * 1000:.0f} ms - \"{str(body.get('message'))[:150]}\"")
    rec.metric("call to a reader the agent has set aside", round(took * 1000, 0), "ms", "instead of a 10 s timeout")

    row, rows = terminal_row(ctx, hung_door["id"])
    others_failing = [t["name"] for t in rows if t["id"] != hung_door["id"] and failing(t)]
    rec.check("the admin's door list flags that door, and only that door",
              failing(row) and not others_failing,
              f"{hung_door['name']}: lastError \"{str(row.get('lastError'))[:90]}\"; other doors flagged: {others_failing or 'none'}")
    _, hubs = ctx.api.get(f"/api/havenzhub/hubs?propertyId={ctx.property_id}", expect=200)
    hub = next(h for h in hubs if h["id"] == ctx.world["agentHub"]["id"])
    last = hub.get("lastFailure") or {}
    rec.check("the site agent's card names the door that is failing",
              last.get("terminalName") == hung_door["name"] and hub.get("recentFailures", 0) >= 3,
              f"recent failures {hub.get('recentFailures')}; last failure at '{last.get('terminalName')}': "
              f"\"{str(last.get('error'))[:90]}\"")
    health = ctx.health()
    mentions = [r for r in health.get("reasons") or [] if "door" in r.lower() or "reader" in r.lower()
                or hung_door["name"].lower() in r.lower()]
    rec.check("the property's health page says a door reader is not answering",
              health.get("state") != "healthy" and bool(mentions),
              f"health state '{health.get('state')}', reasons {health.get('reasons')}", finding="F-HEALTH-SILENT-ON-READERS")

    # ---- recovery ----------------------------------------------------------------------------------
    readers.set_mode(HUNG, "ok")
    t0 = time.monotonic()
    rec.step("reader-recovers", f"The reader at {hung_door['name']} answers again")

    def works():
        s, b, took = unlock(ctx, admin, hung_door["id"])
        return (s, b, took) if s == 200 else None
    ok, waited = wait_until(works, 150, 5.0)
    rec.check("once it answers again the door comes back by itself", bool(ok),
              f"remote unlock works {time.monotonic() - t0:.0f}s after the reader recovered (the agent retries after 60 s)"
              if ok else f"still refused {waited:.0f}s after the reader recovered")
    rec.metric("reader recovers -> door usable again", round(time.monotonic() - t0, 0) if ok else None, "s")

    # ---- power cut on another reader -------------------------------------------------------------------
    door = ctx.terminals[POWERED]
    person = ctx.people["it-1"]
    logins_before = readers.detail(POWERED)["logins"]
    users_before = readers.detail(POWERED)["users"]
    readers.power(POWERED, False)
    rec.step("reader-power-off", f"The reader at {door['name']} loses power", expect={"doors": {}})
    status, body, took = unlock(ctx, admin, door["id"])
    rec.check("a remote unlock to a reader with no power fails cleanly",
              status in (409, 502) and body.get("outcome") in ("failed", "unknown"),
              f"HTTP {status}, outcome {body.get('outcome')} after {took:.1f}s: \"{str(body.get('message'))[:120]}\"")
    time.sleep(40)
    readers.power(POWERED, True)
    t0 = time.monotonic()
    rec.step("reader-power-on", f"The reader at {door['name']} has power again and has rebooted")

    ok, waited = wait_until(lambda: (lambda r: r if r[0] == 200 else None)(unlock(ctx, admin, door["id"])), 150, 5.0)
    detail = readers.detail(POWERED)
    rec.check("after a power cycle the agent signs in to the reader again and it works",
              bool(ok) and detail["logins"] > logins_before,
              f"remote unlock works {time.monotonic() - t0:.0f}s after power-on; the agent signed in again "
              f"({detail['logins'] - logins_before} new session(s))" if ok else f"still failing {waited:.0f}s after power-on")
    rec.check("the reader kept its people through the power cut", detail["users"] == users_before,
              f"{detail['users']} people on the reader before and after")
    mono = time.monotonic()
    result = readers.scan(POWERED, person["id"])
    row, waited = wait_until(lambda: next((r for r in ctx.access_events(since_iso, door["id"])
                                           if r["logId"] == result["logId"]), None), 45, 1.0)
    rec.check("a badge-in after the power cycle opens the door and is recorded", result["opened"] and bool(row),
              f"reader decision event {result['event']}; in the access log {waited:.0f}s later" if row
              else f"reader decision event {result['event']}; not in the access log after {waited:.0f}s")


def cleanup(ctx, rec):
    readers.set_mode(HUNG, "ok")
    readers.power(POWERED, True)
