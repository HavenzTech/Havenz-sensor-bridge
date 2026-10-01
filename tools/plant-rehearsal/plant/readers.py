"""Client for the reader simulator's control API (what the doors did, seen from the device end)."""

from . import config
from .util import http


def state():
    _, body, _ = http("GET", f"{config.READERS}/state", timeout=15, expect=200)
    return body


def detail(number):
    _, body, _ = http("GET", f"{config.READERS}/readers/{number}", timeout=15, expect=200)
    return body


def scan(number, registration):
    """A person presents their face at reader `number`. Returns what the reader decided."""
    _, body, _ = http("POST", f"{config.READERS}/readers/{number}/scan",
                      body={"registration": registration}, timeout=15, expect=200)
    return body


def set_mode(number, mode, latency_ms=0):
    _, body, _ = http("POST", f"{config.READERS}/readers/{number}/mode",
                      body={"mode": mode, "latencyMs": latency_ms}, timeout=15, expect=200)
    return body


def power(number, on):
    _, body, _ = http("POST", f"{config.READERS}/readers/{number}/power", body={"on": on},
                      timeout=15, expect=200)
    return body


def scan_every(seconds):
    _, body, _ = http("POST", f"{config.READERS}/scan-every", body={"seconds": seconds},
                      timeout=15, expect=200)
    return body


def opens(number):
    return detail(number)["openList"]


def address(number):
    return f"{config.READER_SUBNET}.{config.READER_FIRST_HOST + number - 1}"
