"""
The plant's sensor and engine feed.

Posts readings the way the Home Assistant gateway add-on does in production: `POST /api/iot/ingest`
with the gateway's own key, an array of readings, each carrying when the device observed the value
(`sourceObservedAt`) and when the gateway saw it (`gatewayReceivedAt`). One batch every 15 seconds,
which is the gateway's default poll.

Four engines run between roughly 1,900 and 2,400 kW with slow drift around their own set points;
server-room temperatures wander a few tenths of a degree; contacts stay closed; leak sensors stay
dry - until a scenario says otherwise. Scenarios steer individual devices through a small control
file, so the feed can keep running as its own process between scenarios (the walls need live data
while screens are being paired) and still be driven wet / dry / silent on cue:

    {"leak-engine-1": {"mode": "wet"}, "temp-server-104": {"mode": "silent"}}

Run on its own:  python -m plant.feeder --world <run-dir>/world.json
"""

import argparse
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .util import http, iso

TICK_SECONDS = 15
BINARY_HEARTBEAT_SECONDS = 40


def control_path():
    return config.stack_dir() / "feeder-control.json"


def read_control():
    try:
        return json.loads(control_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def set_control(device_key, mode=None):
    """
    Steer one device: mode is 'wet' | 'dry' | 'silent' | 'open' | 'closed' | None (back to normal).
    Returns the moment of the change, which is the time the device "observed" its new state.
    """
    control = read_control()
    since = iso()
    if mode is None:
        control.pop(device_key, None)
    else:
        control[device_key] = {"mode": mode, "since": since}
    tmp = control_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(control), encoding="utf-8")
    os.replace(tmp, control_path())
    return since


def binary_observed(entry, now=None):
    """
    When a leak or contact sensor last spoke.

    These devices report when their state changes and then only now and again (a heartbeat). The
    gateway, polling every 15 seconds, re-posts the same observation in between - which the
    backend correctly counts as "repeated", not as fresh evidence. Getting this wrong in a
    simulator matters: a leak sensor that seemed to re-measure every poll would confirm a leak
    early and would never look stale. The heartbeat is every BINARY_HEARTBEAT_SECONDS, counted
    from the last change, so this process and a scenario posting the change itself agree on it.
    """
    now = time.time() if now is None else now
    base = 0.0
    if entry and entry.get("since"):
        parsed = datetime.fromisoformat(entry["since"].replace("Z", "+00:00"))
        base = parsed.timestamp()
    beats = int(max(0.0, now - base) // BINARY_HEARTBEAT_SECONDS)
    moment = base + beats * BINARY_HEARTBEAT_SECONDS
    return iso(datetime.fromtimestamp(moment, timezone.utc))


def clear_control():
    try:
        control_path().unlink()
    except FileNotFoundError:
        pass


class Feeder:
    def __init__(self, world, seed=107):
        self.world = world
        self.api = config.API
        self.hub_key = world["sensorHub"]["apiKey"]
        self.devices = world["devices"]
        self.rng = random.Random(seed)
        self.sent = 0
        self.batches = 0
        self.failures = 0
        self.last_error = None
        self.energy_kwh = {}
        self.engine_kw = {}
        setpoints = [2250.0, 2180.0, 2320.0, 2060.0]
        self.setpoint = {}
        for i, dev in enumerate(d for d in self.devices if d["kind"] == "engine"):
            self.setpoint[dev["key"]] = setpoints[i % len(setpoints)]
            self.engine_kw[dev["key"]] = setpoints[i % len(setpoints)]
            self.energy_kwh[dev["key"]] = 1_000_000.0 + 250_000.0 * i
        self.temp = {d["key"]: d.get("base", 22.0) for d in self.devices if d["kind"] == "temperature"}
        self.last_tick = time.time()

    # -- one reading -----------------------------------------------------------

    @staticmethod
    def reading(device, metric, value, unit, observed):
        row = {"deviceKey": device["name"], "metricType": metric, "value": round(value, 3),
               "sourceObservedAt": observed, "sourceChangedAt": observed,
               "gatewayReceivedAt": iso(), "sourceAvailable": True}
        if unit:
            row["unit"] = unit           # the gateway sends a unit only when the entity has one
        return row

    def build(self, control=None, only=None):
        """Every device's readings for this moment, honouring the control file."""
        control = read_control() if control is None else control
        now = time.time()
        elapsed_h = max(0.0, now - self.last_tick) / 3600.0
        self.last_tick = now
        observed = iso()
        out = []
        total_engine_kw = 0.0
        for dev in self.devices:
            key, kind = dev["key"], dev["kind"]
            mode = (control.get(key) or {}).get("mode")
            if only is not None and key not in only:
                continue
            if mode == "silent":
                continue
            if kind == "engine":
                # Mean-reverting drift, clamped to the band a loaded 2518 kW set actually runs in.
                kw = self.engine_kw[key]
                kw += 0.15 * (self.setpoint[key] - kw) + self.rng.gauss(0, 18.0)
                kw = max(1900.0, min(2400.0, kw))
                self.engine_kw[key] = kw
                self.energy_kwh[key] += kw * elapsed_h
                volts = 13800.0 + self.rng.gauss(0, 25.0)
                amps = kw * 1000.0 / (math.sqrt(3) * volts * 0.95)
                total_engine_kw += kw
                out += [self.reading(dev, "power_consumption", kw, "kW", observed),
                        self.reading(dev, "voltage_ac", volts, "V", observed),
                        self.reading(dev, "current", amps, "A", observed),
                        self.reading(dev, "frequency", 60.0 + self.rng.gauss(0, 0.01), "Hz", observed),
                        self.reading(dev, "energy_usage", self.energy_kwh[key], "kWh", observed),
                        self.reading(dev, "equipment_temp", 88.0 + self.rng.gauss(0, 0.6), "°C", observed)]
            elif kind == "temperature":
                t = self.temp[key] + 0.1 * (dev.get("base", 22.0) - self.temp[key]) + self.rng.gauss(0, 0.08)
                self.temp[key] = t
                out.append(self.reading(dev, "temperature", t, "°C", observed))
            elif kind == "leak":
                out.append(self.reading(dev, "water_detection", 1 if mode == "wet" else 0, "",
                                        binary_observed(control.get(key), now)))
            elif kind == "contact":
                out.append(self.reading(dev, "door_status", 1 if mode == "open" else 0, "",
                                        binary_observed(control.get(key), now)))
            elif kind == "meter":
                house = 180.0 + self.rng.gauss(0, 4.0)
                kw = house if key == "meter-house" else max(0.0, sum(self.engine_kw.values()) - house)
                out += [self.reading(dev, "power_consumption", kw, "kW", observed),
                        self.reading(dev, "voltage_ac", 13800.0 + self.rng.gauss(0, 25.0), "V", observed)]
        return out

    def post(self, readings):
        if not readings:
            return 200, {"accepted": 0}
        try:
            status, body, _ = http("POST", f"{self.api}/api/iot/ingest", body=readings,
                                   headers={"X-Hub-Key": self.hub_key, "X-Agent-Version": "rehearsal-feeder"},
                                   timeout=20)
        except Exception as e:  # noqa: BLE001 - the backend restarting mid-run is one of the scenarios
            self.failures += 1
            self.last_error = str(e)
            return 0, None
        self.batches += 1
        if status == 200 and isinstance(body, dict):
            self.sent += body.get("accepted", 0)
            if body.get("rejected") or body.get("unknownDevices"):
                self.last_error = f"rejected={body.get('rejected')} unknown={body.get('unknownDevices')}"
        else:
            self.failures += 1
            self.last_error = f"HTTP {status}: {str(body)[:200]}"
        return status, body

    def tick(self):
        return self.post(self.build())

    def post_now(self, device_key, metric, value, unit="", observed=None):
        """One reading, immediately - a sensor reporting a change between two gateway polls."""
        dev = next(d for d in self.devices if d["key"] == device_key)
        return self.post([self.reading(dev, metric, value, unit, observed or iso())])


def status_path():
    return config.stack_dir() / "feeder-status.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--tick", type=float, default=TICK_SECONDS)
    args = ap.parse_args()
    world = json.loads(Path(args.world).read_text(encoding="utf-8"))
    feeder = Feeder(world)
    print(f"feeding {len(feeder.devices)} devices to {feeder.api} every {args.tick:g}s", flush=True)
    while True:
        started = time.time()
        status, _ = feeder.tick()
        try:
            status_path().write_text(json.dumps({
                "at": iso(), "pid": os.getpid(), "batches": feeder.batches, "accepted": feeder.sent,
                "failures": feeder.failures, "lastError": feeder.last_error, "lastStatus": status}),
                encoding="utf-8")
        except OSError:
            pass
        time.sleep(max(0.5, args.tick - (time.time() - started)))


if __name__ == "__main__":
    main()
