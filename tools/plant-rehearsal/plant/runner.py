"""Runs scenarios in order, records each one, settles the checks that had to wait, builds the report."""

import importlib
import os
import time
import traceback

from . import apps, config, record, report, seed as seed_mod
from .context import Context
from .util import iso

# name -> (module, title). Order is the order a full run uses.
SCENARIOS = [
    ("shift-change", "shift_change", "Shift change: 40 people badge in across 20 doors in two minutes"),
    ("refusals", "refusals", "Refusals: unknown face, no access, expired access, a leaver deactivated mid-run"),
    ("contractor", "contractor", "Contractor lifecycle: before the window, inside it, after close-out"),
    ("remote-unlock", "remote_unlock", "Remote unlock: retried five times opens once; a request that dies ends defined"),
    ("leak", "leak", "Leak: wet, page after the confirmation window, splash, lapping probe, silent sensor"),
    ("agent-offline", "agent_offline", "Site agent offline: refusal, health, one alert, no re-page, resolve"),
    ("internet-cut", "internet_cut", "Internet cut between agent and backend while people badge in"),
    ("backend-restart", "backend_restart", "Backend restart mid-shift"),
    ("hung-reader", "hung_reader", "A reader that stops answering, and one that loses power"),
    ("emergency", "emergency", "Emergency: company evacuation, one area only, all-clear, reconnect after missing it"),
]
OPTIONAL = [
    ("soak", "soak", "Soak: steady badge-ins and sensor feed, watching for anything that grows"),
]


def list_scenarios():
    for name, _, title in SCENARIOS + OPTIONAL:
        print(f"  {name:<16} {title}")


def parse_duration(text):
    text = text.strip().lower()
    unit = {"s": 1, "m": 60, "h": 3600}.get(text[-1])
    return int(float(text[:-1]) * unit) if unit else int(text)


def lock_path():
    return config.stack_dir() / "run.lock"


def acquire_lock(chosen, wait):
    """
    One run at a time. Two people running scenarios against the same plant at once make each
    other's counts wrong (a contractor tapping a door in the middle of someone else's shift
    change is an extra opening), so a second run is refused - or, with --wait, queued.
    """
    path = lock_path()
    announced = False
    while True:
        held = record.load_json(path)
        if not held or not apps.pid_alive(held.get("pid")):
            break
        if not wait:
            raise SystemExit(f"another run is in progress (pid {held['pid']}, started {held['startedAt']}, scenarios: "
                             f"{', '.join(held['scenarios'])}). Wait for it, or use `run --wait`.")
        if not announced:
            print(f"[run] waiting for the run in progress (pid {held['pid']}: {', '.join(held['scenarios'])})", flush=True)
            announced = True
        time.sleep(5)
    record.save_json(path, {"pid": os.getpid(), "startedAt": iso(), "scenarios": chosen})


def release_lock():
    held = record.load_json(lock_path())
    if held and held.get("pid") == os.getpid():
        lock_path().unlink(missing_ok=True)


def run(names, run_dir=None, soak=None, options=None, wait=False):
    run_dir = record.current_run_dir(run_dir)
    known = {n: (m, t) for n, m, t in SCENARIOS + OPTIONAL}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)} - `rehearsal.py run --list` shows them")
    chosen = list(names) if names else [n for n, _, _ in SCENARIOS]
    if soak and "soak" not in chosen:
        chosen.append("soak")

    acquire_lock(chosen, wait)
    try:
        return _run_locked(chosen, known, run_dir, soak, options)
    finally:
        release_lock()


def _run_locked(chosen, known, run_dir, soak, options):
    ctx = Context(run_dir)
    ctx.options = dict(options or {})
    if soak:
        ctx.options["soakSeconds"] = parse_duration(soak)
    seed_mod.ensure_feeder(run_dir)
    ctx.timeline.write("_run", "start", f"scenarios: {', '.join(chosen)}", scenarios=chosen)
    print(f"[run] {run_dir}", flush=True)

    records = {}
    for name in chosen:
        module_name, title = known[name]
        module = importlib.import_module(f"plant.scenarios.{module_name}")
        rec = record.ScenarioRecord(name, title, module.SIMULATED, run_dir, ctx.timeline)
        records[name] = rec
        print(f"\n== {title}", flush=True)
        rec.start()
        try:
            ctx.connect_hub()
            module.run(ctx, rec)
        except Exception as e:  # noqa: BLE001 - a scenario that blows up is a failed scenario, not a failed run
            rec.error = f"{type(e).__name__}: {e}"
            rec.check("scenario ran to the end", False, f"stopped by {rec.error}")
            traceback.print_exc()
        finally:
            try:
                cleanup = getattr(module, "cleanup", None)
                if cleanup:
                    cleanup(ctx, rec)
            except Exception as e:  # noqa: BLE001
                rec.note(f"cleanup problem: {e}")
        rec.end()
        record.save_scenario(run_dir, rec)

    # Checks that had to wait on a slow job (a five-minute sweep, say) are settled last, so the
    # waiting overlaps the other scenarios instead of stretching each one.
    for item in sorted(ctx.deferred, key=lambda d: d["due"]):
        rec = records.get(item["scenario"])
        if rec is None:
            continue
        wait = item["due"] - time.monotonic()
        if wait > 0:
            print(f"\n[run] waiting {wait:.0f}s to settle: {item['describe']}", flush=True)
            time.sleep(wait)
        try:
            item["fn"](rec)
        except Exception as e:  # noqa: BLE001
            rec.check(item["describe"], False, f"could not be checked: {e}")
        rec.ended_at = iso()
        record.save_scenario(run_dir, rec)
        ctx.timeline.write(item["scenario"], "settled", item["describe"], result=rec.result())

    ctx.timeline.write("_run", "end", ", ".join(f"{n}: {r.result()}" for n, r in records.items()),
                       results={n: r.result() for n, r in records.items()})
    ctx.close()
    report.build(run_dir)
    failed = [n for n, r in records.items() if r.result() != "pass"]
    print(f"\n[run] {len(records) - len(failed)} of {len(records)} scenarios passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""), flush=True)
    return 1 if failed else 0
