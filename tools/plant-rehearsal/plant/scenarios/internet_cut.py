"""
Scenario 7 - the internet goes down between the site and the backend.

The plant LAN stays up; only the agent's route to the backend is cut, for three minutes, while
people keep badging in. Doors must keep opening. Every event must be kept on the agent's disk and
arrive exactly once when the link returns - and a door panel must not flash "Welcome" for someone
who walked through minutes ago.
"""

import random
import threading
import time

from .. import readers, stack
from ..util import iso, wait_until

SIMULATED = ("The agent is disconnected from the uplink network (the readers stay reachable) for 180 s. "
             "Twelve people badge in at panel doors during the first 100 s of the cut, so every event is more "
             "than a minute old when the link returns.")

CUT_SECONDS = 180
TAP_WINDOW = 100
FRESHNESS_LIMIT = 60          # the backend's rule: an event older than this is recorded, not greeted


def run(ctx, rec):
    rng = random.Random(7)
    cut = int(ctx.options.get("cutSeconds") or CUT_SECONDS)
    panel_doors = [t for t in ctx.world["terminals"] if t["hasPanel"] and t["areaKey"] in ("101", "102", "103", "107")]
    people = [p for p in ctx.world["people"] if p["group"] in ("shift-a", "shift-b") and not p.get("deactivated")][6:18]
    taps = [{"person": p, "terminal": panel_doors[i % len(panel_doors)], "offset": rng.uniform(5, TAP_WINDOW)}
            for i, p in enumerate(people)]
    taps.sort(key=lambda t: t["offset"])
    since_iso = iso()
    before = ctx.open_counts()
    notify_before = {r["number"]: r["notifyOk"] for r in readers.state()["readers"]}

    stack.agent_uplink(False)
    t0 = time.monotonic()
    rec.step("internet-cut", f"The site's internet link drops for {cut}s; {len(taps)} people badge in during the outage",
             expect={"walls": {"note": "walls keep their own connection in this rehearsal; live data continues"},
                     "doors": {"state": "idle", "note": "NO welcome may appear for these badge-ins, now or later"}},
             within=cut)

    def fire(tap):
        delay = tap["offset"] - (time.monotonic() - t0)
        if delay > 0:
            time.sleep(delay)
        tap["sentMono"] = time.monotonic()
        tap["result"] = readers.scan(tap["terminal"]["number"], tap["person"]["id"])
    threads = [threading.Thread(target=fire, args=(t,), daemon=True) for t in taps]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TAP_WINDOW + 30)

    # ---- during the cut ---------------------------------------------------------------------------
    after = ctx.open_counts()
    opened = sum(after[n]["face"] - before[n]["face"] for n in after)
    rec.check("doors keep opening for faces while the link is down",
              opened == len(taps) and all(t["result"]["opened"] for t in taps),
              f"{opened} openings for {len(taps)} badge-ins, counted at the readers")
    time.sleep(3)
    notify_after = {r["number"]: r["notifyOk"] for r in readers.state()["readers"]}
    accepted = sum(notify_after[n] - notify_before[n] for n in notify_after)
    pending = ctx.agent_queue_on_disk()
    dao = [p for p in pending if p.get("kind") == "dao"]
    rec.check("the agent takes custody of every event on the LAN and keeps it on disk",
              accepted >= len(taps) and len(dao) >= len(taps),
              f"readers were answered for {accepted} events; the agent's journal on disk holds {len(dao)} undelivered")
    rows = [r for r in ctx.access_events(since_iso) if (r["terminalId"], r["logId"]) in
            {(t["terminal"]["id"], t["result"]["logId"]) for t in taps}]
    rec.check("nothing reaches the backend while the link is down", len(rows) == 0,
              f"{len(rows)} of these events in the access log during the cut")
    heard = [w for w in ctx.welcomes(t0) if w["userId"] in {t["person"]["id"] for t in taps}]
    rec.check("no panel greets anyone during the cut", len(heard) == 0, f"{len(heard)} welcome broadcasts during the cut")

    # ---- the link returns -------------------------------------------------------------------------
    remaining = cut - (time.monotonic() - t0)
    if remaining > 0:
        time.sleep(remaining)
    ages = [time.monotonic() - t["sentMono"] for t in taps]
    stack.agent_uplink(True)
    t_back = time.monotonic()
    rec.step("internet-back", f"The link returns after {time.monotonic() - t0:.0f}s; queued events are delivered "
             f"(the youngest is {min(ages):.0f}s old)",
             expect={"doors": {"state": "idle", "note": "still no welcome - these people walked through over a minute ago"}},
             within=90)

    expected = {(t["terminal"]["id"], t["result"]["logId"]): t for t in taps}

    def arrived():
        rows = ctx.access_events(since_iso)
        seen = {(r["terminalId"], r["logId"]) for r in rows}
        return rows if all(k in seen for k in expected) else None
    rows, waited = wait_until(arrived, 180, 2.0)
    rows = rows or ctx.access_events(since_iso)
    counts = {}
    for r in rows:
        key = (r["terminalId"], r["logId"])
        if key in expected:
            counts[key] = counts.get(key, 0) + 1
    rec.check("every event arrives after the link returns, exactly once",
              len(counts) == len(expected) and all(v == 1 for v in counts.values()),
              f"{len(counts)} of {len(expected)} arrived within {waited:.0f}s; recorded twice: "
              f"{sum(1 for v in counts.values() if v > 1)}")
    rec.metric("link back -> all queued events in the access log", round(waited, 1) if len(counts) == len(expected) else None, "s",
               "includes the agent's retry back-off; it does not know the link is back until its next attempt")

    by_key = {(r["terminalId"], r["logId"]): r for r in rows}
    wrong_time = [k for k, t in expected.items() if k in by_key and abs(by_key[k]["occurred"] - t["result"]["t"]) > 5]
    late = [by_key[k]["recorded"] - t["result"]["t"] for k, t in expected.items() if k in by_key]
    rec.check("each late event keeps the time the person actually badged in",
              not wrong_time and bool(late),
              f"{len(expected) - len(wrong_time)} of {len(expected)} carry the badge-in time; they were written "
              f"{min(late):.0f}-{max(late):.0f}s later" if late else "no rows to compare")
    order_ok = True
    for tid in {t["terminal"]["id"] for t in taps}:
        seq = [r["logId"] for r in rows if r["terminalId"] == tid and (tid, r["logId"]) in expected]
        order_ok = order_ok and seq == sorted(seq)
    rec.check("each door's backlog is delivered in the order it happened", order_ok,
              "in order for every door" if order_ok else "at least one door's backlog arrived out of order")
    named = [k for k, t in expected.items() if k in by_key and by_key[k]["userId"] == t["person"]["id"]]
    rec.check("late events are still attributed to the right people", len(named) == len(expected),
              f"{len(named)} of {len(expected)} name the person")

    time.sleep(8)
    heard = [w for w in ctx.welcomes(t0) if w["userId"] in {t["person"]["id"] for t in taps}]
    rec.check("no stale welcome is flashed when the backlog arrives",
              len(heard) == 0, f"{len(heard)} welcome broadcasts for events {min(ages):.0f}-{max(ages):.0f}s old "
              f"(the rule: nothing older than {FRESHNESS_LIMIT}s is greeted)"
              + (f": {[(w['userName'], w['eventType']) for w in heard[:4]]}" if heard else ""))

    def drained():
        return not [p for p in ctx.agent_queue_on_disk() if p.get("kind") == "dao"]
    empty, waited = wait_until(drained, 60, 2.0)
    rec.check("the agent's queue is empty again", bool(empty), "0 events waiting on disk" if empty
              else f"{len(ctx.agent_queue_on_disk())} still waiting")

    # ---- and a fresh badge-in is greeted as normal ----------------------------------------------------
    person = ctx.people["shift-b-2"]
    door = ctx.terminals[2]
    mono = time.monotonic()
    result = readers.scan(2, person["id"])
    rec.step("after-cut-tap", f"{person['name']} badges in at {door['name']} once the link is back",
             expect={"doors": {"state": "welcome", "person": person["name"].split()[0], "only": [f"{door['name']} panel"]}},
             within=10)
    got, waited = wait_until(lambda: [w for w in ctx.welcomes(mono) if w["userId"] == person["id"]], 15, 0.2)
    rec.check("a badge-in after the link returns is greeted normally", bool(got) and result["opened"],
              f"welcome broadcast {waited * 1000:.0f} ms after the tap" if got else "no welcome within 15 s")
    rec.note("While the link was down the backend marked the site agent offline and, past the alert threshold, "
             "raised and then resolved the 'site agent offline' alert - that path is asserted in its own scenario.")


def cleanup(ctx, rec):
    stack.agent_uplink(True)
