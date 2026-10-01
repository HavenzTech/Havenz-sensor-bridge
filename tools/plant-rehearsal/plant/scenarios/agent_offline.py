"""
Scenario 6 - the site agent goes offline.

The Pi in the communication room loses power. Doors must keep opening for faces (the readers
decide on their own), remote unlock must be refused cleanly rather than time out, the property's
health must say the agent is offline, and the people responsible must get exactly one alert - and
one all-clear when it comes back.
"""

import re
import time
import uuid

from .. import readers, stack
from ..util import iso, parse_iso, wait_until

SIMULATED = ("The agent container is killed outright (a power cut, not a shutdown), left down for about four "
             "minutes, then started again. The alert threshold is shortened to 120 s for the rehearsal; "
             "production is 300 s.")

REFUSE_AFTER = 90           # the backend's own rule: no word from the agent for 90 s = offline
ALERT_AFTER = 120           # rehearsal value of Alerts:AgentOfflineAfterSeconds (production 300)
SWEEP = 30                  # rehearsal value of Alerts:SweepIntervalSeconds (production 300)


def recipients_mail(ctx, since_iso, subject):
    return {r["email"]: ctx.sink_messages(since_iso, to=r["email"], subject=subject) for r in ctx.world["alertRecipients"]}


def try_unlock(ctx, account, terminal_id):
    t0 = time.monotonic()
    status, body = ctx.api.post(f"/api/amico/terminals/{terminal_id}/open", {"requestId": str(uuid.uuid4())},
                                account=account, timeout=40)
    return status, (body if isinstance(body, dict) else {}), time.monotonic() - t0


def agent_row(ctx):
    for agent in (ctx.health().get("siteAgents") or {}).get("agents", []):
        if agent["id"] == ctx.world["agentHub"]["id"]:
            return agent
    return None


def run(ctx, rec):
    account = ctx.account_for("shift-a-2")
    door = ctx.terminals[3]
    hub_id = ctx.world["agentHub"]["id"]
    since_iso = iso()

    status, body, took = try_unlock(ctx, account, door["id"])
    rec.check("with the agent up, a remote unlock works", status == 200, f"HTTP {status} in {took * 1000:.0f} ms")

    killed_at = ctx.vm_now()
    stack.agent_kill()
    t0 = time.monotonic()
    rec.step("agent-killed", "The site agent loses power",
             expect={"walls": {"note": "health tiles should turn to 'agent offline' within about two minutes"},
                     "doors": {"note": "panels are unaffected; faces still open doors"}})

    # ---- doors still open for faces --------------------------------------------------------------
    person = ctx.people["shift-b-3"]
    before = ctx.open_counts()[9]["face"]
    result = readers.scan(9, person["id"])
    rec.check("with the agent down, a face still opens its door", result["opened"]
              and ctx.open_counts()[9]["face"] == before + 1,
              f"reader decision: event {result['event']} - decided on the device, no agent needed")
    offline_tap = {"terminalId": ctx.terminals[9]["id"], "logId": result["logId"], "userId": person["id"]}

    # ---- remote unlock: refused, and after 90 s refused at once ------------------------------------
    status, body, took = try_unlock(ctx, account, door["id"])
    rec.check("a remote unlock right after the agent dies ends as a clear failure, and the door stays shut",
              status == 502 and body.get("outcome") == "failed",
              f"HTTP {status}, outcome {body.get('outcome')}, after {took:.1f}s: \"{str(body.get('message'))[:120]}\"")

    wait = REFUSE_AFTER + 8 - (time.monotonic() - t0)
    if wait > 0:
        time.sleep(wait)
    opens_before = ctx.open_counts()[3]["all"]
    status, body, took = try_unlock(ctx, account, door["id"])
    rec.step("remote-unlock-refused", "A remote unlock is tried more than 90 s after the agent went quiet")
    rec.check("after 90 s of silence a remote unlock is refused at once, saying the agent is offline",
              status == 502 and "offline" in str(body.get("message")).lower() and took < 3
              and ctx.open_counts()[3]["all"] == opens_before,
              f"HTTP {status} in {took * 1000:.0f} ms: \"{str(body.get('message'))[:150]}\"")

    # ---- health ----------------------------------------------------------------------------------
    def offline_in_health():
        h = ctx.health()
        agents = h.get("siteAgents") or {}
        return h if agents.get("offline") == 1 else None
    h, waited = wait_until(offline_in_health, 60, 3.0)
    row = agent_row(ctx)
    rec.check("the property's health shows the agent offline and turns critical",
              bool(h) and h.get("state") == "critical" and row and row.get("state") == "offline",
              f"health state {h.get('state') if h else '?'}; site agents: "
              f"{(h or {}).get('siteAgents', {}).get('online')} online / {(h or {}).get('siteAgents', {}).get('offline')} offline; "
              f"first reason: \"{((h or {}).get('reasons') or ['-'])[0] if h else '-'}\"")
    rec.metric("agent dies -> health says offline", round(time.monotonic() - t0, 0) if h else None, "s",
               "90 s rule plus a 30 s health cache; both production values")

    # ---- one alert --------------------------------------------------------------------------------
    def alert_open():
        rows = ctx.alert_rows(since_iso, rule="agent_offline", hub_id=hub_id)
        return rows or None
    rows, _ = wait_until(alert_open, ALERT_AFTER + SWEEP + 60 - (time.monotonic() - t0), 3.0)
    t_alert = (rows[0]["createdAt"] - killed_at) if rows else time.monotonic() - t0
    rec.step("agent-offline-alert", "The 'site agent offline' alert is raised",
             expect={"walls": {"banner": "live", "text": "Site agent offline", "tone": "critical"}}, within=30)
    rec.check("one critical 'site agent offline' alert is raised after the threshold",
              bool(rows) and len(rows) == 1 and rows[0]["severity"] == "critical" and t_alert >= ALERT_AFTER - 35,
              f"{len(rows or [])} alert(s), raised {t_alert:.0f}s after the agent died (threshold {ALERT_AFTER}s here, "
              f"checked every {SWEEP}s)")
    rec.metric("agent dies -> alert raised", round(t_alert, 0) if rows else None, "s",
               f"threshold {ALERT_AFTER}s + sweep {SWEEP}s here; 300 s + 300 s in production")

    def paged():
        got = recipients_mail(ctx, since_iso, "Site agent offline")
        got = {k: [m for m in v if "[Resolved]" not in m["subject"]] for k, v in got.items()}
        return got if all(len(v) >= 1 for v in got.values()) else None
    got, _ = wait_until(paged, 40, 2.0)
    mailed = min((parse_iso(m["receivedAt"]).timestamp() for v in (got or {}).values() for m in v), default=None)
    rec.metric("agent dies -> alert email in the mailbox", round(mailed - killed_at, 0) if mailed else None, "s")

    time.sleep(2 * SWEEP + 15)               # two more sweeps: would a second page have gone out?
    mail = recipients_mail(ctx, since_iso, "Site agent offline")
    mail = {k: [m for m in v if "[Resolved]" not in m["subject"]] for k, v in mail.items()}
    rows = ctx.alert_rows(since_iso, rule="agent_offline", hub_id=hub_id)
    rec.check("each recipient is paged once and not again while it stays offline",
              all(len(v) == 1 for v in mail.values()) and len(rows) == 1,
              f"emails per recipient after two more checks: { {k.split('@')[0]: len(v) for k, v in mail.items()} }; "
              f"{len(rows)} alert row(s)")

    # ---- back -------------------------------------------------------------------------------------
    started_at = ctx.vm_now()
    stack.agent_start()
    t1 = time.monotonic()
    rec.step("agent-back", "The site agent has power again", expect={"walls": {"banner": "none"}}, within=90)

    def resolved():
        r = ctx.alert_rows(since_iso, rule="agent_offline", hub_id=hub_id)
        return r if r and r[0]["resolvedAt"] else None
    done, waited = wait_until(resolved, 120, 2.0)
    if done:
        waited = done[0]["resolvedAt"] - started_at
    rec.check("the alert resolves on the agent's next heartbeat", bool(done),
              f"resolved {waited:.0f}s after the agent was started" if done else f"still open after {waited:.0f}s")
    rec.metric("agent back -> alert resolved", round(waited, 1) if done else None, "s")

    def all_clear():
        got = recipients_mail(ctx, since_iso, "[Resolved] Site agent offline")
        return got if all(len(v) >= 1 for v in got.values()) else None
    got, _ = wait_until(all_clear, 40, 2.0)
    final = recipients_mail(ctx, since_iso, "[Resolved] Site agent offline")
    rec.check("each recipient gets one all-clear", all(len(v) == 1 for v in final.values()),
              f"resolved emails per recipient: { {k.split('@')[0]: len(v) for k, v in final.items()} }")
    if final and any(final.values()):
        text = re.sub(r"<[^>]+>", " ", next(iter(v for v in final.values() if v))[0]["text"])
        sentence = re.search(r"Site agent[^.]*back after[^.]*\.[^.]*\.", " ".join(text.split()))
        if sentence:
            rec.note("the all-clear says: \"" + sentence.group(0).strip() + "\"")

    def healthy():
        row = agent_row(ctx)
        return row if row and row.get("state") == "online" else None
    row, waited = wait_until(healthy, 90, 3.0)
    rec.check("health shows the agent online again", bool(row), f"online in health {time.monotonic() - t1:.0f}s after restart"
              if row else "still not online in health after 90 s")
    status, body, took = try_unlock(ctx, account, door["id"])
    rec.check("remote unlock works again", status == 200, f"HTTP {status} in {took * 1000:.0f} ms")

    # ---- the tap made while it was down -------------------------------------------------------------
    def recovered():
        return [r for r in ctx.access_events(since_iso, offline_tap["terminalId"]) if r["logId"] == offline_tap["logId"]]
    found, waited = wait_until(recovered, 120, 3.0)
    rec.check("the badge-in made while the agent was down is recovered into the access log, once",
              bool(found) and len(found) == 1 and found[0]["userId"] == offline_tap["userId"],
              f"{len(found or [])} row(s), {waited:.0f}s after the agent came back (read from the reader's own log)")


def cleanup(ctx, rec):
    stack.agent_start()
