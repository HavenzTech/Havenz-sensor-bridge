"""
The findings register.

A finding is a product behaviour the rehearsal showed to be wrong, risky or surprising for the
plant. Nothing here is fixed by the rehearsal - each entry says how to see it again, where in the
product it comes from, and how much it matters. An entry only appears in a run's findings.md when
that run actually observed it: either an assertion that names it failed, or its own check (the
`seen` function) found it again.

Severity:
    blocks go-live   the plant should not be commissioned until this is dealt with
    should fix       will hurt in operation; fix before or soon after go-live
    note             worth knowing; a trap for whoever installs or operates
"""

import re
from pathlib import Path

from . import config

BLOCKS, SHOULD, NOTE = "blocks go-live", "should fix", "note"


def _failed(results, finding_id):
    """Assertions in this run that failed and named this finding."""
    hits = []
    for name, scenario in (results.get("scenarios") or {}).items():
        for a in scenario.get("assertions", []):
            if a.get("finding") == finding_id and not a.get("passed"):
                hits.append((name, a))
    return hits


def _metric(results, scenario, name):
    m = ((results.get("scenarios") or {}).get(scenario) or {}).get("metrics", {}).get(name)
    return None if not m else (m.get("value"), m.get("unit"), m.get("note"))


def _enrolment_numbers(state, world, results):
    """What enrolling the roster cost, beyond the failed assertion itself."""
    bulk = world.get("bulkEnrolment") or {}
    lines = []
    if bulk.get("pairs"):
        lines.append(f"bulk grant, {bulk.get('waitSeconds', 0) / 60:.0f} min in: {bulk.get('onReadersAfterWait')} of "
                     f"{bulk['pairs']} person-on-reader pairs done; {bulk.get('commandsExpiredUncollected')} reader commands "
                     f"expired uncollected and {bulk.get('commandsEndedUnknown')} ended 'unknown'; the agent discarded "
                     f"{bulk.get('agentDiscardedAsExpired', 'some')} as already expired when it collected them")
    people = _metric(results, "commissioning", "paced enrolment: people put on their readers")
    typical = _metric(results, "commissioning", "paced enrolment: per person, typical")
    if people and typical:
        lines.append(f"finished by hand at the pace the queue can take: {people[0]} people, {people[2]}, about "
                     f"{typical[0]:.0f} s per person - so a 60-person roster is the best part of an hour even when paced")
    return "\n".join(lines) if lines else None


def _rate_limit_numbers(state, world, results):
    bulk = world.get("bulkEnrolment") or {}
    lines = []
    if bulk.get("agentCallsRefused429") is not None:
        lines.append(f"during the bulk grant: {bulk['agentCallsRefused429']} agent calls refused; {bulk.get('agentResultsLost')} "
                     f"were results of work the reader had already done, {bulk.get('agentHeartbeatsFailed')} were heartbeats")
    paced = _metric(results, "commissioning", "paced enrolment: agent calls refused as too many (HTTP 429)")
    if paced and paced[0]:
        lines.append(f"during paced enrolment (two people at a time): {paced[0]} agent calls refused")
    return "\n".join(lines) if lines else None


def _store_rewrite(state, world, results):
    """The agent's record of finished commands, as it stands on disk after the run."""
    import json
    path = config.stack_dir() / "agent-data" / "executed.json"
    try:
        entries = len(json.loads(path.read_text(encoding="utf-8")).get("entries") or [])
        size_kb = path.stat().st_size / 1024
    except (OSError, ValueError):
        return None
    if entries < 1000:
        return None
    lag = _metric(results, "remote-unlock", "remote unlock: tap -> the app has its answer, typical")
    door = _metric(results, "remote-unlock", "remote unlock: tap -> the door actually opens, typical")
    text = (f"the agent's record holds {entries} entries ({size_kb:.0f} KB) after one enrolment and a day's scenarios, and is "
            "written out in full after every command that is not a read")
    if lag and door and lag[0] and door[0]:
        text += (f"; a remote unlock opens the door {door[0]:.0f} ms after the tap, and the app has its answer at "
                 f"{lag[0]:.0f} ms - the difference is mostly this write. Timed on its own in the agent's container: "
                 "0.2-0.4 s per write")
    return text


def _door_page_hardcoded(state, world, results=None):
    door = ((state.get("apps") or {}).get("door") or {}).get("source")
    if not door:
        return None
    hits = []
    for page in ("screen.html", "pair.html"):
        path = Path(door) / "public" / page
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(r"https://havenz-backend-[\w.\-]+\.run\.app", line) and "var API" in line:
                hits.append(f"public/{page}:{number}  `{line.strip()}`")
    return "\n".join(hits) if hits else None


def _migration_runner(state, world, results=None):
    db = state.get("database") or {}
    if db.get("productionRunner") != "refused":
        return None
    errors = db.get("fallbackErrors") or {}
    lines = [f"scripts/migrate.sh stopped at: {db.get('productionRunnerError')}"]
    for name, items in errors.items():
        lines.append(f"{name}: {len(items)} SQL error(s) when applied on top of the base schema, e.g. \"{items[0]}\"")
    return "\n".join(lines)


def _healthcheck(state, world, results=None):
    from . import stack
    code, out, _ = stack.docker("inspect", "-f", "{{.State.Health.Status}}|{{range .State.Health.Log}}{{.Output}}{{end}}",
                                config.CONTAINER["api"], check=False)
    if code == 0 and out.startswith("unhealthy") and "curl: not found" in out:
        return "docker reports the container `unhealthy` while GET /health answers 200; health log: `curl: not found`"
    return None


FINDINGS = [
    {
        "id": "F-BULK-ENROL",
        "title": "Granting door access to a whole roster at once swamps the door command queue",
        "severity": BLOCKS,
        "where": "commissioning / grants",
        "seen": _enrolment_numbers,
        "needs_assertion": True,
        "reproduction": (
            "Seed the plant (20 readers on one agent), enrol 60 people with photos, then grant access with the bulk "
            "grant the admin app offers - six calls to `POST /api/admin/area-access/bulk`, one per group (806 "
            "person-on-reader pairs). Watch `iot.terminal_user_syncs`, the backend log and the agent log."),
        "evidence": [
            "WebApp/Controllers/Admin/UserAreaAccessController.cs:214-343 - the bulk grant starts one push per person per reader, all at once",
            "WebApp/Services/Amico/RoutedAmicoApiService.cs:45 - every user push has 45 s to be collected (`StandardTtl`)",
            "WebApp/Services/Amico/AgentCommandLeasing.cs:89-179 - one command per reader in flight, so a reader's queue drains serially",
            "WebApp/Services/Amico/AgentCommandQueue.cs:64,220 - each waiting push re-reads its row every 50 ms (800 waiters = the whole connection pool)",
            "WebApp/Services/Amico/AgentCommandQueue.cs:182-185 - the failure text: \"No site agent collected ... before it expired. The agent is probably offline.\" (the agent was online throughout)",
            "WebApp/Services/Amico/TerminalSyncRetry.cs:21-53 - after three quick retries a push waits 1, 2, 4, 8, 16, 30 minutes; pushes are given up on after 20 attempts",
        ],
        "cause": (
            "Nothing meters the pushes against what one agent can deliver. Each reader takes one command at a time and "
            "the agent is allowed about 120 calls a minute in total, so a reader clears roughly four commands in the 45 s "
            "a command lives; a roster puts 30-40 on every reader at once and the rest expire uncollected, are retried "
            "together, and expire again. Meanwhile every waiting push polls the database 20 times a second."),
    },
    {
        "id": "F-AGENT-RATE-LIMIT",
        "title": "The site agent is rate-limited below what twenty doors need",
        "severity": BLOCKS,
        "where": "commissioning / readers, grants",
        "seen": _rate_limit_numbers,
        "needs_assertion": True,
        "reproduction": (
            "Pair the agent and bootstrap 20 readers; or push users to 20 readers. `docker logs havenz_rehearsal-agent-1 | grep 429`."),
        "evidence": [
            "WebApp/Program.cs:900-913 - policy `device`: 120 requests a minute, no queue",
            "WebApp/Services/Security/RateLimitPartitionKeys.cs:40-52 - the bucket is per hub key, so one bucket for the whole site",
            "WebApp/Controllers/Devices/AgentController.cs:34-37 - heartbeat, command poll, every result and the reader list share that policy",
            "havenz-agent/agent.py `report()` - a refused result is logged and dropped; the backend then records the command as 'unknown' though the reader did the work",
        ],
        "cause": (
            "The limit was sized for a sensor gateway (a poll every 15 s). An agent serving 20 doors reports one result per "
            "command: the 30-second log read alone is 40 results a minute, bootstrapping 20 readers is 60 commands in a few "
            "seconds, and any enrolment adds two per person per door. Heartbeats share the same bucket, so under load the "
            "agent can also look offline."),
    },
    {
        "id": "F-VOLTAGE-BAND",
        "title": "The default voltage alarm band is 100-140 V; the engines generate at 13,800 V",
        "severity": SHOULD,
        "where": "commissioning / sensors",
        "reproduction": (
            "Register any device, post one `voltage_ac` reading of 13800 V through `/api/iot/ingest`. A critical "
            "'threshold' incident opens at once and pages the property's recipients."),
        "evidence": [
            "WebApp/Services/Sensors/MetricThresholdDefaults.cs:71 - `voltage_ac`: warn 108-132 V, critical 100-140 V",
        ],
        "cause": (
            "The default band assumes a 120 V circuit. The day engine or meter voltage is first connected, every such "
            "device raises a critical alert until someone sets a per-device band (the rehearsal sets 12.4-15.2 kV before "
            "the first reading). Either give medium-voltage devices no default band or set the bands as part of commissioning."),
    },
    {
        "id": "F-OPERATOR-ROLE",
        "title": "The operator who creates a company cannot register its screens",
        "severity": NOTE,
        "where": "commissioning / company",
        "reproduction": (
            "As a platform super-admin, `POST /api/havenzhub/companies`, then `POST /api/havenzhub/screens` with "
            "`X-Company-Id` of the new company: 403."),
        "evidence": [
            "WebApp/Controllers/HavenzHub/CompanyController.cs:105-114 - the creator is added to the new company as `admin`",
            "WebApp/Controllers/HavenzHub/FacilityScreenController.cs:217 - registering a screen needs `super_admin` in that company",
            "WebApp/Controllers/Admin/UserAdminController.cs:610 - 'Cannot modify your own role', so the creator cannot raise themselves",
        ],
        "cause": (
            "Roles are per company and the creator gets `admin`. The way through is to create a second account in the "
            "new company with the super-admin role and register the screens as that account - which is what the rehearsal does."),
    },
    {
        "id": "F-DOOR-PAGE-BACKEND",
        "title": "The door panel page can only talk to the production backend",
        "severity": SHOULD,
        "where": "bringing the plant up (door app on :3200)",
        "seen": _door_page_hardcoded,
        "reproduction": (
            "Open `public/screen.html` or `public/pair.html` from any copy of the door app - a laptop, a bench server, "
            "a preview deployment. It pairs with and listens to production."),
        "evidence": [],
        "cause": (
            "The backend address is a literal in the page. There is no way to point a panel at a bench or staging "
            "backend, so the page that runs on the Tizen panels cannot be rehearsed without altering it (the rehearsal "
            "swaps the address as it serves the page), and any stray copy opened anywhere is a production client."),
    },
    {
        "id": "F-FRESH-DATABASE",
        "title": "The production migration runner cannot build a database from nothing",
        "severity": NOTE,
        "where": "bringing the plant up (database)",
        "seen": _migration_runner,
        "reproduction": (
            "Empty PostgreSQL 15; apply `DataAccess/database/havenz_hub_schema_postgresql.sql`; run `scripts/migrate.sh`."),
        "evidence": [
            "DataAccess/database/migrations/001_add_user_department_project_tables.sql:43 - `CREATE INDEX` without `IF NOT EXISTS` on an index the base schema already has",
            "DataAccess/database/migrations/036_add_search_and_preferences.sql:49-56 - `COALESCE(tags, '')` on a json column",
            "docker-compose.tasks.yml `migrate` - the only path that builds a fresh database runs every file and ignores errors",
        ],
        "cause": (
            "The base schema already contains what the earliest migrations create, and the strict runner stops at the "
            "first error. A new environment or a disaster-recovery rebuild therefore depends on the developer task that "
            "continues past errors. The rehearsal builds its database that way and logs every error; the resulting "
            "schema matched the development database's tables and columns."),
    },
    {
        "id": "F-HEALTHCHECK",
        "title": "The backend image's health check calls a program the image does not contain",
        "severity": NOTE,
        "where": "bringing the plant up (backend container)",
        "seen": _healthcheck,
        "reproduction": "`docker build` the backend, run it, `docker ps`: the container is `unhealthy` though it serves.",
        "evidence": ["Dockerfile:50-51 - `HEALTHCHECK ... CMD curl --fail http://localhost/health`; the aspnet:8.0 runtime image has no curl"],
        "cause": "Harmless on Cloud Run (which ignores it); misleading anywhere Docker's own health status is watched.",
    },
    {
        "id": "F-LEAK-FLICKER",
        "title": "A leak probe that flickers wet and dry faster than the confirmation window never pages",
        "severity": SHOULD,
        "where": "leak / flicker",
        "reproduction": (
            "Post `water_detection` 1 then 0 for one device every 5 s for 70 s (water lapping at the probe's edge). "
            "Each wet opens an incident; each dry inside the 30 s window closes it as 'brief' and cancels its page."),
        "evidence": [
            "WebApp/Services/Alerts/AlertEvaluator.cs:242-262 - a dry reading before `firedAt + wetWindow` resolves the incident as brief and cancels the held deliveries",
            "WebApp/appsettings.json `Alerts:BinaryDangerConfirmWetSeconds` = 30",
        ],
        "cause": (
            "The splash rule looks at one incident at a time. It has no memory that the same probe has gone wet again "
            "and again in the last minute, so real water at the edge of a probe reads as a string of harmless splashes."),
    },
    {
        "id": "F-REMOTE-OPEN-TWO-ROWS",
        "title": "One remote unlock is recorded twice and greeted twice",
        "severity": SHOULD,
        "where": "remote-unlock / remote-unlock",
        "reproduction": (
            "Open a door from the app once. Read `iot.amico_access_events` for that door and listen to its panel's broadcasts."),
        "evidence": [
            "WebApp/Services/Amico/RemoteUnlockLedger.cs:212-224,283-312 - on 'opened' the backend writes its own RemoteOpen row (naming the person)",
            "WebApp/Controllers/Devices/AmicoTerminalsController.cs:1028-1051 - and broadcasts a WelcomeEvent 'RemoteOpen'",
            "WebApp/Controllers/Devices/AmicoNotificationsController.cs:954-997 - the reader then reports the same opening in its own log (event 12, 'WebInterface', no person), which is stored and broadcast as well",
            "DomainModel/HavenzHub/AmicoAccessEvent.cs:209-211 - both types count as events for the screen",
        ],
        "cause": (
            "The backend's audit row and the reader's own log row describe the same opening and nothing ties them "
            "together. The door's history shows two openings (one named, one anonymous) and the panel is told to greet twice."),
    },
    {
        "id": "F-AGENT-STORE-REWRITE",
        "title": "The site agent rewrites its whole record of finished commands after every command",
        "severity": SHOULD,
        "where": "remote-unlock / remote-unlock (also slows every enrolment)",
        "seen": _store_rewrite,
        "reproduction": (
            "After a few thousand reader commands (one enrolment day), open a door remotely and compare when the reader "
            "opened with when the backend received the agent's result."),
        "evidence": [
            "havenz-agent/agent.py `_remember()` -> `_executed_save_locked()` - every command that is not a read is added to one JSON file, which is then written out in full, fsynced and swapped in, under one lock",
            "havenz-agent/agent.py `EXECUTED_MAX = 5000`, `EXECUTED_TTL_SECONDS` = a week - user pushes and photo uploads are remembered too, each with its result, so the file is thousands of entries within a day",
            "havenz-agent/agent.py `handle()` - the result is reported only after that write returns",
        ],
        "cause": (
            "The record exists so a door is never opened twice, and for unlocks it is right. But it also holds every "
            "user push and photo upload, and its cost grows with its size: here (a desktop SSD through a Docker bind "
            "mount) 0.2-0.4 s per command once it held ~2,700 entries. The door has long since opened while the app "
            "still waits, and during enrolment it is a single-file queue every door's work passes through. On a Pi's "
            "SD card the write will not be faster."),
    },
    {
        "id": "F-AGENT-HEAD-OF-LINE",
        "title": "One reader that stops answering holds up every other door's commands for ten seconds at a time",
        "severity": SHOULD,
        "where": "hung-reader / reader-hangs",
        "reproduction": (
            "Make one reader accept connections and never answer. Send it a command (a remote unlock, or wait for the "
            "30-second log read) and, a second later, send a remote unlock to a healthy door."),
        "evidence": [
            "havenz-agent/agent.py:1376-1382 (`command_loop`) - a batch is carried out to the end before the next poll; doors are worked in parallel only inside one batch",
            "havenz-agent/agent.py:287 - each call to a reader may take 10 s before it is given up",
            "havenz-agent/agent.py:1336-1337 - the breaker opens after 3 failures and re-tries the reader every 60 s, so the stall repeats",
            "WebApp/Services/Amico/RoutedAmicoApiService.cs:39 - a remote unlock lives 10 s, so one stall is enough to expire it",
        ],
        "cause": (
            "The agent asks for work, does all of it, then asks again. While it waits out a silent reader it is not "
            "asking, so a command for any other door sits in the backend's queue for the whole timeout. The breaker does "
            "set the reader aside, but only after three such stalls, and it probes again every minute. Doors still "
            "open for faces - readers decide on their own - but a greeting can be late too: when the backend needs a "
            "reader's user list to put a name to a badge-in, that request waits in the same queue."),
    },
    {
        "id": "F-FIRST-TAP-SLOW",
        "title": "The first badge-in at a door after a quiet spell is greeted a second or more late",
        "severity": SHOULD,
        "where": "shift-change",
        "reproduction": (
            "Leave a door unused for ten minutes (or restart the backend), then badge in. Compare tap-to-welcome with "
            "the next badge-in at the same door."),
        "evidence": [
            "WebApp/Services/Amico/TerminalUserMapCache.cs:49,63-73 - who a reader's numeric user id belongs to is cached per door for 10 minutes; on a miss the whole map is fetched from the reader",
            "WebApp/Controllers/Devices/AmicoNotificationsController.cs:906 - the event is not stored or broadcast until that fetch returns",
            "WebApp/Services/Amico/AgentCommandLeasing.cs:89-179 - the fetch is an agent command, so it waits its turn behind whatever that door's reader is already doing (the 30-second log read, a user push)",
        ],
        "cause": (
            "A reader reports who badged in as its own number, and turning that into a person needs the reader's user "
            "list. That list is fetched through the agent at the moment of the tap when the cached copy is more than ten "
            "minutes old, so the person at the door waits for a round trip that has nothing to do with them. At a shift "
            "change every door's first tap pays it, which is what pushes the slowest greetings past 1.5 s."),
    },
    {
        "id": "F-PUSH-SAID-SENT",
        "title": "A push that never reached a phone is recorded as sent",
        "severity": NOTE,
        "where": "leak / leak-page",
        "reproduction": (
            "Run with no push provider configured (or one that is failing) and raise a critical alert. Read the "
            "alert's deliveries (`GET /api/havenzhub/alerts/{id}`) and `notifications.notifications.fcm_sent`."),
        "evidence": [
            "WebApp/Services/Alerts/AlertOutbox.cs:189-205,335 - the push delivery is marked sent once the in-app notification row is written",
            "WebApp/Services/Notifications/NotificationService.cs:732-805 - the phone push itself is sent separately, not awaited, and its failure only clears `fcm_sent`",
            "WebApp/Services/Notifications/FcmService.cs:51-90 - with no credentials the service logs once and returns 'not sent'",
        ],
        "cause": (
            "'Sent' on a push delivery means 'written to the in-app inbox', not 'delivered to a phone'. If the push "
            "provider's credentials are missing or wrong in production, every alert's delivery summary still says the "
            "recipients were told by push, and the retry and 'undelivered' machinery never engages."),
    },
    {
        "id": "F-POLLER-RACE",
        "title": "When a door event arrives by push and by the 30-second log read at the same moment, one of the two fails with a database error",
        "severity": NOTE,
        "where": "soak",
        "reproduction": (
            "Steady badge-ins on 20 doors (one per door every 20 s). Within a few minutes the backend logs "
            "'Error during Amico access log polling' with a duplicate-key error on `idx_amico_events_idempotency_v2`."),
        "evidence": [
            "WebApp/Services/Jobs/AmicoAccessLogPollingJob.cs:217-226 - the pass saves the rows it believes are new",
            "WebApp/Services/Jobs/AmicoAccessLogPollingJob.cs:89 - the per-door loop only catches reader errors",
            "WebApp/Services/Jobs/AmicoAccessLogPollingJob.cs:58-60 - so the database error is caught outside the loop and ends the pass for every door after that one",
            "WebApp/Controllers/Devices/AmicoNotificationsController.cs:882-891 - the push path makes the same check-then-insert; when it loses the race the request ends as an unhandled 500 and the agent sends the event again",
        ],
        "cause": (
            "Both paths check 'do I already have this event?' and then insert; when they overlap the unique index "
            "rightly stops the second insert, but neither path treats that as 'already have it'. If the log read "
            "loses, its whole pass ends and the doors later in the list wait for the next one; if the push loses, the "
            "agent is answered 500 and sends the event again a second later. No event is lost or doubled either way, "
            "but the log fills with errors that are not errors - about one every two minutes at 60 badge-ins a minute."),
    },
    {
        "id": "F-AGENT-JOURNAL-STALL",
        "title": "A slow disk write on the agent makes every reader's event post time out at once",
        "severity": NOTE,
        "where": "soak",
        "reproduction": (
            "Steady badge-ins on 20 doors while the agent's /data volume has a slow moment. The readers' posts to the "
            "agent time out (the reader allows 5 s); the agent's log shows 'Broken pipe' as it answers readers that "
            "have already hung up."),
        "evidence": [
            "havenz-agent/agent.py `EventQueue.put()` - the event is appended and fsynced while holding the queue's one lock; the reader is answered only afterwards",
            "havenz-agent/agent.py `Reader.configure_monitor()` - the reader is told `request_timeout` 5000 ms",
        ],
        "cause": (
            "Writing before answering is the right order - it is why no event was lost here: every one of these events "
            "was kept and delivered. But all doors share one lock around that write, so the writes queue: twenty doors "
            "posting in the same second wait for twenty writes in a row, and at a quarter of a second a write the last "
            "reader has waited its full five seconds. In the rehearsal the agent's /data is a folder shared from Windows "
            "into Docker on a machine that was also running 23 browsers, which is slow storage; a Pi's SD card is slow "
            "storage too. What a real reader does after such a timeout (retry, or give up) has not been observed."),
    },
    {
        "id": "F-HEALTH-SILENT-ON-READERS",
        "title": "A reader that has stopped answering does not show on the property's health",
        "severity": SHOULD,
        "where": "hung-reader / reader-isolated",
        "reproduction": (
            "Make one reader accept connections and never answer; send it three commands. The door list and the agent's "
            "card show the failure; `GET /api/havenzhub/properties/{id}/health` is unchanged."),
        "evidence": [
            "WebApp/Models/HavenzHub/PropertyHealthDto.cs:17-77 - health has telemetry, alerts, contractors and site agents; nothing about door readers",
            "WebApp/Services/Sensors/Health/PropertyHealthService.cs:164-168 - the only door figure is a count of doors per agent",
        ],
        "cause": (
            "Per-reader health exists (`lastError`, `lastErrorAt` against `lastOkAt`) but is not folded into the page "
            "people watch. A dead door is found by someone standing at it, or by opening the door list."),
    },
]


def collect(results, world, state):
    """The findings this run observed, each with the evidence from this run."""
    out = []
    for f in FINDINGS:
        observed = []
        for scenario, a in _failed(results, f["id"]):
            observed.append(f"{scenario}: \"{a['name']}\" - {a['detail']}")
        seen = f.get("seen")
        # Some checks only add numbers to a finding an assertion has already raised; others (a
        # hard-coded address in a file, say) are the whole evidence.
        if seen and (observed or not f.get("needs_assertion")):
            try:
                extra = seen(state, world, results)
            except Exception:  # noqa: BLE001 - a broken detector must not take the report down
                extra = None
            if extra:
                observed.append(extra)
        if observed:
            out.append({**{k: v for k, v in f.items() if k not in ("seen", "needs_assertion")}, "observed": observed})
    order = {BLOCKS: 0, SHOULD: 1, NOTE: 2}
    out.sort(key=lambda f: order[f["severity"]])
    return out
