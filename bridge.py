#!/usr/bin/env python3
"""
Havenz Sensor Bridge
====================

Reads sensor readings from Home Assistant and forwards them, HMAC-signed, to the
HavenzBMS IoT ingestion webhook (POST /api/iot/ingest).

This is the "consumer/facility" bridge: Home Assistant already speaks every sensor
protocol/brand (Zigbee, WiFi, Bluetooth, Tuya, Shelly, Aqara, Govee…), so this bridge
stays brand-agnostic — it just maps Home Assistant entities to Havenz devices/metrics,
signs, and posts. Add a new sensor = add one line to the config, never touch code.

Pure standard library (no pip installs) so it runs anywhere, including a bare Raspberry Pi.

Config: a JSON file (see config.example.json). Run:  python3 bridge.py config.json
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("havenz-bridge")


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    required = ["api_url", "secret", "home_assistant", "mappings"]
    for key in required:
        if key not in cfg:
            raise SystemExit(f"config error: missing '{key}'")
    cfg.setdefault("ingest_path", "/api/iot/ingest")
    cfg.setdefault("poll_interval_seconds", 30)
    return cfg


def read_ha_state(ha_url, token, entity_id, timeout=10):
    """GET one entity's current state from the Home Assistant REST API."""
    req = urllib.request.Request(
        f"{ha_url.rstrip('/')}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect_readings(cfg):
    """Turn each configured mapping into a normalized reading, skipping unavailable ones."""
    ha = cfg["home_assistant"]
    readings = []
    for m in cfg["mappings"]:
        try:
            state = read_ha_state(ha["url"], ha["token"], m["entity"])
        except Exception as e:  # noqa: BLE001 - one bad entity shouldn't stop the batch
            log.warning("skip %s: could not read from Home Assistant (%s)", m["entity"], e)
            continue

        raw = state.get("state")
        if raw in (None, "unknown", "unavailable", ""):
            log.debug("skip %s: state is %r", m["entity"], raw)
            continue

        try:
            value = float(raw)
        except (TypeError, ValueError):
            log.warning("skip %s: non-numeric state %r", m["entity"], raw)
            continue

        reading = {
            "deviceKey": m["device_key"],
            "metricType": m["metric_type"],
            "value": value,
        }
        # Prefer an explicit config unit; otherwise use HA's reported unit.
        unit = m.get("unit") or state.get("attributes", {}).get("unit_of_measurement")
        if unit:
            reading["unit"] = unit
        for opt in ("thresholdMin", "thresholdMax"):
            if opt in m:
                reading[opt] = m[opt]
        readings.append(reading)
    return readings


def post_signed(cfg, readings):
    """HMAC-sign the batch and POST it to the ingestion webhook (matches WebhookAuthenticator)."""
    body = json.dumps(readings, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_b64 = base64.b64encode(body).decode("ascii")
    signature = "sha256=" + hmac.new(
        cfg["secret"].encode("utf-8"),
        f"{timestamp}.{body_b64}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    url = f"{cfg['api_url'].rstrip('/')}{cfg['ingest_path']}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Nonce": nonce,
            "X-Webhook-Signature": signature,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_once(cfg):
    readings = collect_readings(cfg)
    if not readings:
        log.info("no readings this cycle")
        return
    try:
        result = post_signed(cfg, readings)
        log.info(
            "posted %d readings — accepted=%s rejected=%s unknown=%s",
            len(readings),
            result.get("accepted"),
            result.get("rejected"),
            result.get("unknownDevices"),
        )
    except urllib.error.HTTPError as e:
        log.error("ingest rejected: HTTP %s — %s", e.code, e.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log.error("ingest failed: %s", e)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 bridge.py <config.json> [--once]")
    cfg = load_config(sys.argv[1])
    once = "--once" in sys.argv[2:]

    log.info(
        "Havenz sensor bridge → %s%s  (%d mappings, every %ss)",
        cfg["api_url"], cfg["ingest_path"], len(cfg["mappings"]), cfg["poll_interval_seconds"],
    )

    if once:
        run_once(cfg)
        return

    while True:
        run_once(cfg)
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
