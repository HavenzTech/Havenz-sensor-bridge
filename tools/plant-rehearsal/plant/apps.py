"""
The two browser apps the screens load, served locally and pointed at the rehearsal backend.

Neither checkout is modified:

* The wall app (dashboards) is exported at its HEAD commit into the stack folder, given a
  rehearsal-only `.env.local`, built, and served with `next start` on :3100. Exporting keeps the
  developer's own `.env.local` (which may name another backend, or sign-in credentials) out of
  the picture, and keeps build output out of their checkout.

* The door panel is a single static page (`public/screen.html` + `pair.html`). Its backend
  address is written into the page itself and is the production address, so it cannot be served
  as it is - a rehearsal panel would talk to production. It is served from the checkout by a small
  static server (plant/door_server.py) that replaces that one address on the way out. That it
  needs replacing at all is recorded as a finding.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .util import http, run, wait_until

IS_WINDOWS = os.name == "nt"


def log(msg):
    print(f"[rehearsal] {msg}", flush=True)


def _detached(cmd, cwd, logfile, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    out = open(logfile, "ab")
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=full_env, **kwargs)
    return proc.pid


def _kill_tree(pid):
    if not pid:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, 15)
        except ProcessLookupError:
            pass


def pid_alive(pid):
    if not pid:
        return False
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _link_node_modules(source, target):
    if target.exists():
        return
    if IS_WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)], check=True,
                       capture_output=True)
    else:
        os.symlink(source, target)


def _unlink_node_modules(target):
    """Remove the link only - never follow it into the developer's node_modules."""
    if not os.path.lexists(target):
        return
    if IS_WINDOWS:
        subprocess.run(["cmd", "/c", "rmdir", str(target)], capture_output=True)
    else:
        os.unlink(target)


DASHBOARDS_ENV = {
    "NEXT_PUBLIC_API_URL": config.API_PUBLIC,
    "NEXT_PUBLIC_FACILITY_API_MODE": "backend",
    "NEXT_PUBLIC_MARKET_API": "backend",
    "NEXT_PUBLIC_TWIN_ANCHORS_API": "backend",
    # No Autodesk credentials and no model: the digital twin is deliberately not rehearsed.
}


def start_dashboards(checkout):
    checkout = Path(checkout)
    if not (checkout / "package.json").exists():
        raise SystemExit(f"{checkout} does not look like the dashboards checkout")
    sd = config.stack_dir()
    target = sd / "dashboards-src"
    logs = sd / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    _, sha, _ = run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    sha = sha.strip()
    marker = target / ".rehearsal-commit"
    fresh = not (marker.exists() and marker.read_text().strip() == sha)
    if fresh:
        if target.exists():
            _unlink_node_modules(target / "node_modules")
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        log(f"exporting the wall app at {sha[:7]} from {checkout}")
        archive = subprocess.Popen(["git", "-C", str(checkout), "archive", "HEAD"], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(target)], stdin=archive.stdout, check=True)
        archive.wait()
        marker.write_text(sha)

    (target / ".env.local").write_text(
        "# written by the plant rehearsal - this export is disposable\n"
        + "".join(f"{k}={v}\n" for k, v in DASHBOARDS_ENV.items()), encoding="utf-8")

    if (checkout / "node_modules").is_dir():
        _link_node_modules(checkout / "node_modules", target / "node_modules")
    elif not (target / "node_modules").is_dir():
        log("installing the wall app's packages (npm ci) - the checkout has no node_modules")
        run(["npm.cmd" if IS_WINDOWS else "npm", "ci"], cwd=str(target), timeout=1800)

    next_bin = str(target / "node_modules" / "next" / "dist" / "bin" / "next")
    build_marker = target / ".next" / "BUILD_ID"
    if fresh or not build_marker.exists():
        log("building the wall app (next build) - a couple of minutes")
        with open(logs / "dashboards-build.log", "wb") as out:
            built = subprocess.run(["node", next_bin, "build", "--webpack"], cwd=str(target),
                                   stdout=out, stderr=subprocess.STDOUT,
                                   env={**os.environ, **DASHBOARDS_ENV, "NEXT_TELEMETRY_DISABLED": "1"})
        if built.returncode != 0:
            raise SystemExit("the wall app did not build; see " + str(logs / "dashboards-build.log"))

    pid = _detached(["node", next_bin, "start", "-p", str(config.DASHBOARDS_PORT)], target,
                    logs / "dashboards.log", env={**DASHBOARDS_ENV, "NEXT_TELEMETRY_DISABLED": "1"})
    return {"pid": pid, "url": config.DASHBOARDS, "commit": sha, "source": str(checkout),
            "entry": f"{config.DASHBOARDS}/screen"}


def start_door(checkout):
    checkout = Path(checkout)
    public = checkout / "public"
    if not (public / "screen.html").exists():
        raise SystemExit(f"{checkout} does not look like the door app checkout (no public/screen.html)")
    _, sha, _ = run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    logs = config.stack_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    pid = _detached([sys.executable, "-m", "plant.door_server", "--root", str(public),
                     "--port", str(config.DOOR_PORT), "--api", config.API_PUBLIC],
                    config.TOOL_DIR, logs / "door.log")
    return {"pid": pid, "url": config.DOOR, "commit": sha.strip(), "source": str(checkout),
            "entry": f"{config.DOOR}/screen.html"}


def _up(url):
    try:
        status, _, _ = http("GET", url, timeout=4)
        return status < 500
    except Exception:  # noqa: BLE001
        return False


def start(dashboards_checkout, door_checkout):
    apps = {}
    apps["door"] = start_door(door_checkout)
    apps["dashboards"] = start_dashboards(dashboards_checkout)
    for name, app in apps.items():
        ok, waited = wait_until(lambda a=app: _up(a["entry"]), 120, 1.0)
        if not ok:
            raise SystemExit(f"the {name} app did not answer on {app['entry']}; see the stack logs")
        log(f"{name} app serving {app['entry']} ({app['commit'][:7]})")
    return apps


def stop(apps):
    for name, app in (apps or {}).items():
        _kill_tree(app.get("pid"))
    target = config.stack_dir() / "dashboards-src" / "node_modules"
    _unlink_node_modules(target)
