"""
Tests for the parts of the rehearsal that can be wrong without the stack being up: the stand-ins'
own logic and the harness's arithmetic. A simulator that is itself wrong produces confident,
false results, so the pieces that decide "did the door open" and "is this reading fresh" are
pinned here.

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tools/plant-rehearsal/tests -q
"""

import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "sim"))
sys.path.insert(0, str(HERE.parent))            # tools/, for fake_reader

import readers_sim  # noqa: E402
import sink  # noqa: E402
from plant import api, config, door_server, feeder, findings, plantdef, util  # noqa: E402


# -- the reader: who gets through a door ---------------------------------------------------------

def reader_with(user=None, face=True, group=True):
    reader = readers_sim.PlantReader(1, "10.107.0.11")
    if user is not None:
        reader.users[1] = dict(user)
        if face:
            reader.faces.add(1)
        if group:
            reader.groups.add((1, 1))
    return reader


def test_known_face_in_group_opens_once():
    reader = reader_with({"registration": "u-1", "name": "A"})
    result = reader.scan("u-1")
    assert result["opened"] and result["event"] == readers_sim.EVENT_GRANTED
    assert len(reader.opens) == 1 and reader.opens[0]["cause"] == "face"


def test_unknown_face_is_not_identified_and_names_nobody():
    reader = reader_with({"registration": "u-1", "name": "A"})
    result = reader.scan("somebody-else")
    assert not result["opened"] and result["event"] == readers_sim.EVENT_NOT_IDENTIFIED
    assert result["userId"] == 0 and reader.opens == []


def test_user_without_a_face_template_cannot_be_recognised():
    reader = reader_with({"registration": "u-1", "name": "A"}, face=False)
    assert reader.scan("u-1")["event"] == readers_sim.EVENT_NOT_IDENTIFIED


def test_user_not_in_the_door_group_is_denied():
    reader = reader_with({"registration": "u-1", "name": "A"}, group=False)
    result = reader.scan("u-1")
    assert not result["opened"] and result["event"] == readers_sim.EVENT_DENIED and result["userId"] == 1


def test_dated_window_is_enforced_in_reader_local_time():
    reader = reader_with({"registration": "u-1", "name": "A"})
    now = reader.now()
    reader.users[1].update(begin_time=now + 60, end_time=now + 120)
    assert reader.scan("u-1")["event"] == readers_sim.EVENT_DENIED            # before it opens
    reader.users[1].update(begin_time=now - 60, end_time=now + 120)
    assert reader.scan("u-1")["event"] == readers_sim.EVENT_GRANTED           # inside
    reader.users[1].update(begin_time=0, end_time=now - 1)
    assert reader.scan("u-1")["event"] == readers_sim.EVENT_DENIED            # after it ends
    assert len(reader.opens) == 1


def test_remote_open_is_counted_as_remote_and_logged_by_the_reader():
    reader = reader_with()
    reader.sessions.add("s")
    code, _ = reader.handle("execute_actions.fcgi", {"actions": [{"action": "sec_box", "parameters": "door=1"}]}, "s")
    assert code == 200 and [o["cause"] for o in reader.opens] == ["remote"]
    assert reader.access_logs[-1]["event"] == readers_sim.EVENT_REMOTE_OPEN


def test_deleting_a_user_removes_the_face_too():
    reader = reader_with({"registration": "u-1", "name": "A"})
    reader.sessions.add("s")
    reader.handle("destroy_objects.fcgi", {"object": "users", "where": {"users": {"id": 1}}}, "s")
    assert reader.users == {} and reader.faces == set()
    assert reader.scan("u-1")["event"] == readers_sim.EVENT_NOT_IDENTIFIED


def test_powered_off_reader_decides_nothing():
    reader = reader_with({"registration": "u-1", "name": "A"})
    reader.mode = "off"
    result = reader.scan("u-1")
    assert result["powered"] is False and not result["opened"] and reader.access_logs == []


# -- the bucket stand-in ---------------------------------------------------------------------------

def test_crc32c_matches_the_published_check_value():
    # CRC-32C of "123456789" is 0xE3069283; the storage client verifies uploads against this.
    assert sink.crc32c_b64(b"123456789") == "4waSgw=="
    assert sink.crc32c_b64(b"") == "AAAAAA=="


def test_object_resource_reports_size_and_hashes():
    resource = sink.object_resource("b", "x/y.jpg", {"data": b"abc", "contentType": "image/jpeg", "created": "t"})
    assert resource["size"] == "3" and resource["name"] == "x/y.jpg" and resource["crc32c"] == sink.crc32c_b64(b"abc")


# -- the feed: when a leak sensor "last spoke" --------------------------------------------------------

def test_binary_sensor_reports_on_change_then_only_on_its_heartbeat():
    since = "2026-10-01T03:00:00.000Z"
    base = util.parse_iso(since).timestamp()
    entry = {"mode": "wet", "since": since}
    assert feeder.binary_observed(entry, base + 1) == since
    assert feeder.binary_observed(entry, base + feeder.BINARY_HEARTBEAT_SECONDS - 1) == since
    later = feeder.binary_observed(entry, base + feeder.BINARY_HEARTBEAT_SECONDS + 1)
    assert util.parse_iso(later).timestamp() == base + feeder.BINARY_HEARTBEAT_SECONDS


def test_heartbeat_is_longer_than_the_leak_confirmation_window():
    # A second NEW wet reading confirms a leak early. The stand-in must not produce one by accident.
    assert feeder.BINARY_HEARTBEAT_SECONDS > 30
    assert feeder.BINARY_HEARTBEAT_SECONDS < 2 * config.BINARY_REPORTING_INTERVAL_SECONDS


def test_engines_stay_in_the_band_a_loaded_set_runs_in():
    world = {"api": "http://unused", "sensorHub": {"apiKey": "k"},
             "devices": [{"id": str(n), "key": f"engine-{n}", "name": f"Engine {n} Generator", "kind": "engine"}
                         for n in range(1, 5)]}
    f = feeder.Feeder(world)
    for _ in range(200):
        rows = f.build(control={})
    kw = [r["value"] for r in rows if r["metricType"] == "power_consumption"]
    assert len(kw) == 4 and all(1900 <= v <= 2400 for v in kw)
    assert all(r["unit"] == "kW" for r in rows if r["metricType"] == "power_consumption")
    assert all("sourceObservedAt" in r and "gatewayReceivedAt" in r for r in rows)


def test_silent_device_is_left_out_of_the_batch():
    world = {"api": "http://unused", "sensorHub": {"apiKey": "k"},
             "devices": [{"id": "1", "key": "leak-a", "name": "Leak A", "kind": "leak"},
                         {"id": "2", "key": "leak-b", "name": "Leak B", "kind": "leak"}]}
    rows = feeder.Feeder(world).build(control={"leak-a": {"mode": "silent"}, "leak-b": {"mode": "wet", "since": util.iso()}})
    assert [r["deviceKey"] for r in rows] == ["Leak B"] and rows[0]["value"] == 1


# -- the plant definition ------------------------------------------------------------------------------

def test_plant_matches_the_site():
    assert len(plantdef.DOORS) == 20 and sorted(d[0] for d in plantdef.DOORS) == list(range(1, 21))
    assert sum(1 for d in plantdef.DOORS if d[3]) == 10
    assert len(plantdef.WALLS) == 13
    assert {w[1] for w in plantdef.WALLS} == {"AHI-107-E", "AHI-107-N", "AHI-107-W"}
    assert sum(1 for w in plantdef.WALLS if w[1] == "AHI-107-E") == 7
    assert all(e["ratedKw"] == 2518 for e in plantdef.ENGINES) and len(plantdef.ENGINES) == 4
    assert all(re.search(r"\bengine\s*\d+\b", e["name"], re.I) for e in plantdef.ENGINES)
    assert not any(t in plantdef.SKIPPED_TEMPLATES for _, _, t in plantdef.WALLS)


def test_roster_is_sixty_distinct_people_with_the_named_parts():
    roster = plantdef.people(60)
    assert len(roster) == 60 and len({p["email"] for p in roster}) == 60
    assert all(p["email"] == p["email"].lower() for p in roster)
    parts = {p["key"]: p.get("part") for p in roster if p.get("part")}
    assert set(parts) == {"admin-1", "shift-a-1", "shift-a-2", "office-1"}
    leaver = next(p for p in roster if p["key"] == "office-1")
    assert set(leaver["areas"]) == set(plantdef.ALL_AREAS)


# -- the harness's own arithmetic -------------------------------------------------------------------------

def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert util.percentile(values, 50) == 50 and util.percentile(values, 95) == 95
    assert util.percentile([7], 95) == 7 and util.percentile([], 50) is None


def test_parse_iso_takes_dotnet_timestamps():
    assert util.parse_iso("2026-10-01T02:16:37.9251203Z").microsecond == 925120
    assert util.parse_iso("2026-10-01T02:16:37").tzinfo is not None
    assert util.parse_iso("") is None


def test_account_pacing_stays_under_the_production_limits():
    assert api.GENERAL_PER_MINUTE < 100 and api.SENSITIVE_PER_MINUTE < 10
    account = api.Account("a@b", "pw")
    started = time.monotonic()
    for _ in range(api.SENSITIVE_PER_MINUTE):
        account.reserve(sensitive=True)
    assert time.monotonic() - started < 1.0
    assert account.sensitive_headroom() == 0 and account.headroom() == api.GENERAL_PER_MINUTE - api.SENSITIVE_PER_MINUTE


def test_door_page_backend_address_is_the_only_thing_replaced():
    page = b'<script>\n  var API = "https://havenz-backend-599129251048.us-central1.run.app";\n  var X = 1;\n</script>'
    body, count = door_server.PRODUCTION_BACKEND.subn(b"http://localhost:5100", page)
    assert count == 1 and b'var API = "http://localhost:5100";' in body and b"var X = 1;" in body


def test_no_override_touches_a_rate_limit():
    assert not any("RateLimiting" in key for key in config.API_ENV)
    assert not any("RateLimit" in row[1] for row in config.OVERRIDES)
    assert config.API_ENV["ASPNETCORE_ENVIRONMENT"] == "Production"


def test_every_time_override_in_the_environment_is_declared_in_the_table():
    declared = " ".join(row[1] for row in config.OVERRIDES)
    for key in config.API_ENV:
        if key.startswith("Alerts__"):
            assert key in declared, f"{key} is set for the rehearsal but missing from the overrides table"


def test_findings_register_is_well_formed():
    ids = [f["id"] for f in findings.FINDINGS]
    assert len(ids) == len(set(ids))
    for f in findings.FINDINGS:
        assert f["severity"] in (findings.BLOCKS, findings.SHOULD, findings.NOTE)
        assert f["reproduction"] and f["cause"] and f["where"]
