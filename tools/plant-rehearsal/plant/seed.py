"""
Commissioning: build the AHI plant in an empty backend, the way it would be built on site.

Everything goes through the real API with real accounts - the company, the property and its rooms,
the site agent (paired with a code typed into its own pairing page), twenty readers claimed and
bootstrapped through that agent, thirteen wall screens and ten door panels, sensors and engines,
sixty people with photos and door access, the alert recipients, and one contractor taken through
invitation, consent, phone photo and host approval. The only thing done directly in the database
is the very first platform operator, because an empty system has nobody to sign in as.

Seeding is itself a rehearsal of install day, so it asserts as it goes and its results sit in the
report beside the scenarios, under "commissioning".
"""

import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from . import apps, config, feeder as feeder_mod, plantdef, readers, record, stack
from .api import Account, Api
from .util import http, iso, parse_iso, utc_now, wait_until

OPERATOR_EMAIL = "operator@rehearsal.local"
OPERATOR_PASSWORD = "Rehearsal-Operator-2026!"
BOOTSTRAP_COMPANY_ID = "10000000-0000-0000-0000-000000000001"
BOOTSTRAP_USER_ID = "10000000-0000-0000-0000-000000000002"
STAFF_PASSWORD = "Rehearsal-Staff-2026!"
CONTRACTOR_PASSWORD = "Rehearsal-Contractor-2026!"
# How long a bulk grant is given to reach the readers before its result is written down.
BULK_PATIENCE_SECONDS = 600


def log(msg):
    print(f"[seed] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------------------------

def bootstrap_operator():
    """
    The first platform operator, written straight into the empty database.

    The role lives on a company membership, so the operator needs a company of its own; that
    company exists only to hold this account. The password hash is made by PostgreSQL's own bcrypt
    (pgcrypto), which is the algorithm the backend verifies.
    """
    stack.psql("create extension if not exists pgcrypto")
    stack.psql(f"""
insert into identity.companies (id, name, status, mfa_policy, face_enrollment_policy)
values ('{BOOTSTRAP_COMPANY_ID}', 'Havenz platform (rehearsal bootstrap)', 'active', 'off', 'off')
on conflict (id) do nothing;
insert into identity.users (id, email, name, password_hash, mfa_enabled, mfa_exempt,
                            password_change_required, face_enrollment_required, face_enrollment_exempt)
values ('{BOOTSTRAP_USER_ID}', '{OPERATOR_EMAIL}', 'Platform Operator',
        crypt('{OPERATOR_PASSWORD}', gen_salt('bf', 12)), false, true, false, false, true)
on conflict (id) do nothing;
insert into identity.user_companies (user_id, company_id, role)
values ('{BOOTSTRAP_USER_ID}', '{BOOTSTRAP_COMPANY_ID}', 'super_admin')
on conflict do nothing;
""")
    return Account(OPERATOR_EMAIL, OPERATOR_PASSWORD, "platform operator")


def photo_jpeg(name, index):
    """A 600x800 portrait JPEG, different for every person - the backend refuses anything under 480 px."""
    from PIL import Image, ImageDraw
    hue = (index * 47) % 360
    img = Image.new("HSV", (600, 800), (int(hue / 360 * 255), 110, 150)).convert("RGB")
    draw = ImageDraw.Draw(img)
    # A head and shoulders, so the picture reads as a person in the admin pages.
    draw.ellipse((190, 150, 410, 400), fill=(236, 214, 190))
    draw.pieslice((90, 420, 510, 1000), 180, 360, fill=(45 + index % 60, 60, 80 + (index * 7) % 90))
    initials = "".join(part[0] for part in name.split()[:2]).upper()
    draw.text((270, 600), initials, fill=(255, 255, 255))
    draw.text((20, 770), f"{name} - rehearsal photo {index}", fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


def new_run_dir():
    stamp = utc_now().strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = config.output_root() / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir, stamp


def save_world(run_dir, world):
    record.save_json(Path(run_dir) / "world.json", world)


def load_world(run_dir):
    world = record.load_json(Path(run_dir) / "world.json")
    if not world:
        raise SystemExit(f"{run_dir} has no world.json - seed has not finished its first stage")
    return world


def api_from_world(world):
    """An API client signed in as the plant's administrator accounts."""
    api = Api(config.API, world["company"]["id"])
    api.admins = [Account(a["email"], a["password"], a["label"]) for a in world["accounts"]["admins"]]
    return api


def screen_url(kind):
    return f"{config.DASHBOARDS}/screen" if kind == "wall" else f"{config.DOOR}/screen.html"


def write_manifest(run_dir, world, stamp=None):
    """The hand-over to the screen half: contract plant-rehearsal, section "The manifest"."""
    area_id = {k: v["id"] for k, v in world["areas"].items()}
    screens = []
    for s in world["screens"]:
        row = {"name": s["name"], "kind": s["kind"]}
        if s["kind"] == "wall":
            row["run"] = s["run"]
        row.update({"orientation": "portrait", "template": s["template"]})
        if s["kind"] == "door":
            row.update({"terminalId": s["terminalId"], "terminalName": s["terminalName"],
                        "areaId": s["areaId"]})
        row.update({"screenId": s["id"], "screenKey": s["screenKey"],
                    "pairingCode": s.get("pairingCode"), "pairingCodeExpiresAt": s.get("pairingCodeExpiresAt"),
                    "url": screen_url(s["kind"])})
        screens.append(row)

    people = world.get("people") or []
    terminals_by_area = {}
    for t in world["terminals"]:
        terminals_by_area.setdefault(t["areaKey"], []).append(t["id"])
    sample = []
    for p in people:
        if p.get("part") or len(sample) < 12:
            sample.append({"id": p["id"], "name": p["name"], "group": p["group"],
                           **({"part": p["part"]} if p.get("part") else {}),
                           "terminals": [tid for a in p["areas"] for tid in terminals_by_area.get(a, [])]})

    manifest = {
        "runId": world["runId"],
        "contract": "plant-rehearsal v1.1",
        "generatedAt": iso(),
        "seeding": world.get("seedingStage", "complete"),
        "api": world["api"],
        "company": {"id": world["company"]["id"], "name": world["company"]["name"]},
        "property": {"id": world["property"]["id"], "name": world["property"]["name"]},
        "screens": screens,
        "terminals": [{"id": t["id"], "name": t["name"], "address": t["address"], "areaId": area_id[t["areaKey"]],
                       "hasPanel": t["hasPanel"]} for t in world["terminals"]],
        "people": {"count": len(people), "sample": sample},
        # Beyond the contract's minimum - ignore what you do not need.
        "areas": [{"id": v["id"], "name": v["name"], "key": k} for k, v in world["areas"].items()],
        "emergencyAreaScope": {"areaId": area_id[plantdef.AREA_SCOPED_EMERGENCY],
                               "name": world["areas"][plantdef.AREA_SCOPED_EMERGENCY]["name"],
                               "panels": [s["name"] for s in world["screens"] if s["kind"] == "door"
                                          and s["areaKey"] == plantdef.AREA_SCOPED_EMERGENCY]},
        "apps": {"walls": f"{config.DASHBOARDS}/screen", "doors": f"{config.DOOR}/screen.html",
                 "doorPairing": f"{config.DOOR}/pair.html"},
        "mailSink": f"http://localhost:{config.SINK_API_PORT}",
        "readerControl": f"http://localhost:{config.READER_CONTROL_PORT}",
        "timeline": str(Path(run_dir) / "timeline.jsonl"),
        "skippedTemplates": plantdef.SKIPPED_TEMPLATES,
    }
    record.save_json(Path(run_dir) / "manifest.json", manifest)
    return manifest


def mint_codes(api, world, only_unpaired=True, names=None):
    """Fresh pairing codes. A new code replaces any earlier unclaimed one for the same screen."""
    _, listed = api.get("/api/havenzhub/screens", expect=200)
    status_by_id = {s["id"]: s.get("keyStatus") for s in listed}
    minted = 0
    for s in world["screens"]:
        if names and s["name"] not in names:
            continue
        if only_unpaired and status_by_id.get(s["id"]) == "paired":
            s["pairingCode"], s["pairingCodeExpiresAt"] = None, None
            continue
        _, body = api.post(f"/api/havenzhub/screens/{s['id']}/pairing-code", expect=200)
        s["pairingCode"], s["pairingCodeExpiresAt"] = body["pairingCode"], body["expiresAt"]
        minted += 1
    return minted


def pairing_codes(run_dir=None, include_paired=False, only=None):
    run_dir = record.current_run_dir(run_dir)
    world = load_world(run_dir)
    api = api_from_world(world)
    minted = mint_codes(api, world, only_unpaired=not include_paired, names=set(only) if only else None)
    save_world(run_dir, world)
    write_manifest(run_dir, world)
    log(f"{minted} pairing code(s) minted, valid 15 minutes; manifest rewritten: {Path(run_dir) / 'manifest.json'}")
    for s in world["screens"]:
        if s.get("pairingCode"):
            print(f"  {s['name']:<22} {s['pairingCode']}")


def ensure_feeder(run_dir):
    """The sensor feed runs as its own process so the walls have live data between scenarios."""
    state = stack.load_state()
    status = record.load_json(feeder_mod.status_path(), {})
    last = parse_iso(status.get("at")) if status else None
    current = state.get("feeder") or {}
    if (apps.pid_alive(current.get("pid")) and current.get("runDir") == str(run_dir)
            and last and (utc_now() - last).total_seconds() < 60):
        return current["pid"]
    old = (state.get("feeder") or {}).get("pid")
    if old:
        apps._kill_tree(old)
    import sys
    logs = config.stack_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    pid = apps._detached([sys.executable, "-m", "plant.feeder", "--world", str(Path(run_dir) / "world.json")],
                         config.TOOL_DIR, logs / "feeder.log")
    stack.update_state(feeder={"pid": pid, "runDir": str(run_dir), "startedAt": iso()})
    return pid


def agent_429_count(since_iso=None):
    return stack.agent_refusals(since_iso)["total"]


def sync_counts():
    """Person-on-reader pairs by state. Counted as distinct pairs: a re-sync adds rows, it does not replace them."""
    rows = stack.psql("select sync_type, status, count(distinct (user_id, terminal_id)) "
                      "from iot.terminal_user_syncs group by 1, 2") or []
    return {(r[0], r[1]): int(r[2]) for r in rows}


def expected_reader_rosters(world):
    """Which people each reader should hold, from the access that was granted."""
    by_area = {}
    for p in world.get("people", []):
        if p.get("deactivated"):
            continue
        for a in p["areas"]:
            by_area.setdefault(a, set()).add(p["id"])
    return {t["number"]: set(by_area.get(t["areaKey"], set())) for t in world["terminals"]}


def reader_roster_problems(world, extra=None):
    """Compare every reader's own user list with what it should hold. Returns a list of problems."""
    expected = expected_reader_rosters(world)
    for number, ids in (extra or {}).items():
        expected[number] = expected.get(number, set()) | set(ids)
    problems = []
    for number, want in sorted(expected.items()):
        d = readers.detail(number)
        have = {u["registration"]: u for u in d["userList"]}
        missing = want - set(have)
        unexpected = set(have) - want
        no_face = [r for r in want & set(have) if not have[r]["hasFace"]]
        no_group = [r for r in want & set(have) if not have[r]["inGroup"]]
        if missing or unexpected or no_face or no_group:
            problems.append({"reader": number, "missing": len(missing), "unexpected": len(unexpected),
                             "withoutFace": len(no_face), "notInGroup": len(no_group)})
    return problems


# ---------------------------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------------------------

def seed(people=60, paced=False, resume=False, bulk_patience=BULK_PATIENCE_SECONDS):
    if resume:
        return resume_seed(people, paced, bulk_patience)
    state = stack.load_state()
    if not state.get("backend") or not stack.container_running(config.CONTAINER["api"]):
        raise SystemExit("the rehearsal stack is not up - run `python rehearsal.py up` first")
    if stack.psql_scalar(f"select count(*) from identity.companies where name = '{plantdef.COMPANY_NAME}'") != "0":
        raise SystemExit("this stack has already been seeded. For a clean plant: "
                         "`python rehearsal.py down` then `up` and `seed`.")

    run_dir, stamp = new_run_dir()
    record.set_current_run_dir(run_dir)
    timeline = record.Timeline(run_dir)
    rec = record.ScenarioRecord(
        "commissioning", "Commissioning: the plant is built through the real API",
        "An empty backend. One platform operator. Then the company, rooms, site agent, 20 readers, "
        "23 screens, 15 sensors and engines, 60 people with photos and access, alert recipients and "
        "one contractor - each created the way an administrator or installer would.", run_dir, timeline)
    rec.start()
    started = time.monotonic()
    seed_started_iso = iso()
    log(f"run folder: {run_dir}")

    world = {"runId": stamp, "runDir": str(run_dir), "api": config.API_PUBLIC, "seedingStage": "screens ready, people still loading",
             "backend": state["backend"], "accounts": {}, "areas": {}, "terminals": [], "screens": [],
             "devices": [], "people": []}

    # ---- platform operator, company, plant operator ------------------------------------------
    rec.step("operator", "First platform operator created in the empty database; signs in")
    operator = bootstrap_operator()
    api = Api(config.API, BOOTSTRAP_COMPANY_ID)
    api.admins = [operator]
    api.login(operator)

    rec.step("company", f"Company '{plantdef.COMPANY_NAME}' and property '{plantdef.PROPERTY_NAME}' created")
    _, company = api.post("/api/havenzhub/companies",
                          {"name": plantdef.COMPANY_NAME, "industry": "Power generation", "city": "Red Deer",
                           "province": "Alberta", "country": "Canada"}, expect=201)
    cid = company["id"]
    api.company_id = cid
    world["company"] = {"id": cid, "name": company["name"]}
    _, policy = api.put(f"/api/havenzhub/companies/{cid}/security-policy",
                        {"mfaPolicy": "encouraged", "faceEnrollmentPolicy": "encouraged"}, expect=200)

    # The operator who created the company is only an "admin" in it, and registering screens
    # needs a super-admin of that company - so a second operator account is created inside it.
    _, created = api.post("/api/admin/users", {"email": "plant.operator@rehearsal.local",
                                                "name": "Plant Operator (platform)", "role": "super_admin"},
                          expect=201)
    plant_operator = Account(created["email"], created["temporaryPassword"], "plant operator (super-admin)")
    api.login(plant_operator)
    api.change_password(plant_operator, STAFF_PASSWORD)
    status, _ = api.post("/api/havenzhub/screens", {"screenType": "dashboard", "propertyId": cid,
                                                    "screenKey": "probe", "label": "probe", "template": "engine-room"},
                         account=operator)
    rec.check("company creator can register screens for it", status != 403,
              f"the operator who created the company got HTTP {status} registering a screen in it; "
              "a second, super-admin account had to be created inside the company to register screens",
              finding="F-OPERATOR-ROLE")
    api.admins = [operator, plant_operator]
    world["accounts"]["operator"] = {"email": operator.email, "password": operator.password}
    world["accounts"]["plantOperator"] = {"email": plant_operator.email, "password": plant_operator.password}

    _, prop = api.post("/api/havenzhub/properties",
                       {"name": plantdef.PROPERTY_NAME, "type": "industrial", "locationCity": "Red Deer",
                        "locationProvince": "Alberta", "locationCountry": "Canada", "sizeFloors": 1,
                        "description": "Simulated copy of the AHI combined heat and power plant."}, expect=201)
    pid = prop["id"]
    world["property"] = {"id": pid, "name": prop["name"]}

    for key, name, area_type in plantdef.AREAS:
        _, area = api.post(f"/api/havenzhub/properties/{pid}/areas", {"name": name, "areaType": area_type},
                           expect=201)
        world["areas"][key] = {"id": area["id"], "name": name}
    rec.check("12 rooms registered", len(world["areas"]) == 12, f"{len(world['areas'])} areas created (101-112)")

    # ---- site agent ----------------------------------------------------------------------------
    rec.step("agent-pair", "Site agent added in the admin app; its pairing code is typed into the agent's own page")
    _, hub = api.post("/api/havenzhub/hubs", {"propertyId": pid, "name": "AHI site agent (room 109)",
                                              "kind": "agent"}, expect=200)
    paired_at = time.monotonic()
    status, body, _ = http("POST", f"{config.AGENT_PAGE}/register", body={"pairingCode": hub["pairingCode"]},
                           timeout=40)
    rec.check("site agent pairs with a code", status == 200, f"agent pairing page answered HTTP {status}: {body}")

    def agent_online():
        _, hubs = api.get(f"/api/havenzhub/hubs?propertyId={pid}", expect=200)
        me = next((h for h in hubs if h["id"] == hub["id"]), None)
        return me if me and me.get("agentState") == "online" else None
    online, waited = wait_until(agent_online, 60, 2)
    rec.check("site agent shows online", bool(online), f"agent state online {waited:.0f}s after pairing"
              if online else "agent never showed online within 60s")
    rec.metric("agent pairing -> online", round(time.monotonic() - paired_at, 1), "s")
    world["agentHub"] = {"id": hub["id"], "name": hub["name"]}

    # ---- readers -------------------------------------------------------------------------------
    rec.step("readers", "20 readers registered, each at its own LAN address, and bootstrapped through the agent")
    boot_times = []
    served_direct = []

    def claim(door):
        number, name, area_key, has_panel = door
        address = readers.address(number)
        _, created = api.post("/api/amico/terminals",
                              {"name": name, "ipAddress": address, "areaId": world["areas"][area_key]["id"],
                               "username": "admin", "password": "admin",
                               "notes": f"Rehearsal reader {number}"}, expect=200)
        if created.get("servedBy") != "agent":
            served_direct.append(name)
        t0 = time.monotonic()
        status, body = api.post(f"/api/amico/terminals/{created['id']}/bootstrap", timeout=600)
        took = time.monotonic() - t0
        return {"id": created["id"], "number": number, "name": name, "areaKey": area_key, "hasPanel": has_panel,
                "address": address, "bootstrapStatus": status, "bootstrapSeconds": round(took, 1),
                "bootstrapMessage": (body or {}).get("message") if isinstance(body, dict) else str(body)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        terminals = list(pool.map(claim, plantdef.DOORS))
    world["terminals"] = sorted(terminals, key=lambda t: t["number"])
    boot_times = [t["bootstrapSeconds"] for t in terminals]
    failed = [t for t in terminals if t["bootstrapStatus"] != 200]
    rec.check("all 20 readers bootstrap through the agent", not failed and not served_direct,
              f"{20 - len(failed)} of 20 bootstrapped (slowest {max(boot_times):.1f}s)"
              + (f"; failed: {[(t['name'], t['bootstrapStatus'], t['bootstrapMessage']) for t in failed]}" if failed else "")
              + (f"; NOT routed through the agent: {served_direct}" if served_direct else ""))
    rec.metric("reader bootstrap, slowest", max(boot_times), "s")

    _, listed = api.get("/api/amico/terminals", expect=200)
    active = [t for t in listed if t["status"] == "Active" and t.get("servedBy") == "agent"]
    device_ids = {t.get("deviceId") for t in listed}
    rec.check("readers are Active with distinct device ids", len(active) == 20 and len(device_ids) == 20,
              f"{len(active)} Active and agent-served, {len(device_ids)} distinct device ids")
    sim = readers.state()
    pointed = [r for r in sim["readers"] if (r.get("monitor") or {}).get("hostname") == config.AGENT_LAN_ADDRESS
               and str((r.get("monitor") or {}).get("port")) == "8100"]
    rec.check("every reader was told to send its events to the agent on the LAN", len(pointed) == 20,
              f"{len(pointed)} of 20 readers hold monitor address {config.AGENT_LAN_ADDRESS}:8100")

    # ---- screens -------------------------------------------------------------------------------
    rec.step("screens", "13 wall screens (portrait, in three runs) and 10 door panels registered; pairing codes minted")
    for name, run, template in plantdef.WALLS:
        _, s = api.post("/api/havenzhub/screens",
                        {"screenType": "dashboard", "propertyId": pid, "screenKey": name.lower(), "label": name,
                         "template": template, "params": {"orientation": "portrait", "group": run}},
                        account=plant_operator, expect=201)
        world["screens"].append({"id": s["id"], "name": name, "kind": "wall", "run": run, "template": template,
                                 "screenKey": s["screenKey"]})
    for t in world["terminals"]:
        if not t["hasPanel"]:
            continue
        label = f"{t['name']} panel"
        _, s = api.post("/api/havenzhub/screens",
                        {"screenType": "welcome", "propertyId": pid, "terminalId": t["id"],
                         "screenKey": t["name"].lower().replace(" ", "-") + "-panel", "label": label,
                         "params": {"orientation": "portrait"}},
                        account=plant_operator, expect=201)
        world["screens"].append({"id": s["id"], "name": label, "kind": "door", "template": s.get("template") or "welcome",
                                 "screenKey": s["screenKey"], "terminalId": t["id"], "terminalName": t["name"],
                                 "areaKey": t["areaKey"], "areaId": world["areas"][t["areaKey"]]["id"]})
    _, listed = api.get("/api/havenzhub/screens", expect=200)
    portrait = [s for s in listed if (s.get("params") or {}).get("orientation") == "portrait"]
    bound = [s for s in listed if s.get("screenType") == "welcome" and s.get("terminalId")]
    rec.check("23 screens registered, portrait explicit, panels bound to their doors",
              len(listed) == 23 and len(portrait) == 23 and len(bound) == 10,
              f"{len(listed)} screens, {len(portrait)} with orientation=portrait, {len(bound)} door panels bound to a terminal")

    world["accounts"]["admins"] = [{"email": a.email, "password": a.password, "label": a.label} for a in api.admins]
    mint_codes(api, world)
    save_world(run_dir, world)
    write_manifest(run_dir, world)
    rec.metric("time to a pairable plant (agent, 20 readers, 23 screens)", round(time.monotonic() - started, 1), "s")
    log(f"MANIFEST READY (screens pairable): {run_dir / 'manifest.json'}")

    # ---- sensors and engines -------------------------------------------------------------------
    rec.step("sensors", "Sensor gateway paired; 4 engines (2518 kW), 3 leak, 4 temperature, 2 contact sensors and 2 meters registered")
    _, shub = api.post("/api/havenzhub/hubs", {"propertyId": pid, "name": "AHI sensor gateway (room 109)",
                                               "kind": "sensor"}, expect=200)
    status, reg, _ = http("POST", f"{config.API}/api/bridge/register",
                          body={"pairingCode": shub["pairingCode"], "agentVersion": "1.4.0", "expectedKind": "sensor"},
                          timeout=30)
    if status != 200:
        raise SystemExit(f"sensor gateway pairing failed: HTTP {status} {reg}")
    world["sensorHub"] = {"id": shub["id"], "apiKey": reg["apiKey"]}

    def add_device(spec, kind, extra=None):
        interval = (config.BINARY_REPORTING_INTERVAL_SECONDS if kind in ("leak", "contact")
                    else config.SENSOR_REPORTING_INTERVAL_SECONDS)
        body = {"propertyId": pid, "name": spec["name"], "type": "sensor", "locationZone": spec["zone"],
                "reportingIntervalSeconds": interval, "status": "online"}
        body.update(extra or {})
        _, dev = api.post("/api/havenzhub/BmsDevice", body, expect=201)
        api.put(f"/api/admin/devices/{dev['id']}/area", {"areaId": world["areas"][spec["area"]]["id"]})
        row = {"id": dev["id"], "key": spec["key"], "name": spec["name"], "kind": kind, "area": spec["area"]}
        if "base" in spec:
            row["base"] = spec["base"]
        world["devices"].append(row)
        return dev

    for spec in plantdef.ENGINES:
        dev = add_device(spec, "engine", {"ratedKw": spec["ratedKw"], "manufacturer": "mtu",
                                          "model": "20V4000 GS"})
    for spec in plantdef.LEAK_SENSORS:
        add_device(spec, "leak")
    for spec in plantdef.TEMPERATURE_SENSORS:
        add_device(spec, "temperature")
    for spec in plantdef.CONTACT_SENSORS:
        add_device(spec, "contact")
    for spec in plantdef.POWER_METERS:
        add_device(spec, "meter")

    # 13.8 kV machines against a default voltage band written for 120 V circuits.
    probe_id = commissioning_voltage_check(api, rec, world)
    for dev in world["devices"]:
        if dev["kind"] in ("engine", "meter"):
            api.put(f"/api/havenzhub/BmsDevice/{dev['id']}/thresholds/voltage_ac",
                    {"warnLow": 13110, "warnHigh": 14490, "critLow": 12420, "critHigh": 15180}, expect=(200, 204))
    _, devs = api.get(f"/api/havenzhub/BmsDevice/property/{pid}?pageSize=100", expect=200)
    devs = devs["data"] if isinstance(devs, dict) else devs
    rated = [d for d in devs if d.get("ratedKw") == plantdef.ENGINES[0]["ratedKw"]]
    rec.check("15 devices registered; four engines rated 2518 kW",
              len([d for d in devs if d["id"] != probe_id]) == 15 and len(rated) == 4,
              f"{len(devs)} devices on the property, {len(rated)} with ratedKw=2518")
    save_world(run_dir, world)
    ensure_feeder(run_dir)

    world["seedStage"] = "devices"
    save_world(run_dir, world)
    record.save_scenario(run_dir, rec)
    return finish_seed(run_dir, world, api, rec, people, paced, started, bulk_patience)


def finish_seed(run_dir, world, api, rec, people, paced, started, bulk_patience=BULK_PATIENCE_SECONDS):
    """Everything after the plant is pairable: people, access, the readers, the contractor."""
    pid = world["property"]["id"]

    # ---- people --------------------------------------------------------------------------------
    if world.get("seedStage") == "devices":
        roster = plantdef.people(people)
        rec.step("people", f"{len(roster)} people created and photographed")
        t_people = time.monotonic()

        def create_person(p):
            _, created = api.post("/api/admin/users", {"email": p["email"], "name": p["name"], "role": p["role"]},
                                  expect=201)
            return {**p, "id": created["userId"], "temporaryPassword": created["temporaryPassword"]}

        with ThreadPoolExecutor(max_workers=4) as pool:
            world["people"] = list(pool.map(create_person, roster))

        # The four administrators sign in (and replace their temporary passwords); set-up work from
        # here on is spread across them, each inside their own production rate limit.
        for p in world["people"]:
            if p["role"] == "admin":
                acct = Account(p["email"], p["temporaryPassword"], f"admin {p['name']}")
                api.login(acct)
                api.change_password(acct, STAFF_PASSWORD)
                p["password"] = STAFF_PASSWORD
                api.admins.append(acct)
        world["accounts"]["admins"] = [{"email": a.email, "password": a.password, "label": a.label} for a in api.admins]

        by_key = {p["key"]: p for p in world["people"]}
        recipients = [("admin-1", "facility_manager"), ("shift-a-1", "operations_lead")]
        for key, role in recipients:
            status, body = api.post(f"/api/havenzhub/properties/{pid}/staff", {"userId": by_key[key]["id"], "role": role})
            if status == 409:
                api.put(f"/api/havenzhub/properties/{pid}/staff/{by_key[key]['id']}", {"role": role}, expect=(200, 204))
        world["alertRecipients"] = [{"id": by_key[k]["id"], "email": by_key[k]["email"], "name": by_key[k]["name"],
                                     "role": r} for k, r in recipients]

        photo_failures = []

        def enrol(indexed):
            index, p = indexed
            status, body = api.upload_photo(f"/api/havenzhub/facialrecognition/enroll/photo/{p['id']}",
                                            photo_jpeg(p["name"], index))
            if status != 200:
                photo_failures.append((p["name"], status, str(body)[:200]))

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(enrol, enumerate(world["people"])))
        rec.check("every person's photo is accepted", not photo_failures,
                  f"{len(world['people']) - len(photo_failures)} of {len(world['people'])} photos enrolled"
                  + (f"; failures: {photo_failures[:3]}" if photo_failures else ""))
        rec.metric("create + photograph 60 people (inside production rate limits)", round(time.monotonic() - t_people, 1), "s")
        world["seedStage"] = "people"
        save_world(run_dir, world)
        record.save_scenario(run_dir, rec)

    pairs = sum(len([t for t in world["terminals"] if t["areaKey"] in p["areas"]]) for p in world["people"])

    # ---- door access ---------------------------------------------------------------------------
    if world.get("seedStage") == "people" and not paced:
        rec.step("grants", "Door access granted group by group with the bulk grant, as an administrator would: "
                 "shifts A and B, office, maintenance, IT, administrators")
        world["grantsStartedAt"] = iso()
        for group, _, areas, _ in plantdef.GROUPS:
            members = [p["id"] for p in world["people"] if p["group"] == group and p["areas"] == areas]
            if members:
                api.post("/api/admin/area-access/bulk",
                         {"userIds": members, "areaIds": [world["areas"][a]["id"] for a in areas],
                          "accessLevel": "standard"}, expect=200, timeout=300)
        for p in world["people"]:
            group_areas = next(g[2] for g in plantdef.GROUPS if g[0] == p["group"])
            if p["areas"] != group_areas:
                api.post("/api/admin/area-access/bulk",
                         {"userIds": [p["id"]], "areaIds": [world["areas"][a]["id"] for a in p["areas"]],
                          "accessLevel": "standard"}, expect=200, timeout=300)
        world["seedStage"] = "granted"
        save_world(run_dir, world)

    if world.get("seedStage") == "granted":
        bulk_outcome(api, rec, world, pairs, bulk_patience)
        world["seedStage"] = "bulk-judged"
        save_world(run_dir, world)
        record.save_scenario(run_dir, rec)

    if world.get("seedStage") in ("people", "bulk-judged"):
        paced_enrolment(api, rec, world, pairs, fresh=world["seedStage"] == "people")
        problems = reader_roster_problems(world)
        rec.check("each reader holds exactly the people granted its door, with a face and in the door group",
                  not problems, "20 of 20 readers match" if not problems else f"mismatches: {problems[:6]}")
        world["seedStage"] = "synced"
        save_world(run_dir, world)
        record.save_scenario(run_dir, rec)

    # ---- contractor ----------------------------------------------------------------------------
    if world.get("seedStage") == "synced":
        world["seedingStage"] = "people loaded; contractor onboarding"
        try:
            onboard_contractor(api, rec, world, run_dir)
        except Exception as e:  # noqa: BLE001 - the plant is still usable without the contractor
            rec.check("contractor onboarding completes", False, f"stopped: {e}")
        world["seedStage"] = "contractor"
        save_world(run_dir, world)
        record.save_scenario(run_dir, rec)

    # ---- finish --------------------------------------------------------------------------------
    world["seedingStage"] = "complete"
    world["seedStage"] = "complete"
    world["seededAt"] = iso()
    mint_codes(api, world)
    save_world(run_dir, world)
    write_manifest(run_dir, world)
    if started is not None:
        rec.metric("whole commissioning", round((time.monotonic() - started) / 60, 1), "min")
    rec.metric("harness API calls refused as too many (HTTP 429)", api.rate_limited, "calls")
    rec.end()
    record.save_scenario(run_dir, rec)
    stack.update_state(seededAt=iso(), runDir=str(run_dir))
    log(f"done: {rec.passed_count()} of {len(rec.assertions)} checks passed")
    log(f"run folder: {run_dir}")
    return run_dir


def bulk_outcome(api, rec, world, pairs, patience):
    """
    What the bulk grant did to the readers, judged after a fixed wait.

    Granting a whole roster in a few clicks is what the product offers and what an administrator
    will do on enrolment day, so that is what is tried first. This gives it `patience` seconds
    from the first grant and then records, in numbers, how far it got and what the backend, the
    database and the agent went through meanwhile.
    """
    started = parse_iso(world.get("grantsStartedAt")) or utc_now()
    refused = {"n": 0}

    def done():
        try:
            counts = sync_counts()
        except Exception:  # noqa: BLE001
            refused["n"] += 1           # the database would not give the harness a connection
            log("  sync: the database refused a connection (all in use)")
            return False
        finished = counts.get(("CreateOrUpdate", "Succeeded"), 0)
        log(f"  sync: {finished} of {pairs} pairs on their readers")
        return finished >= pairs
    left = patience - (utc_now() - started).total_seconds()
    ok, _ = wait_until(done, max(1, left), 20)
    elapsed = (utc_now() - started).total_seconds()

    counts = sync_counts()
    finished = counts.get(("CreateOrUpdate", "Succeeded"), 0)
    errors = stack.psql("select left(last_error, 60), count(*), max(attempt_count), min(next_attempt_at), max(next_attempt_at) "
                        "from iot.terminal_user_syncs where status <> 'Succeeded' group by 1 order by 2 desc limit 1") or []
    since = world.get("grantsStartedAt")
    api_log = stack.container_logs("api", since=since)
    agent_log = stack.container_logs("agent", since=since)
    expired = api_log.count("expired as expired")
    unknown = api_log.count("expired as unknown")
    refusals = stack.agent_refusals(text=agent_log)
    agent_429, results_lost, heartbeats_lost = refusals["total"], refusals["results"], refusals["heartbeats"]
    discarded = agent_log.count("expired before we collected")
    prior = world.get("bulkEnrolment") or {}
    refused_total = refused["n"] + int(prior.get("databaseRefusedHarness") or 0)
    world["bulkEnrolment"] = {"pairs": pairs, "onReadersAfterWait": finished, "waitSeconds": round(elapsed),
                              "commandsExpiredUncollected": expired, "commandsEndedUnknown": unknown,
                              "agentCallsRefused429": agent_429, "agentResultsLost": results_lost,
                              "agentHeartbeatsFailed": heartbeats_lost, "agentDiscardedAsExpired": discarded,
                              "databaseRefusedHarness": refused_total,
                              "lastError": errors[0][0] if errors else None,
                              "nextRetryBetween": [errors[0][3], errors[0][4]] if errors else None}
    rec.metric("bulk grant: person-on-reader pairs done after the wait", f"{finished} of {pairs}", "",
               f"{elapsed / 60:.1f} min after the grants were made")
    rec.metric("bulk grant: reader commands that expired before the agent could take them", expired, "commands")
    rec.metric("bulk grant: agent calls refused as too many (HTTP 429)", agent_429, "calls",
               f"{results_lost} were results of work the reader had already done; {heartbeats_lost} were heartbeats; "
               f"{refusals['polls']} were requests for work")
    rec.check("a bulk grant for the whole roster reaches every reader", bool(ok),
              f"all {pairs} pairs on their readers in {elapsed / 60:.1f} min" if ok else
              f"{finished} of {pairs} pairs on their readers {elapsed / 60:.1f} min after the grants; the other "
              f"{pairs - finished} are waiting for a retry"
              + (f" ({errors[0][2]} attempts so far, next at {str(errors[0][3])[11:19]}-{str(errors[0][4])[11:19]} UTC) with the error "
                 f"\"{errors[0][0]}...\"" if errors else "")
              + f"; {expired} reader commands expired uncollected", finding="F-BULK-ENROL")
    rec.check("the agent's own traffic stays inside its rate limit during enrolment", agent_429 == 0,
              f"{agent_429} agent call(s) refused with HTTP 429 ({results_lost} of them results of work the reader had "
              f"already done, {heartbeats_lost} heartbeats, {refusals['polls']} requests for work) - the limit is 120 "
              "calls a minute for the whole site agent",
              finding="F-AGENT-RATE-LIMIT")
    rec.check("the database stays reachable while the roster is pushed", refused_total == 0,
              "the harness could always get a database connection" if refused_total == 0 else
              f"the database refused the harness {refused_total} time(s) with 'too many clients': the backend was "
              "holding every connection the server allows", finding="F-BULK-ENROL")


def paced_enrolment(api, rec, world, pairs, fresh):
    """
    Enrolment at a pace the door command queue can take: two people at a time, each finished
    before the next is started.

    After a bulk grant has stalled this uses the admin page's own "re-sync" for each person still
    outstanding. On a fresh plant (`seed --paced`) it grants each person's access one person at a
    time instead of in bulk.
    """
    rec.step("paced-enrolment", "People are put on the readers two at a time, waiting for each to finish"
             + ("" if fresh else " (the admin page's re-sync, person by person, for everyone the bulk grant left behind)"))
    t0 = time.monotonic()
    since = iso()
    terminals_by_area = {}
    for t in world["terminals"]:
        terminals_by_area.setdefault(t["areaKey"], []).append(t)

    # "Done" is asked of the readers themselves. The backend's sync table cannot answer it by
    # counting: a re-sync adds new rows beside the old ones, so one door can hold three
    # "succeeded" rows while another door of the same person holds none.
    cache = {"at": 0.0, "rosters": {}}
    cache_lock = threading.Lock()

    def rosters():
        with cache_lock:
            if time.monotonic() - cache["at"] > 2.0:
                with ThreadPoolExecutor(max_workers=10) as pool:
                    details = list(pool.map(readers.detail, [t["number"] for t in world["terminals"]]))
                cache["rosters"] = {d["number"]: {u["registration"] for u in d["userList"] if u["hasFace"] and u["inGroup"]}
                                    for d in details}
                cache["at"] = time.monotonic()
            return cache["rosters"]

    def outstanding(person):
        held = rosters()
        doors = [t["number"] for a in person["areas"] for t in terminals_by_area.get(a, [])]
        return sum(1 for n in doors if person["id"] not in held.get(n, set()))

    todo = [p for p in world["people"] if fresh or outstanding(p) > 0]
    slow = []

    def enrol(person):
        t1 = time.monotonic()
        for attempt in range(3):
            if fresh and attempt == 0:
                api.post("/api/admin/area-access/bulk",
                         {"userIds": [person["id"]], "areaIds": [world["areas"][a]["id"] for a in person["areas"]],
                          "accessLevel": "standard"}, expect=200, timeout=300)
            else:
                api.post(f"/api/admin/area-access/users/{person['id']}/resync", sensitive=True, timeout=300)
            ok, _ = wait_until(lambda: outstanding(person) == 0, 150, 3)
            if ok:
                log(f"  {person['name']}: on all their readers after {time.monotonic() - t1:.0f}s")
                return time.monotonic() - t1
        log(f"  {person['name']}: still missing from {outstanding(person)} reader(s) after three tries")
        slow.append(person["name"])
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        times = [t for t in pool.map(enrol, todo) if t is not None]
    took = time.monotonic() - t0
    finished = pairs - sum(outstanding(p) for p in world["people"])
    agent_429 = agent_429_count(since)
    rec.metric("paced enrolment: people put on their readers", len(todo), "people", f"in {took / 60:.1f} min, two at a time")
    rec.metric("paced enrolment: per person, typical", round(sorted(times)[len(times) // 2], 1) if times else None, "s")
    rec.metric("paced enrolment: agent calls refused as too many (HTTP 429)", agent_429, "calls")
    rec.check("paced two at a time, every person reaches every reader they have access to",
              finished >= pairs and not slow,
              f"all {pairs} pairs on their readers; the {len(todo)} people took {took / 60:.1f} min" if finished >= pairs and not slow
              else f"{finished} of {pairs} pairs done; not finished for: {slow[:5]}")


def resume_seed(people, paced, bulk_patience):
    """Pick a seed up where it stopped (a crash, or a run interrupted while waiting on the readers)."""
    run_dir = record.current_run_dir()
    world = load_world(run_dir)
    if world.get("seedStage") == "complete":
        raise SystemExit("this plant is already fully seeded")
    if world.get("seedStage") is None:
        raise SystemExit("the seed stopped before the plant was pairable; `rehearsal.py reset` and seed again")
    api = api_from_world(world)
    timeline = record.Timeline(run_dir)
    rec = record.ScenarioRecord("commissioning", "Commissioning: the plant is built through the real API", "",
                                run_dir, timeline)
    saved = (record.load_json(Path(run_dir) / "results.json", {}).get("scenarios") or {}).get("commissioning")
    if saved:
        rec.simulated, rec.started_at = saved.get("simulated", ""), saved.get("startedAt")
        rec.assertions, rec.metrics = saved.get("assertions", []), saved.get("metrics", {})
        rec.notes, rec.finding_ids = saved.get("notes", []), saved.get("findings", [])
    log(f"resuming the seed in {run_dir} from stage '{world['seedStage']}'")
    ensure_feeder(run_dir)
    return finish_seed(run_dir, world, api, rec, people, paced, None, bulk_patience)


def commissioning_voltage_check(api, rec, world):
    """
    What a 13.8 kV reading does before anyone has set a band for it.

    The default alert band for AC voltage is written for 120 V circuits. These engines generate
    at 13,800 V. A throwaway device is given one honest reading to see what the plant would get
    on the day engine data is first connected; it is then removed.
    """
    pid = world["property"]["id"]
    name = "Voltage probe (rehearsal, removed)"
    _, dev = api.post("/api/havenzhub/BmsDevice", {"propertyId": pid, "name": name, "type": "sensor",
                                                   "reportingIntervalSeconds": 15}, expect=201)
    status, body, _ = http("POST", f"{config.API}/api/iot/ingest",
                           body=[{"deviceKey": name, "metricType": "voltage_ac", "value": 13800, "unit": "V",
                                  "sourceObservedAt": iso(), "gatewayReceivedAt": iso(), "sourceAvailable": True}],
                           headers={"X-Hub-Key": world["sensorHub"]["apiKey"]}, timeout=20)

    def alert():
        _, page = api.get(f"/api/havenzhub/alerts?propertyId={pid}&status=open&pageSize=50", expect=200)
        return next((a for a in page["data"] if a.get("deviceId") == dev["id"]), None)
    found, _ = wait_until(alert, 15, 1.5)
    rec.check("a 13.8 kV reading on a new device does not page anyone", found is None,
              "no alert" if found is None else
              f"a {found['severity']} '{found['rule']}' alert opened at once: \"{found['message']}\" "
              "- the default voltage band is 100-140 V", finding="F-VOLTAGE-BAND")
    if found is not None:
        # Put it right the way an administrator would - a band that fits the machine, then two
        # in-band readings - so no stale alert is left standing on the walls.
        api.put(f"/api/havenzhub/BmsDevice/{dev['id']}/thresholds/voltage_ac",
                {"warnLow": 13110, "warnHigh": 14490, "critLow": 12420, "critHigh": 15180}, expect=(200, 204))
        for _ in range(3):
            time.sleep(1.2)
            http("POST", f"{config.API}/api/iot/ingest",
                 body=[{"deviceKey": name, "metricType": "voltage_ac", "value": 13800, "unit": "V",
                        "sourceObservedAt": iso(), "gatewayReceivedAt": iso(), "sourceAvailable": True}],
                 headers={"X-Hub-Key": world["sensorHub"]["apiKey"]}, timeout=20)
        cleared, _ = wait_until(lambda: alert() is None, 20, 1.5)
        rec.check("the probe alert clears once the band fits the machine", bool(cleared),
                  "resolved after two in-band readings" if cleared else "still open after 20 s")
    api.delete(f"/api/havenzhub/BmsDevice/{dev['id']}")
    world["voltageProbe"] = {"deviceId": dev["id"], "alert": found}
    return dev["id"]


def latest_invite_token(email, since_iso, timeout=90):
    """The contractor's invitation link exists only in the email, so the sink is where it is read."""
    def find():
        _, rows, _ = http("GET", f"{config.SINK}/messages?to={email}&since={since_iso}", timeout=10)
        for m in reversed(rows or []):
            match = re.search(r"#t=(ci1_[A-Za-z0-9_\-]+)", m.get("text") or "")
            if match:
                return match.group(1), m
        return None
    return wait_until(find, timeout, 3)


def onboard_contractor(api, rec, world, run_dir):
    """Phase-1 contractor flow, end to end: job, invitation, account, consent, phone photo, host approval."""
    c = plantdef.CONTRACTOR
    pid = world["property"]["id"]
    rec.step("contractor", f"Contractor {c['name']} ({c['vendorName']}): job, invitation email, phone enrolment, host approval")
    host = next(p for p in world["people"] if p["key"] == "admin-1")
    host_acct = next(a for a in api.admins if a.email == host["email"])
    reviewer_acct = next(a for a in api.admins if a.email == world["people"][1]["email"]) \
        if world["people"][1]["role"] == "admin" else api.admins[1]

    _, project = api.post("/api/havenzhub/projects", {"name": "Plant maintenance 2026", "status": "active"},
                          account=host_acct)
    if not isinstance(project, dict) or "id" not in project:
        _, project = api.post("/api/havenzhub/projects", {"name": "Plant maintenance 2026", "status": "planning"},
                              account=host_acct, expect=201)
    _, task = api.post("/api/havenzhub/tasks", {"title": "Coolant skid service - engine 1", "projectId": project["id"],
                                                "propertyId": pid, "status": "todo"}, account=host_acct, expect=201)
    since = iso()
    start = utc_now() - timedelta(minutes=1)
    end = utc_now() + timedelta(hours=8)
    _, job = api.put(f"/api/havenzhub/tasks/{task['id']}/access",
                     {"propertyId": pid, "windowStart": iso(start), "windowEnd": iso(end),
                      "areaIds": [world["areas"][a]["id"] for a in c["areas"]], "escortRequired": False,
                      "hostUserId": host["id"],
                      "contractors": [{"email": c["email"], "name": c["name"], "vendorName": c["vendorName"],
                                       "phone": c["phone"]}]}, account=host_acct, expect=200)
    profile = job["contractors"][0]

    found, waited = latest_invite_token(c["email"], since)
    rec.check("the contractor receives one invitation email", bool(found),
              f"invitation arrived in the mail sink after {waited:.0f}s" if found
              else f"no invitation email for {c['email']} within {waited:.0f}s")
    if not found:
        raise RuntimeError("no invitation email")
    token, message = found
    rec.metric("job created -> invitation email", round(waited, 1), "s", "sent by a worker that wakes every 30 s")

    status, accepted, _ = http("POST", f"{config.API}/api/contractor-invites/accept",
                               body={"token": token, "name": c["name"], "password": CONTRACTOR_PASSWORD}, timeout=30)
    rec.check("the contractor creates their account from the link", status == 201, f"accept answered HTTP {status}")
    acct = Account(c["email"].lower(), CONTRACTOR_PASSWORD, "contractor")
    login = api.login(acct)
    walled = login.get("requiredActions") or []

    _, notice = api.get("/api/contractor/me/consent-notice", account=acct, expect=200)
    api.post("/api/contractor/me/consent", {"version": notice["version"], "accepted": True}, account=acct, expect=200)
    status, enrolled = api.upload_photo("/api/havenzhub/facialrecognition/enroll/photo",
                                        photo_jpeg(c["name"], 999), account=acct)
    review = (enrolled or {}).get("review") if isinstance(enrolled, dict) else None
    rec.check("the contractor's phone photo waits for a host", status == 200 and bool(review)
              and enrolled.get("status") == "pending_review",
              f"upload answered HTTP {status}, status {(enrolled or {}).get('status') if isinstance(enrolled, dict) else enrolled}")
    holders = [n for n in range(1, 21) if any(u["registration"] == accepted.get("userId")
                                              for u in readers.detail(n)["userList"])]
    rec.check("no reader holds the contractor before a host approves the photo", not holders,
              "0 readers hold the contractor" if not holders else f"readers {holders} already hold them")

    t_approve = time.monotonic()
    status, approved = api.post(f"/api/havenzhub/contractor-photo-reviews/{review['id']}/approve",
                                account=reviewer_acct)
    rec.check("a host approves the photo", status == 200, f"approve answered HTTP {status}")
    doors = [t for t in world["terminals"] if t["areaKey"] in c["areas"]]

    def on_readers():
        held = [t["number"] for t in doors
                if any(u["registration"] == accepted["userId"] and u["hasFace"] and u["inGroup"]
                       for u in readers.detail(t["number"])["userList"])]
        return held if len(held) == len(doors) else None
    held, waited = wait_until(on_readers, 180, 3)
    rec.check("after approval the contractor is on the job's doors and no others", bool(held),
              f"on {len(doors)} of {len(doors)} job doors {waited:.0f}s after approval" if held
              else f"not on all {len(doors)} job doors after {waited:.0f}s")
    rec.metric("host approval -> contractor on the job's readers", round(time.monotonic() - t_approve, 1), "s")

    world["contractor"] = {"id": accepted["userId"], "profileId": profile["profileId"], "name": c["name"],
                           "email": c["email"].lower(), "password": CONTRACTOR_PASSWORD,
                           "areas": c["areas"], "projectId": project["id"], "seedTaskId": task["id"],
                           "requiredActionsAtFirstLogin": walled}
    save_world(run_dir, world)
