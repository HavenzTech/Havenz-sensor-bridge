#!/usr/bin/env python3
"""
Home Assistant add-on entrypoint.

Builds the bridge config from the add-on options + the Supervisor's API access, persists it in
/data (so the gateway's key survives restarts), then hands off to the bridge. The bridge serves
the pairing / status page, which Home Assistant exposes via ingress.
"""
import json
import os

CONFIG = "/data/config.json"


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


opts = read_json("/data/options.json", {})     # user's add-on options
cfg = read_json(CONFIG, {})                     # keep hub_key etc. across restarts

cfg["api_url"] = opts.get("api_url", cfg.get("api_url"))
cfg["poll_interval_seconds"] = int(opts.get("poll_interval_seconds", cfg.get("poll_interval_seconds", 30)))
# Reach Home Assistant through the Supervisor proxy — no long-lived token to create.
cfg["home_assistant"] = {"url": "http://supervisor/core", "token": os.environ.get("SUPERVISOR_TOKEN", "")}
cfg["setup_server"] = True                      # always host the ingress pairing/status page
cfg.setdefault("mappings", [])

with open(CONFIG, "w") as f:
    json.dump(cfg, f, indent=2)

os.execvp("python3", ["python3", "/opt/bridge.py", CONFIG])
