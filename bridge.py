#!/usr/bin/env python3
"""
Havenz Sensor Bridge
====================

Two jobs each cycle, against the HavenzBMS backend:

  1. DISCOVERY  — report every sensor entity Home Assistant can see to
     POST /api/iot/discovery. New ones appear in Zhub as "available to connect".
     The response hands back the active mappings (what an admin has adopted).
  2. FORWARD    — for each active mapping (plus any static ones in config), read
     that entity's current value and POST it, HMAC-signed, to /api/iot/ingest.

So connecting a new sensor is done entirely in the Zhub UI — no editing this
config. `mappings` in the config still work as a fallback/override, but they're
optional now; the source of truth is what's adopted in the backend.

Pure standard library (no pip installs). Run:  python3 bridge.py config.json [--once]
"""

import base64
import hashlib
import hmac
import json
import logging
import sys
import time
import urllib.error
import urllib.request
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("havenz-bridge")

# HA device_classes that aren't physical sensor readings we care about.
JUNK_DEVICE_CLASSES = {
    "timestamp", "date", "duration", "enum", "data_size", "data_rate", "monetary",
}


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("api_url", "secret", "home_assistant"):
        if key not in cfg:
            raise SystemExit(f"config error: missing '{key}'")
    cfg.setdefault("ingest_path", "/api/iot/ingest")
    cfg.setdefault("discovery_path", "/api/iot/discovery")
    cfg.setdefault("poll_interval_seconds", 30)
    cfg.setdefault("mappings", [])
    return cfg


def fetch_states(ha, timeout=10):
    """GET every entity's current state from Home Assistant; return {entity_id: state_obj}."""
    req = urllib.request.Request(
        f"{ha['url'].rstrip('/')}/api/states",
        headers={"Authorization": f"Bearer {ha['token']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        states = json.loads(resp.read().decode("utf-8"))
    return {s.get("entity_id"): s for s in states if s.get("entity_id")}


def discovery_entities(states):
    """Filter HA states down to sensor-like entities worth reporting for discovery."""
    out = []
    for eid, st in states.items():
        domain = eid.split(".", 1)[0] if "." in eid else ""
        if domain not in ("sensor", "binary_sensor"):
            continue
        attrs = st.get("attributes", {}) or {}
        device_class = attrs.get("device_class")
        unit = attrs.get("unit_of_measurement")
        if not device_class and not unit:
            continue  # untyped/unitless — not a useful sensor reading
        if device_class in JUNK_DEVICE_CLASSES:
            continue
        raw = st.get("state")
        if raw in (None, "unknown", "unavailable", ""):
            continue
        out.append({
            "entityId": eid,
            "friendlyName": attrs.get("friendly_name"),
            "deviceClass": device_class,
            "unit": unit,
            "state": str(raw),
        })
    return out


def _sign(cfg, body):
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_b64 = base64.b64encode(body).decode("ascii")
    signature = "sha256=" + hmac.new(
        cfg["secret"].encode("utf-8"),
        f"{timestamp}.{body_b64}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Nonce": nonce,
        "X-Webhook-Signature": signature,
    }


def signed_post(cfg, path, payload, timeout=15):
    """HMAC-sign a JSON body and POST it; return the parsed JSON response."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    url = f"{cfg['api_url'].rstrip('/')}{path}"
    req = urllib.request.Request(url, data=body, method="POST", headers=_sign(cfg, body))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def open_permit_join(cfg, duration):
    """Ask Home Assistant to open its Zigbee join window (so a new sensor can pair)."""
    ha = cfg["home_assistant"]
    # HA services API path, e.g. /api/services/zha/permit. Override for Zigbee2MQTT setups.
    service = cfg.get("permit_join_service", "zha/permit")
    url = f"{ha['url'].rstrip('/')}/api/services/{service}"
    body = json.dumps({"duration": int(duration)}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {ha['token']}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10):
            log.info("opened Home Assistant pairing window for %ss — press the sensor's button now", duration)
    except Exception as e:  # noqa: BLE001
        log.error("failed to open pairing window (%s): %s", service, e)


def run_discovery(cfg, states):
    """Report the entity catalog; return the active mappings the backend hands back."""
    property_id = cfg.get("property_id")
    if not property_id:
        return []  # discovery needs to know which site; skip if not configured
    entities = discovery_entities(states)
    try:
        resp = signed_post(cfg, cfg["discovery_path"], {
            "propertyId": property_id,
            "source": "home-assistant",
            "entities": entities,
        })
        mappings = resp.get("mappings", []) or []
        log.info("discovery: reported %d entities, %d active mapping(s)", len(entities), len(mappings))
        permit = resp.get("permitJoin")
        if permit:
            open_permit_join(cfg, permit.get("duration", 60))
        return mappings
    except urllib.error.HTTPError as e:
        log.error("discovery rejected: HTTP %s — %s", e.code, e.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log.warning("discovery failed (will still forward known mappings): %s", e)
    return []


def combined_mappings(cfg, dynamic):
    """Union backend-adopted mappings with any static config mappings; dedupe by (entity, metric)."""
    out, seen = [], set()

    def add(entity, device_key, metric, unit, tmin, tmax):
        if not entity or not device_key or not metric:
            return
        key = (entity.lower(), metric)
        if key in seen:
            return
        seen.add(key)
        out.append({"entity": entity, "deviceKey": device_key, "metricType": metric,
                    "unit": unit, "thresholdMin": tmin, "thresholdMax": tmax})

    for m in dynamic:  # adopted-in-UI mappings win
        add(m.get("entityId"), m.get("deviceKey"), m.get("metricType"),
            m.get("unit"), m.get("thresholdMin"), m.get("thresholdMax"))
    for m in cfg.get("mappings", []):  # static fallback/override
        add(m.get("entity"), m.get("device_key"), m.get("metric_type"),
            m.get("unit"), m.get("thresholdMin"), m.get("thresholdMax"))
    return out


def build_readings(mappings, states):
    """Turn each mapping into a normalized reading using the already-fetched states."""
    readings = []
    for m in mappings:
        st = states.get(m["entity"])
        if not st:
            continue
        raw = st.get("state")
        if raw in (None, "unknown", "unavailable", ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue  # non-numeric (e.g. a binary_sensor word) — skip for metrics
        reading = {"deviceKey": m["deviceKey"], "metricType": m["metricType"], "value": value}
        unit = m.get("unit") or st.get("attributes", {}).get("unit_of_measurement")
        if unit:
            reading["unit"] = unit
        if m.get("thresholdMin") is not None:
            reading["thresholdMin"] = m["thresholdMin"]
        if m.get("thresholdMax") is not None:
            reading["thresholdMax"] = m["thresholdMax"]
        readings.append(reading)
    return readings


def run_once(cfg):
    try:
        states = fetch_states(cfg["home_assistant"])
    except Exception as e:  # noqa: BLE001
        log.error("could not read Home Assistant states: %s", e)
        return

    dynamic = run_discovery(cfg, states)
    mappings = combined_mappings(cfg, dynamic)
    readings = build_readings(mappings, states)

    if not readings:
        log.info("no readings this cycle (%d mappings)", len(mappings))
        return
    try:
        result = signed_post(cfg, cfg["ingest_path"], readings)
        log.info("posted %d readings — accepted=%s rejected=%s unknown=%s",
                 len(readings), result.get("accepted"), result.get("rejected"),
                 result.get("unknownDevices"))
    except urllib.error.HTTPError as e:
        log.error("ingest rejected: HTTP %s — %s", e.code, e.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log.error("ingest failed: %s", e)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 bridge.py <config.json> [--once]")
    cfg = load_config(sys.argv[1])
    once = "--once" in sys.argv[2:]

    log.info("Havenz sensor bridge -> %s  (discovery=%s, every %ss)",
             cfg["api_url"], bool(cfg.get("property_id")), cfg["poll_interval_seconds"])

    if once:
        run_once(cfg)
        return
    while True:
        run_once(cfg)
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
