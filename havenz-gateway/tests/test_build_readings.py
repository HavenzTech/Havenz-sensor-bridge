"""
A dead sensor must not look alive.

The gateway repeats whatever Home Assistant last knew about an entity on every poll. Until now
it posted only the number, so the cloud stamped every post with its own clock and a sensor whose
battery died three hours ago kept looking freshly measured. Home Assistant's state object already
says when the entity last reported (`last_reported`, or `last_updated` on older cores) and when
its value last changed (`last_changed`); it also says outright when the entity is `unavailable`.
These tests pin down that `build_readings` forwards all of that, and posts an unavailable entity
as an explicit "the source said gone" row instead of silently skipping it.

The states below are shaped exactly like a captured `GET /api/states` response (one object per
entity, the timestamp fields in HA's ISO-8601-with-offset form).

Run:  python3 -m pytest havenz-gateway/tests -q
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge  # noqa: E402


# A captured-shape /api/states object, keyed by entity id the way fetch_states returns it.
STATES = {
    # A live numeric sensor on a current core: last_reported moves on every report, last_updated /
    # last_changed only when something actually changed.
    "sensor.engine_1_power": {
        "entity_id": "sensor.engine_1_power",
        "state": "912.4",
        "attributes": {"unit_of_measurement": "kW", "device_class": "power", "friendly_name": "Engine 1 power"},
        "last_changed": "2026-09-16T13:45:10.204118+00:00",
        "last_updated": "2026-09-16T13:45:10.204118+00:00",
        "last_reported": "2026-09-16T13:59:58.771302+00:00",
        "context": {"id": "01J8", "parent_id": None, "user_id": None},
    },
    # An older core (before last_reported existed): last_updated is the best "observed" time.
    "sensor.hall_temperature": {
        "entity_id": "sensor.hall_temperature",
        "state": "21.5",
        "attributes": {"unit_of_measurement": "°C", "device_class": "temperature"},
        "last_changed": "2026-09-16T13:50:00.000000+00:00",
        "last_updated": "2026-09-16T13:55:00.000000+00:00",
        "context": {"id": "01J9", "parent_id": None, "user_id": None},
    },
    # A contact sensor: a word, not a number.
    "binary_sensor.rear_door": {
        "entity_id": "binary_sensor.rear_door",
        "state": "on",
        "attributes": {"device_class": "opening"},
        "last_changed": "2026-09-16T13:58:30.000000+00:00",
        "last_updated": "2026-09-16T13:58:30.000000+00:00",
        "last_reported": "2026-09-16T13:59:45.000000+00:00",
    },
    # The one that matters most: Home Assistant says the entity is gone.
    "sensor.exhaust_temperature": {
        "entity_id": "sensor.exhaust_temperature",
        "state": "unavailable",
        "attributes": {"unit_of_measurement": "°C", "device_class": "temperature", "restored": True},
        "last_changed": "2026-09-16T11:02:17.500000+00:00",
        "last_updated": "2026-09-16T11:02:17.500000+00:00",
        "last_reported": "2026-09-16T11:02:17.500000+00:00",
    },
    # unknown is the same story as unavailable.
    "sensor.fuel_level": {
        "entity_id": "sensor.fuel_level",
        "state": "unknown",
        "attributes": {"unit_of_measurement": "%"},
        "last_changed": "2026-09-16T12:00:00.000000+00:00",
        "last_updated": "2026-09-16T12:00:00.000000+00:00",
    },
    # Not a number and not a binary word: nothing to report, then or now.
    "sensor.genset_mode": {
        "entity_id": "sensor.genset_mode",
        "state": "idle",
        "attributes": {},
        "last_changed": "2026-09-16T13:00:00.000000+00:00",
        "last_updated": "2026-09-16T13:00:00.000000+00:00",
    },
}


def mapping(entity, key, metric, unit=None, tmin=None, tmax=None):
    return {"entity": entity, "deviceKey": key, "metricType": metric, "unit": unit,
            "thresholdMin": tmin, "thresholdMax": tmax}


MAPPINGS = [
    mapping("sensor.engine_1_power", "Engine 1 Power Feed", "power_consumption", tmax=2200),
    mapping("sensor.hall_temperature", "Hall Air", "temperature"),
    mapping("binary_sensor.rear_door", "Rear Door", "door_status"),
    mapping("sensor.exhaust_temperature", "Engine 1 Exhaust", "temperature", tmax=500),
    mapping("sensor.fuel_level", "Day Tank", "waste_level"),
    mapping("sensor.genset_mode", "Genset", "alarm_status"),
    mapping("sensor.not_in_ha", "Ghost", "temperature"),
]


def by_key(readings):
    return {r["deviceKey"]: r for r in readings}


def test_a_live_reading_carries_the_four_provenance_fields():
    r = by_key(bridge.build_readings(MAPPINGS, STATES))["Engine 1 Power Feed"]

    assert r["value"] == 912.4
    assert r["unit"] == "kW"
    assert r["thresholdMax"] == 2200
    assert r["sourceObservedAt"] == "2026-09-16T13:59:58.771302+00:00", "last_reported, not last_updated"
    assert r["sourceChangedAt"] == "2026-09-16T13:45:10.204118+00:00"
    assert r["sourceAvailable"] is True
    # The gateway's own clock, as an ISO-8601 UTC instant the backend can parse.
    received = datetime.fromisoformat(r["gatewayReceivedAt"])
    assert received.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - received).total_seconds()) < 5


def test_source_observed_at_prefers_last_reported_and_falls_back_to_last_updated():
    # last_reported is the only field that moves when a sensor re-reports an unchanged value;
    # on a core without it, last_updated is the best we have.
    r = by_key(bridge.build_readings(MAPPINGS, STATES))["Hall Air"]
    assert r["sourceObservedAt"] == "2026-09-16T13:55:00.000000+00:00"
    assert r["sourceChangedAt"] == "2026-09-16T13:50:00.000000+00:00"


def test_a_binary_state_is_still_mapped_and_carries_provenance():
    r = by_key(bridge.build_readings(MAPPINGS, STATES))["Rear Door"]
    assert r["value"] == 1.0
    assert r["sourceObservedAt"] == "2026-09-16T13:59:45.000000+00:00"
    assert r["sourceAvailable"] is True


def test_an_unavailable_entity_is_posted_as_gone_with_no_value():
    readings = by_key(bridge.build_readings(MAPPINGS, STATES))

    r = readings["Engine 1 Exhaust"]
    assert "value" not in r, "the source said gone; there is no number to send"
    assert r["sourceAvailable"] is False
    assert r["metricType"] == "temperature"
    assert r["sourceObservedAt"] == "2026-09-16T11:02:17.500000+00:00", "when HA last heard from it"
    assert r["thresholdMax"] == 500, "the band still travels so the device's stored band is not lost"
    assert "gatewayReceivedAt" in r


def test_unknown_is_treated_like_unavailable():
    r = by_key(bridge.build_readings(MAPPINGS, STATES))["Day Tank"]
    assert "value" not in r
    assert r["sourceAvailable"] is False


def test_a_state_that_is_neither_numeric_nor_binary_is_still_skipped():
    readings = by_key(bridge.build_readings(MAPPINGS, STATES))
    assert "Genset" not in readings


def test_a_mapping_with_no_state_in_home_assistant_is_skipped():
    readings = by_key(bridge.build_readings(MAPPINGS, STATES))
    assert "Ghost" not in readings


def test_every_reading_in_one_cycle_shares_one_gateway_clock():
    readings = bridge.build_readings(MAPPINGS, STATES)
    assert len({r["gatewayReceivedAt"] for r in readings}) == 1


def test_a_state_without_any_timestamps_still_posts_without_source_times():
    # Defensive: a hand-written state object (or a very old core) with no time fields.
    states = {"sensor.x": {"entity_id": "sensor.x", "state": "3", "attributes": {}}}
    r = bridge.build_readings([mapping("sensor.x", "X", "temperature")], states)[0]
    assert r["value"] == 3.0
    assert "sourceObservedAt" not in r
    assert "sourceChangedAt" not in r
    assert r["sourceAvailable"] is True
    assert "gatewayReceivedAt" in r
