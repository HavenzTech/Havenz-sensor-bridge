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

The agent also keeps a small record at `/data/executed.jsonl` of the work it has already carried
out, so that restarting it — for an update, after a power cut — cannot make it repeat a door it
has already opened. It holds about a week, prunes itself, and needs no attention. Deleting it is
harmless but pointless; the only thing it costs you is that protection for a few minutes. (Up to
0.5.0 this was `/data/executed.json`; the first start of 0.6.0 takes its contents over and removes
it.)

### Twenty doors on one agent (0.6.0)

Four changes, all found by rehearsing a whole plant - twenty readers, sixty people - on one agent.
Nothing to configure, and nothing changes at the doors.

**One door that stops answering no longer holds up the others.** The agent used to collect a batch
of work, finish all of it, and only then ask Havenz for more. A reader that accepted a connection
and then said nothing kept it waiting ten seconds, and in that time a remote unlock for any other
door simply sat in Havenz. Now each reader has its own worker and the agent keeps asking for work
while they are busy. In the rehearsal a healthy door beside a hanging one took 9 seconds to open
remotely; that is what this removes. A reader that fails three times running is still set aside for
a minute and re-tried by itself, as before.

**The result of finished work is kept until Havenz has it.** When the agent told Havenz "done" and
Havenz was busy or briefly unreachable, the answer used to be logged and thrown away, and Havenz
then showed the command as "unknown" although the reader had done it. Results now wait in
`/data/results.jsonl` and are sent again - after the pause Havenz asks for, or with a growing pause
if the connection is down - until Havenz has them, including across a restart of the add-on. The
status panel shows "N result(s) waiting to be sent" while there are any. A result is given up on
only if Havenz says the command is not this agent's, or after a day; the log says so and the panel
counts it. (The result of a *read*, such as collecting a reader's log, is retried for ten minutes
but not written to disk: Havenz simply reads again.)

**The record of finished work is added to, not rewritten.** `/data/executed.jsonl` now grows by one
line per command. It used to be one file written out in full after every command, which after a
day's enrolment took a few tenths of a second each time - time the app spent waiting after a door
had already opened, and that every enrolment paid for every person on every door. For a door
unlock the line is still safely on disk before Havenz is told the door opened; for everything else
(which is harmless to repeat) it is written at once and made safe within a second. The file is
tidied up in the background when it has grown.

**A reader is always answered within three seconds.** Reader events are still written to
`/data/events.jsonl` before the reader is told "got it", but they are now written in batches -
twenty doors reporting in the same second share one disk write instead of queueing for one each -
so a slow moment on the storage no longer makes the last reader in the queue wait past its own
five-second limit. One deliberate trade goes with this: if the disk has not confirmed an event
after three seconds, the agent answers the reader anyway. At that point the event is in the agent's
memory, is already being sent to Havenz, and is still on its way to the disk; it could only be lost
if the agent stopped in that same moment, and even then it is still in the reader's own log, which
Havenz collects every thirty seconds. We chose that over making a reader wait, because what a
reader does when its report times out is not something anyone has been able to observe. It should
essentially never happen on healthy storage; if it does, the log says so and `status.json` counts it
(`answered_before_durable`) - a sign the box's storage needs looking at.

The agent also now waits exactly as long as Havenz asks when it is told "too many requests", for
its check-ins, its requests for work and its event reports, instead of guessing.

Works with an older Havenz backend. A newer backend gives an agent an allowance sized for the
number of doors it serves; with an older one the agent may be asked to slow down while a whole
roster is being enrolled, and from 0.6.0 that costs time but no longer loses anything.

### Door events are kept until Havenz has them (0.5.0)

When a reader reports an event - someone badged in, someone was refused, a face was enrolled - the
agent now writes it to `/data/events.jsonl` **before** it tells the reader "got it", and sends it
to Havenz from there. If the internet connection is down, or the add-on is restarted or updated in
the middle of sending, the event is still on disk and goes up as soon as it can, in the order the
reader sent it. Before this, an event that could not be delivered within about three seconds was
given up on.

Each event is sent with how long it waited. Havenz records a late one in the access history as
usual but does **not** show it as someone arriving, so after an outage the door panels do not flash
"Welcome" for people who walked through hours ago.

The file looks after itself: delivered events are cleared out, and it is capped (10,000 events,
64 MB, one week) so it cannot fill a small box's storage - if the connection is down long enough to
hit a cap, the oldest events are dropped, the log says so, and the status panel shows the count.
The readers' own logs are still collected by Havenz as a second line of defence. The panel shows
"N door event(s) waiting to be sent" whenever there is a backlog. The reader's keepalive is
deliberately not queued: delivered late, it would make a dead reader look alive.

`/data/roster.json` remembers which address is which door (addresses only, never a password), so an
event that arrives while Havenz is unreachable after a restart is still attributed and kept.

Also in 0.5.0: if a reader takes an unlock and then never answers, the agent now reports that as
"unknown - the door may have opened" instead of "failed". It was seen on a test bench: the reader
opened the door, answered after the agent's ten-second timeout, and the app said the unlock had
failed - which invites a second tap on a door that is already open. And when Havenz reads a reader's access log it now asks only for rows newer than the
ones it already has, instead of the reader's whole history every thirty seconds; and those reads
are no longer written into `executed.json`, which had been growing with copies of reader logs.
Nothing to configure. Works with an older Havenz backend, which simply ignores the new details -
but update the backend first if you can, because that is what stops late events being announced.

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
- Live event notifications and welcome screens. Events the readers send while the agent is running
  but offline are kept on this box and delivered when the connection returns; events from while the
  agent itself was stopped stay in the readers' own logs and are collected afterwards. Either way
  they arrive as history, not as people arriving.

The panel in the sidebar shows when the agent last reached Havenz and which doors it is responsible
for. If something is wrong, that page says what.

## Why this is a separate add-on

The Havenz Gateway add-on handles sensors; this one handles doors. They are deliberately kept apart
so that a problem with sensor polling can never delay someone getting through a door. Run either,
or both, on the same device.
