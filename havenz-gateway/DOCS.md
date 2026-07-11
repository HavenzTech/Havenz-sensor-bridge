# Havenz Gateway

Connects this site's Home Assistant sensors to Havenz. Runs on the same box as Home Assistant —
no separate computer, no terminal, no token to create.

## What it does

Every ~30 seconds it reads your Home Assistant sensor entities and forwards their readings to Havenz,
where they appear under the property you paired this gateway to. New sensors you pair in Home
Assistant show up in the Havenz app ready to connect.

## Install

1. In Home Assistant → **Settings → Add-ons → Add-on Store**.
2. Top-right menu (⋮) → **Repositories** → add
   `https://github.com/HavenzTech/Havenz-sensor-bridge` → **Add**.
3. Find **Havenz Gateway** in the store → **Install**.
4. (Optional) On the **Configuration** tab, confirm **api_url** points at your Havenz backend.
5. **Start** the add-on, and enable **Start on boot**.

## Pair it to a property

1. In the **Havenz app**: open the property → **Gateways → Add gateway** → copy the pairing code.
2. In Home Assistant, open this add-on's **Web UI** (the "Havenz" item in the sidebar, or **Open Web
   UI** on the add-on page) → paste the code → **Connect**.
3. That's it — the gateway registers itself and starts reporting. The Web UI then shows
   **Connected**.

## Connect sensors

Pair your Zigbee/Wi-Fi sensors in Home Assistant as usual (or use **Add sensor → Pair new device**
in the Havenz app, which opens the pairing window for you). Then in the Havenz app →
**Sensors → Available to connect → Connect**.

## Options

| Option | Meaning |
|--------|---------|
| `api_url` | Your Havenz backend URL. Leave the default unless self-hosting. |
| `poll_interval_seconds` | How often readings are sent (5–3600s). Default 30. |

## Notes

- The gateway authenticates with its **own key** (obtained at pairing); no shared secret.
- It reaches Home Assistant through the **Supervisor**, so you never create a long-lived token.
- Its key and settings are stored in the add-on's `/data`, surviving restarts and updates.
- To move it to a different property, **revoke** it in the Havenz app and pair again with a new code.
