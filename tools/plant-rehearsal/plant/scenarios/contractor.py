"""
Scenario 3 - contractor lifecycle.

A contractor already onboarded at commissioning (invitation, consent, phone photo, host approval)
is given a new job with a short window on three rooms. Before the window their face is refused;
inside it the job's doors open and no others; after the host closes the job out they are off the
readers and refused. Then the retention rule: the stored face must be deleted when its retention
period ends, and not before.
"""

import time
from datetime import timedelta

from .. import readers, seed as seed_mod, stack
from ..api import Account
from ..util import iso, utc_now, wait_until

SIMULATED = ("One contractor, one new job with a window that opens about 75 s after it is created. The "
             "retention period (30 days) is not waited out: the job's purge date is moved to now in the database "
             "and the product's own five-minute sweep is left to do the deleting.")

REMOVAL_BAR_SECONDS = 120


def job_doors(ctx, area_keys):
    return [t for t in ctx.world["terminals"] if t["areaKey"] in area_keys]


def held_on(ctx, user_id, doors, need_face=False):
    return ctx.readers_holding(user_id, [t["number"] for t in doors], need_face=need_face)


def end_job(ctx, task_id, host, reason):
    return ctx.api.post(f"/api/havenzhub/tasks/{task_id}/access/end", {"reason": reason}, account=host)


def run(ctx, rec):
    c = ctx.world.get("contractor")
    if not c:
        rec.check("a contractor was onboarded at commissioning", False, "the seed did not finish the contractor flow")
        return
    host = ctx.admin("admin-1")
    reviewer = ctx.admin("admin-2")
    pid = ctx.property_id
    doors = job_doors(ctx, c["areas"])
    since_iso = iso()

    # ---- 0. close out whatever job is still open -------------------------------------------------
    for task_id in [c.get("seedTaskId")] + list(c.get("openTaskIds", [])):
        if not task_id:
            continue
        closed = stack.psql_scalar(f"select closed_at is not null from tasks.task_access where task_id = '{task_id}'")
        if closed == "f":
            status, _ = end_job(ctx, task_id, host, "Rehearsal: earlier job finished")
            rec.step("earlier-job-closed", "The contractor's earlier job is closed out by the host")
            gone, waited = wait_until(lambda: not held_on(ctx, c["id"], doors), 300, 2.0)
            rec.check("closing the earlier job takes the contractor off its readers", status == 200 and bool(gone),
                      f"off {len(doors)} readers {waited:.0f}s after close-out" if gone
                      else f"still on readers {held_on(ctx, c['id'], doors)} after {waited:.0f}s")
    c["openTaskIds"] = []

    # ---- 1. is the photo still approved? (it is deleted once its retention period has ended) -----
    acct = Account(c["email"], c["password"], "contractor")
    ctx.api.login(acct)
    _, me = ctx.api.get("/api/contractor/me", account=acct, expect=200)
    if not ((me.get("photo") or {}).get("approved")):
        rec.step("photo-again", "The contractor's earlier photo has been purged; they take a new one and a host approves it")
        status, enrolled = ctx.api.upload_photo("/api/havenzhub/facialrecognition/enroll/photo",
                                                seed_mod.photo_jpeg(c["name"], int(time.time()) % 900 + 1000), account=acct)
        review = (enrolled or {}).get("review") or {}
        status, _ = ctx.api.post(f"/api/havenzhub/contractor-photo-reviews/{review.get('id')}/approve", account=reviewer)
        rec.check("a returning contractor can be photographed and approved again", status == 200,
                  f"approve answered HTTP {status}")

    # ---- 2. a new job, window opening shortly ------------------------------------------------------
    start = utc_now() + timedelta(seconds=75)
    end = start + timedelta(minutes=10)
    _, task = ctx.api.post("/api/havenzhub/tasks",
                           {"title": f"Coolant skid follow-up {utc_now().strftime('%H:%M')}", "projectId": c["projectId"],
                            "propertyId": pid, "status": "todo"}, account=host, expect=201)
    status, job = ctx.api.put(f"/api/havenzhub/tasks/{task['id']}/access",
                              {"propertyId": pid, "windowStart": iso(start), "windowEnd": iso(end),
                               "areaIds": [ctx.areas[a]["id"] for a in c["areas"]], "escortRequired": False,
                               "hostUserId": ctx.people["admin-1"]["id"],
                               "contractors": [{"email": c["email"], "name": c["name"]}]}, account=host)
    c["openTaskIds"] = [task["id"]]
    ctx.save_world()
    rec.step("job-created", f"New job for {c['name']}: {', '.join(ctx.areas[a]['name'] for a in c['areas'])}; "
             f"window opens {iso(start)[11:19]} UTC", expect={"doors": {}})
    outcome = (job.get("contractorResults") or [{}])[0].get("outcome") if isinstance(job, dict) else None
    rec.check("the job is created with the contractor on it", status == 200 and outcome in ("added", "already_on_job"),
              f"HTTP {status}, contractor outcome '{outcome}'")

    # With their face, not just their name: until the photo has landed a reader cannot recognise them at all.
    on, waited = wait_until(lambda: len(held_on(ctx, c["id"], doors, need_face=True)) == len(doors), 120, 2.0)
    rec.check("the contractor is put on the job's readers ahead of the window, with the window attached",
              bool(on), f"on {len(doors)} of {len(doors)} job readers, face included, {waited:.0f}s after the job was made" if on
              else f"on {len(held_on(ctx, c['id'], doors, need_face=True))} of {len(doors)} after {waited:.0f}s")
    extra = [n for n in ctx.readers_holding(c["id"], need_face=False) if n not in [t["number"] for t in doors]]
    rec.check("and on no other reader", not extra, "0 readers outside the job hold them" if not extra
              else f"readers outside the job that hold them: {extra}")

    # ---- 3. before the window ---------------------------------------------------------------------
    door = next(t for t in doors if t["hasPanel"])
    if utc_now() < start - timedelta(seconds=5):
        before = ctx.open_counts()[door["number"]]["all"]
        result = readers.scan(door["number"], c["id"])
        rec.step("before-window", f"{c['name']} tries {door['name']} before the window opens",
                 expect={"doors": {"state": "denied", "only": [f"{door['name']} panel"]}}, within=10)
        rec.check("before the window the door stays shut", not result["opened"]
                  and ctx.open_counts()[door["number"]]["all"] == before,
                  f"reader decision: event {result['event']} "
                  f"({'denied' if result['event'] == 6 else 'not identified' if result['event'] == 3 else 'other'}), "
                  f"{(start - utc_now()).total_seconds():.0f}s before the window")
    else:
        rec.check("before the window the door stays shut", False,
                  "the readers were not loaded until after the window had opened, so this could not be tried")

    # ---- 4. inside the window ----------------------------------------------------------------------
    wait = (start - utc_now()).total_seconds() + 3
    if wait > 0:
        time.sleep(wait)
    mono = time.monotonic()
    before = ctx.open_counts()[door["number"]]["face"]
    result = readers.scan(door["number"], c["id"])
    rec.step("inside-window", f"{c['name']} tries {door['name']} inside the window",
             expect={"doors": {"state": "welcome", "person": c["name"].split()[0], "only": [f"{door['name']} panel"]}},
             within=10)
    rec.check("inside the window the door opens, once", result["opened"]
              and ctx.open_counts()[door["number"]]["face"] == before + 1,
              f"reader decision: event {result['event']}; openings {ctx.open_counts()[door['number']]['face'] - before}")
    row, _ = wait_until(lambda: next((r for r in ctx.access_events(since_iso, door["id"])
                                      if r["logId"] == result["logId"]), None), 30, 0.5)
    _, page = ctx.api.get(f"/api/amico/terminals/{door['id']}/access-events?page=1&pageSize=10", expect=200)
    listed = next((e for e in page.get("data", []) if e.get("userId") == c["id"] and e.get("eventType") == "Granted"), None)
    rec.check("the entry is logged against the contractor and marked as a contractor",
              bool(row) and row["userId"] == c["id"] and bool(listed) and listed.get("isContractor") is True,
              f"access log row: {row['type'] if row else 'none'}; admin list says isContractor="
              f"{listed.get('isContractor') if listed else None}, vendor '{listed.get('vendorName') if listed else None}'")
    time.sleep(1.5)
    heard = [w for w in ctx.welcomes(mono) if w["terminalId"] == door["id"] and w["userId"] == c["id"]]
    rec.check("the panel greets them", len(heard) == 1 and heard[0]["eventType"] == "Granted",
              f"broadcasts: {[w['eventType'] for w in heard]}")

    other = ctx.terminals[18]
    before = ctx.open_counts()[18]["all"]
    result = readers.scan(18, c["id"])
    rec.check("a door that is not on the job stays shut even inside the window",
              not result["opened"] and ctx.open_counts()[18]["all"] == before,
              f"{other['name']}: reader decision event {result['event']}")

    # ---- 5. close-out ------------------------------------------------------------------------------
    counts_before = ctx.user_counts()
    t0 = time.monotonic()
    status, closed = end_job(ctx, task["id"], host, "Rehearsal: work finished")
    rec.step("job-closed", "The host closes the job out", expect={"doors": {}}, within=REMOVAL_BAR_SECONDS)
    c["openTaskIds"] = []
    ctx.save_world()
    dropped = ctx.wait_until_dropped(counts_before, [t["number"] for t in doors], 600)
    gone, waited = wait_until(lambda: not held_on(ctx, c["id"], doors), 60, 2.0)
    took = (time.monotonic() - t0) if dropped is None else dropped - t0
    rec.metric("close-out -> off the job's readers", round(took, 1) if gone else None, "s")
    rec.check(f"after close-out the contractor is off every job reader within {REMOVAL_BAR_SECONDS}s",
              status == 200 and bool(gone) and took <= REMOVAL_BAR_SECONDS,
              f"off {len(doors)} readers in {took:.0f}s" if gone else f"still on {held_on(ctx, c['id'], doors)} after {took:.0f}s")
    def confirmed():
        _, page = ctx.api.get(f"/api/havenzhub/tasks/{task['id']}/access/readers", account=host, expect=200)
        return page if page.get("removed") == page.get("total") and not page.get("pending") and not page.get("failed") else None
    confirm, waited = wait_until(confirmed, 60, 2.0)
    if not confirm:
        _, confirm = ctx.api.get(f"/api/havenzhub/tasks/{task['id']}/access/readers", account=host, expect=200)
    rec.check("the host's close-out page confirms every reader",
              confirm.get("removed") == confirm.get("total") and confirm.get("pending") == 0 and confirm.get("failed") == 0,
              f"removed {confirm.get('removed')} of {confirm.get('total')}, pending {confirm.get('pending')}, failed "
              f"{confirm.get('failed')}, {time.monotonic() - t0:.0f}s after close-out")
    before = ctx.open_counts()[door["number"]]["all"]
    result = readers.scan(door["number"], c["id"])
    rec.step("after-close-out", f"{c['name']} tries {door['name']} after close-out",
             expect={"doors": {"state": "denied", "only": [f"{door['name']} panel"]}}, within=10)
    rec.check("after close-out the door stays shut", not result["opened"]
              and ctx.open_counts()[door["number"]]["all"] == before, f"reader decision: event {result['event']}")

    # ---- 6. retention ------------------------------------------------------------------------------
    row = stack.psql(f"select extract(epoch from closed_at), extract(epoch from purge_due_at) "
                     f"from tasks.task_access where task_id = '{task['id']}'")
    days = (float(row[0][1]) - float(row[0][0])) / 86400 if row and row[0][1] else None
    faces = stack.psql_scalar(f"select count(*) from iot.facial_recognition where user_id = '{c['id']}'")
    rec.check("close-out sets a purge date one retention period away and keeps the face until then",
              days is not None and abs(days - 30) < 0.01 and faces != "0",
              f"purge due {days:.1f} days after close-out; stored face records now: {faces}" if days is not None
              else "no purge date was set")

    stack.psql(f"update tasks.task_access set purge_due_at = now() - interval '1 minute' "
               f"where task_id in (select task_id from tasks.task_contractors where contractor_profile_id = '{c['profileId']}') "
               f"and closed_at is not null")
    rec.step("retention-elapsed", "The retention period is treated as over (purge date moved to now in the database); "
             "the five-minute sweep is left to act")

    def purged(r):
        faces = stack.psql_scalar(f"select count(*) from iot.facial_recognition where user_id = '{c['id']}'")
        stamp = stack.psql_scalar(f"select face_purged_at is not null from identity.contractor_profiles where id = '{c['profileId']}'")
        live = stack.psql_scalar(f"select count(*) from identity.contractor_photo_reviews where contractor_profile_id = "
                                 f"'{c['profileId']}' and status in ('approved', 'pending')")
        r.check("once retention has ended the sweep deletes the contractor's face records",
                faces == "0" and stamp == "t" and live == "0",
                f"face records {faces}, profile marked purged: {stamp == 't'}, reviews still approved or pending: {live}")
    ctx.defer("contractor", 345, "contractor face deleted after retention ends (five-minute sweep)", purged)
