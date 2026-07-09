# Havenz Sensor Bridge

Forwards sensor readings from **Home Assistant** to the HavenzBMS IoT ingestion webhook
(`POST /api/iot/ingest`), HMAC-signed.

This is the **consumer / facility bridge**. Home Assistant already speaks every sensor
protocol and brand (Zigbee, WiFi, Bluetooth, Tuya, Shelly, Aqara, Govee, …), so this bridge
stays brand-agnostic: it maps Home Assistant entities → Havenz devices/metrics, signs, and
posts. **Adding a new sensor is one line in the config — never a code change.**

> The power plant's *process* data (turbines, generators) comes from the plant's industrial
> control system via a separate bridge. This bridge covers homes and the *building/facility*
> layer of any site. Both feed the same webhook.

## What runs where

```
Zigbee/WiFi sensors → Home Assistant (on the Pi) → THIS BRIDGE (on the Pi) → /api/iot/ingest → dashboards
```

The bridge is pure Python 3 standard library — **no pip installs**. It runs anywhere,
including a bare Raspberry Pi running Home Assistant OS.

## Setup on the Raspberry Pi

1. **Install Home Assistant OS** on the Pi, plug in the Zigbee dongle, pair your sensors.
2. **Create a Long-Lived Access Token** in Home Assistant: your profile → Security →
   Long-Lived Access Tokens → Create. Copy it.
3. **Register each sensor in Havenz** (Zhub admin → devices): the `device_key` in the config
   must match a registered device's Name, SerialNumber, or MacAddress.
4. **Set the ingestion secret** on the backend (`IotIngest:Secret`) and use the *same* value
   in this bridge's config.
5. **Copy `config.example.json` → `config.json`** and fill in:
   - `api_url` — the HavenzBMS URL (Cloud Run in prod)
   - `secret` — matches `IotIngest:Secret`
   - `home_assistant.url` / `.token` — usually `http://localhost:8123` + the token from step 2
   - `mappings` — one line per (entity → device + metric); see below
6. **Run it:**
   ```bash
   python3 bridge.py config.json          # runs forever, polling every N seconds
   python3 bridge.py config.json --once   # single pass, for testing
   ```
7. **Keep it running** — install as a systemd service (see below) or a Home Assistant add-on.

## Mapping entities

Each mapping ties one Home Assistant entity to one Havenz device + metric type:

```json
{ "entity": "sensor.living_room_temperature", "device_key": "Living Room Temp",
  "metric_type": "temperature", "unit": "°C", "thresholdMax": 30 }
```

- `entity` — the Home Assistant entity_id (find it in HA → Developer Tools → States).
- `device_key` — must match a registered Havenz device (Name / SerialNumber / MacAddress).
- `metric_type` — e.g. `temperature`, `humidity`, `power_consumption`, `energy_usage`,
  `water_detection`, `air_quality_pm25`, `air_quality_co2`, `voltage_ac`, `water_flow`.
- `unit` — optional; falls back to Home Assistant's reported unit.
- `thresholdMin` / `thresholdMax` — optional; a breach flags the reading as an alert.

Non-numeric or `unavailable` states are skipped safely.

## Run as a service (systemd)

`/etc/systemd/system/havenz-bridge.service`:
```ini
[Unit]
Description=Havenz Sensor Bridge
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/havenz-sensor-bridge/bridge.py /home/pi/havenz-sensor-bridge/config.json
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now havenz-bridge
journalctl -u havenz-bridge -f   # watch it
```

## Security

- Readings are HMAC-SHA256 signed (timestamp + nonce + signature) — the same scheme the
  camera webhook uses. The backend rejects unsigned/expired/replayed requests.
- The bridge holds the secret; sensors never do. Keep `config.json` readable only by the
  service user (`chmod 600`).
- The backend **fails closed**: if `IotIngest:Secret` is unset in production it rejects
  everything. `AllowInsecure=true` (dev only) bypasses signing for local testing.

## Verified

The read → sign → POST → persist path is verified end-to-end against a live backend with HMAC
enforced (bridge → ingestion webhook → `iot_metrics`). See HavenzBMS commit history.
