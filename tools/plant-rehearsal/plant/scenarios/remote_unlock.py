"""
Scenario 4 - remote unlock.

Someone opens a door from the mobile app. Phones retry: the same request can arrive five times.
The door must open once. And when the request's answer is lost on the way back - the agent opens
the door but cannot say so - the outcome must be a defined one the app can show ("check the
door"), and nothing may ever send the unlock again.
"""

import threading
import time
import uuid

from .. import config, readers, stack
from ..util import iso, percentile, wait_until

SIMULATED = ("A shift worker signed in as themselves, opening a corridor door through the same API the mobile app "
             "uses: one request repeated five times in a row, one repeated five times at the same instant, and one "
             "whose result cannot be reported because the agent's uplink drops while the reader is opening.")


def open_door(ctx, account, terminal_id, request_id, timeout=40):
    t0 = time.monotonic()
    status, body = ctx.api.post(f"/api/amico/terminals/{terminal_id}/open", {"requestId": request_id},
                                account=account, timeout=timeout)
    return status, body if isinstance(body, dict) else {"raw": body}, time.monotonic() - t0


def remote_rows(ctx, since_iso, terminal_id):
    return [r for r in ctx.access_events(since_iso, terminal_id) if r["type"] in ("RemoteOpen", "WebInterface")]


def run(ctx, rec):
    person = ctx.people["shift-a-2"]
    account = ctx.account_for("shift-a-2")
    door = ctx.terminals[3]                              # Door 103-A, corridor, has a panel
    panel = f"{door['name']} panel"

    # ---- 1. the same request, five times in a row ------------------------------------------------
    since_iso = iso()
    mono = time.monotonic()
    before = ctx.open_counts()[3]
    request_id = str(uuid.uuid4())
    rec.step("remote-unlock", f"{person['name']} opens {door['name']} from the app; the phone retries the same request 5 times",
             expect={"doors": {"state": "welcome", "badge": "Opened Remotely", "only": [panel]}}, within=10)
    answers = [open_door(ctx, account, door["id"], request_id) for _ in range(5)]
    after = ctx.open_counts()[3]
    opened = after["remote"] - before["remote"]
    first = answers[0]
    rec.check("the first request opens the door", first[0] == 200 and first[1].get("outcome") == "opened"
              and first[1].get("replayed") is False,
              f"HTTP {first[0]}, outcome {first[1].get('outcome')}, in {first[2] * 1000:.0f} ms")
    rec.metric("remote unlock: tap -> door open (answer back at the app)", round(first[2] * 1000, 0), "ms")
    rec.check("five identical requests open the door once (counted at the reader)", opened == 1,
              f"{opened} opening(s); answers: {[(a[0], a[1].get('outcome'), a[1].get('replayed')) for a in answers]}")
    rec.check("every retry gets the first answer back, marked as a replay",
              all(a[0] == 200 and a[1].get("replayed") is True and a[1].get("requestId") == request_id for a in answers[1:]),
              f"retries: {[(a[0], a[1].get('replayed')) for a in answers[1:]]}")

    time.sleep(35)        # long enough for the reader's own log of the opening to have been collected
    rows = remote_rows(ctx, since_iso, door["id"])
    named = [r for r in rows if r["userId"] == person["id"]]
    rec.check("the access log names who opened it", len(named) == 1,
              f"{len(named)} row(s) naming {person['name']}")
    rec.check("one remote opening is one line in the access log", len(rows) == 1,
              f"{len(rows)} lines for one opening: {[(r['type'], 'named' if r['userId'] else 'anonymous') for r in rows]}",
              finding="F-REMOTE-OPEN-TWO-ROWS")
    greetings = [w for w in ctx.welcomes(mono) if w["terminalId"] == door["id"]]
    rec.check("the panel is told once", len(greetings) == 1,
              f"{len(greetings)} broadcast(s) for one opening: {[w['eventType'] for w in greetings]}",
              finding="F-REMOTE-OPEN-TWO-ROWS")

    # ---- 2. the same request, five at once ---------------------------------------------------------
    before = ctx.open_counts()[3]
    request_id = str(uuid.uuid4())
    results = []
    threads = [threading.Thread(target=lambda: results.append(open_door(ctx, account, door["id"], request_id)))
               for _ in range(5)]
    rec.step("remote-unlock-burst", "Five copies of one request arrive at the same instant",
             expect={"doors": {"state": "welcome", "badge": "Opened Remotely", "only": [panel]}}, within=10)
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    opened = ctx.open_counts()[3]["remote"] - before["remote"]
    rec.check("five simultaneous copies open the door once", opened == 1 and len(results) == 5
              and all(r[0] == 200 for r in results),
              f"{opened} opening(s); answers {sorted((r[0], r[1].get('outcome'), r[1].get('replayed')) for r in results)}")

    # ---- how long a remote unlock takes, over a spread of moments ---------------------------------------
    clock = ctx.vm_clock_offset()
    timings, door_ms, report_lag = [], [], []
    for _ in range(8):
        sent_wall = time.time()
        answer = open_door(ctx, account, door["id"], str(uuid.uuid4()))
        if answer[0] == 200:
            timings.append(answer[2] * 1000)
            last_open = readers.detail(3)["openList"][-1]
            opened_ms = (last_open["t"] - (sent_wall + clock)) * 1000
            if last_open["cause"] == "remote" and 0 <= opened_ms <= answer[2] * 1000 + 50:
                door_ms.append(opened_ms)
                report_lag.append(answer[2] * 1000 - opened_ms)
        time.sleep(4)
    rec.check("eight remote unlocks in a row all open the door", len(timings) == 8,
              f"{len(timings)} of 8 opened; app answered in {[round(t) for t in timings]} ms")
    rec.metric("remote unlock: tap -> the door actually opens, typical", round(percentile(door_ms, 50)) if door_ms else None,
               "ms", "measured at the reader")
    rec.metric("remote unlock: tap -> the door actually opens, slowest of 8", round(max(door_ms)) if door_ms else None, "ms",
               "a remote unlock queues behind whatever the agent is already doing for that door")
    rec.metric("remote unlock: tap -> the app has its answer, typical", round(percentile(timings, 50)) if timings else None, "ms")
    rec.metric("remote unlock: tap -> the app has its answer, slowest of 8", round(max(timings)) if timings else None, "ms")
    store = config.stack_dir() / "agent-data" / "executed.json"
    size_kb = store.stat().st_size / 1024 if store.exists() else 0
    lag = percentile(report_lag, 50) if report_lag else None
    rec.check("the app hears within half a second of the door opening",
              lag is not None and lag <= 500,
              f"the door opens {percentile(door_ms, 50):.0f} ms after the tap, the app's answer arrives {lag:.0f} ms after "
              f"that (typical of {len(report_lag)}); the agent's record of finished commands is {size_kb:.0f} KB and is "
              "rewritten in full after every command" if lag is not None else "could not be measured",
              finding="F-AGENT-STORE-REWRITE")

    # ---- 3. a request whose answer is lost -----------------------------------------------------------
    before = ctx.open_counts()[3]
    request_id = str(uuid.uuid4())
    readers.set_mode(3, "ok", latency_ms=4000)           # the reader takes four seconds to act
    box = {}
    worker = threading.Thread(target=lambda: box.update(answer=open_door(ctx, account, door["id"], request_id, timeout=60)))
    rec.step("remote-unlock-lost", "A remote unlock is sent; while the reader is opening, the agent's uplink drops, "
             "so the result cannot be reported", expect={"doors": {"note": "the door does open; the app must say 'check the door'"}})
    worker.start()
    time.sleep(1.5)
    stack.agent_uplink(False)
    worker.join(70)
    answer = box.get("answer", (0, {}, 0))
    time.sleep(1)
    opened = ctx.open_counts()[3]["remote"] - before["remote"]
    rec.check("the door did open (the reader acted before the link dropped)", opened == 1, f"{opened} opening(s) at the reader")
    rec.check("the app is told the outcome is unknown, not that it failed",
              answer[0] == 409 and answer[1].get("outcome") == "unknown",
              f"HTTP {answer[0]}, outcome {answer[1].get('outcome')}, after {answer[2]:.0f}s: \"{str(answer[1].get('message'))[:140]}\"")
    retry = open_door(ctx, account, door["id"], request_id)
    stack.agent_uplink(True)
    readers.set_mode(3, "ok", latency_ms=0)
    rec.check("retrying that request is answered from the record and does not reach the door",
              retry[1].get("replayed") is True and retry[1].get("outcome") in ("unknown", "opened"),
              f"retry: HTTP {retry[0]}, outcome {retry[1].get('outcome')}, replayed {retry[1].get('replayed')}")

    def agent_back():
        status = ctx.agent_status()
        return status if status and status.get("seconds_since_heartbeat") is not None and status["seconds_since_heartbeat"] < 20 \
            and not status.get("last_error") else None
    back, waited = wait_until(agent_back, 120, 2.0)
    rec.metric("uplink restored -> agent talking to the backend again", round(waited, 1) if back else None, "s")
    time.sleep(30)
    final = ctx.open_counts()[3]["remote"] - before["remote"]
    ledger = stack.psql(f"select status, http_status from iot.remote_unlock_requests where request_id = '{request_id}'")
    rec.check("after the link is back the unlock is never sent again", final == 1,
              f"{final} opening(s) for that request 30 s after the agent reconnected; recorded outcome: {ledger[0][0] if ledger else '?'}")


def cleanup(ctx, rec):
    stack.agent_uplink(True)
    readers.set_mode(3, "ok", latency_ms=0)
