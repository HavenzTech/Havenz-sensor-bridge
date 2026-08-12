#!/usr/bin/env python3
"""
Home Assistant add-on entrypoint for the Havenz Site Agent.

Builds the agent config from the add-on options, persists it in /data (so the agent's key survives
restarts and add-on updates), then hands off to the agent.

Deliberately simpler than the gateway's entry point: the agent never talks to Home Assistant, so
there is no Supervisor token to chase and none of the s6 environment handling that goes with it.
Its only credentials are its own Havenz key and the reader credentials Havenz hands it.
"""
import json
import os
import sys

CONFIG = "/data/config.json"


def log(msg):
    print(f"[havenz-agent-entry] {msg}", file=sys.stderr, flush=True)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


opts = read_json("/data/options.json", {})   # the user's add-on options
cfg = read_json(CONFIG, {})                  # keeps hub_key etc. across restarts

cfg["api_url"] = opts.get("api_url", cfg.get("api_url"))
cfg["heartbeat_interval_seconds"] = int(
    opts.get("heartbeat_interval_seconds", cfg.get("heartbeat_interval_seconds", 30)))
cfg["discovery_enabled"] = bool(opts.get("discovery_enabled", cfg.get("discovery_enabled", False)))

# Always host the pairing/status page: it is the only UI on site, and it has to be reachable to
# re-pair a replacement box as much as to set up a new one.
cfg["setup_server"] = True

with open(CONFIG, "w") as f:
    json.dump(cfg, f, indent=2)

log(f"starting agent -> {cfg['api_url']}")
os.execvp("python3", ["python3", "/opt/agent.py", CONFIG])
