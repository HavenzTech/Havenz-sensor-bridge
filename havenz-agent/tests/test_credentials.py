"""
Credentials corrected in Havenz reach the reader without a restart.

Found on the first home bench (2026-10-26): the reader was claimed with the wrong password, the
password was corrected in Zhub, and the bootstrap kept failing with "login rejected: HTTP 401"
until the add-on was restarted. The agent fetches each reader's credentials once and holds them;
nothing told it that Havenz now had different ones.

Run:  python3 -m pytest havenz-agent/tests -q
"""

import email.message
import io
import json
import sys
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402

DOOR_A = "11111111-1111-1111-1111-111111111111"
DOOR_B = "22222222-2222-2222-2222-222222222222"
LONG_AGO = 3600


class FakeReaderHttp:
    """
    One HID Amico reader on the LAN, as urllib sees it.

    Accepts the login endpoint with exactly one password and answers every other endpoint with an
    empty JSON object once a session has been issued.
    """

    def __init__(self, password):
        self.password = password
        self.logins = []        # every password offered to hidlogin.fcgi, in order
        self.calls = []         # every other endpoint reached

    def __call__(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/hidlogin.fcgi"):
            offered = json.loads(req.data.decode("utf-8"))["password"]
            self.logins.append(offered)
            if offered != self.password:
                raise urllib.error.HTTPError(url, 401, "Unauthorized", email.message.Message(),
                                             io.BytesIO(b'{"error":"bad login"}'))
            return _Response(b'{"session":"s-1"}')
        self.calls.append(url.rsplit("/", 1)[1].split("?")[0])
        return _Response(b"{}")


class _Response:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHavenz:
    """The backend's view of this site's readers, and how often the agent asked for it."""

    def __init__(self, rows):
        self.rows = rows
        self.asked = 0

    def __call__(self, cfg, path, timeout=40):
        assert path == "/api/agent/terminals"
        self.asked += 1
        return [dict(r) for r in self.rows]


def row(terminal_id, password, ip="192.168.213.101", name="Bench", **extra):
    return {"id": terminal_id, "name": name, "ipAddress": ip, "username": "admin",
            "password": password, **extra}


def open_door(terminal_id=DOOR_A):
    return {"id": "cmd-1", "terminalId": terminal_id, "type": "OpenDoor",
            "payload": json.dumps({"door": 1}), "notValidAfter": "2099-01-01T00:00:00Z"}


@pytest.fixture(autouse=True)
def clean():
    agent.STATE["readers"] = {}
    agent._readers_fetched["at"] = 0.0
    agent._breakers.clear()
    yield
    agent.STATE["readers"] = {}
    agent._readers_fetched["at"] = 0.0
    agent._breakers.clear()


@pytest.fixture
def cfg(tmp_path):
    return {"api_url": "http://havenz.invalid", "hub_key": "k",
            "roster_path": str(tmp_path / "roster.json")}


def hold(cfg, havenz, fetched_seconds_ago):
    """Make the agent hold what Havenz says now, as if it had asked that long ago."""
    held = agent.readers_for(cfg, force=True)
    agent._readers_fetched["at"] = time.monotonic() - fetched_seconds_ago
    return held


def test_a_reader_that_rejects_the_held_login_is_retried_with_credentials_fetched_from_havenz(cfg, monkeypatch):
    """The bench case: wrong password claimed, corrected in Zhub, command sent again."""
    havenz = FakeHavenz([row(DOOR_A, "wrong")])
    monkeypatch.setattr(agent, "backend_get", havenz)
    reader = FakeReaderHttp(password="right")
    monkeypatch.setattr(agent.urllib.request, "urlopen", reader)

    hold(cfg, havenz, fetched_seconds_ago=LONG_AGO)
    havenz.rows = [row(DOOR_A, "right")]          # corrected in Zhub after the agent asked

    assert agent.execute(cfg, open_door()) == {}

    assert reader.logins == ["wrong", "right"], "one rejected login, one with the corrected password"
    assert reader.calls == ["execute_actions.fcgi"], "the door was opened exactly once"
    assert havenz.asked == 2, "the roster was fetched once more, after the rejection"
    assert agent.STATE["readers"][DOOR_A]._password == "right", "the corrected credentials are now held"


def test_when_havenz_holds_the_same_rejected_credentials_the_command_fails_once_and_says_why(cfg, monkeypatch):
    havenz = FakeHavenz([row(DOOR_A, "wrong")])
    monkeypatch.setattr(agent, "backend_get", havenz)
    reader = FakeReaderHttp(password="right")
    monkeypatch.setattr(agent.urllib.request, "urlopen", reader)

    hold(cfg, havenz, fetched_seconds_ago=LONG_AGO)

    with pytest.raises(agent.ReaderError) as failure:
        agent.execute(cfg, open_door())

    assert "login rejected" in str(failure.value)
    assert "same credentials" in str(failure.value), "the message says the fix is in Zhub, not a restart"
    assert reader.logins == ["wrong"], "no second attempt with the same password"
    assert reader.calls == [], "the door was not touched"
    assert havenz.asked == 2, "Havenz was asked once more, and only once"


def test_a_rejection_moments_after_the_roster_was_fetched_does_not_ask_havenz_again(cfg, monkeypatch):
    """Twenty doors with a wrong password must not turn into twenty roster fetches a cycle."""
    havenz = FakeHavenz([row(DOOR_A, "wrong"), row(DOOR_B, "wrong", ip="192.168.213.105")])
    monkeypatch.setattr(agent, "backend_get", havenz)
    reader = FakeReaderHttp(password="right")
    monkeypatch.setattr(agent.urllib.request, "urlopen", reader)

    hold(cfg, havenz, fetched_seconds_ago=0)

    with pytest.raises(agent.ReaderError):
        agent.execute(cfg, open_door(DOOR_A))
    with pytest.raises(agent.ReaderError):
        agent.execute(cfg, open_door(DOOR_B))

    assert havenz.asked == 1, "the answer was seconds old; asking again would get the same one"
    assert reader.logins == ["wrong", "wrong"]


def test_a_refetch_keeps_the_session_of_readers_whose_credentials_did_not_change(cfg, monkeypatch):
    """Correcting one door's password must not log the other nineteen out."""
    havenz = FakeHavenz([row(DOOR_A, "a"), row(DOOR_B, "b", ip="192.168.213.105")])
    monkeypatch.setattr(agent, "backend_get", havenz)

    before = hold(cfg, havenz, fetched_seconds_ago=LONG_AGO)
    before[DOOR_A]._session = "session-a"
    before[DOOR_B]._session = "session-b"

    havenz.rows = [row(DOOR_A, "a"), row(DOOR_B, "b-new", ip="192.168.213.105")]
    after = agent.readers_for(cfg, force=True)

    assert after[DOOR_A] is before[DOOR_A], "unchanged reader: same client, session kept"
    assert after[DOOR_A]._session == "session-a"
    assert after[DOOR_B] is not before[DOOR_B], "changed reader: new client, logs in afresh"
    assert after[DOOR_B]._session is None
    assert after[DOOR_B]._password == "b-new"


def test_a_heartbeat_saying_the_credentials_changed_drops_the_held_reader(cfg, monkeypatch):
    """Havenz can also tell the agent outright, so the correction lands before the next failure."""
    havenz = FakeHavenz([row(DOOR_A, "a", credentialsUpdatedAt="2026-10-26T20:00:00Z")])
    monkeypatch.setattr(agent, "backend_get", havenz)
    hold(cfg, havenz, fetched_seconds_ago=LONG_AGO)

    def heartbeat_answer(stamp):
        return lambda cfg, path, payload, timeout=20: {
            "terminals": [{"id": DOOR_A, "ipAddress": "192.168.213.101", "credentialsUpdatedAt": stamp}],
            "serverTimeUtc": "2026-10-26T20:05:00Z"}

    monkeypatch.setattr(agent, "backend_post", heartbeat_answer("2026-10-26T20:00:00Z"))
    agent.heartbeat({**cfg, "heartbeat_path": "/api/agent/heartbeat"})
    assert DOOR_A in agent.STATE["readers"], "same stamp: nothing to reload"

    monkeypatch.setattr(agent, "backend_post", heartbeat_answer("2026-10-26T20:04:00Z"))
    agent.heartbeat({**cfg, "heartbeat_path": "/api/agent/heartbeat"})
    assert agent.STATE["readers"] == {}, "newer stamp: credentials are refetched on next use"


def test_a_heartbeat_without_a_credentials_stamp_changes_nothing(cfg, monkeypatch):
    """Against a backend that does not send the stamp yet, behaviour is exactly as before."""
    havenz = FakeHavenz([row(DOOR_A, "a")])
    monkeypatch.setattr(agent, "backend_get", havenz)
    hold(cfg, havenz, fetched_seconds_ago=LONG_AGO)

    monkeypatch.setattr(agent, "backend_post", lambda cfg, path, payload, timeout=20: {
        "terminals": [{"id": DOOR_A, "ipAddress": "192.168.213.101"}],
        "serverTimeUtc": "2026-10-26T20:05:00Z"})
    agent.heartbeat({**cfg, "heartbeat_path": "/api/agent/heartbeat"})
    assert DOOR_A in agent.STATE["readers"]
