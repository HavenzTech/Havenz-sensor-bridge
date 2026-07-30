#!/usr/bin/env python3
"""
Home Assistant add-on entrypoint.

Builds the bridge config from the add-on options + the Supervisor's API access, persists it in
/data (so the gateway's key survives restarts), then hands off to the bridge. The bridge serves
the pairing / status page, which Home Assistant exposes via ingress.

Two ways to reach Home Assistant:

  1. The Supervisor proxy (default, nothing to configure) — http://supervisor/core, authenticated
     with SUPERVISOR_TOKEN. Requires `homeassistant_api: true` in config.yaml.
  2. A long-lived access token set in the add-on's `ha_token` option — talks to Core directly.
     The escape hatch for when the Supervisor proxy rejects the token, which does happen.
"""
import json
import os
import sys

CONFIG = "/data/config.json"


def log(msg):
    print(f"[havenz-entry] {msg}", file=sys.stderr, flush=True)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


opts = read_json("/data/options.json", {})     # user's add-on options
cfg = read_json(CONFIG, {})                     # keep hub_key etc. across restarts

cfg["api_url"] = opts.get("api_url", cfg.get("api_url"))
cfg["poll_interval_seconds"] = int(opts.get("poll_interval_seconds", cfg.get("poll_interval_seconds", 15)))

def s6_env(name):
    # s6-overlay does not pass the container's environment to services: the Supervisor sets
    # SUPERVISOR_TOKEN on the container, but s6 strips it and writes each variable to a file
    # under /run/s6/container_environment instead (opt-in via with-contenv, which we don't get
    # to control from a plain CMD). Read the file back so the token flows as designed.
    try:
        with open(f"/run/s6/container_environment/{name}", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


ha_token = str(opts.get("ha_token") or "").strip()
sup_token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""
token_source = "environment"
if not sup_token:
    sup_token = s6_env("SUPERVISOR_TOKEN") or s6_env("HASSIO_TOKEN")
    token_source = "/run/s6/container_environment"

if ha_token:
    # Straight to Core, bypassing the Supervisor entirely.
    cfg["home_assistant"] = {"url": "http://homeassistant:8123", "token": ha_token}
    log("Home Assistant: using the long-lived token from add-on options (direct to Core).")
else:
    cfg["home_assistant"] = {"url": "http://supervisor/core", "token": sup_token}
    if sup_token:
        log(f"Home Assistant: using the Supervisor proxy (token from {token_source}, {len(sup_token)} chars).")
    else:
        # This is the failure that produces "401 Unauthorized" on every poll. Name it loudly, and
        # print which token-ish variables DO exist so the cause is visible in one glance.
        present = sorted(k for k in os.environ if "TOKEN" in k.upper()) or ["none"]
        log("WARNING: no SUPERVISOR_TOKEN in the environment. Every Home Assistant call will 401.")
        log(f"WARNING: token-like variables present: {', '.join(present)}")
        log("WARNING: fix by setting a long-lived token in the add-on's 'ha_token' option.")

cfg["setup_server"] = True                      # always host the ingress pairing/status page
cfg.setdefault("mappings", [])

with open(CONFIG, "w") as f:
    json.dump(cfg, f, indent=2)

os.execvp("python3", ["python3", "/opt/bridge.py", CONFIG])
