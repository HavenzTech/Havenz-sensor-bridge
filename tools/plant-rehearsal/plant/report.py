"""Builds REPORT.md and findings.md from a run's results. Reads only; safe to run again at any time."""

from pathlib import Path

from . import config, findings as findings_mod, plantdef, record, stack
from .util import iso

ORDER = ["commissioning", "shift-change", "refusals", "contractor", "remote-unlock", "leak", "agent-offline",
         "internet-cut", "backend-restart", "hung-reader", "emergency", "soak"]

# The contract's timings table: (label, scenario, metric name, how to read it)
TIMINGS = [
    ("Tap -> door open (face)", "shift-change", "tap -> door open, p95",
     "decided on the reader; the stand-in reader's time, not HID's"),
    ("Tap -> row in the access log (p50 / p95)", "shift-change",
     ("tap -> row in the access log, p50", "tap -> row in the access log, p95"), "production path, production timing"),
    ("Tap -> welcome broadcast for the panel (p50 / p95)", "shift-change",
     ("tap -> welcome broadcast for the panel, p50", "tap -> welcome broadcast for the panel, p95"),
     "production path; what the panel then takes to paint is in the screen half's record"),
    ("Remote unlock: tap -> the door actually opens (typical)", "remote-unlock",
     "remote unlock: tap -> the door actually opens, typical", "measured at the reader"),
    ("Remote unlock: tap -> the app has its answer (typical / slowest)", "remote-unlock",
     ("remote unlock: tap -> the app has its answer, typical", "remote unlock: tap -> the app has its answer, slowest of 8"),
     "production path; includes the agent recording the command before it reports"),
    ("Leak -> incident exists (what a banner is drawn from)", "leak", "leak -> incident visible", "production timing"),
    ("Leak -> page in the mailbox", "leak", "leak -> page (email in the mailbox)",
     "30 s hold is the production value; delivery worker wakes every 5 s here, 30 s in production"),
    ("Sensor goes quiet -> marked stale on screens", "leak", "last report -> marked stale on screens",
     "COMPRESSED: 30 s reporting interval here; production devices default to 900 s"),
    ("Agent dies -> health says offline", "agent-offline", "agent dies -> health says offline", "production timing"),
    ("Agent dies -> alert raised", "agent-offline", "agent dies -> alert raised",
     "COMPRESSED: threshold 120 s + check every 30 s here; 300 s + 300 s in production"),
    ("Agent back -> alert resolved", "agent-offline", "agent back -> alert resolved", "production timing"),
    ("Internet back -> queued door events all recorded", "internet-cut",
     "link back -> all queued events in the access log", "production timing"),
    ("Deactivation -> off all 20 readers", "refusals", "deactivation -> off all 20 readers", "production timing"),
    ("Contractor close-out -> off the job's readers", "contractor", "close-out -> off the job's readers", "production timing"),
    ("Backend down for", "backend-restart", "backend down for", "container kill and start on this machine"),
    ("Emergency announce -> screens told (company)", "emergency", "announce -> screens told (company scope)", "production timing"),
    ("Screen reconnect -> correct state (all-clear replay)", "emergency",
     "reconnect -> correct emergency state (all-clear replay)", "production timing"),
]

LIMITS = [
    ("Real reader firmware", "The readers are a stand-in that answers the HID Amico's local API the way the one "
     "reader on the bench has been seen to answer. How a real reader behaves under twenty-way load, what it does "
     "with a dated access window at the minute it ends, how long it takes to recognise a face, and any payload it "
     "sends that has not been observed yet are outside what this shows."),
    ("Face recognition", "Nothing here recognises a face. A 'tap' tells the stand-in reader which enrolled person "
     "is standing there; the reader then applies its roster, group and window rules. False accepts, false rejects "
     "and photo quality are not rehearsed."),
    ("The plant network", "Everything runs on one machine. Latency, packet loss, the PoE switch, the reader subnet "
     "mask, DHCP and the real internet uplink are not present. The 'internet cut' removes the agent's route to the "
     "backend cleanly; a flapping or half-working link is a different failure."),
    ("The Raspberry Pi", "The agent is the real add-on, built from its own Dockerfile and started by its own entry "
     "point, but on a desktop CPU and SSD. SD-card write speed, the Pi's memory and its clock at boot are not rehearsed. "
     "Home Assistant's Supervisor is stood in for by a file (the add-on's options) and a published port (its pairing page)."),
    ("Screens' hardware", "The screen half drives real browsers, not Tizen panels or mini-PCs. Whether a Tizen panel "
     "keeps its device key through a hard power cut is still a bench test on the real panel."),
    ("Engine data", "Engine and meter readings are generated. There is still no real data path from the gensets; "
     "units, signal names and fault codes are assumptions until one exists."),
    ("Email, push and storage", "Mail goes to a local sink; push is recorded but sent nowhere; the documents bucket is "
     "a local stand-in for Google Cloud Storage. Deliverability, spam filtering, phone notifications arriving, and "
     "bucket permissions are not shown."),
    ("One backend instance", "Production can run several instances behind a load balancer. The rehearsal runs one, "
     "so anything that depends on two instances sharing state (live-update fan-out across instances, in-memory rate "
     "limits and caches) is not exercised."),
    ("Scale beyond AHI", "Twenty doors, sixty people, twenty-three screens. Numbers here say nothing about two hundred doors."),
]


def fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.1f}" if abs(value) < 100 and value != int(value) else f"{value:,.0f}"
    return str(value)


def metric(scenario, name):
    m = (scenario or {}).get("metrics", {}).get(name)
    if not m:
        return None
    return f"{fmt(m['value'])} {m['unit']}".strip()


def build(run_dir=None):
    run_dir = record.current_run_dir(run_dir)
    results = record.load_json(Path(run_dir) / "results.json", {"scenarios": {}})
    world = record.load_json(Path(run_dir) / "world.json", {})
    state = stack.load_state()
    scenarios = results.get("scenarios", {})
    found = findings_mod.collect(results, world, state)
    backend = world.get("backend") or state.get("backend") or {}
    apps = state.get("apps") or {}

    out = []
    w = out.append
    w("# Plant rehearsal - AHI, simulated")
    w("")
    w(f"Run `{world.get('runId', Path(run_dir).name)}` - report written {iso()[:19].replace('T', ' ')} UTC.")
    w("")
    w("The whole AHI deployment running on one machine: the real backend, the real site agent, twenty stand-in door "
      "readers on their own LAN, fifteen sensors and engines on the real ingest path, and scripted scenarios that each "
      "end in hard assertions. Nothing was changed in the product to make a scenario pass; where the product fell "
      "short, the scenario is red and the reason is in `findings.md`.")
    w("")
    w("| What ran | Version |")
    w("|---|---|")
    w(f"| Backend (HavenzBMS) | `{backend.get('branch', '?')}` at `{str(backend.get('commit', '?'))[:7]}`, "
      f"{backend.get('migrations', '?')} migration files up to `{backend.get('highestMigration', '?')}`, built as the production image |")
    w("| Site agent | the add-on in this repository (`havenz-agent`, version 0.5.0), from its own Dockerfile |")
    w(f"| Wall app | `{str((apps.get('dashboards') or {}).get('commit', 'not started'))[:7]}`, production build |")
    w(f"| Door panel page | `{str((apps.get('door') or {}).get('commit', 'not started'))[:7]}` |")
    w(f"| Plant | {len(world.get('terminals', []))} readers, {len(world.get('screens', []))} screens, "
      f"{len(world.get('devices', []))} sensors and engines, {len(world.get('people', []))} people |")
    w("")

    # ---- summary -------------------------------------------------------------------------------
    w("## Result")
    w("")
    screen = (record.load_json(Path(run_dir) / "screen-results.json", {}) or {}).get("scenarios") or {}
    w("| Scenario | Result | Assertions | Findings it raised | On the screens | What it is |")
    w("|---|---|---|---|---|---|")
    ran = [n for n in ORDER if n in scenarios]
    for name in ran:
        s = scenarios[name]
        passed = sum(1 for a in s["assertions"] if a["passed"])
        mark = "PASS" if s["result"] == "pass" else "**FAIL**"
        raised = ", ".join(sorted({a["finding"] for a in s["assertions"] if a.get("finding") and not a["passed"]})) or "-"
        seen = screen.get(name) or {}
        on_screens = f"{seen.get('met')} of {seen.get('checked')} steps as expected" if seen.get("checked") else "-"
        w(f"| {name} | {mark} | {passed} of {len(s['assertions'])} | {raised} | {on_screens} | {s['title']} |")
    w("")
    w("A scenario is red when any one of its assertions is. Every red assertion in this run is tied to a finding, "
      "named in the table; a red assertion with no finding would be a fault in the rehearsal itself."
      if all(a.get("finding") for n in ran for a in scenarios[n]["assertions"] if not a["passed"])
      else "A scenario is red when any one of its assertions is. Red assertions that are NOT tied to a finding below "
           "need reading before anything else: " + "; ".join(
               f"{n}: {a['name']}" for n in ran for a in scenarios[n]["assertions"]
               if not a["passed"] and not a.get("finding")) + ".")
    if screen:
        w("")
        w("\"On the screens\" is the screen fleet's own count of timeline steps where every targeted wall and door "
          "panel showed what the step expected; its record and film are in this folder (`screen-results.json`, `video/`).")
    not_run = [n for n in ORDER if n not in scenarios and n != "soak"]
    if not_run:
        w("")
        w("Not run in this folder: " + ", ".join(not_run) + ".")
    w("")
    if found:
        counts = {}
        for f in found:
            counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        w("**Findings:** " + ", ".join(f"{v} {k}" for k, v in counts.items()) + " - see below and `findings.md`.")
        w("")

    # ---- timings ---------------------------------------------------------------------------------
    w("## Timings")
    w("")
    w("| What | Measured | How to read it |")
    w("|---|---|---|")
    for label, scenario, names, note in TIMINGS:
        s = scenarios.get(scenario)
        if not s:
            continue
        if isinstance(names, tuple):
            values = [metric(s, n) for n in names]
            value = " / ".join(v or "n/a" for v in values)
        else:
            value = metric(s, names) or "n/a"
        w(f"| {label} | {value} | {note} |")
    w("")
    w("Anything marked COMPRESSED was measured across a timer the rehearsal shortened; it is not what production "
      "will take. The full list of shortened timers is at the end.")
    w("")

    # ---- findings table ----------------------------------------------------------------------------
    w("## Findings")
    w("")
    if found:
        w("| | Severity | Finding | Seen in |")
        w("|---|---|---|---|")
        for f in found:
            w(f"| {f['id']} | {f['severity']} | {f['title']} | {f['where']} |")
        w("")
        w("Each one is written up in `findings.md` with how to reproduce it, the evidence from this run, and where in "
          "the product it comes from.")
    else:
        w("None observed in this run.")
    w("")

    # ---- per scenario --------------------------------------------------------------------------------
    w("## Scenario by scenario")
    for name in ran:
        s = scenarios[name]
        w("")
        w(f"### {name} - {'PASS' if s['result'] == 'pass' else 'FAIL'}")
        w("")
        w(f"*{s['title']}*")
        w("")
        if s.get("simulated"):
            w(f"**Simulated:** {s['simulated']}")
            w("")
        w("| | Assertion | What was seen |")
        w("|---|---|---|")
        for a in s["assertions"]:
            detail = a["detail"].replace("|", "\\|").replace("\n", " ")
            tag = f" ({a['finding']})" if a.get("finding") else ""
            w(f"| {'pass' if a['passed'] else '**FAIL**'} | {a['name']}{tag} | {detail} |")
        if s.get("metrics"):
            w("")
            w("| Measured | Value | Note |")
            w("|---|---|---|")
            for key, m in s["metrics"].items():
                w(f"| {key} | {fmt(m['value'])} {m['unit']} | {m.get('note', '')} |")
        for note in s.get("notes", []):
            w("")
            w(f"Note: {note}")
        if s.get("error"):
            w("")
            w(f"The scenario stopped early: `{s['error']}`")

    # ---- overrides -----------------------------------------------------------------------------------
    w("")
    w("## Every setting that differs from production")
    w("")
    w("| Kind | Setting | Here | Production | Why |")
    w("|---|---|---|---|---|")
    for kind, setting, here, prod, why in config.OVERRIDES:
        w(f"| {kind} | `{setting}` | {here} | {prod} | {why} |")
    w("")
    w("**isolation** keeps the rehearsal on this machine and changes where things go, not how the product behaves. "
      "**time** shortens a wait; anything measured across one is marked COMPRESSED above. **setup** is a choice an "
      "administrator could make differently. Rate limits are not in the table because none was changed.")
    w("")
    w("Not rehearsed on purpose: " + "; ".join(f"`{k}` ({v})" for k, v in plantdef.SKIPPED_TEMPLATES.items()) + ".")

    # ---- limits ----------------------------------------------------------------------------------------
    w("")
    w("## What a simulation cannot prove")
    w("")
    for title, text in LIMITS:
        w(f"- **{title}.** {text}")
    w("")
    screens = Path(run_dir) / "screens.jsonl"
    w("## The screen half")
    w("")
    if screens.exists():
        w("The screen fleet's probe log (`screens.jsonl`), frames and video are in this folder; what each wall and door "
          "panel actually showed at each timeline step is recorded there. Both halves ran on the same desktop, so the "
          "23 browsers and their recording compete with the backend for the processor. Timings above that come from "
          "the backend's own clock (row times, mail arrival) are not stretched by that; the two that are taken at the "
          "harness (tap to welcome broadcast, remote unlock answer) can be.")
    else:
        w("No screen probe log (`screens.jsonl`) is in this folder: the scenarios above were asserted at the API, "
          "database and device level only. The timeline (`timeline.jsonl`) carries, for every step, what a correct "
          "screen should show, for the screen fleet to assert against.")
    w("")
    (Path(run_dir) / "REPORT.md").write_text("\n".join(out), encoding="utf-8")

    # ---- findings.md -------------------------------------------------------------------------------------
    fo = []
    f_ = fo.append
    f_("# Findings")
    f_("")
    f_(f"Run `{world.get('runId', Path(run_dir).name)}`. Product behaviour the rehearsal showed to be wrong, risky or "
       "surprising for the plant. None of these was fixed here; each says how to see it again and where it comes from.")
    f_("")
    if not found:
        f_("None observed in this run.")
    for f in found:
        f_(f"## {f['id']} - {f['title']}")
        f_("")
        f_(f"**Severity:** {f['severity']}  ")
        f_(f"**Seen in:** {f['where']}")
        f_("")
        f_(f"**Reproduce:** {f['reproduction']}")
        f_("")
        f_("**What this run saw:**")
        f_("")
        for item in f["observed"]:
            for i, line in enumerate(item.split("\n")):
                f_(("- " if i == 0 else "  ") + line)
        if f.get("evidence"):
            f_("")
            f_("**Where in the product:**")
            f_("")
            for e in f["evidence"]:
                f_(f"- {e}")
        f_("")
        f_(f"**Suspected cause:** {f['cause']}")
        f_("")
    (Path(run_dir) / "findings.md").write_text("\n".join(fo), encoding="utf-8")
    results["findings"] = [{k: v for k, v in f.items()} for f in found]
    record.save_json(Path(run_dir) / "results.json", results)
    print(f"[report] {Path(run_dir) / 'REPORT.md'}", flush=True)
    return found
