"""
Bringing the plant up and taking it down.

`up` builds a backend image from a named HavenzBMS checkout, starts the rehearsal's own database,
applies that checkout's schema and every migration to it, and starts the backend, the mail sink,
the twenty readers and the site agent - then the wall and door apps on the host. `down` removes
all of it, volumes included. Nothing outside the `havenz_rehearsal` compose project is touched.
"""

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

from . import config
from .util import http, iso, run, wait_until


def log(msg):
    print(f"[rehearsal] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------------

def state_path():
    return config.stack_dir() / "stack.json"


def load_state():
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(state):
    config.stack_dir().mkdir(parents=True, exist_ok=True)
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, state_path())


def update_state(**changes):
    state = load_state()
    state.update(changes)
    save_state(state)
    return state


# ---------------------------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------------------------

def compose(*args, check=True, timeout=None):
    env_file = config.stack_dir() / "compose.env"
    cmd = ["docker", "compose", "--env-file", str(env_file), "-f", str(config.TOOL_DIR / "compose.yml"),
           *args]
    return run(cmd, check=check, timeout=timeout, cwd=str(config.TOOL_DIR))


def docker(*args, check=True, timeout=None, input_text=None):
    return run(["docker", *args], check=check, timeout=timeout, input_text=input_text)


def psql(sql, check=True):
    """Run SQL in the rehearsal database; rows come back as lists of strings."""
    code, out, err = docker("exec", "-i", config.CONTAINER["db"], "psql", "-U", "postgres", "-d", "havenzhub",
                            "-X", "-q", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                            input_text=sql, check=False)
    if code != 0:
        if check:
            raise RuntimeError(f"psql failed: {err.strip()[:600]}")
        return None
    return [line.split("\t") for line in out.splitlines() if line != ""]


def psql_scalar(sql):
    rows = psql(sql)
    return rows[0][0] if rows and rows[0] else None


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def container_running(name):
    code, out, _ = docker("inspect", "-f", "{{.State.Running}}", name, check=False)
    return code == 0 and out.strip() == "true"


# ---------------------------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------------------------

def export_backend(bms_path):
    """
    Copy the checkout's HEAD commit into the stack folder and build from that.

    Not the working tree, on purpose. The image is then exactly one commit - named in the report -
    and a Windows checkout whose shell scripts picked up CRLF line endings (which breaks the
    backend's own Dockerfile) builds cleanly, without anything in the checkout being changed.
    Uncommitted edits in the checkout are therefore NOT part of the rehearsal.
    """
    bms_path = Path(bms_path)
    if not (bms_path / "WebApp").is_dir():
        raise SystemExit(f"{bms_path} does not look like a HavenzBMS checkout (no WebApp folder)")
    _, sha, _ = run(["git", "-C", str(bms_path), "rev-parse", "HEAD"])
    _, branch, _ = run(["git", "-C", str(bms_path), "rev-parse", "--abbrev-ref", "HEAD"])
    _, dirty, _ = run(["git", "-C", str(bms_path), "status", "--porcelain"])
    sha, branch = sha.strip(), branch.strip()

    target = config.stack_dir() / "bms-src"
    marker = target / ".rehearsal-commit"
    if marker.exists() and marker.read_text().strip() == sha:
        log(f"backend source already exported at {sha[:7]}")
    else:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        log(f"exporting backend commit {sha[:7]} ({branch}) from {bms_path}")
        archive = subprocess.Popen(
            ["git", "-C", str(bms_path), "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "HEAD"],
            stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(target)], stdin=archive.stdout, check=True)
        archive.wait()
        if archive.returncode != 0:
            raise SystemExit("git archive failed")
        marker.write_text(sha)

    migrations = sorted(p.name for p in (target / "DataAccess" / "database" / "migrations").glob("*.sql"))
    return {"path": str(bms_path), "commit": sha, "branch": branch,
            "uncommittedChangesIgnored": bool(dirty.strip()),
            "migrations": len(migrations), "highestMigration": migrations[-1] if migrations else None}


def write_env_files(state):
    sd = config.stack_dir()
    (sd / "agent-data").mkdir(parents=True, exist_ok=True)
    (sd / "logs").mkdir(parents=True, exist_ok=True)

    jwt = state.get("jwtSecret") or secrets.token_hex(48)
    state["jwtSecret"] = jwt
    api_env = dict(config.API_ENV)
    api_env["JwtSettings__SecretKey"] = jwt
    (sd / "api.env").write_text("".join(f"{k}={v}\n" for k, v in api_env.items()), encoding="utf-8")

    (sd / "compose.env").write_text(
        f"REHEARSAL_DB_PASSWORD={config.DB_PASSWORD}\n"
        f"REHEARSAL_BMS_PATH={(sd / 'bms-src').as_posix()}\n"
        f"REHEARSAL_STACK_DIR={sd.as_posix()}\n", encoding="utf-8")

    # The add-on's options, the one thing the Home Assistant Supervisor would have written.
    options = sd / "agent-data" / "options.json"
    options.write_text(json.dumps({
        "api_url": "http://api",
        "heartbeat_interval_seconds": 30,
        "discovery_enabled": False,
    }, indent=2), encoding="utf-8")
    return state


def build_database(state):
    """
    A fresh database from the checkout: the base schema, then every migration in filename order.

    The repository's own migration runner (scripts/migrate.sh) is tried first because it is what
    production uses. If it refuses - see the finding this records - the database is built the way
    the repository's developer task builds one: each file in order, continuing past errors, with
    every error counted and kept in the stack log so nothing is hidden.
    """
    if psql_scalar("select to_regclass('public.rehearsal_database_built') is not null") == "t":
        log("database already built; leaving it alone")
        return state.get("database") or {}

    db = config.CONTAINER["db"]
    logf = config.stack_dir() / "logs" / "db-build.log"
    # No marker means a build that never finished (or a brand-new volume). Start from nothing.
    docker("exec", db, "psql", "-U", "postgres", "-d", "postgres", "-q",
           "-c", "DROP DATABASE IF EXISTS havenzhub WITH (FORCE)", "-c", "CREATE DATABASE havenzhub")
    log("building the database: base schema")
    code, out, err = docker("exec", db, "psql", "-U", "postgres", "-d", "havenzhub", "-v", "ON_ERROR_STOP=1",
                            "-X", "-q", "-f", "/bms/DataAccess/database/havenz_hub_schema_postgresql.sql",
                            check=False)
    if code != 0:
        raise SystemExit(f"the base schema did not apply:\n{err[-1500:]}")

    log("building the database: migrations with the production runner (scripts/migrate.sh)")
    code, out, err = docker("exec", "-e", "PGHOST=localhost", "-e", "PGUSER=postgres",
                            "-e", "PGDATABASE=havenzhub", "-e", f"PGPASSWORD={config.DB_PASSWORD}",
                            db, "bash", "/bms/scripts/migrate.sh", check=False, timeout=900)
    runner_log = (out + "\n" + err).strip()
    result = {"productionRunner": "applied everything" if code == 0 else "refused",
              "productionRunnerError": None, "fallbackErrors": {}}
    logf.write_text("== scripts/migrate.sh ==\n" + runner_log + "\n", encoding="utf-8")

    if code != 0:
        failing = [line for line in runner_log.splitlines() if "ERROR" in line or "applying" in line][-2:]
        result["productionRunnerError"] = " | ".join(failing)[:500]
        log("the production runner stopped (" + result["productionRunnerError"] + "); "
            "falling back to file-by-file, errors counted")
        # Start again from a clean database so a half-applied first migration cannot colour the rest.
        docker("exec", db, "psql", "-U", "postgres", "-d", "postgres", "-q",
               "-c", "DROP DATABASE havenzhub WITH (FORCE)", "-c", "CREATE DATABASE havenzhub")
        docker("exec", db, "psql", "-U", "postgres", "-d", "havenzhub", "-v", "ON_ERROR_STOP=1", "-X", "-q",
               "-f", "/bms/DataAccess/database/havenz_hub_schema_postgresql.sql")
        script = r'''
set -u
for f in $(ls /bms/DataAccess/database/migrations/*.sql | sort); do
  out=$(psql -U postgres -d havenzhub -X -q -f "$f" 2>&1 | grep "ERROR:" || true)
  if [ -n "$out" ]; then echo "##FILE $(basename "$f")"; echo "$out"; fi
done
echo "##DONE"
'''
        code, out, err = docker("exec", "-i", db, "bash", "-s", input_text=script, check=False, timeout=900)
        current = None
        for line in out.splitlines():
            if line.startswith("##FILE "):
                current = line[7:].strip()
                result["fallbackErrors"][current] = []
            elif line.startswith("##DONE"):
                current = None
            elif current:
                result["fallbackErrors"][current].append(line.split("ERROR:", 1)[-1].strip()[:200])
        with logf.open("a", encoding="utf-8") as f:
            f.write("\n== file-by-file fallback ==\n" + out + "\n" + err + "\n")
        if "##DONE" not in out:
            raise SystemExit("the fallback migration pass did not finish; see " + str(logf))

    psql("create table public.rehearsal_database_built (built_at timestamptz not null default now())")
    tables = psql_scalar("select count(*) from information_schema.tables "
                         "where table_schema not in ('pg_catalog','information_schema')")
    result["tables"] = int(tables)
    log(f"database built: {tables} tables"
        + (f", {sum(len(v) for v in result['fallbackErrors'].values())} SQL error(s) in "
           f"{len(result['fallbackErrors'])} migration file(s) (logged)" if result["fallbackErrors"] else ""))
    return result


def wait_for_api(timeout=180):
    def healthy():
        try:
            status, _, _ = http("GET", f"{config.API}/health", timeout=3)
            return status == 200
        except Exception:  # noqa: BLE001
            return False
    ok, waited = wait_until(healthy, timeout, 1.5)
    if not ok:
        raise SystemExit("the backend did not become healthy; see: docker logs " + config.CONTAINER["api"])
    return waited


def up(bms=None, dashboards=None, door=None, apps=True, rebuild=False):
    paths = config.default_paths()
    bms = Path(bms) if bms else paths["bms"]
    state = load_state()
    config.stack_dir().mkdir(parents=True, exist_ok=True)

    code, _, _ = docker("version", "--format", "{{.Server.Version}}", check=False)
    if code != 0:
        raise SystemExit("Docker is not running.")

    state["backend"] = export_backend(bms)
    state = write_env_files(state)
    state["startedAt"] = state.get("startedAt") or iso()
    save_state(state)

    log("building images (backend, mail sink, readers, site agent)")
    compose("build" if not rebuild else "build", *(["--no-cache"] if rebuild else []),
            "api", "sink", "readers", "agent", timeout=3600)

    log("starting database, mail sink and readers")
    compose("up", "-d", "db", "sink", "readers", timeout=300)
    ok, _ = wait_until(lambda: psql("select 1", check=False), 90, 1.5)
    if not ok:
        raise SystemExit("the rehearsal database did not come up")

    state["database"] = build_database(state)
    save_state(state)

    log("starting the backend")
    compose("up", "-d", "api", timeout=300)
    waited = wait_for_api()
    log(f"backend healthy on {config.API} after {waited:.0f}s")

    log("starting the site agent (unpaired - `seed` pairs it the way an installer does)")
    compose("up", "-d", "agent", timeout=300)

    if apps:
        from . import apps as apps_mod
        state["apps"] = apps_mod.start(Path(dashboards) if dashboards else paths["dashboards"],
                                       Path(door) if door else paths["door"])
        save_state(state)
    else:
        log("--no-apps: wall and door apps not started (README has the two commands)")

    status()
    return state


def status():
    _, out, _ = compose("ps", "--format", "{{.Name}}\t{{.Status}}\t{{.Ports}}", check=False)
    for line in out.splitlines():
        log("  " + line.replace("\t", "  "))
    state = load_state()
    for name, app in (state.get("apps") or {}).items():
        log(f"  {name}: {app.get('url')} (pid {app.get('pid')})")


def down(keep_output=True):
    from . import apps as apps_mod
    state = load_state()
    apps_mod._kill_tree((state.get("feeder") or {}).get("pid"))
    apps_mod.stop(state.get("apps") or {})
    if (config.stack_dir() / "compose.env").exists():
        log("removing the rehearsal containers, networks and volumes")
        compose("--profile", "*", "down", "-v", "--remove-orphans", check=False, timeout=300)
    sd = config.stack_dir()
    if sd.exists():
        # The stack folder holds the exported source, the agent's /data and the keys. All of it
        # belongs to this stack and none of it is a run's results.
        shutil.rmtree(sd, ignore_errors=True)
    pointer = config.output_root() / "current.json"
    if pointer.exists():
        pointer.unlink()
    log("down. Run folders under " + str(config.output_root()) + " are kept.")


def reset_data():
    """
    Back to an empty plant without rebuilding anything: a new database, readers with no users,
    an agent that has never been paired. The images, the exported sources and the two apps stay.
    """
    from . import apps as apps_mod
    state = load_state()
    feeder = (state.get("feeder") or {}).get("pid")
    if feeder:
        apps_mod._kill_tree(feeder)
    log("stopping the agent and the backend")
    compose("stop", "-t", "2", "agent", "api", check=False, timeout=120)
    data = config.stack_dir() / "agent-data"
    for item in data.iterdir():
        if item.name != "options.json":
            item.unlink()
    log("fresh readers")
    compose("up", "-d", "--force-recreate", "readers", "sink", timeout=300)
    psql("drop table if exists public.rehearsal_database_built", check=False)
    state["database"] = build_database(state)
    for key in ("feeder", "seededAt", "runDir"):
        state.pop(key, None)
    save_state(state)
    for name in ("feeder-control.json", "feeder-status.json"):
        try:
            (config.stack_dir() / name).unlink()
        except FileNotFoundError:
            pass
    log("starting the backend and the agent")
    compose("up", "-d", "--force-recreate", "api", "agent", timeout=300)
    wait_for_api()
    pointer = config.output_root() / "current.json"
    if pointer.exists():
        pointer.unlink()
    log("reset: the plant is empty again - `python rehearsal.py seed` builds it")


# ---------------------------------------------------------------------------------------------
# Faults the scenarios inject
# ---------------------------------------------------------------------------------------------

def agent_uplink(connected):
    """Cut or restore the agent's route to the backend. The plant LAN stays up either way."""
    if connected:
        docker("network", "connect", config.UPLINK_NETWORK, config.CONTAINER["agent"], check=False)
    else:
        docker("network", "disconnect", config.UPLINK_NETWORK, config.CONTAINER["agent"], check=False)


def agent_stop():
    docker("stop", "-t", "2", config.CONTAINER["agent"], check=False)


def agent_kill():
    docker("kill", config.CONTAINER["agent"], check=False)


def agent_start():
    docker("start", config.CONTAINER["agent"], check=False)


def api_restart(hard=False):
    """Restart the backend container. `hard` kills it mid-request instead of asking it to stop."""
    if hard:
        docker("kill", config.CONTAINER["api"], check=False)
        docker("start", config.CONTAINER["api"], check=False)
    else:
        docker("restart", "-t", "5", config.CONTAINER["api"], check=False)
    return wait_for_api()


def container_logs(name, since=None, tail=None):
    args = ["logs"]
    if since:
        args += ["--since", since]
    if tail:
        args += ["--tail", str(tail)]
    code, out, err = docker(*args, config.CONTAINER[name], check=False, timeout=60)
    return (out or "") + (err or "")


REFUSED_429 = re.compile(r"HTTP (?:Error )?429")


def agent_refusals(since=None, text=None):
    """
    Agent calls the backend refused as too many, read from the agent's own log.

    Matched on the words the agent logs ("HTTP 429" / "HTTP Error 429"), never on the bare digits:
    a timestamp ending ",429" or an id containing 429 is not a refusal.
    """
    text = container_logs("agent", since=since) if text is None else text
    lines = [line for line in text.splitlines() if REFUSED_429.search(line)]
    return {"total": len(lines),
            "results": sum(1 for line in lines if "could not report the result" in line),
            "polls": sum(1 for line in lines if "command poll failed" in line),
            "heartbeats": sum(1 for line in lines if "heartbeat failed" in line)}


def container_memory_mb(name):
    code, out, _ = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", config.CONTAINER[name],
                          check=False, timeout=30)
    if code != 0 or not out.strip():
        return None
    used = out.split("/")[0].strip()
    try:
        number = float("".join(c for c in used if c.isdigit() or c == "."))
    except ValueError:
        return None
    if used.lower().endswith("gib"):
        return number * 1024
    if used.lower().endswith("kib"):
        return number / 1024
    return number
