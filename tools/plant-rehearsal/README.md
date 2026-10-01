# Plant rehearsal

The whole AHI power-plant deployment, running on one machine in simulation, with scripted
scenarios that each end in hard assertions. It exists so that the things that only show up at
scale or under failure - twenty doors at a shift change, a leak, the site losing its internet, a
reader that stops answering, the backend restarting mid-shift - are found here and not at the
plant.

What runs:

| Piece | What it is |
|---|---|
| Backend | The real HavenzBMS API, built as the production image from a checkout you name, on its own PostgreSQL with that checkout's schema and every migration applied |
| Site agent | The real add-on in this repository (`havenz-agent`), built from its own Dockerfile and started by its own entry point |
| 20 door readers | `tools/fake_reader.py` (the stand-in for the HID Amico), one address each on a private "plant LAN", port 80, with the people, faces, groups and dated windows the agent pushes to them |
| Sensors and engines | A feeder on the real ingest path (`/api/iot/ingest`, gateway key, measured-at times): four engines around 1,900-2,400 kW, temperatures, leak and contact sensors, meters |
| Mail, push, storage | A local sink: mail is captured and delivered to nobody, push goes nowhere, face photos go to a local stand-in for the documents bucket |
| Wall and door apps | The dashboards app (production build) on :3100 and the door panel page on :3200, both pointed at the rehearsal backend, for the screen fleet to open |

Nothing here touches the development stack (`havenz_api` :5000, `havenz_db` :5433), production, a
real mailbox, a real phone or a real bucket. It is its own Docker Compose project,
`havenz_rehearsal`.

## Run it

Needs Docker Desktop, Python 3.11+, Node 20+ (for the wall app), and `pip install -r requirements.txt`
(one package, Pillow, used to make the enrolment photos).

```
python rehearsal.py up        # build and start everything            (first time: ~10 min)
python rehearsal.py seed      # build the plant through the real API   (~45-60 min - see below)
python rehearsal.py run       # every scenario, then the report        (~45 min)
python rehearsal.py down      # remove all of it; run folders are kept
```

`up` takes the three checkouts it needs from the workspace next to this repository, or from
`--bms <path>`, `--dashboards <path>`, `--door <path>`. It builds from each checkout's **HEAD
commit**, not its working tree, and never writes into any of them.

Other commands:

```
python rehearsal.py run leak emergency     # only these scenarios
python rehearsal.py run --soak 30m         # every scenario, then a 30-minute soak
python rehearsal.py run soak --soak 2h     # only the soak
python rehearsal.py run --list             # scenario names
python rehearsal.py pairing-codes          # fresh pairing codes for screens that are not paired
python rehearsal.py report                 # rebuild REPORT.md and findings.md from results.json
python rehearsal.py status                 # what is running
python rehearsal.py reset                  # empty the plant (new database, blank readers) without rebuilding
python rehearsal.py seed --resume          # continue a seed that was interrupted
python rehearsal.py seed --paced           # skip the bulk grant, enrol people two at a time
python rehearsal.py up --no-apps           # do not start the wall and door apps (see "The two apps")
```

### Why seeding takes most of an hour

Seeding is install day, done through the real API, and it is asserted like a scenario
("commissioning" in the report). Sixty people on twenty doors is 806 person-on-reader pairs, each
a user record and a face photo, and every rate limit is left at its production value - including
the one on the site agent. The seed first grants access the way the admin app offers (in bulk),
gives that ten minutes, records how far it got, and then finishes the enrolment at the pace the
system can take. That the bulk grant does not finish is a finding, not a harness problem.
`seed --paced` skips the bulk attempt.

## Ports

| | |
|---|---|
| Backend API | http://localhost:5100 |
| PostgreSQL | localhost:5434 |
| Mail sink | SMTP 1026, read API http://localhost:8026 (`/messages`, `/gcs/objects`) |
| Wall app | http://localhost:3100/screen |
| Door panel page | http://localhost:3200/screen.html |
| Reader control (scenarios only) | http://localhost:9100 (`/state`, `/readers/<n>`) |
| Site agent's pairing / status page | http://localhost:8199 (`/status.json`) |
| Readers | 10.107.0.11 - 10.107.0.30, port 80, inside the compose network `plant_lan` |

## What a run leaves behind

Everything goes to `<workspace>/output/plant-rehearsal/<UTC timestamp>/` (set `REHEARSAL_OUT` to
put it elsewhere). Run output is never committed.

| File | |
|---|---|
| `manifest.json` | The plant, for the screen fleet: every screen (unpaired, with a pairing code), every reader, a sample of people |
| `timeline.jsonl` | One line per step as it happens, with what a correct screen should be showing |
| `results.json` | Every assertion of every scenario, with what was seen; timings; findings |
| `REPORT.md` | The readable report: result table, timings, every assertion, every setting that differs from production, and what a simulation cannot prove |
| `findings.md` | Product problems the run observed: how to reproduce, evidence, where in the product |
| `world.json` | Ids and rehearsal-only credentials the scenarios use |
| `soak.jsonl` | Samples from the soak, when it was run |

## Scenarios

| Name | What happens | Ends by asserting |
|---|---|---|
| (commissioning) | The seed itself | Agent pairs; 20 readers bootstrap through it; 23 screens; 15 devices; 60 people on exactly the readers they should be; contractor onboarded |
| `shift-change` | 40 people badge in across 20 doors inside two minutes | One opening per tap at the reader; each event in the access log once, in order, against the right person; a welcome for every panel door; p50/p95 |
| `refusals` | Unknown face; no access to this door; access that expires; a leaver deactivated mid-run | Doors stay shut; the refusal is logged; the leaver is off all 20 readers within two minutes and refused everywhere |
| `contractor` | A new job with a short window | Refused before the window, opens inside it on the job's doors only, refused after close-out; face deleted when retention ends |
| `remote-unlock` | One request retried five times; five at once; one whose result is lost | One opening each time; a lost result ends as "unknown", is never re-sent |
| `leak` | Wet; splash; lapping probe; flicker; silent sensor | Incident at once; page after 30 s; one email per recipient; splash pages nobody; one incident for a lapping probe; stale shown as stale |
| `agent-offline` | The agent is killed for about four minutes | Faces still open doors; remote unlock refused cleanly; health says offline; one alert, no re-page; resolves with an all-clear |
| `internet-cut` | The agent loses its uplink for three minutes while people badge in | Doors open; events kept on the agent's disk; each arrives once, late, with its real time; no late welcome |
| `backend-restart` | The backend is killed mid-shift | In-flight unlock ends defined and is not repeated; no event lost or doubled; pollers resume |
| `hung-reader` | One reader stops answering; another loses power | The agent sets the silent one aside; the other 19 unaffected; the admin pages name the door; both recover by themselves |
| `emergency` | Company evacuation, all-clear, one area only, all-clear with a screen off | Reach per scope; "N of M showing"; a screen that missed the all-clear gets the right state on reconnect |
| `soak` (optional) | Steady load for as long as asked | Nothing grows: queues, outboxes, memory, error lines |

Product bugs a scenario finds are **findings, not fixes**. An assertion is never loosened to make a
scenario pass; a red scenario with a clear finding is the point.

## Settings that differ from production

The complete list is in `plant/config.py` (`OVERRIDES`) and printed in every report. In short:

- **Isolation** - database, mail, push, storage and outbound webhooks are local; the wall and door
  apps' origins are added to the backend's allowed origins.
- **Time** - the site-agent-offline alert fires after 120 s instead of 300 s; the periodic check
  for silent sensors, leak timers and the offline agent runs every 30 s instead of 300 s; the
  delivery worker wakes every 5 s instead of 30 s; sensors report every 15-30 s so a sensor going
  quiet shows as stale in a minute instead of half an hour; the contractor's job window is minutes
  long.
- **Set-up** - the plant company's sign-in policy is "authenticator encouraged" rather than "required".
- **Rate limits are not changed.** The harness paces itself and spreads set-up work across the
  plant's administrator accounts.

The leak confirmation window (30 s wet, 30 s dry), the 90-second agent-offline rule, command
lifetimes, the reader log poll (30 s), the 60-second "too old to greet" rule and the door-event
path are all production values.

## The two apps

`up` starts both. To start them yourself (`up --no-apps`):

Wall app - from an export of the dashboards checkout (so its own `.env.local` is not used):

```
set NEXT_PUBLIC_API_URL=http://localhost:5100
set NEXT_PUBLIC_FACILITY_API_MODE=backend
set NEXT_PUBLIC_MARKET_API=backend
set NEXT_PUBLIC_TWIN_ANCHORS_API=backend
npx next build --webpack
npx next start -p 3100
```

Door panel page - served from the door checkout's `public/` folder with the backend address
replaced on the way out (the page has the production address written into it):

```
python -m plant.door_server --root <door checkout>\public --port 3200 --api http://localhost:5100
```

## What is stood in for, and what is not

Stood in for: the readers (the repository's own stand-in, which answers the reader's local API
the way the bench reader has been seen to); Home Assistant's Supervisor (a file with the add-on's
options, and a published port for its pairing page); Google Cloud Storage and its token endpoint
(the sink answers as both, over TLS, through a private certificate authority that only the
rehearsal's backend container trusts); the mail server; push.

Not stood in for: the backend, the agent, the database, the two browser apps, the ingest path,
the alert pipeline, the command queue, the unlock ledger, the emergency state - all real code,
unmodified.

Every report ends with what a simulation cannot prove. Read that section before quoting a number.

## Layout

```
rehearsal.py              the entry point
compose.yml               the stack
docker/                   Dockerfiles for the sink and the readers
sim/readers_sim.py        20 readers on a LAN, built on tools/fake_reader.py, plus a control API
sim/sink.py               mail sink, webhook sink, bucket stand-in
plant/config.py           ports and every setting that differs from production
plant/plantdef.py         the plant as data: rooms, doors, screens, sensors, people
plant/stack.py            up / down / reset, and the faults scenarios inject
plant/seed.py             commissioning
plant/api.py              API client that stays inside the production rate limits
plant/feeder.py           the sensor and engine feed
plant/signalr.py          hears what the screens hear
plant/screenprobe.py      a screen without a browser, on the screens' own protocol
plant/scenarios/          one file per scenario
plant/findings.py         the findings register
plant/report.py           REPORT.md and findings.md
```
