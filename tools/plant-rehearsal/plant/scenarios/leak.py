"""
Scenario 5 - leak.

A leak sensor under an engine goes wet. The incident must show at once; the page (email + push)
is held for the confirmation window so a splash does not wake anyone; one page per recipient, not
two; a probe lapping at the water's edge must be one incident, not a string of them; and a sensor
that goes quiet must be shown as stale to the screens rather than as a comforting "dry".
"""

import time

from .. import config, feeder as feeder_mod, stack
from ..screenprobe import ScreenProbe
from ..util import iso, parse_iso, wait_until

SIMULATED = ("Three leak sensors on the real ingest path (gateway key, measured-at times). One goes wet and stays "
             "wet; one is splashed for 8 s; one laps wet/dry after it has been confirmed, and separately flickers "
             "faster than the confirmation window; one stops reporting. Mail lands in the local sink.")

WET_WINDOW = 30          # production value (Alerts:BinaryDangerConfirmWetSeconds)
DRY_WINDOW = 30          # production value (Alerts:BinaryDangerConfirmDrySeconds)


def device(ctx, key):
    return next(d for d in ctx.world["devices"] if d["key"] == key)


def incident_for(ctx, device_id, since_iso):
    rows = ctx.alert_rows(since_iso, rule="binary_danger", device_id=device_id)
    return rows


def emails(ctx, since_iso, subject):
    by_recipient = {}
    for r in ctx.world["alertRecipients"]:
        by_recipient[r["email"]] = ctx.sink_messages(since_iso, to=r["email"], subject=subject)
    return by_recipient


def run(ctx, rec):
    recipients = [r["email"] for r in ctx.world["alertRecipients"]]
    feeder_mod.clear_control()
    probe = ScreenProbe(ctx, "rehearsal-probe-sensors", "Rehearsal probe (sensor data)", "live-sensors").register()
    ctx._leak_probe = probe

    # ================= 1. wet, and stays wet =====================================================
    dev = device(ctx, "leak-engine-1")
    since = iso()
    t_wet = time.monotonic()
    wet_at = ctx.vm_now()
    at = feeder_mod.set_control("leak-engine-1", "wet")
    ctx.feeder.post_now("leak-engine-1", "water_detection", 1, observed=at)
    rec.step("leak-wet", f"{dev['name']} reports wet",
             expect={"walls": {"banner": "live", "text": "Water detected", "tone": "critical"}, "doors": {}}, within=20)

    found, waited = wait_until(lambda: incident_for(ctx, dev["id"], since), 15, 0.5)
    opened_after = found[0]["createdAt"] - wet_at if found else None
    rec.check("a wet reading opens a critical incident at once",
              bool(found) and found[0]["severity"] == "critical" and opened_after <= 3,
              f"critical incident written {opened_after * 1000:.0f} ms after the wet reading was sent" if found
              else f"no incident after {waited:.0f}s")
    rec.metric("leak -> incident visible", round(opened_after * 1000) if found else None, "ms",
               "from the reading being sent to the incident row existing, on the backend's clock")
    if found:
        _, detail = ctx.api.get(f"/api/havenzhub/alerts/{found[0]['id']}", expect=200)
        rec.check("the incident starts unconfirmed, with its page held",
                  detail.get("confirmationState") == "pending",
                  f"confirmationState={detail.get('confirmationState')}, deliveries "
                  f"{[(d['channel'], d['status']) for d in detail.get('deliveries', [])]}")

    time.sleep(max(0, 12 - (time.monotonic() - t_wet)))
    early = emails(ctx, since, "Water detected")
    rec.check("nobody is paged inside the confirmation window",
              all(len(v) == 0 for v in early.values()),
              f"{sum(len(v) for v in early.values())} emails 12 s after the wet reading (window is {WET_WINDOW}s)")

    def paged():
        got = emails(ctx, since, "Water detected")
        return got if all(len(v) >= 1 for v in got.values()) else None
    got, _ = wait_until(paged, WET_WINDOW + 45, 1.0)
    t_page = time.monotonic() - t_wet
    first_mail = min((parse_iso(m["receivedAt"]).timestamp() for v in (got or {}).values() for m in v), default=None)
    if first_mail is not None:
        t_page = first_mail - wet_at                      # when the mail arrived, not when the harness looked
    rec.step("leak-page", "confirmation window over: email and push go to the two alert recipients",
             expect={"walls": {"banner": "live", "text": "Water detected"}})
    rec.check("after the window each recipient gets the page", bool(got),
              f"both recipients emailed {t_page:.0f}s after the wet reading" if got
              else f"no page for everyone after {t_page:.0f}s: { {k: len(v) for k, v in emails(ctx, since, 'Water detected').items()} }")
    rec.metric("leak -> page (email in the mailbox)", round(t_page, 1), "s",
               f"held {WET_WINDOW}s on purpose (production value); delivery worker polls every 5 s here, 30 s in production")

    time.sleep(40)            # long enough for a second page to have gone out, if one was going to
    final = emails(ctx, since, "Water detected")
    rec.check("one email per recipient, not two", all(len(v) == 1 for v in final.values()),
              f"emails per recipient 70 s after the page: { {k.split('@')[0]: len(v) for k, v in final.items()} }")
    if found:
        rows = ctx.deliveries(found[0]["id"])
        opened = [d for d in rows if d["event"] == "opened"]
        pushes = [d for d in opened if d["channel"] == "push"]
        rec.check("push is recorded once per recipient (it goes nowhere in a rehearsal)",
                  len(pushes) == len(recipients) and all(d["status"] == "sent" for d in pushes),
                  f"{len(pushes)} push rows, statuses {[d['status'] for d in pushes]}; "
                  f"{len([d for d in opened if d['channel'] == 'email'])} email rows")
        reached = stack.psql_scalar("select count(*) from notifications.notifications where reference_id = "
                                    f"'{found[0]['id']}' and fcm_sent")
        rec.check("a push that reached no phone is not counted as sent",
                  not (pushes and all(d["status"] == "sent" for d in pushes) and reached == "0"),
                  f"{len(pushes)} push deliveries are recorded 'sent'; notifications that actually went to a phone: {reached} "
                  "(no push provider is configured in the rehearsal - the same as a provider that is down)",
                  finding="F-PUSH-SAID-SENT")
    incidents = incident_for(ctx, dev["id"], since)
    rec.check("a sensor that stays wet is one incident", len(incidents) == 1,
              f"{len(incidents)} incident(s) for {dev['name']} since it went wet")

    # ---- the wall's own data says wet ------------------------------------------------------------
    d, reading = probe.reading_for(dev["id"], "water_detection")
    rec.check("the screen data shows the wet reading as live",
              bool(reading) and reading.get("value") == 1 and reading.get("liveness") == "reporting",
              f"screen reading: value={reading.get('value') if reading else None}, liveness="
              f"{reading.get('liveness') if reading else None}, alert={reading.get('alertSeverity') if reading else None}")

    # ================= 2. dry again ===============================================================
    since_dry = iso()
    t_dry = time.monotonic()
    dry_at = ctx.vm_now()
    at = feeder_mod.set_control("leak-engine-1", "dry")
    ctx.feeder.post_now("leak-engine-1", "water_detection", 0, observed=at)
    rec.step("leak-dry", f"{dev['name']} reports dry", expect={"walls": {"banner": "live", "note": "stays up until dry for 30 s"}})

    def resolved():
        rows = incident_for(ctx, dev["id"], since)
        return rows if rows and rows[0]["resolvedAt"] else None
    done, waited = wait_until(resolved, DRY_WINDOW + 75, 2.0)
    if done:
        waited = done[0]["resolvedAt"] - dry_at
    rec.step("leak-cleared", "incident resolved after the dry window", expect={"walls": {"banner": "none"}})
    rec.check("the incident resolves only after it has stayed dry", bool(done) and waited >= DRY_WINDOW - 3,
              f"resolved {waited:.0f}s after the dry reading (dry window {DRY_WINDOW}s; checked every 30 s here)"
              if done else f"still open {waited:.0f}s after going dry")
    rec.metric("dry -> incident resolved", round(waited, 1) if done else None, "s",
               "dry window is the production 30 s; the sweep that closes it runs every 30 s here, 300 s in production")
    got, _ = wait_until(lambda: (lambda g: g if all(len(v) >= 1 for v in g.values()) else None)(
        emails(ctx, since_dry, "[Resolved]")), 40, 2.0)
    final = emails(ctx, since_dry, "[Resolved]")
    rec.check("each recipient gets one all-clear", all(len(v) == 1 for v in final.values()),
              f"resolved emails per recipient: { {k.split('@')[0]: len(v) for k, v in final.items()} }")

    # ================= 3. a splash ================================================================
    dev2 = device(ctx, "leak-mechanical")
    since = iso()
    at = feeder_mod.set_control("leak-mechanical", "wet")
    ctx.feeder.post_now("leak-mechanical", "water_detection", 1, observed=at)
    rec.step("splash-wet", f"{dev2['name']} is splashed", expect={"walls": {"banner": "live", "text": "Water detected"}})
    found, _ = wait_until(lambda: incident_for(ctx, dev2["id"], since), 15, 0.5)
    time.sleep(8)
    at = feeder_mod.set_control("leak-mechanical", "dry")
    ctx.feeder.post_now("leak-mechanical", "water_detection", 0, observed=at)
    rec.step("splash-dry", f"{dev2['name']} dry again 8 s later", expect={"walls": {"banner": "none"}})
    time.sleep(WET_WINDOW + 25)
    rows = incident_for(ctx, dev2["id"], since)
    mails = emails(ctx, since, "Water detected")
    rec.check("a splash shows on the record but pages nobody",
              bool(found) and len(rows) == 1 and rows[0]["confirmation"] == "brief" and rows[0]["resolvedAt"] is not None
              and all(len(v) == 0 for v in mails.values()),
              f"{len(rows)} incident(s), state {rows[0]['confirmation'] if rows else None}, "
              f"resolved={bool(rows and rows[0]['resolvedAt'])}, emails {sum(len(v) for v in mails.values())}")

    # ================= 4. a lapping probe (after it has been confirmed) ============================
    dev3 = device(ctx, "leak-engine-3")
    since = iso()
    at = feeder_mod.set_control("leak-engine-3", "wet")
    ctx.feeder.post_now("leak-engine-3", "water_detection", 1, observed=at)
    rec.step("lapping-wet", f"{dev3['name']} goes wet; water then laps at the probe for a minute",
             expect={"walls": {"banner": "live", "text": "Water detected"}}, within=20)
    wait_until(lambda: (lambda g: g if all(len(v) >= 1 for v in g.values()) else None)(
        emails(ctx, since, "Water detected")), WET_WINDOW + 45, 1.0)
    for i in range(4):
        at = feeder_mod.set_control("leak-engine-3", "dry")
        ctx.feeder.post_now("leak-engine-3", "water_detection", 0, observed=at)
        time.sleep(8)
        at = feeder_mod.set_control("leak-engine-3", "wet")
        ctx.feeder.post_now("leak-engine-3", "water_detection", 1, observed=at)
        time.sleep(8)
    rows = incident_for(ctx, dev3["id"], since)
    mails = emails(ctx, since, "Water detected")
    cleared = emails(ctx, since, "[Resolved]")
    rec.check("a lapping probe is one incident and one page",
              len(rows) == 1 and rows[0]["resolvedAt"] is None and all(len(v) == 1 for v in mails.values())
              and all(len(v) == 0 for v in cleared.values()),
              f"{len(rows)} incident(s), open={bool(rows and rows[0]['resolvedAt'] is None)}, pages per recipient "
              f"{[len(v) for v in mails.values()]}, all-clears {[len(v) for v in cleared.values()]} after 4 wet/dry laps of 8 s")
    at = feeder_mod.set_control("leak-engine-3", "dry")
    ctx.feeder.post_now("leak-engine-3", "water_detection", 0, observed=at)
    rec.step("lapping-dry", f"{dev3['name']} dry for good", expect={"walls": {"banner": "none"}}, within=DRY_WINDOW + 60)
    wait_until(lambda: (lambda r: r if r and r[0]["resolvedAt"] else None)(incident_for(ctx, dev3["id"], since)),
               DRY_WINDOW + 75, 2.0)

    # ================= 5. a probe flickering faster than the confirmation window ===================
    since = iso()
    rec.step("flicker", f"{dev2['name']} flickers wet 5 s / dry 5 s for 70 s (water at the probe's edge)",
             expect={"walls": {"banner": "live", "note": "may blink with the probe"}})
    t0 = time.monotonic()
    while time.monotonic() - t0 < 70:
        at = feeder_mod.set_control("leak-mechanical", "wet")
        ctx.feeder.post_now("leak-mechanical", "water_detection", 1, observed=at)
        time.sleep(5)
        at = feeder_mod.set_control("leak-mechanical", "dry")
        ctx.feeder.post_now("leak-mechanical", "water_detection", 0, observed=at)
        time.sleep(5)
    rows = incident_for(ctx, dev2["id"], since)
    mails = emails(ctx, since, "Water detected")
    paged_anyone = any(len(v) >= 1 for v in mails.values())
    rec.check("a probe that flickers wet/dry for over a minute still pages someone", paged_anyone,
              f"{len(rows)} incident(s) in 70 s (states {sorted(set(r['confirmation'] for r in rows))}), "
              f"{sum(len(v) for v in mails.values())} page(s) sent", finding="F-LEAK-FLICKER")
    rec.metric("incidents opened by 70 s of flicker", len(rows), "incidents")
    feeder_mod.set_control("leak-mechanical", None)
    time.sleep(DRY_WINDOW + 35)

    # ================= 6. a sensor that goes quiet =================================================
    interval = config.BINARY_REPORTING_INTERVAL_SECONDS
    since = iso()
    t_silent = time.monotonic()
    silent_at = ctx.vm_now()
    feeder_mod.set_control("leak-engine-1", "silent")
    rec.step("sensor-silent", f"{dev['name']} stops reporting (battery, range, or unplugged)",
             expect={"walls": {"banner": "stale", "note": "its last 'dry' must not be shown as current"}}, within=60)

    def stale():
        d, reading = probe.reading_for(dev["id"], "water_detection")
        return (d, reading) if d and d.get("liveness") in ("stale", "silent") else None
    got, waited = wait_until(stale, 120, 3.0)
    rec.check("a sensor silent for longer than its freshness window is marked stale in the screen data",
              bool(got), (f"screen data says device liveness={got[0]['liveness']}, reading liveness="
                          f"{got[1].get('liveness') if got[1] else None}, {waited:.0f}s after its last report "
                          f"(freshness window {2 * interval}s)") if got else f"still 'reporting' after {waited:.0f}s")
    rec.metric("last report -> marked stale on screens", round(waited, 1) if got else None, "s",
               f"reporting interval {interval}s here (stale after {2 * interval}s); 900 s when unset in production. "
               "Counted from when the sensor was silenced; its last heartbeat can be up to 40 s older")

    def silent_alert():
        rows = ctx.alert_rows(since, rule="silent", device_id=dev["id"])
        return rows or None
    alert, waited = wait_until(silent_alert, 260, 5.0)
    rec.check("a sensor that stays silent raises a 'sensor silent' warning", bool(alert),
              f"warning opened {alert[0]['createdAt'] - silent_at:.0f}s after it was silenced (silent after {6 * interval}s "
              "without a report, checked every 30 s here)" if alert else f"no silent alert after {time.monotonic() - t_silent:.0f}s")

    at = feeder_mod.set_control("leak-engine-1", "dry")
    ctx.feeder.post_now("leak-engine-1", "water_detection", 0, observed=at)
    rec.step("sensor-back", f"{dev['name']} reports again", expect={"walls": {"banner": "none"}})
    back, waited = wait_until(lambda: (lambda x: x if x[0] and x[0].get("liveness") == "reporting" else None)(
        probe.reading_for(dev["id"], "water_detection")), 60, 3.0)
    rec.check("when it reports again the screen data is live again", bool(back),
              f"liveness=reporting {waited:.0f}s after the first new reading" if back else "still not reporting after 60 s")


def cleanup(ctx, rec):
    for key in ("leak-engine-1", "leak-engine-3", "leak-mechanical"):
        try:
            at = feeder_mod.set_control(key, "dry")
            ctx.feeder.post_now(key, "water_detection", 0, observed=at)
        except Exception:  # noqa: BLE001
            pass
    probe = getattr(ctx, "_leak_probe", None)
    if probe:
        probe.retire()
