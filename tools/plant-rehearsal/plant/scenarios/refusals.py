"""
Scenario 2 - refusals.

Four ways a door must stay shut: a face nobody has enrolled; a member of staff at a door they have
no access to; access that has run out; and someone who left the company a minute ago.
"""

import time
import uuid
from datetime import timedelta

from .. import config, plantdef, readers, stack
from ..util import http, iso, utc_now, wait_until

SIMULATED = ("An unknown face at a panel door; an office worker at the engine-room door; a visitor pass that "
             "expires 75 s after it is granted; and a member of staff with access to every door who is "
             "deactivated in the admin app mid-run, then tries all 20 doors.")

REMOVAL_BAR_SECONDS = 120      # stated bar: off every reader within two minutes of deactivation


def row_for(ctx, since_iso, terminal_id, log_id, timeout=30):
    def find():
        return next((r for r in ctx.access_events(since_iso, terminal_id) if r["logId"] == log_id), None)
    return wait_until(find, timeout, 0.5)


def run(ctx, rec):
    since_iso = iso()

    # ---- 1. unknown face -------------------------------------------------------------------------
    door = ctx.terminals[2]                     # Door 102-A, lobby, has a panel
    before = ctx.open_counts()[2]["all"]
    mono = time.monotonic()
    result = readers.scan(2, str(uuid.uuid4()))
    rec.step("unknown-face", f"A face nobody has enrolled is presented at {door['name']}",
             expect={"doors": {"state": "denied", "text": "Face not recognised", "only": [f"{door['name']} panel"]}},
             within=10)
    row, _ = row_for(ctx, since_iso, door["id"], result["logId"])
    rec.check("an unknown face does not open the door", not result["opened"] and ctx.open_counts()[2]["all"] == before,
              f"reader decision: event {result['event']} (not identified), door openings unchanged")
    rec.check("it is recorded as not identified, against nobody",
              bool(row) and row["type"] == "NotIdentified" and row["userId"] is None,
              f"access log: {row['type'] if row else 'no row'}, user {row['userId'] if row else '-'}")
    time.sleep(1.5)
    heard = [w for w in ctx.welcomes(mono) if w["terminalId"] == door["id"]]
    rec.check("the panel is told to show a refusal, not a welcome",
              len(heard) == 1 and heard[0]["eventType"] == "NotIdentified",
              f"broadcasts for that door: {[w['eventType'] for w in heard]}")

    # ---- 2. no access to this door ------------------------------------------------------------------
    person = ctx.people["office-2"]
    door = ctx.terminals[18]                    # Door 112-A, engine room
    before = ctx.open_counts()[18]["all"]
    result = readers.scan(18, person["id"])
    rec.step("no-access", f"{person['name']} (office) tries {door['name']} (engine room), which they have no access to",
             expect={"doors": {}})
    row, _ = row_for(ctx, since_iso, door["id"], result["logId"])
    rec.check("staff without access to a door cannot open it", not result["opened"] and ctx.open_counts()[18]["all"] == before,
              f"reader decision: event {result['event']}; the reader holds only people granted that door")
    rec.check("the refusal is in the access log", bool(row) and row["type"] in ("NotIdentified", "Denied"),
              f"access log: {row['type'] if row else 'no row'}, user {row['userId'] if row else '-'}")
    if row and row["userId"] is None:
        rec.note("A member of staff refused at a door they are not on is logged as 'not identified' with no name: "
                 "the reader was never given their face for that door, so it cannot say who it was.")

    # ---- 3. access that runs out ---------------------------------------------------------------------
    person = ctx.people["office-3"]
    door = ctx.terminals[14]                    # Door 108-A, mechanical
    until = utc_now() + timedelta(seconds=75)
    status, grant = ctx.api.post("/api/admin/area-access",
                                 {"userId": person["id"], "areaId": ctx.areas["108"]["id"], "accessLevel": "standard",
                                  "effectiveUntil": iso(until), "notes": "Rehearsal: short visitor pass"})
    rec.step("timed-access-granted", f"{person['name']} is given access to {door['name']} until {iso(until)[11:19]} UTC",
             expect={"doors": {}})
    held, waited = wait_until(lambda: ctx.readers_holding(person["id"], [14]), 60, 1.0)
    rec.check("a dated grant reaches the reader with its end time", status == 201 and bool(held),
              f"grant HTTP {status}; on the reader {waited:.0f}s later" if held else f"grant HTTP {status}; not on the reader after {waited:.0f}s")
    result = readers.scan(14, person["id"])
    rec.check("inside the dated window the door opens", result["opened"], f"reader decision: event {result['event']}")
    wait = (until - utc_now()).total_seconds() + 3
    if wait > 0:
        time.sleep(wait)
    before = ctx.open_counts()[14]["all"]
    result = readers.scan(14, person["id"])
    rec.step("timed-access-expired", f"{person['name']} tries {door['name']} after the access has run out",
             expect={"doors": {}})
    row, _ = row_for(ctx, since_iso, door["id"], result["logId"])
    rec.check("after the end time the reader refuses on its own", not result["opened"] and result["event"] == 6
              and ctx.open_counts()[14]["all"] == before,
              f"reader decision: event {result['event']} (denied) {wait:.0f}s after the grant was made; no opening")
    rec.check("the refusal names the person", bool(row) and row["type"] == "Denied" and row["userId"] == person["id"],
              f"access log: {row['type'] if row else 'no row'}, user {'named' if row and row['userId'] == person['id'] else 'missing'}")

    def expired_grant_cleaned(r):
        active = stack.psql_scalar(f"select count(*) from iot.user_area_access where user_id = '{person['id']}' "
                                   f"and area_id = '{ctx.areas['108']['id']}' and is_active")
        still = ctx.readers_holding(person["id"], [14], need_face=False)
        r.check("the expired grant is retired and the person taken off that reader by the five-minute job",
                active == "0" and not still,
                f"active grants for that door: {active}; still on the reader: {'yes' if still else 'no'}")
    ctx.defer("refusals", 330, "expired access is cleaned up by the five-minute job", expired_grant_cleaned)

    # ---- 4. a leaver, deactivated mid-run --------------------------------------------------------------
    leaver = ctx.people.get("office-1")
    if leaver is None or leaver.get("deactivated"):
        stamp = utc_now().strftime("%H%M%S")
        rec.step("leaver-hired", "A new member of staff with access to every door is created for this run")
        leaver = ctx.new_person(f"Robin Leaver {stamp}", f"robin.leaver.{stamp}@{plantdef.EMAIL_DOMAIN}", plantdef.ALL_AREAS)
    all_doors = list(range(1, config.READER_COUNT + 1))
    held = ctx.readers_holding(leaver["id"], all_doors)
    rec.check("before leaving, the person is on all 20 readers", len(held) == 20, f"on {len(held)} of 20 readers")
    result = readers.scan(2, leaver["id"])
    rec.check("before leaving, their face opens a door", result["opened"], f"reader decision: event {result['event']}")
    photo = stack.psql_scalar(f"select enrollment_storage_path from iot.facial_recognition where user_id = '{leaver['id']}' limit 1")

    counts_before = ctx.user_counts()
    t0 = time.monotonic()
    status, body = ctx.api.post(f"/api/admin/users/{leaver['id']}/deactivate",
                                {"reason": "Rehearsal: left the company"}, account=ctx.admin("admin-2"))
    rec.step("leaver-deactivated", f"{leaver['name']} is deactivated in the admin app",
             expect={"doors": {"note": "from here on their face must be refused at every door"}}, within=REMOVAL_BAR_SECONDS)
    rec.check("deactivation queues a removal for every reader and revokes their access",
              status == 200 and body.get("terminalRemovalsQueued") == 20 and body.get("faceRecordCleared"),
              f"HTTP {status}: removals queued {body.get('terminalRemovalsQueued') if isinstance(body, dict) else body}, "
              f"grants revoked {body.get('areaGrantsRevoked') if isinstance(body, dict) else '-'}, "
              f"face record cleared {body.get('faceRecordCleared') if isinstance(body, dict) else '-'}")
    leaver["deactivated"] = True
    ctx.save_world()

    dropped = ctx.wait_until_dropped(counts_before, all_doors, 600)
    gone, waited = wait_until(lambda: not ctx.readers_holding(leaver["id"], all_doors, need_face=False), 60, 2.0)
    took = (time.monotonic() - t0) if dropped is None else dropped - t0
    remaining = [] if gone else ctx.readers_holding(leaver["id"], all_doors, need_face=False)
    rec.metric("deactivation -> off all 20 readers", round(took, 1) if gone else None, "s")
    rec.check(f"the leaver is off all 20 readers within {REMOVAL_BAR_SECONDS}s", bool(gone) and took <= REMOVAL_BAR_SECONDS,
              f"removed from all 20 in {took:.0f}s" if gone else f"still on readers {remaining} after {took:.0f}s")

    before = ctx.open_counts()
    results = {n: readers.scan(n, leaver["id"]) for n in all_doors}
    after = ctx.open_counts()
    opened = [n for n in all_doors if results[n]["opened"] or after[n]["all"] != before[n]["all"]]
    rec.step("leaver-tries-doors", f"{leaver['name']} tries all 20 doors", expect={"doors": {"state": "denied"}})
    rec.check("the leaver is refused at every door", not opened,
              "refused at 20 of 20 doors" if not opened else f"doors that OPENED: {opened}")

    def none_outstanding():
        _, outstanding = ctx.api.get("/api/admin/area-access/terminal-sync/outstanding", expect=200)
        return not [i for i in outstanding.get("items", []) if i["userId"] == leaver["id"]]
    clear, waited = wait_until(none_outstanding, 60, 2.0)
    rec.check("the admin page shows no removal still outstanding for them", bool(clear),
              f"0 outstanding removals {time.monotonic() - t0:.0f}s after deactivation (the page trails the readers by "
              f"{max(0, time.monotonic() - t0 - took):.0f}s)" if clear else "removals still listed as outstanding 60 s after the readers had dropped them")
    if photo:
        name = photo.split("/", 3)[-1] if photo.startswith("gs://") else photo
        _, objects, _ = http("GET", f"{config.SINK}/gcs/objects", timeout=15)
        still = [o for o in objects or [] if o["name"] == name]
        rec.check("their stored face photo is deleted", not still,
                  "photo no longer in storage" if not still else "photo is still in storage")
    status, _, _ = http("POST", f"{config.API}/api/auth/login",
                        body={"email": leaver["email"], "password": leaver.get("password") or leaver["temporaryPassword"]}, timeout=20)
    rec.check("they can no longer sign in", status == 401, f"sign-in answered HTTP {status}")
