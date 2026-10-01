"""
Scenario 11 (optional) - soak.

Nothing dramatic happens: every reader produces a badge-in on a timer and the sensor feed keeps
posting, for as long as asked (`run --soak 30m`). The point is what creeps: a queue that grows, an
outbox that backs up, memory that only goes up, errors that start appearing in the logs.
A sample is taken every 30 seconds and kept in `soak.jsonl`.
"""

import json
import time
from pathlib import Path

from .. import readers, stack
from ..util import iso

SIMULATED = ("All 20 readers invent a badge-in every 20 s (one of the people each holds, in turn) and the sensor "
             "feed posts every 15 s, for the requested time. Sampled every 30 s: the agent's event queue, the "
             "command queue, the alert and contractor outboxes, outstanding reader syncs, memory per container, "
             "and error lines in the backend and agent logs.")

SCAN_EVERY = 20
SAMPLE_EVERY = 30


def sample(ctx, since_iso, t0):
    counts = stack.psql("""
select
  (select count(*) from iot.agent_commands where status in ('queued', 'leased')),
  (select count(*) from iot.alert_deliveries where status in ('pending', 'failed')),
  (select count(*) from notifications.deliveries where status in ('pending', 'failed')),
  (select count(distinct (user_id, terminal_id)) from iot.terminal_user_syncs where status <> 'Succeeded'),
  (select count(*) from iot.amico_access_events where created_at >= '%s'),
  (select count(*) from iot.agent_commands where status in ('expired', 'unknown') and created_at >= '%s'),
  (select count(*) from pg_stat_activity where datname = 'havenzhub')
""" % (since_iso, since_iso)) or [["0"] * 7]
    c = [int(x) for x in counts[0]]
    status = ctx.agent_status() or {}
    events = status.get("events") or {}
    state = readers.state()
    api_log = stack.container_logs("api", since=since_iso)
    agent_log = stack.container_logs("agent", since=since_iso)
    return {
        "t": iso(), "minute": round((time.monotonic() - t0) / 60, 1),
        "scans": state["soakScans"],
        "readerNotifyFailed": sum(r["notifyFailed"] for r in state["readers"]),
        "accessEvents": c[4],
        "agentQueuePending": events.get("pending"), "agentQueueDropped": events.get("dropped"),
        "agentSecondsSinceHeartbeat": status.get("seconds_since_heartbeat"),
        "commandsWaiting": c[0], "commandsExpiredOrUnknown": c[5],
        "alertOutboxBacklog": c[1], "contractorOutboxBacklog": c[2], "syncsOutstanding": c[3],
        "dbConnections": c[6],
        "memApiMb": stack.container_memory_mb("api"), "memAgentMb": stack.container_memory_mb("agent"),
        "memDbMb": stack.container_memory_mb("db"), "memReadersMb": stack.container_memory_mb("readers"),
        "apiErrorLines": sum(1 for line in api_log.splitlines() if line.startswith(("fail:", "crit:"))),
        "agentWarningLines": sum(1 for line in agent_log.splitlines() if " WARNING " in line or " ERROR " in line),
        "agent429": stack.agent_refusals(text=agent_log)["total"],
    }


def slope_per_hour(points):
    """Least-squares trend of (minute, value) points, in units per hour. None with too few points."""
    if len(points) < 4:
        return None
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    denom = sum((x - mx) ** 2 for x, _ in points)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in points) / denom * 60


def run(ctx, rec):
    seconds = int(ctx.options.get("soakSeconds") or 1800)
    since_iso = iso()
    out = Path(ctx.run_dir) / "soak.jsonl"
    t0 = time.monotonic()
    baseline = sample(ctx, since_iso, t0)
    scans_before = baseline["scans"]
    notify_failed_before = baseline["readerNotifyFailed"]
    readers.scan_every(SCAN_EVERY)
    rec.step("soak-begin", f"Steady load for {seconds // 60} min: every reader badges someone in every {SCAN_EVERY}s",
             expect={"doors": {"state": "welcome", "note": "each panel greets someone every 20 s"},
                     "walls": {"live": True}}, within=seconds)
    samples = []
    try:
        while time.monotonic() - t0 < seconds:
            time.sleep(SAMPLE_EVERY)
            s = sample(ctx, since_iso, t0)
            samples.append(s)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(s) + "\n")
            if len(samples) % 4 == 0:
                print(f"      soak {s['minute']:.0f} min: scans {s['scans'] - scans_before}, events {s['accessEvents']}, "
                      f"agent queue {s['agentQueuePending']}, commands waiting {s['commandsWaiting']}, "
                      f"api {s['memApiMb']:.0f} MB, agent {s['memAgentMb']:.0f} MB, api errors {s['apiErrorLines']}", flush=True)
            try:
                if not ctx.hub.connected:
                    ctx.connect_hub()
            except Exception:  # noqa: BLE001
                pass
    finally:
        readers.scan_every(None)
    rec.step("soak-end", "Load stopped; queues are given a minute to drain", expect={"doors": {"state": "idle"}})
    time.sleep(60)
    last = sample(ctx, since_iso, t0)
    scans = last["scans"] - scans_before

    rec.metric("badge-ins generated", scans, "taps", f"over {seconds / 60:.0f} min on 20 doors")
    rec.check("every badge-in reached the access log, none twice",
              last["accessEvents"] == scans, f"{last['accessEvents']} rows for {scans} badge-ins")
    unanswered = last["readerNotifyFailed"] - notify_failed_before
    broken = stack.container_logs("agent", since=since_iso).count("BrokenPipeError")
    rec.check("the agent answers every reader's event post within the reader's five seconds", unanswered == 0,
              f"{unanswered} of {scans} posts were not answered in time ({broken} 'broken pipe' entries in the agent's log: "
              "it had kept the event and answered a reader that had already hung up)" if unanswered
              else f"all {scans} posts answered", finding="F-AGENT-JOURNAL-STALL")
    peak_queue = max((s["agentQueuePending"] or 0) for s in samples) if samples else 0
    rec.metric("agent event queue, peak", peak_queue, "events")
    rec.check("the agent's event queue does not grow", (last["agentQueuePending"] or 0) == 0 and peak_queue < 50
              and (last["agentQueueDropped"] or 0) == 0,
              f"peak {peak_queue}, {last['agentQueuePending']} at the end, {last['agentQueueDropped']} dropped")
    peak_cmd = max(s["commandsWaiting"] for s in samples) if samples else 0
    rec.metric("door commands waiting, peak", peak_cmd, "commands")
    rec.check("the door command queue does not back up", last["commandsWaiting"] <= 20 and peak_cmd <= 60,
              f"peak {peak_cmd} waiting, {last['commandsWaiting']} at the end")
    rec.check("no door command expires or ends unknown under steady load", last["commandsExpiredOrUnknown"] == 0,
              f"{last['commandsExpiredOrUnknown']} command(s) expired uncollected or ended unknown")
    rec.check("the alert and contractor outboxes stay empty", last["alertOutboxBacklog"] == 0 and last["contractorOutboxBacklog"] == 0,
              f"alert outbox {last['alertOutboxBacklog']}, contractor outbox {last['contractorOutboxBacklog']} waiting at the end")
    rec.check("the agent is never refused for sending too much", last["agent429"] == 0,
              f"{last['agent429']} agent call(s) refused with HTTP 429 during the soak", finding="F-AGENT-RATE-LIMIT")

    for name, key in (("backend", "memApiMb"), ("site agent", "memAgentMb"), ("database", "memDbMb")):
        series = [(s["minute"], s[key]) for s in samples if s.get(key) is not None]
        settled = series[len(series) // 3:]                 # the first third is warm-up
        slope = slope_per_hour(settled)
        if slope is None:
            continue
        mean = sum(v for _, v in settled) / len(settled)
        allowed = max(100.0, 0.15 * mean)
        rec.metric(f"{name} memory", f"{settled[0][1]:.0f} -> {settled[-1][1]:.0f}", "MB",
                   f"trend {slope:+.0f} MB an hour over the last two-thirds of the soak")
        rec.check(f"{name} memory is not climbing", slope <= allowed,
                  f"trend {slope:+.0f} MB an hour (allowed {allowed:.0f}); {settled[0][1]:.0f} MB at the end of warm-up, "
                  f"{settled[-1][1]:.0f} MB at the end")
    rec.metric("database connections in use, peak", max(s["dbConnections"] for s in samples) if samples else None, "connections")
    api_errors = last["apiErrorLines"]
    api_text = stack.container_logs("api", since=since_iso) if api_errors else ""
    poller = api_text.count("Error during Amico access log polling")
    rec.check("the backend logs no errors", api_errors == 0,
              f"{api_errors} error line(s) in the backend log during the soak"
              + (f"; {poller} of them are the reader log read failing on a duplicate key" if poller else ""),
              finding="F-POLLER-RACE" if poller else None)
    rec.metric("agent warnings logged", last["agentWarningLines"], "lines")
    if api_errors:
        text = stack.container_logs("api", since=since_iso)
        lines = [line for line in text.splitlines() if line.startswith(("fail:", "crit:"))]
        kinds = {}
        for line in lines:
            kinds[line[:90]] = kinds.get(line[:90], 0) + 1
        rec.note("backend error lines: " + "; ".join(f"{v}x {k}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:5]))


def cleanup(ctx, rec):
    readers.scan_every(None)
