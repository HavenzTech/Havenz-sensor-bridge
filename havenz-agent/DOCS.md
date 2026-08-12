# Havenz Site Agent

Lets Havenz manage this site's door terminals without opening the network to the outside.

## What it does

Door readers sit on your building's own network, which nothing on the internet can reach — and that
is exactly how it should stay. This add-on runs on the same box as Home Assistant, makes an outbound
connection to Havenz, and carries out door work locally on Havenz's behalf.

Nothing connects *in*. No VPN, no port forwarding, no firewall rules, no static public address. The
only thing your network needs to allow is what it already allows: an outgoing HTTPS request.

Once it is running, Havenz can unlock doors remotely, sync who has access, enrol faces, and receive
access events the instant they happen.

## Install

1. In Home Assistant → **Settings → Add-ons → Add-on Store**.
2. Top-right menu (⋮) → **Repositories** → add
   `https://github.com/HavenzTech/Havenz-sensor-bridge` → **Add**.
3. Find **Havenz Site Agent** in the store → **Install**.
4. **Start** the add-on, and enable **Start on boot**.
5. Open the **Havenz Agent** panel in the sidebar and enter the pairing code from the Havenz app
   (**Property → Site agents → Add agent**).

That is the whole install. One code, once, for the whole site — not one per door.

## Configuration

| Option | Default | What it does |
|---|---|---|
| `api_url` | Havenz production | The Havenz backend to connect to. Change only if you are told to. |
| `heartbeat_interval_seconds` | `30` | How often the agent reports in. Havenz raises an alert if it goes quiet. |
| `discovery_enabled` | `false` | Lets the agent look for door readers on this network so nobody has to type in twenty IP addresses. Off by default — see below. |

### About discovery

When enabled, the agent looks for HID readers on the local network and reports what it finds to
Havenz, where they appear as **unclaimed readers**.

Finding a reader does not connect it to anything. A discovered reader sits inert until someone in
the Havenz app names it, assigns it to a door, and supplies its credentials. That step is always a
person's decision — the agent will never adopt or configure a device on its own.

Leave this off unless you want it. Readers can always be added by hand instead.

## Running it well

- **Use a wired connection** and give this device a fixed address on your network. Wi-Fi works, but
  when it drops the symptom is confusing: the doors themselves keep working normally while remote
  unlock and live events quietly stop.
- **Put the device somewhere locked.** It holds the credentials for this site's readers, so it
  deserves the same physical protection as the door controller itself.
- **Keep it powered.** If the site has a UPS, this belongs on it.

## If the agent stops

**Your doors keep working.** Face recognition runs on the reader itself and needs no network at all,
so people badge in exactly as before. Nothing about physical access depends on this add-on.

What stops until it returns:

- Unlocking a door remotely from the app
- New access changes reaching the readers (they queue up and apply when the agent is back)
- Live event notifications and welcome screens (events are stored on the readers and collected later
  — nothing is lost)

The panel in the sidebar shows when the agent last reached Havenz and which doors it is responsible
for. If something is wrong, that page says what.

## Why this is a separate add-on

The Havenz Gateway add-on handles sensors; this one handles doors. They are deliberately kept apart
so that a problem with sensor polling can never delay someone getting through a door. Run either,
or both, on the same device.
