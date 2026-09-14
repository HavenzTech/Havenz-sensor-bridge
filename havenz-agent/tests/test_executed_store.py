"""
The agent must not forget a door it just opened.

Delivery from the backend is at-least-once by design, so the agent is the only party that knows
whether a reader was actually touched. It used to know that in memory only: an agent restarted
between opening a door and reporting it came back blank, the backend retried, and the door opened
a second time. A Raspberry Pi restarts — on an add-on update, on a power blip, on a crash — so that
was not a theoretical gap.

These tests cover the two halves of the fix: dedupe on the INTENT (one tap) rather than only on the
command id (one delivery), and keep the set on disk so a restart cannot lose it.

Run:  python3 -m pytest havenz-agent/tests -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402


@pytest.fixture(autouse=True)
def clean_store(tmp_path):
    """A fresh process-level set and a store path of its own for every test."""
    agent._executed.clear()
    yield
    agent._executed.clear()


@pytest.fixture
def cfg(tmp_path):
    return {"api_url": "http://localhost", "executed_store_path": str(tmp_path / "executed.json")}


class FakeReport:
    """Captures what the agent told the backend, instead of telling it."""

    def __init__(self):
        self.calls = []

    def __call__(self, cfg, command_id, success, result, error, duration_ms):
        self.calls.append({
            "commandId": command_id, "success": success, "result": result,
            "error": error, "durationMs": duration_ms,
        })


def open_door(command_id, intent_id=None, not_valid_after=None):
    return {
        "id": command_id,
        "intentId": intent_id,
        "terminalId": "terminal-1",
        "type": "OpenDoor",
        "payload": json.dumps({"door": 1}),
        # Far enough out that expiry never decides the outcome of these tests.
        "notValidAfter": not_valid_after or "2099-01-01T00:00:00Z",
    }


@pytest.fixture
def executed(monkeypatch):
    """Counts real reader calls, so 'the door opened once' is a number and not a vibe."""
    calls = []

    def fake_execute(cfg, command):
        calls.append(command)
        return {"opened": True}

    monkeypatch.setattr(agent, "execute", fake_execute)
    monkeypatch.setattr(agent, "breaker_is_open", lambda _terminal: False)
    monkeypatch.setattr(agent, "breaker_record", lambda *a, **k: None)
    return calls


# ---------------------------------------------------------------------------
# Dedupe on the intent, not only the delivery
# ---------------------------------------------------------------------------

def test_the_same_intent_under_two_command_ids_touches_the_reader_once(cfg, executed, monkeypatch):
    reported = FakeReport()
    monkeypatch.setattr(agent, "report", reported)

    agent.handle(cfg, open_door("command-a", intent_id="tap-1"))
    agent.handle(cfg, open_door("command-b", intent_id="tap-1"))

    assert len(executed) == 1, "one tap must reach the reader once, whatever the backend re-mints"
    assert [c["commandId"] for c in reported.calls] == ["command-a", "command-b"]
    assert all(c["success"] for c in reported.calls), (
        "the repeat is acknowledged, not failed — a failure would make the backend retry it")
    assert reported.calls[1]["result"] == {"opened": True}, "and it replays the original result"


def test_two_different_intents_are_two_unlocks(cfg, executed, monkeypatch):
    monkeypatch.setattr(agent, "report", FakeReport())

    agent.handle(cfg, open_door("command-a", intent_id="tap-1"))
    agent.handle(cfg, open_door("command-b", intent_id="tap-2"))

    assert len(executed) == 2, "deduplication must not swallow a second deliberate tap"


def test_a_repeat_of_the_same_command_id_is_still_deduped_without_an_intent(cfg, executed, monkeypatch):
    # An older backend sends no intentId. The command-id behaviour it relied on must be unchanged.
    monkeypatch.setattr(agent, "report", FakeReport())

    agent.handle(cfg, open_door("command-a", intent_id=None))
    agent.handle(cfg, open_door("command-a", intent_id=None))

    assert len(executed) == 1


# ---------------------------------------------------------------------------
# Surviving a restart
# ---------------------------------------------------------------------------

def test_the_executed_set_is_written_to_disk(cfg, executed, monkeypatch):
    monkeypatch.setattr(agent, "report", FakeReport())

    agent.handle(cfg, open_door("command-a", intent_id="tap-1"))

    stored = json.loads(Path(cfg["executed_store_path"]).read_text(encoding="utf-8"))
    assert stored["version"] == agent.EXECUTED_STORE_VERSION
    keys = {e["key"] for e in stored["entries"]}
    assert keys == {"command-a", "tap-1"}, "both the delivery and the intent have to survive"


def test_a_restart_does_not_reopen_a_door_the_agent_already_opened(cfg, executed, monkeypatch):
    reported = FakeReport()
    monkeypatch.setattr(agent, "report", reported)

    agent.handle(cfg, open_door("command-a", intent_id="tap-1"))
    assert len(executed) == 1

    # The restart: the process forgets everything and reloads from /data.
    agent._executed.clear()
    agent.executed_store_load(cfg)

    # The backend never got the result, so it re-delivers the same tap under a new command id.
    agent.handle(cfg, open_door("command-b", intent_id="tap-1"))

    assert len(executed) == 1, "the door must not open a second time because the agent restarted"
    assert reported.calls[-1]["success"] is True
    assert reported.calls[-1]["result"] == {"opened": True}


def test_loading_an_empty_or_missing_store_is_not_an_error(cfg):
    assert agent.executed_store_load(cfg) == 0

    Path(cfg["executed_store_path"]).write_text("{ this is not json", encoding="utf-8")
    assert agent.executed_store_load(cfg) == 0, (
        "a store truncated by a power cut must not stop the agent starting")


def test_a_store_from_a_future_version_is_ignored_rather_than_misread(cfg):
    Path(cfg["executed_store_path"]).write_text(
        json.dumps({"version": 999, "entries": [{"key": "tap-1", "at": 0, "result": None}]}),
        encoding="utf-8")

    assert agent.executed_store_load(cfg) == 0
    assert agent._already_executed(None, "tap-1") == (None, None)


# ---------------------------------------------------------------------------
# Bounds — this runs for months on a small box
# ---------------------------------------------------------------------------

def test_entries_older_than_a_week_are_dropped_on_load(cfg):
    import time
    old = time.time() - agent.EXECUTED_TTL_SECONDS - 60
    Path(cfg["executed_store_path"]).write_text(json.dumps({
        "version": agent.EXECUTED_STORE_VERSION,
        "entries": [
            {"key": "ancient", "at": old, "result": None},
            {"key": "recent", "at": time.time(), "result": {"opened": True}},
        ],
    }), encoding="utf-8")

    assert agent.executed_store_load(cfg) == 1
    assert agent._already_executed(None, "recent")[1] == "intent"
    assert agent._already_executed("ancient", None) == (None, None)


def test_the_set_is_capped_and_keeps_the_newest(cfg, monkeypatch):
    monkeypatch.setattr(agent, "EXECUTED_MAX", 10)

    for i in range(25):
        agent._remember(cfg, f"command-{i}", None, {"i": i})

    with agent._executed_lock:
        assert len(agent._executed) == 10
        assert "command-24" in agent._executed
        assert "command-0" not in agent._executed

    stored = json.loads(Path(cfg["executed_store_path"]).read_text(encoding="utf-8"))
    assert len(stored["entries"]) == 10, "the file is bounded too, not just the memory"


def test_a_store_that_cannot_be_written_does_not_fail_the_command(cfg, executed, monkeypatch, tmp_path):
    # The work is already done. Losing the record of it costs a possible repeat after a restart;
    # refusing to acknowledge a door we just opened costs a retry immediately.
    monkeypatch.setattr(agent, "report", FakeReport())
    cfg["executed_store_path"] = str(tmp_path / "no" / "such" / "dir" / "executed.json")

    agent.handle(cfg, open_door("command-a", intent_id="tap-1"))

    assert len(executed) == 1
    assert agent._already_executed("command-a", None)[1] == "command", (
        "and the in-memory half still works, so a repeat in this process is still caught")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_the_store_path_defaults_to_the_addons_persistent_volume(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_url": "http://localhost"}), encoding="utf-8")

    loaded = agent.load_config(str(path))

    assert loaded["executed_store_path"] == "/data/executed.json", (
        "/data is the add-on's own volume — anywhere else is lost on an update")
