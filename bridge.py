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
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("havenz-bridge")

# HA device_classes that aren't physical sensor readings we care about.
JUNK_DEVICE_CLASSES = {
    # non-physical / meta
    "timestamp", "date", "duration", "enum", "data_size", "data_rate", "monetary",
    # hub / HA diagnostics (e.g. the Pi's own power-supply status, connectivity, updates)
    "problem", "connectivity", "update", "tamper", "running",
}

# Entity ids that are clearly the hub/host itself, never a user sensor.
INFRA_ENTITY_HINTS = ("raspberry_pi", "_supervisor", "home_assistant", "hacs", "backup")

AGENT_VERSION = "1.1.1"


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("api_url", "home_assistant"):
        if key not in cfg:
            raise SystemExit(f"config error: missing '{key}'")
    cfg.setdefault("ingest_path", "/api/iot/ingest")
    cfg.setdefault("discovery_path", "/api/iot/discovery")
    cfg.setdefault("register_path", "/api/bridge/register")
    cfg.setdefault("poll_interval_seconds", 30)
    cfg.setdefault("mappings", [])
    return cfg


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def fetch_states(ha, timeout=10):
    """GET every entity's current state from Home Assistant; return {entity_id: state_obj}."""
    url = f"{ha['url'].rstrip('/')}/api/states"
    if not ha.get("token"):
        raise RuntimeError(
            f"no Home Assistant token — every call to {url} will be rejected. In the add-on, set a "
            "long-lived access token in the 'ha_token' option."
        )
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {ha['token']}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            states = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # A bare "HTTP Error 401: Unauthorized" says nothing about which of the two auth paths
        # failed, so surface the server's own reason and the URL we actually called.
        body = e.read().decode("utf-8", "replace").strip()[:200]
        hint = ""
        if e.code in (401, 403):
            hint = (
                " — the token was rejected. If this is the Home Assistant add-on, the Supervisor "
                "proxy refused SUPERVISOR_TOKEN; work around it by creating a long-lived access "
                "token in Home Assistant (profile -> Security) and pasting it into the add-on's "
                "'ha_token' option."
            )
        raise RuntimeError(f"GET {url} -> HTTP {e.code}{hint} {body}".rstrip()) from None
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
        if any(h in eid for h in INFRA_ENTITY_HINTS):
            continue  # the hub/host's own diagnostics, not a user sensor
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


def _hmac_headers(cfg, body):
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_b64 = base64.b64encode(body).decode("ascii")
    signature = "sha256=" + hmac.new(
        cfg.get("secret", "").encode("utf-8"),
        f"{timestamp}.{body_b64}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Nonce": nonce,
        "X-Webhook-Signature": signature,
    }


def backend_headers(cfg, body):
    """A registered hub authenticates with its API key; otherwise fall back to HMAC signing."""
    if cfg.get("hub_key"):
        return {
            "Content-Type": "application/json",
            "X-Hub-Key": cfg["hub_key"],
            "X-Agent-Version": AGENT_VERSION,
        }
    return _hmac_headers(cfg, body)


def backend_post(cfg, path, payload, timeout=15):
    """POST a JSON body to the backend (hub-key or HMAC auth); return the parsed response."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    url = f"{cfg['api_url'].rstrip('/')}{path}"
    req = urllib.request.Request(url, data=body, method="POST", headers=backend_headers(cfg, body))
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
    entities = discovery_entities(states)
    if cfg.get("hub_key"):
        # A registered hub: identity + property come from the key, not the body.
        payload = {"source": "home-assistant", "agentVersion": AGENT_VERSION, "entities": entities}
    elif cfg.get("property_id"):
        payload = {"propertyId": cfg["property_id"], "source": "home-assistant", "entities": entities}
    else:
        return []  # not registered and no property configured — nothing to report to
    try:
        resp = backend_post(cfg, cfg["discovery_path"], payload)
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
        result = backend_post(cfg, cfg["ingest_path"], readings)
        log.info("posted %d readings — accepted=%s rejected=%s unknown=%s",
                 len(readings), result.get("accepted"), result.get("rejected"),
                 result.get("unknownDevices"))
    except urllib.error.HTTPError as e:
        log.error("ingest rejected: HTTP %s — %s", e.code, e.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log.error("ingest failed: %s", e)


def register(cfg_path, cfg, code):
    """Exchange a pairing code for this hub's API key and store it in the config."""
    code = code.strip().upper()
    body = json.dumps({"pairingCode": code, "agentVersion": AGENT_VERSION}).encode("utf-8")
    url = f"{cfg['api_url'].rstrip('/')}{cfg['register_path']}"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"registration failed: HTTP {e.code} — {e.read().decode('utf-8', 'replace')}")

    cfg["hub_key"] = data["apiKey"]
    cfg["hub_id"] = data.get("hubId")
    cfg["property_id"] = data.get("propertyId")
    cfg.pop("secret", None)  # no longer needed once we have a hub key
    save_config(cfg_path, cfg)
    log.info("registered hub '%s' to property %s — key saved to %s",
             data.get("name"), data.get("propertyId"), cfg_path)


SETUP_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect your Havenz gateway</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
         min-height: 100vh; display: grid; place-items: center; background: #0b0f14; color: #e7edf3; }
  .card { width: min(92vw, 420px); background: #121821; border: 1px solid #223; border-radius: 16px;
          padding: 28px; box-shadow: 0 10px 40px rgba(0,0,0,.4); }
  h1 { font-size: 20px; margin: 0 0 4px; }
  p  { color: #93a1b0; font-size: 14px; margin: 0 0 20px; }
  input { width: 100%; box-sizing: border-box; font-size: 22px; letter-spacing: .12em; text-align: center;
          padding: 14px; border-radius: 10px; border: 1px solid #2a3a4a; background: #0d131a; color: #fff;
          text-transform: uppercase; }
  button { width: 100%; margin-top: 14px; padding: 14px; font-size: 16px; font-weight: 600; border: 0;
           border-radius: 10px; background: #06b6d4; color: #012; cursor: pointer; }
  button:disabled { opacity: .6; cursor: default; }
  .msg { margin-top: 14px; font-size: 14px; text-align: center; min-height: 20px; }
  .ok { color: #34d399; } .err { color: #f87171; }
</style></head>
<body><div class="card">
  <h1>Connect your gateway</h1>
  <p>Enter the pairing code from the Havenz app (Property &rarr; Gateways &rarr; Add gateway).</p>
  <input id="code" placeholder="HVNZ-XXXX-XXXX" autocomplete="off" autofocus>
  <button id="go">Connect</button>
  <div class="msg" id="msg"></div>
</div>
<script>
  const btn = document.getElementById('go'), inp = document.getElementById('code'), msg = document.getElementById('msg');
  btn.onclick = async () => {
    const code = inp.value.trim().toUpperCase();
    if (!code) { msg.className='msg err'; msg.textContent='Enter your pairing code'; return; }
    btn.disabled = true; msg.className='msg'; msg.textContent='Connecting…';
    try {
      // relative URL so it works both standalone and behind Home Assistant ingress
      const r = await fetch('register', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pairingCode: code})});
      const d = await r.json();
      if (r.ok) { msg.className='msg ok'; msg.textContent='Connected! Your sensors will appear in the app shortly.'; inp.disabled=true; }
      else { msg.className='msg err'; msg.textContent = d.error || 'That code did not work.'; btn.disabled=false; }
    } catch (e) { msg.className='msg err'; msg.textContent='Could not reach the gateway.'; btn.disabled=false; }
  };
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') btn.click(); });
</script></body></html>"""


STATUS_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Havenz gateway</title>
<style>:root{color-scheme:light dark}body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
min-height:100vh;display:grid;place-items:center;background:#0b0f14;color:#e7edf3}
.card{width:min(92vw,420px);background:#121821;border:1px solid #223;border-radius:16px;padding:28px;text-align:center}
h1{font-size:20px;margin:0 0 6px}.ok{color:#34d399;font-size:15px}p{color:#93a1b0;font-size:14px}</style></head>
<body><div class="card"><h1>Havenz gateway</h1><div class="ok">&#10003; Connected</div>
<p>This gateway is paired and reporting its sensors to Havenz. Manage them in the Havenz app.</p></div></body></html>"""


def start_web_server(cfg_path, cfg, port, paired_event):
    """Non-blocking local web server: setup form until paired, status page after. Returns the server."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            html = STATUS_HTML if cfg.get("hub_key") else SETUP_HTML
            self._send(200, html, "text/html; charset=utf-8")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                code = (payload.get("pairingCode") or "").strip()
                if not code:
                    return self._send(400, json.dumps({"error": "Enter your pairing code"}))
                register(cfg_path, cfg, code)  # saves config + sets hub_key
                self._send(200, json.dumps({"ok": True}))
                paired_event.set()
            except SystemExit as e:
                self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(e)}))

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 bridge.py <config.json> [--register CODE | --setup | --once]")
    cfg_path = sys.argv[1]
    cfg = load_config(cfg_path)
    args = sys.argv[2:]

    if "--register" in args:
        i = args.index("--register")
        if i + 1 >= len(args):
            raise SystemExit("usage: python3 bridge.py <config.json> --register HVNZ-XXXX-XXXX")
        register(cfg_path, cfg, args[i + 1])
        return

    # Local setup/status page: always on in the add-on (setup_server), otherwise when unpaired.
    paired = threading.Event()
    if cfg.get("hub_key"):
        paired.set()
    legacy = bool(cfg.get("property_id") and cfg.get("secret"))
    if cfg.get("setup_server") or "--setup" in args or (not cfg.get("hub_key") and not legacy):
        start_web_server(cfg_path, cfg, int(cfg.get("setup_port", 8099)), paired)
        if not paired.is_set() and not legacy:
            log.info("not paired yet — open the setup page (port %s) and enter your code",
                     cfg.get("setup_port", 8099))
            paired.wait()
            log.info("gateway paired — starting up")

    auth = "hub-key" if cfg.get("hub_key") else ("hmac" if cfg.get("secret") else "none")
    log.info("Havenz sensor bridge v%s -> %s  (auth=%s, every %ss)",
             AGENT_VERSION, cfg["api_url"], auth, cfg["poll_interval_seconds"])

    if "--once" in args:
        run_once(cfg)
        return
    while True:
        run_once(cfg)
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
