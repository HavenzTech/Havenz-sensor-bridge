"""Small shared helpers: time, HTTP, subprocess, the database. Standard library only."""

import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    """UTC, millisecond precision, trailing Z - the format the timeline and the manifest use."""
    dt = dt or utc_now()
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(text):
    """Parse an API timestamp. .NET emits up to 7 fractional digits and sometimes no offset."""
    if not text:
        return None
    t = text.strip().replace("Z", "+00:00")
    if "." in t:
        head, _, tail = t.partition(".")
        i = 0
        while i < len(tail) and tail[i].isdigit():
            i += 1
        t = f"{head}.{tail[:i][:6]}{tail[i:]}" if i else f"{head}{tail}"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class HttpError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} from {url}: {str(body)[:400]}")
        self.status, self.body, self.url = status, body, url


def http(method, url, body=None, headers=None, timeout=30, raw=None, expect=None):
    """
    One HTTP call. Returns (status, parsed-or-text, headers). Never raises on an HTTP status
    unless `expect` is given and the status is not in it - scenarios assert on refusals as often
    as on successes, so a 4xx is an answer, not an exception.
    """
    hdrs = dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, payload, rh = resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        status, payload, rh = e.code, e.read(), dict(e.headers)
    text = payload.decode("utf-8", "replace") if payload else ""
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    if expect is not None and status not in (expect if isinstance(expect, (tuple, list, set)) else (expect,)):
        raise HttpError(status, parsed, url)
    return status, parsed, rh


def run(cmd, check=True, timeout=None, env=None, cwd=None, input_text=None):
    """Run a command, return (code, stdout, stderr). Text mode, UTF-8, never a shell."""
    full_env = dict(os.environ)
    # Git Bash rewrites arguments that look like POSIX paths ("/keys" -> "C:/Program Files/Git/keys").
    full_env["MSYS_NO_PATHCONV"] = "1"
    if env:
        full_env.update(env)
    # Bytes in, bytes out: text mode on Windows would turn every line feed sent to a container's
    # stdin into CR LF, which bash and psql both choke on.
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=full_env, cwd=cwd,
                       input=None if input_text is None else input_text.encode("utf-8"))
    out = p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    err = p.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
    if check and p.returncode != 0:
        tail = out[-2000:] + "\n" + err[-2000:]
        raise RuntimeError(f"{' '.join(cmd[:6])}... exited {p.returncode}\n{tail}")
    return p.returncode, out, err


def wait_until(predicate, timeout, interval=0.5, describe="condition"):
    """
    Poll until `predicate()` returns something truthy; return (value, seconds waited).
    Returns (None, timeout) when it never does - callers turn that into a failed assertion with
    the time they waited, rather than an exception with no number in it.
    """
    start = time.monotonic()
    last = None
    while True:
        try:
            last = predicate()
        except Exception as e:  # noqa: BLE001 - a transient error mid-poll is not the answer
            last = None
            _ = e
        if last:
            return last, time.monotonic() - start
        if time.monotonic() - start >= timeout:
            return None, time.monotonic() - start
        time.sleep(interval)


def percentile(values, pct):
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(pct / 100.0 * len(ordered))            # nearest rank: the smallest value with pct% at or below it
    return ordered[max(0, min(len(ordered) - 1, rank - 1))]
