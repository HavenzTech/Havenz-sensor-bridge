"""
A screen without a browser.

Some assertions can only be made from where a screen stands: what the screen data says about a
sensor that has gone quiet, and what a screen is told when an emergency is announced for its
scope, or when it reconnects after missing the all-clear. The screen half proves what the real
pages paint; this proves what the backend sends them, using the same protocol the pages use -
pair with a code, trade the device key for a short token, read `/api/screens/me/*`, hold the hub
connection, and report the displayed emergency version in the heartbeat.

Probe screens are registered when a scenario needs them and retired when it ends, so they do not
sit in the fleet as screens that never show anything.
"""

import threading
import time

from . import config
from .signalr import SignalRClient
from .util import http


class ScreenProbe:
    def __init__(self, ctx, key, label, template="live-sensors", area_key=None):
        self.ctx = ctx
        self.key = key
        self.label = label
        self.template = template
        self.area_key = area_key
        self.screen_id = None
        self.device_key = None
        self.token = None
        self.token_at = 0.0
        self.hub = None
        self.version = None                 # the emergency version this "screen" is displaying
        self.standing = []                  # what it is displaying: list of {kind, scope, areaId}
        self.frames = []                    # (monotonic, target, payload)
        self._beat = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- registration -----------------------------------------------------------

    def register(self):
        api, ctx = self.ctx.api, self.ctx
        operator = ctx.platform_operator()
        _, listed = api.get("/api/havenzhub/screens", expect=200)
        existing = next((s for s in listed if s["screenKey"] == self.key), None)
        area_id = ctx.areas[self.area_key]["id"] if self.area_key else None
        if existing is None:
            body = {"screenType": "dashboard", "propertyId": ctx.property_id, "screenKey": self.key,
                    "label": self.label, "template": self.template, "params": {"orientation": "portrait"}}
            if area_id:
                body["areaId"] = area_id
            _, existing = api.post("/api/havenzhub/screens", body, account=operator, expect=201)
        elif not existing.get("isActive"):
            api.post(f"/api/havenzhub/screens/{existing['id']}/reactivate", account=operator, expect=(200, 204))
        self.screen_id = existing["id"]
        _, code = api.post(f"/api/havenzhub/screens/{self.screen_id}/pairing-code", expect=200)
        status, paired, _ = http("POST", f"{config.API}/api/screens/pair", body={"pairingCode": code["pairingCode"]},
                                 timeout=20)
        if status != 200:
            raise RuntimeError(f"probe screen {self.key} could not pair: HTTP {status} {paired}")
        self.device_key = paired["deviceKey"]
        return self

    def retire(self):
        self.disconnect()
        if self.screen_id:
            self.ctx.api.delete(f"/api/havenzhub/screens/{self.screen_id}", account=self.ctx.platform_operator())

    # -- the screen's own calls ---------------------------------------------------

    def _token(self):
        if self.token is None or time.time() - self.token_at > 420:
            status, body, _ = http("POST", f"{config.API}/api/screens/token",
                                   headers={"X-Screen-Key": self.device_key}, body={}, timeout=20)
            if status != 200:
                raise RuntimeError(f"probe screen {self.key} got no token: HTTP {status} {body}")
            self.token, self.token_at = body["token"], time.time()
        return self.token

    def get(self, path):
        status, body, headers = http("GET", f"{config.API}{path}",
                                     headers={"Authorization": f"Bearer {self._token()}"}, timeout=20)
        return status, body

    def readings(self):
        """The sensor block the wall templates read."""
        status, body = self.get("/api/screens/me/readings/latest")
        if status != 200:
            raise RuntimeError(f"screen readings: HTTP {status} {body}")
        return body

    def reading_for(self, device_id, metric):
        for sensor in self.readings().get("sensors", []):
            if sensor["device"]["id"] == device_id:
                reading = next((r for r in sensor.get("readings", []) if r["metricType"] == metric), None)
                return sensor["device"], reading
        return None, None

    def emergency_snapshot(self):
        return self.get("/api/screens/me/emergency")

    # -- the hub -------------------------------------------------------------------

    def connect(self):
        self._token()
        self.hub = SignalRClient(config.API, lambda: self.token)
        original = self.hub._on_frame

        def on_frame(frame):
            before = len(self.hub.messages)
            original(frame)
            for mono, wall, target, args in self.hub.messages[before:]:
                self._apply(mono, target, args[0] if args else {})
        self.hub._on_frame = on_frame
        self.hub.connect()
        self._stop.clear()
        self._beat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._beat.start()
        return self

    def disconnect(self):
        self._stop.set()
        if self.hub:
            self.hub.close()
            self.hub = None

    def _apply(self, mono, target, payload):
        """Do what the real pages do: a snapshot replaces everything; a frame older than what is shown is ignored."""
        if target not in ("EmergencySnapshot", "EmergencyState"):
            return
        with self._lock:
            self.frames.append((mono, target, payload))
            version = payload.get("version")
            if target == "EmergencySnapshot":
                self.version = version
                self.standing = [{"kind": s.get("kind"), "scope": s.get("scope"), "areaId": s.get("areaId")}
                                 for s in payload.get("standing") or []]
                return
            if version is not None and self.version is not None and version < self.version:
                return
            scope, area = payload.get("scope"), payload.get("areaId")
            if payload.get("kind") == "all-clear":
                if scope == "company":
                    self.standing = []
                else:
                    self.standing = [s for s in self.standing if not (s["scope"] == "area" and s["areaId"] == area)]
            else:
                self.standing = [s for s in self.standing if not (s["scope"] == scope and s["areaId"] == area)]
                self.standing.append({"kind": payload.get("kind"), "scope": scope, "areaId": area})
            if version is not None:
                self.version = version if self.version is None else max(self.version, version)

    def showing(self):
        with self._lock:
            kinds = [s["kind"] for s in self.standing]
        for kind in ("evacuation", "fire-alarm", "lockdown"):
            if kind in kinds:
                return kind
        return "none"

    def heartbeat(self):
        if self.hub and self.hub.connected:
            current = self.showing()
            self.hub.invoke("ScreenHeartbeatV2", {"currentTemplate": self.template if current == "none" else current,
                                                  "emergencyVersion": self.version})

    def _heartbeat_loop(self):
        while not self._stop.wait(5):
            try:
                self.heartbeat()
            except Exception:  # noqa: BLE001 - a dropped connection is something scenarios cause on purpose
                pass

    def frames_since(self, mono, target=None):
        with self._lock:
            return [f for f in self.frames if f[0] >= mono and (target is None or f[1] == target)]
