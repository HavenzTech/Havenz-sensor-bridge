# Havenz Gateway (sensor bridge)

Forwards sensor readings from **Home Assistant** to Havenz. Home Assistant already speaks every
sensor protocol/brand (Zigbee, Wi-Fi, Bluetooth, Tuya, Shelly, Aqara, …), so this stays
brand-agnostic: it reports what HA sees, and forwards what an admin connects in the Havenz app.

Each gateway authenticates with **its own key**, obtained by entering a one-time **pairing code**
(from the Havenz app → property → Gateways → Add gateway). No shared secret, no property IDs to
hand-edit, no long-lived Home Assistant token.

```
sensors → Home Assistant → THIS GATEWAY → Havenz backend → dashboards
```

## Two ways to run it

### 1. As a Home Assistant add-on (recommended — one box)
Runs on the same Pi as Home Assistant. Install it from the add-on store, pair it from a phone.
See [`havenz-gateway/DOCS.md`](havenz-gateway/DOCS.md). This is the packaging for a shipped
"Havenz Hub."

### 2. Standalone (a mini-PC or any always-on computer on the network)
Pure Python 3 standard library — **no pip installs**.

```bash
cp config.example.json config.json      # set api_url + home_assistant.url/token
python3 bridge.py config.json           # unpaired → hosts a setup page on :8099
```
Then either:
- open `http://<this-computer>:8099` and enter the pairing code, **or**
- run `python3 bridge.py config.json --register HVNZ-XXXX-XXXX`.

Once paired it stores its key in `config.json` and runs, polling every N seconds. Keep it running
with a systemd service:

```ini
# /etc/systemd/system/havenz-gateway.service
[Unit]
Description=Havenz Gateway
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /opt/havenz-gateway/bridge.py /opt/havenz-gateway/config.json
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
```

## How it works

- **Discovery:** reports every HA sensor entity to `POST /api/iot/discovery`; new ones appear in the
  Havenz app under *Available to connect*. Home-Assistant/host diagnostics are filtered out.
- **Forwarding:** the discovery response returns the active device mappings (what an admin connected);
  the gateway reads those entities and posts readings to `POST /api/iot/ingest`.
- **Remote pairing:** when an admin taps *Pair new device*, the gateway opens Home Assistant's Zigbee
  join window (`zha.permit`).
- **Auth:** a per-gateway API key in the `X-Hub-Key` header (only its hash is stored server-side).

## Config keys (standalone `config.json`)

| Key | Meaning |
|-----|---------|
| `api_url` | Havenz backend URL |
| `home_assistant.url` / `.token` | HA REST URL + a long-lived token (standalone only; the add-on uses the Supervisor) |
| `poll_interval_seconds` | how often to report/forward (default 30) |
| `setup_port` | port for the setup/status page (default 8099) |
| `hub_key` | written automatically after pairing — do not set by hand |

`config.json` holds secrets — it is gitignored. `mappings` is an optional static fallback, normally
empty (sensors are connected in the app).

> The `havenz-gateway/bridge.py` in the add-on folder is a copy of the root `bridge.py` (the HA
> add-on build context is that folder). Keep them in sync when editing.
