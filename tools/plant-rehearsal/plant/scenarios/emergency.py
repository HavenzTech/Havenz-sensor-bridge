"""
Scenario 10 - emergency.

An evacuation is announced to the whole company, cleared, then announced for one area only (the
corridor, where four door panels sit), and cleared again while one screen in that area is off.
When that screen comes back it must not still be telling people to evacuate.

What is asserted here is what the backend sends and counts: which screens are reached per scope,
the "N of M screens showing" figure the admin page reads, and the snapshot a returning screen is
given. Three probe screens of the harness's own stand in three positions (no area, the corridor,
a different room) and speak the screens' real protocol, so the routing is proven even with no
browser running. What the real wall and door pages paint is the screen half's to assert, off the
same timeline steps.
"""

import time

from .. import plantdef
from ..screenprobe import ScreenProbe
from ..util import wait_until

SIMULATED = ("Announcements made through the admin API exactly as the web app makes them. Three harness probe "
             "screens (no area / corridor 103 / engine room 112) on the screens' own protocol; the real 23 "
             "screens are reached too if the screen half has them open.")

PAUSE = 22        # seconds each state is held, so the screen half can photograph it


def announce(ctx, kind, area_key=None, message=None):
    body = {"kind": kind}
    if area_key:
        body["areaId"] = ctx.areas[area_key]["id"]
    if message:
        body["message"] = message
    status, resp = ctx.api.post("/api/admin/area-access/emergency/announce", body,
                                account=ctx.admin("admin-1"), sensitive=True)
    if status != 200:
        raise RuntimeError(f"announce {kind} answered HTTP {status}: {resp}")
    return resp


def showing(ctx):
    _, body = ctx.api.get("/api/admin/area-access/emergency/showing", expect=200)
    return body


def told_after(probes, t0):
    """Seconds from `t0` to the moment the LAST of these probes received its emergency frame."""
    firsts = []
    for p in probes:
        frames = p.frames_since(t0, "EmergencyState")
        if not frames:
            return None
        firsts.append(frames[0][0] - t0)
    return max(firsts) if firsts else None


def scope_row(body, scope, area_id=None):
    for row in body.get("scopes", []):
        if row["scope"] == scope and (scope == "company" or row.get("areaId") == area_id):
            return row
    return None


def n_of_m(row):
    if not row:
        return "no row"
    return (f"{row['screensShowing']} of {row['screensInScope']} showing "
            f"({row['screensBehind']} behind, {row['screensNotReporting']} not reporting, {row['screensOffline']} offline)")


def run(ctx, rec):
    area_key = plantdef.AREA_SCOPED_EMERGENCY
    area_id = ctx.areas[area_key]["id"]
    area_name = ctx.areas[area_key]["name"]
    panels_in_area = [s["name"] for s in ctx.world["screens"] if s["kind"] == "door" and s.get("areaKey") == area_key]
    walls = [s["name"] for s in ctx.world["screens"] if s["kind"] == "wall"]
    panels = [s["name"] for s in ctx.world["screens"] if s["kind"] == "door"]

    everywhere = ScreenProbe(ctx, "rehearsal-probe-no-area", "Rehearsal probe (no area)", "campus-health").register()
    corridor = ScreenProbe(ctx, "rehearsal-probe-corridor", "Rehearsal probe (corridor 103)", "campus-health",
                           area_key=area_key).register()
    elsewhere = ScreenProbe(ctx, "rehearsal-probe-engine-room", "Rehearsal probe (engine room 112)", "campus-health",
                            area_key="112").register()
    probes = {"no area": everywhere, "corridor": corridor, "engine room": elsewhere}
    ctx._emergency_probes = list(probes.values())
    for p in probes.values():
        p.connect()
    time.sleep(3)

    first = {name: p.frames_since(0, "EmergencySnapshot") for name, p in probes.items()}
    rec.check("a connecting screen is handed the standing state first, as a snapshot",
              all(len(v) >= 1 for v in first.values()) and all(p.showing() == "none" for p in probes.values()),
              f"snapshots on connect: { {k: len(v) for k, v in first.items()} }; all show nothing standing")

    # ---- 1. evacuation, whole company ------------------------------------------------------------
    t0 = time.monotonic()
    resp = announce(ctx, "evacuation", message="Rehearsal: evacuate the building")
    v1 = resp["version"]
    rec.step("evacuation-company", f"Evacuation announced to the whole company (version {v1})",
             expect={"walls": {"emergency": "evacuation"}, "doors": {"emergency": "evacuation"}}, within=20)
    reached, waited = wait_until(lambda: all(p.showing() == "evacuation" for p in probes.values()), 15, 0.2)
    told = told_after(probes.values(), t0)
    rec.check("a company evacuation reaches screens in every position", bool(reached),
              f"all three probes were told within {told * 1000:.0f} ms of the announcement" if reached and told is not None
              else f"after {waited:.0f}s: { {k: p.showing() for k, p in probes.items()} }")
    rec.metric("announce -> screens told (company scope)", round(told * 1000) if told is not None else None, "ms",
               "from the admin's request being sent to the last probe screen receiving the frame")

    def company_counted():
        row = scope_row(showing(ctx), "company")
        ids = {s["screenId"]: s for s in (row or {}).get("screens", [])}
        mine = [ids.get(p.screen_id, {}).get("state") for p in probes.values()]
        return row if row and row.get("version") == v1 and all(m == "showing" for m in mine) else None
    row, waited = wait_until(company_counted, 30, 2.0)
    final = row or scope_row(showing(ctx), "company")
    rec.check("the admin page's 'N of M screens showing' counts the screens that reported it",
              bool(row), f"company scope v{final.get('version') if final else '?'}: {n_of_m(final)}; the three probes "
              f"are {'all showing' if row else 'NOT all showing'} {waited:.0f}s after the announcement")
    rec.metric("screens showing the company evacuation", n_of_m(final), "",
               "M counts every paired, active screen, including any that are powered off")
    time.sleep(max(0, PAUSE - (time.monotonic() - t0)))

    # ---- 2. all clear, whole company --------------------------------------------------------------
    resp = announce(ctx, "all-clear")
    v2 = resp["version"]
    rec.step("all-clear-company", f"All clear for the company (version {v2})",
             expect={"walls": {"emergency": "all-clear"}, "doors": {"emergency": "all-clear"}}, within=20)
    cleared, waited = wait_until(lambda: all(p.showing() == "none" for p in probes.values()), 15, 0.2)
    rec.check("the all-clear reaches every screen and versions only go up", bool(cleared) and v2 > v1,
              f"all three probes cleared in {waited:.1f}s; version {v1} -> {v2}" if cleared
              else f"after {waited:.0f}s: { {k: p.showing() for k, p in probes.items()} }")
    time.sleep(PAUSE)

    # ---- 3. evacuation, one area only --------------------------------------------------------------
    t0 = time.monotonic()
    resp = announce(ctx, "evacuation", area_key=area_key, message=f"Rehearsal: evacuate {area_name}")
    v3 = resp["version"]
    rec.step("evacuation-area",
             f"Evacuation announced for {area_name} only (version {v3}); panels there: {', '.join(panels_in_area)}",
             expect={"walls": {"emergency": "none"},
                     "doors": {"emergency": "evacuation", "only": panels_in_area, "others": "none"}}, within=20)
    reached, waited = wait_until(lambda: corridor.showing() == "evacuation", 15, 0.2)
    told = told_after([corridor], t0)
    time.sleep(4)             # long enough for a wrongly routed frame to have arrived
    rec.check("an area evacuation reaches the screen in that area",
              bool(reached), f"corridor probe told within {told * 1000:.0f} ms" if reached and told is not None
              else "corridor probe never switched")
    rec.check("an area evacuation does not reach screens outside the area",
              everywhere.showing() == "none" and elsewhere.showing() == "none",
              f"probe with no area shows {everywhere.showing()}, probe in the engine room shows {elsewhere.showing()}")
    rec.metric("announce -> screens told (area scope)", round(told * 1000) if told is not None else None, "ms")

    def area_counted():
        row = scope_row(showing(ctx), "area", area_id)
        if not row or row.get("version") != v3:
            return None
        ids = {s["screenId"] for s in row.get("screens", [])}
        return row if corridor.screen_id in ids else None
    row, _ = wait_until(area_counted, 30, 2.0)
    final = row or scope_row(showing(ctx), "area", area_id)
    in_scope = {s["label"] for s in (final or {}).get("screens", [])}
    expected_scope = set(panels_in_area) | {corridor.label}
    rec.check("the area's 'N of M' is made up of exactly the screens in that area",
              bool(final) and in_scope == expected_scope,
              f"in scope: {sorted(in_scope)}" + ("" if in_scope == expected_scope
                                                  else f"; expected {sorted(expected_scope)}"))
    rec.metric(f"screens showing the {area_name} evacuation", n_of_m(final), "")

    # ---- 4. one screen in the area is off when the all-clear goes out ------------------------------
    off_panel = panels_in_area[0] if panels_in_area else None
    time.sleep(max(0, PAUSE - (time.monotonic() - t0)))
    corridor.disconnect()
    shown_when_off = corridor.showing()
    rec.step("screen-off", f"One screen in {area_name} loses power while the evacuation stands"
             + (f" (screen half: power off '{off_panel}')" if off_panel else ""),
             expect={"doors": {"powerOff": off_panel}})
    time.sleep(12)

    resp = announce(ctx, "all-clear", area_key=area_key)
    v4 = resp["version"]
    rec.step("all-clear-area", f"All clear for {area_name} (version {v4}); the powered-off screen misses it",
             expect={"walls": {"emergency": "none"},
                     "doors": {"emergency": "all-clear", "only": [p for p in panels_in_area if p != off_panel]}},
             within=20)
    time.sleep(PAUSE)

    # ---- 5. it comes back --------------------------------------------------------------------------
    rec.step("screen-on", "The powered-off screen comes back and reconnects"
             + (f" (screen half: power on '{off_panel}')" if off_panel else ""),
             expect={"doors": {"powerOn": off_panel, "emergency": "none",
                               "note": "must NOT still show the evacuation it was showing when it went off"}},
             within=30)
    t_back = time.monotonic()
    before = time.monotonic()
    corridor.connect()
    got, waited = wait_until(lambda: corridor.frames_since(before, "EmergencySnapshot"), 15, 0.2)
    snapshot = got[0][2] if got else {}
    rec.check("a screen that was off during the all-clear is given the current state when it reconnects",
              bool(got) and snapshot.get("standing") == [] and (snapshot.get("version") or 0) >= v4
              and corridor.showing() == "none",
              f"it was showing '{shown_when_off}' when it went off; on reconnect the snapshot says version "
              f"{snapshot.get('version')} with {len(snapshot.get('standing') or [])} standing, {waited:.1f}s after connecting"
              if got else "no snapshot received on reconnect")
    rec.metric("reconnect -> correct emergency state (all-clear replay)", round(waited, 2) if got else None, "s")

    status, http_snapshot = corridor.emergency_snapshot()
    rec.check("the polled snapshot agrees with the pushed one",
              status == 200 and http_snapshot.get("standing") == [] and http_snapshot.get("version") == snapshot.get("version"),
              f"GET /api/screens/me/emergency -> HTTP {status}, version {http_snapshot.get('version') if isinstance(http_snapshot, dict) else None}, "
              f"{len(http_snapshot.get('standing') or []) if isinstance(http_snapshot, dict) else '?'} standing")

    _, current = ctx.api.get("/api/admin/area-access/emergency/current", expect=200)
    rec.check("nothing is left standing at the end", not current.get("active"),
              f"emergency/current says active={current.get('active')}")
    final = showing(ctx)
    company = scope_row(final, "company")
    rec.metric("screens at the end (company scope)", n_of_m(company), "")
    rec.note(f"{len(walls)} wall screens and {len(panels)} door panels exist; how many were paired and open during "
             "this run is in the 'N of M' figures above. What each real screen painted is in the screen half's record.")
    time.sleep(max(0, 10 - (time.monotonic() - t_back)))


def cleanup(ctx, rec):
    try:
        _, current = ctx.api.get("/api/admin/area-access/emergency/current", expect=200)
        if current.get("active"):
            announce(ctx, "all-clear")
            rec.note("an emergency was still standing when the scenario ended; cleared company-wide")
    except Exception as e:  # noqa: BLE001
        rec.note(f"could not confirm the all-clear: {e}")
    for probe in getattr(ctx, "_emergency_probes", []):
        try:
            probe.retire()
        except Exception:  # noqa: BLE001
            pass
