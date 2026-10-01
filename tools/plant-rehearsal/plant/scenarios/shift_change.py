"""
Scenario 1 - shift change.

Forty different people present their faces across all twenty doors inside two minutes, bunched the
way an arriving shift is. The reader at each door decides and opens on its own; the event goes to
the site agent on the LAN, the agent passes it to the backend, the backend writes the access log
and tells the door's panel to greet the person.
"""

import random
import threading
import time

from .. import readers
from ..util import iso, percentile, wait_until

SIMULATED = ("40 people, each tapping once at a door they are allowed through, spread over all 20 doors "
             "within 110 seconds (bursts of several doors at the same instant). Readers decide on the device; "
             "events travel reader -> site agent -> backend -> door panel.")

WINDOW_SECONDS = 110
WELCOME_BAR_SECONDS = 1.5          # the bench bar: tap to greeting on the panel


def plan_taps(ctx, rng):
    """40 distinct people over 20 doors: every door gets at least one tap, nobody taps twice."""
    people = [p for p in ctx.world["people"] if not p.get("deactivated") and p.get("part") != "leaves the company mid-run"]
    by_area = {}
    for p in people:
        for a in p["areas"]:
            by_area.setdefault(a, []).append(p)
    used, taps = set(), []

    def pick(area_key, prefer):
        pool = [p for p in by_area.get(area_key, []) if p["id"] not in used]
        pool.sort(key=lambda p: (0 if p["group"] in prefer else 1, p["key"]))
        if not pool:
            return None
        used.add(pool[0]["id"])
        return pool[0]

    # First pass: one person at every door, the scarcest doors first (server rooms, communication).
    scarcity = sorted(ctx.world["terminals"], key=lambda t: len(by_area.get(t["areaKey"], [])))
    for t in scarcity:
        p = pick(t["areaKey"], ("it", "maintenance", "admin") if len(by_area.get(t["areaKey"], [])) < 20 else ("shift-b",))
        if p:
            taps.append({"person": p, "terminal": t})
    # Second pass: the arriving shift through the common doors until there are forty taps.
    common = [t for t in ctx.world["terminals"] if len(by_area.get(t["areaKey"], [])) >= 20]
    i = 0
    while len(taps) < 40 and i < 400:
        t = common[i % len(common)]
        p = pick(t["areaKey"], ("shift-b", "shift-a"))
        if p:
            taps.append({"person": p, "terminal": t})
        i += 1
    # Arrival times: three waves, so several doors fire in the same second.
    for tap in taps:
        wave = rng.choice([8, 45, 85])
        tap["offset"] = max(0.0, min(WINDOW_SECONDS, rng.gauss(wave, 9)))
    taps.sort(key=lambda x: x["offset"])
    return taps


def run(ctx, rec):
    rng = random.Random(107)
    taps = plan_taps(ctx, rng)
    doors_used = {t["terminal"]["number"] for t in taps}
    panel_doors = {t["number"] for t in ctx.world["terminals"] if t["hasPanel"]}
    before = ctx.open_counts()
    since_iso = iso()
    since_mono = time.monotonic()
    clock = ctx.vm_clock_offset()

    rec.step("shift-change-begin",
             f"{len(taps)} people start badging in across {len(doors_used)} doors over {WINDOW_SECONDS}s",
             expect={"walls": {"banner": "none"},
                     "doors": {"state": "welcome", "note": "each panel door greets by first name as its taps arrive"}},
             within=WINDOW_SECONDS + 20)

    def fire(tap):
        delay = tap["offset"] - (time.monotonic() - since_mono)
        if delay > 0:
            time.sleep(delay)
        tap["sentMono"] = time.monotonic()
        tap["result"] = readers.scan(tap["terminal"]["number"], tap["person"]["id"])
        tap["doneMono"] = time.monotonic()

    threads = [threading.Thread(target=fire, args=(tap,), daemon=True) for tap in taps]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WINDOW_SECONDS + 30)
    elapsed = time.monotonic() - since_mono
    rec.step("shift-change-last-tap", f"last of {len(taps)} taps made {elapsed:.0f}s after the first",
             expect={"doors": {"state": "idle", "note": "panels return to idle about 5 s after their last greeting"}})

    # ---- at the device: every allowed tap opened its door, once ---------------------------------
    granted = [t for t in taps if t.get("result", {}).get("opened")]
    after = ctx.open_counts()
    wrong = []
    for number in sorted(doors_used):
        expected = sum(1 for t in taps if t["terminal"]["number"] == number)
        opened = after[number]["face"] - before[number]["face"]
        extra = after[number]["remote"] - before[number]["remote"]
        if opened != expected or extra:
            wrong.append((number, expected, opened, extra))
    rec.check("every allowed tap opened its door exactly once (counted at the readers)",
              len(granted) == len(taps) and not wrong,
              f"{len(granted)} of {len(taps)} taps granted; {len(doors_used)} doors, "
              f"{sum(after[n]['face'] - before[n]['face'] for n in doors_used)} openings"
              + (f"; doors with a wrong count (door, expected, opened, remote): {wrong}" if wrong else ""))

    # ---- in the access log: once each, attributed, in order --------------------------------------
    expected_keys = {(t["terminal"]["id"], t["result"]["logId"]): t for t in taps if "result" in t}

    def all_recorded():
        rows = ctx.access_events(since_iso)
        seen = {(r["terminalId"], r["logId"]) for r in rows}
        return rows if all(k in seen for k in expected_keys) else None
    rows, waited = wait_until(all_recorded, 60, 1.0)
    rows = rows or ctx.access_events(since_iso)
    counts = {}
    for r in rows:
        counts[(r["terminalId"], r["logId"])] = counts.get((r["terminalId"], r["logId"]), 0) + 1
    missing = [k for k in expected_keys if counts.get(k, 0) == 0]
    doubled = [k for k in expected_keys if counts.get(k, 0) > 1]
    rec.check("every tap is in the access log exactly once",
              not missing and not doubled,
              f"{len(expected_keys) - len(missing)} of {len(expected_keys)} recorded, {len(doubled)} recorded twice"
              + (f"; missing after {waited:.0f}s: {len(missing)}" if missing else ""))

    by_key = {(r["terminalId"], r["logId"]): r for r in rows}
    misattributed = [k for k, t in expected_keys.items()
                     if k in by_key and (by_key[k]["userId"] != t["person"]["id"] or by_key[k]["type"] != "Granted")]
    rec.check("each row names the person who tapped, as Granted", not misattributed,
              f"{len(expected_keys) - len(misattributed)} of {len(expected_keys)} rows carry the right person"
              + (f"; wrong: {[(by_key[k]['type'], by_key[k]['userId']) for k in misattributed[:4]]}" if misattributed else ""))

    out_of_order = []
    for number in sorted(doors_used):
        tid = ctx.terminals[number]["id"]
        seq = [r["logId"] for r in rows if r["terminalId"] == tid and (tid, r["logId"]) in expected_keys]
        if seq != sorted(seq):
            out_of_order.append((number, seq))
    rec.check("each door's events were recorded in the order the door produced them", not out_of_order,
              f"{len(doors_used)} doors in order" if not out_of_order else f"out of order: {out_of_order[:3]}")

    record_lat = [(by_key[k]["recorded"] - t["result"]["t"]) * 1000 for k, t in expected_keys.items() if k in by_key]
    rec.metric("tap -> row in the access log, p50", round(percentile(record_lat, 50), 0) if record_lat else None, "ms")
    rec.metric("tap -> row in the access log, p95", round(percentile(record_lat, 95), 0) if record_lat else None, "ms")
    rec.metric("tap -> row in the access log, slowest", round(max(record_lat), 0) if record_lat else None, "ms")

    # ---- to the panels: one greeting per tap, promptly ------------------------------------------
    time.sleep(2)
    welcomes = ctx.welcomes(since_mono)
    panel_taps = [t for t in taps if t["terminal"]["number"] in panel_doors and "result" in t]
    lat, no_welcome, repeated = [], [], []
    for t in taps:
        if "result" not in t:
            continue
        mine = [w for w in welcomes if w["terminalId"] == t["terminal"]["id"] and w["userId"] == t["person"]["id"]
                and w["eventType"] == "Granted"]
        if t in panel_taps:
            if not mine:
                no_welcome.append(t["person"]["name"])
            else:
                lat.append((mine[0]["mono"] - t["sentMono"]) * 1000)
        if len(mine) > 1:
            repeated.append((t["terminal"]["name"], t["person"]["name"], len(mine)))
    rec.check("a welcome was broadcast for every tap at a door with a panel",
              not no_welcome, f"{len(panel_taps) - len(no_welcome)} of {len(panel_taps)} panel-door taps were greeted"
              + (f"; none for: {no_welcome[:5]}" if no_welcome else ""))
    rec.check("no tap was greeted twice", not repeated,
              "one welcome per tap" if not repeated else f"repeated greetings: {repeated[:5]}")
    first_at_door, later_at_door, seen_doors = [], [], set()
    for t in sorted((t for t in taps if "sentMono" in t), key=lambda t: t["sentMono"]):
        mine = [w for w in welcomes if w["terminalId"] == t["terminal"]["id"] and w["userId"] == t["person"]["id"]]
        if not mine:
            continue
        ms = (mine[0]["mono"] - t["sentMono"]) * 1000
        (later_at_door if t["terminal"]["id"] in seen_doors else first_at_door).append(ms)
        seen_doors.add(t["terminal"]["id"])
    p50, p95 = percentile(lat, 50), percentile(lat, 95)
    rec.metric("tap -> welcome broadcast for the panel, p50", round(p50, 0) if p50 is not None else None, "ms")
    rec.metric("tap -> welcome broadcast for the panel, p95", round(p95, 0) if p95 is not None else None, "ms")
    rec.metric("tap -> welcome broadcast for the panel, slowest", round(max(lat), 0) if lat else None, "ms")
    rec.metric("tap -> welcome broadcast, first tap at a door in this run (typical)",
               round(percentile(first_at_door, 50)) if first_at_door else None, "ms", f"{len(first_at_door)} taps")
    rec.metric("tap -> welcome broadcast, later taps at the same door (typical)",
               round(percentile(later_at_door, 50)) if later_at_door else None, "ms", f"{len(later_at_door)} taps")
    rec.check(f"95% of greetings are broadcast within {WELCOME_BAR_SECONDS}s of the tap",
              p95 is not None and p95 <= WELCOME_BAR_SECONDS * 1000,
              (f"p95 {p95:.0f} ms, slowest {max(lat):.0f} ms over {len(lat)} panel greetings; the first tap at a door "
               f"typically takes {percentile(first_at_door, 50):.0f} ms, later taps {percentile(later_at_door, 50):.0f} ms")
              if lat and first_at_door and later_at_door else "no greetings measured", finding="F-FIRST-TAP-SLOW")

    decide = [(t["doneMono"] - t["sentMono"]) * 1000 for t in taps if "doneMono" in t]
    rec.metric("tap -> door open, p50", round(percentile(decide, 50), 0), "ms",
               "decided and opened by the reader itself; no backend round trip. Measured on the stand-in reader, "
               "so this is the harness's call time, not HID's recognition time")
    rec.metric("tap -> door open, p95", round(percentile(decide, 95), 0), "ms", "as above")
    rec.metric("people / doors / seconds", f"{len(taps)} / {len(doors_used)} / {elapsed:.0f}", "")
    rec.note(f"clock difference between this machine and the containers during the run: {clock * 1000:.0f} ms "
             "(access-log timings use the containers' clock on both ends; greeting timings use this machine's on both ends)")
