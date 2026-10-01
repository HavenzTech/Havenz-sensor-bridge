"""
Twenty doors on one agent.

The first plant rehearsal - twenty readers, sixty people, one agent - found four things a single
door on a bench never shows:

  - one reader that stops answering held up every other door's commands for ten seconds at a time,
    because the agent finished a whole batch before asking for more work;
  - the record of finished commands was rewritten in full after every command;
  - a result the backend refused (HTTP 429) was logged and thrown away, so work the reader had
    done was recorded as "unknown";
  - a slow disk made every reader's event post wait in one line, past the reader's five seconds.

Run:  python3 -m pytest havenz-agent/tests -q
"""

import email.message
import io
import json
import os
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402

DOOR_A = "11111111-1111-1111-1111-111111111111"
DOOR_B = "22222222-2222-2222-2222-222222222222"


class StopLoop(BaseException):
    """Ends a loop that is written to outlive every Exception."""


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def command(command_id, terminal_id, kind="OpenDoor", intent_id=None):
    return {"id": command_id, "intentId": intent_id, "terminalId": terminal_id, "type": kind,
            "payload": json.dumps({"door": 1}), "notValidAfter": "2099-01-01T00:00:00Z"}


def http_error(code, retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    reason = {429: "Too Many Requests", 404: "Not Found", 503: "Service Unavailable"}.get(code, "Error")
    return urllib.error.HTTPError("http://havenz.invalid/x", code, reason, headers, io.BytesIO(b"{}"))


@pytest.fixture(autouse=True)
def clean():
    agent._executed.clear()
    agent._breakers.clear()
    yield
    agent._executed.clear()
    agent._breakers.clear()


@pytest.fixture
def cfg(tmp_path):
    return {"api_url": "http://havenz.invalid", "hub_key": "k",
            "executed_store_path": str(tmp_path / "executed.jsonl"),
            "result_outbox_path": str(tmp_path / "results.jsonl"),
            "event_queue_path": str(tmp_path / "events.jsonl"),
            "roster_path": str(tmp_path / "roster.json")}


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def journal(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# One reader that stops answering must not hold up the others
# ---------------------------------------------------------------------------

def run_command_loop(cfg, monkeypatch, batches):
    """
    Runs the real command loop against a scripted backend: each poll hands out the next batch,
    then the poll is held open (an idle long poll) until the test ends.

    Returns the poll times and a function that ends the loop and waits for its thread - while the
    scripted backend is still in place, so the loop cannot wander off and poll something else.
    """
    polls = []
    finished = threading.Event()
    remaining = list(batches)

    def backend_get(cfg, path, timeout=40):
        if not path.startswith("/api/agent/commands"):
            return []
        polls.append(time.monotonic())
        if remaining:
            return {"commands": remaining.pop(0)}
        finished.wait(30)
        raise StopLoop()

    def run():
        try:
            agent.command_loop(cfg)
        except StopLoop:
            pass

    def stop():
        finished.set()
        thread.join(10)
        assert not thread.is_alive(), "the command loop did not stop"

    monkeypatch.setattr(agent, "backend_get", backend_get)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return polls, stop


def test_a_reader_that_hangs_does_not_hold_up_another_doors_command(cfg, monkeypatch):
    """
    Door A accepts the connection and never answers. An unlock for door B arrives a moment later.
    It used to wait in the backend's queue for A's whole timeout, because the agent did not ask
    for more work until A's batch was finished.
    """
    release = threading.Event()
    reported = []

    def execute(cfg, command):
        if command["terminalId"] == DOOR_A:
            release.wait(30)                       # the silent reader
        return {"opened": True}

    monkeypatch.setattr(agent, "execute", execute)
    monkeypatch.setattr(agent, "report",
                        lambda cfg, command_id, *a, **k: reported.append(command_id))
    _, stop = run_command_loop(cfg, monkeypatch,
                               [[command("cmd-a", DOOR_A)], [command("cmd-b", DOOR_B)]])
    try:
        assert wait_for(lambda: "cmd-b" in reported, 3.0), (
            "door B's unlock must be collected and carried out while door A is still hanging")
        assert "cmd-a" not in reported, "door A is still waiting on its reader"
        release.set()
        assert wait_for(lambda: "cmd-a" in reported, 3.0), "and door A's own command still completes"
    finally:
        release.set()
        stop()


def test_two_commands_for_the_same_door_run_one_after_the_other_in_order(cfg, monkeypatch):
    """Grant then revoke must not invert: a door's commands are worked by one worker, in order."""
    running = []
    overlap = []
    order = []
    lock = threading.Lock()

    def execute(cfg, command):
        with lock:
            if running:
                overlap.append(command["id"])
            running.append(command["id"])
        time.sleep(0.05)
        with lock:
            running.remove(command["id"])
            order.append(command["id"])
        return {}

    monkeypatch.setattr(agent, "execute", execute)
    monkeypatch.setattr(agent, "report", lambda *a, **k: None)
    batch = [command(f"cmd-{n}", DOOR_A, kind="UpdateUser") for n in range(4)]
    _, stop = run_command_loop(cfg, monkeypatch, [batch[:2], batch[2:]])
    try:
        assert wait_for(lambda: len(order) == 4, 5.0)
    finally:
        stop()
    assert order == ["cmd-0", "cmd-1", "cmd-2", "cmd-3"]
    assert overlap == [], "one reader is never sent two commands at once"


def test_polls_are_spaced_so_one_poll_collects_several_doors_work(cfg, monkeypatch):
    """Under load a poll returns at once; without a gap the agent would poll once per command."""
    monkeypatch.setattr(agent, "execute", lambda cfg, command: {})
    monkeypatch.setattr(agent, "report", lambda *a, **k: None)
    polls, stop = run_command_loop(
        cfg, monkeypatch, [[command(f"cmd-{n}", DOOR_A, kind="UpdateUser")] for n in range(4)])
    try:
        assert wait_for(lambda: len(polls) >= 5, 5.0)
    finally:
        stop()
    gaps = [b - a for a, b in zip(polls, polls[1:])][:4]
    assert min(gaps) >= agent.POLL_MIN_INTERVAL_SECONDS - 0.02, f"polls started {gaps} s apart"


def test_twenty_workers_asking_for_the_roster_fetch_it_once(cfg, monkeypatch):
    fetches = []
    gate = threading.Event()

    def backend_get(cfg, path, timeout=40):
        fetches.append(path)
        gate.wait(2)
        return [{"id": DOOR_A, "name": "A", "ipAddress": "10.0.0.1", "username": "u", "password": "p"}]

    monkeypatch.setattr(agent, "backend_get", backend_get)
    monkeypatch.setitem(agent.STATE, "readers", {})
    threads = [threading.Thread(target=agent.readers_for, args=(cfg,), kwargs={"force": True}, daemon=True)
               for _ in range(20)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(fetches) == 1, "the refresh is shared, not repeated by every worker that asked"


# ---------------------------------------------------------------------------
# The record of finished commands is appended to, not rewritten
# ---------------------------------------------------------------------------

def test_remembering_a_command_appends_one_line_and_never_rewrites_the_file(cfg, monkeypatch):
    """
    The whole store used to be written out, fsynced and swapped in after every command: 0.2-0.4 s
    each once a day's enrolment was in it, on the path every door's work passes through.
    """
    replaces = []
    real_replace = os.replace
    monkeypatch.setattr(agent.os, "replace", lambda a, b: (replaces.append(b), real_replace(a, b))[1])
    path = Path(agent.executed_store_path(cfg))

    sizes = []
    for n in range(50):
        agent._remember(cfg, f"command-{n}", f"tap-{n}", {"n": n})
        sizes.append(path.stat().st_size)

    assert replaces == [], "nothing is rewritten on the command path"
    assert sizes == sorted(sizes) and len(set(sizes)) == 50, "the file only ever grows, a line at a time"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 50


def test_the_journal_brings_everything_back_after_a_restart(cfg):
    for n in range(5):
        agent._remember(cfg, f"command-{n}", f"tap-{n}", {"n": n})
    agent._executed.clear()                               # the process dies

    assert agent.executed_store_load(cfg) == 10           # five commands, five taps
    assert agent._already_executed(None, "tap-3") == ({"n": 3}, "intent")
    assert agent._already_executed("command-4", None) == ({"n": 4}, "command")


def test_a_store_left_by_0_5_0_is_imported_once(cfg, tmp_path):
    legacy = tmp_path / "executed.json"
    legacy.write_text(json.dumps({"version": 1, "entries": [
        {"key": "old-command", "at": time.time(), "result": {"opened": True}},
        {"key": "old-tap", "at": time.time(), "result": {"opened": True}},
    ]}), encoding="utf-8")

    assert agent.executed_store_load(cfg) == 2
    assert agent._already_executed(None, "old-tap")[1] == "intent"
    assert not legacy.exists(), "imported, so it is not read again (or mistaken for the store) later"

    agent._executed.clear()
    assert agent.executed_store_load(cfg) == 2, "and it is in the journal now"


def test_a_0_5_0_path_saved_in_the_config_means_the_journal_beside_it(tmp_path):
    """A paired 0.5.0 agent's config.json already names /data/executed.json."""
    cfg = {"executed_store_path": str(tmp_path / "executed.json")}
    assert agent.executed_store_path(cfg) == str(tmp_path / "executed.jsonl")


def test_a_torn_last_line_costs_only_that_line(cfg):
    agent._remember(cfg, "command-a", "tap-a", {"opened": True})
    with open(agent.executed_store_path(cfg), "ab") as f:
        f.write(b'{"v":2,"keys":["command-b"],"at":17')     # the power went here
    agent._executed.clear()

    assert agent.executed_store_load(cfg) == 2
    agent._remember(cfg, "command-c", None, {})               # not glued onto the torn line
    agent._executed.clear()
    assert agent.executed_store_load(cfg) == 3


def test_an_unlock_is_on_disk_before_it_is_reported_and_a_user_push_is_not_made_to_wait(cfg, monkeypatch):
    """A door is the reason the record exists; only a door pays for the fsync on its own path."""
    events = []
    real_fsync = os.fsync
    monkeypatch.setattr(agent.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(agent, "execute", lambda cfg, command: {})
    monkeypatch.setattr(agent, "report", lambda *a, **k: events.append("report"))

    agent.handle(cfg, command("cmd-push", DOOR_A, kind="UpdateUser"))
    assert events == ["report"], "a user push is appended and reported; the flusher syncs it within a second"

    events.clear()
    agent.handle(cfg, command("cmd-open", DOOR_A, kind="OpenDoor", intent_id="tap-1"))
    assert events[:2] == ["fsync", "report"]


def test_the_journal_is_compacted_in_the_background_and_keeps_the_newest(cfg, monkeypatch):
    monkeypatch.setattr(agent, "EXECUTED_MAX", 10)
    monkeypatch.setattr(agent, "EXECUTED_COMPACT_MIN_LINES", 20)
    for n in range(25):
        agent._remember(cfg, f"command-{n}", None, {"n": n})

    agent.executed_store_sync(cfg)                         # what the background thread does

    lines = Path(agent.executed_store_path(cfg)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 10, "the file is bounded too, not just the memory"
    agent._executed.clear()
    assert agent.executed_store_load(cfg) == 10
    assert agent._already_executed("command-24", None)[1] == "command"
    assert agent._already_executed("command-0", None) == (None, None)


# ---------------------------------------------------------------------------
# A result is never dropped
# ---------------------------------------------------------------------------

class Backend:
    """Stands in for POST /api/agent/commands/{id}/result: answers as scripted, then 200."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.received = []

    def __call__(self, cfg, rec):
        self.received.append(rec["id"])
        return self.answers.pop(0) if self.answers else ("ok", None, None)


def body(n=1):
    return {"success": True, "result": json.dumps({"n": n}), "error": None, "durationMs": 5}


def test_a_refused_result_is_retried_by_the_running_agent_and_reaches_havenz(cfg, monkeypatch):
    """
    The whole fault in one test: the backend answers 429 to a result. It used to be logged and
    thrown away, and the backend then recorded the command as 'unknown' though the reader did it.
    """
    posts = []

    def backend_post(cfg, path, payload, timeout=20):
        posts.append(path)
        if len(posts) == 1:
            raise http_error(429, retry_after=1)
        return {"accepted": True}

    monkeypatch.setattr(agent, "backend_post", backend_post)
    monkeypatch.setitem(agent.STATE, "result_outbox", None)

    agent.report(cfg, "cmd-1", True, {"terminalUserId": 7}, None, 12)

    assert wait_for(lambda: len(posts) >= 2, 6.0), "the result must be sent again after the refusal"
    assert posts[0] == posts[1] == "/api/agent/commands/cmd-1/result"


def test_a_429_is_honoured_and_the_result_is_delivered_once(cfg):
    clock = Clock()
    outbox = agent.ResultOutbox(cfg["result_outbox_path"], clock=clock)
    backend = Backend(("rate_limited", "HTTP Error 429: Too Many Requests", 7))

    rec = outbox.put("cmd-1", body())
    assert outbox.deliver(cfg, rec, post=backend) == "rate_limited"
    assert outbox.take(timeout=0) is None, "nothing is sent before Retry-After has passed"
    clock.advance(6.9)
    assert outbox.take(timeout=0) is None
    clock.advance(0.2)
    again = outbox.take(timeout=0)
    assert again is rec
    assert outbox.deliver(cfg, again, post=backend) == "ok"

    snapshot = outbox.snapshot()
    assert backend.received == ["cmd-1", "cmd-1"]
    assert snapshot["pending"] == 0 and snapshot["delivered"] == 1 and snapshot["dropped"] == 0
    assert snapshot["refused_429"] == 1


def test_a_429_holds_back_every_other_result_until_retry_after_has_passed(cfg):
    clock = Clock()
    outbox = agent.ResultOutbox(cfg["result_outbox_path"], clock=clock)
    first, second = outbox.put("cmd-1", body(1)), outbox.put("cmd-2", body(2))
    backend = Backend(("rate_limited", "HTTP Error 429: Too Many Requests", 5))

    outbox.deliver(cfg, first, post=backend)

    assert outbox.blocked(), "twenty workers must not keep knocking on a closed door"
    assert outbox.take(timeout=0) is None, "cmd-2 waits too, though it was never refused itself"
    clock.advance(5.1)
    sent = []
    while (rec := outbox.take(timeout=0)) is not None:
        outbox.deliver(cfg, rec, post=backend)
        sent.append(rec["id"])
    assert sorted(sent) == ["cmd-1", "cmd-2"] and outbox.snapshot()["pending"] == 0
    assert second["attempts"] == 1


def test_an_outage_backs_off_and_keeps_the_result(cfg):
    clock = Clock()
    outbox = agent.ResultOutbox(cfg["result_outbox_path"], clock=clock)
    rec = outbox.put("cmd-1", body())
    backend = Backend(("retry", "timed out", None), ("retry", "HTTP Error 503: Service Unavailable", None))

    waits = []
    for _ in range(2):
        taken = outbox.take(timeout=0)
        assert outbox.deliver(cfg, taken, post=backend) == "retry"
        waits.append(round(rec["next_try"] - clock()))
        clock.advance(waits[-1] + 0.1)
    assert waits == [1, 2], "1, 2, 4 ... 60 s"
    assert outbox.deliver(cfg, outbox.take(timeout=0), post=backend) == "ok"
    assert outbox.snapshot()["dropped"] == 0


def test_a_result_waiting_to_be_sent_survives_a_restart(cfg):
    before = agent.ResultOutbox(cfg["result_outbox_path"])
    rec = before.put("cmd-1", body())
    before.deliver(cfg, rec, post=Backend(("retry", "no route to host", None)))
    before.sync()
    del before                                             # the add-on is restarted

    after = agent.ResultOutbox(cfg["result_outbox_path"])
    assert after.load() == 1
    backend = Backend()
    assert after.deliver(cfg, after.take(timeout=0), post=backend) == "ok"
    assert backend.received == ["cmd-1"]
    after.sync()
    assert agent.ResultOutbox(cfg["result_outbox_path"]).load() == 0, "and it is not sent a third time"


def test_a_result_havenz_says_is_not_ours_is_dropped_and_counted(cfg):
    outbox = agent.ResultOutbox(cfg["result_outbox_path"])
    rec = outbox.put("cmd-1", body())

    assert outbox.deliver(cfg, rec, post=Backend(("gone", "HTTP Error 404: Not Found", None))) == "dropped"

    snapshot = outbox.snapshot()
    assert snapshot["pending"] == 0 and snapshot["dropped"] == 1
    outbox.sync()
    assert [r["op"] for r in journal(cfg["result_outbox_path"])] == ["put", "drop"], "never silently"


def test_the_result_of_a_read_is_not_written_to_disk(cfg):
    """The log read runs every thirty seconds per door and is repeatable; it stays in memory."""
    outbox = agent.ResultOutbox(cfg["result_outbox_path"])
    rec = outbox.put("cmd-read", body(), durable=False)

    assert not Path(cfg["result_outbox_path"]).exists()
    assert outbox.snapshot()["pending"] == 1, "but it is still retried while the agent is running"
    assert outbox.deliver(cfg, rec, post=Backend()) == "ok"
    assert not Path(cfg["result_outbox_path"]).exists()


def test_a_result_nobody_would_take_for_a_day_is_dropped_not_kept_for_ever(cfg):
    clock = Clock()
    outbox = agent.ResultOutbox(cfg["result_outbox_path"], clock=clock)
    outbox.put("cmd-1", body())
    clock.advance(agent.RESULT_MAX_AGE_SECONDS + 1)

    assert outbox.take(timeout=0) is None
    assert outbox.snapshot()["dropped"] == 1 and outbox.snapshot()["pending"] == 0


def test_a_full_outbox_drops_the_oldest_and_says_so(cfg, monkeypatch):
    monkeypatch.setattr(agent, "RESULT_MAX_PENDING", 3)
    outbox = agent.ResultOutbox(cfg["result_outbox_path"])
    for n in range(5):
        outbox.put(f"cmd-{n}", body(n))

    snapshot = outbox.snapshot()
    assert snapshot["pending"] == 3 and snapshot["dropped"] == 2
    assert outbox.take(timeout=0)["id"] == "cmd-2", "the newest are the ones kept"
    outbox.sync()
    assert [r["op"] for r in journal(cfg["result_outbox_path"])].count("drop") == 2, "never silently"


def test_the_refusal_is_logged_in_words_the_rehearsal_counts(cfg, caplog):
    outbox = agent.ResultOutbox(cfg["result_outbox_path"])
    rec = outbox.put("cmd-1", body())
    with caplog.at_level("WARNING", logger="havenz-agent"):
        outbox.deliver(cfg, rec, post=Backend(("rate_limited", "HTTP Error 429: Too Many Requests", 3)))
    line = caplog.messages[-1]
    assert "could not report the result of cmd-1" in line and "HTTP Error 429" in line
    assert "kept, retrying in 3s" in line


def test_retry_after_is_read_from_the_refusal():
    assert agent.retry_after_seconds(http_error(429, retry_after=17), 2) == 17
    assert agent.retry_after_seconds(http_error(429), 2) == 2
    assert agent.retry_after_seconds(http_error(429, retry_after="soon"), 2) == 2
    assert agent.retry_after_seconds(http_error(429, retry_after=9999), 2) == agent.RETRY_AFTER_MAX_SECONDS


def test_handle_keeps_the_result_of_a_read_off_the_disk_and_a_push_on_it(cfg, monkeypatch):
    posted = []
    monkeypatch.setattr(agent, "backend_post", lambda cfg, path, payload, timeout=20: posted.append(path) or {})
    monkeypatch.setattr(agent, "execute", lambda cfg, command: {"entries": []})
    monkeypatch.setitem(agent.STATE, "result_outbox", None)

    agent.handle(cfg, command("cmd-read", DOOR_A, kind="GetAccessLogs"))
    assert not Path(cfg["result_outbox_path"]).exists()

    agent.handle(cfg, command("cmd-push", DOOR_A, kind="UpdateUser"))
    agent.STATE["result_outbox"].sync()
    assert [r["op"] for r in journal(cfg["result_outbox_path"])] == ["put", "ack"]
    assert len(posted) == 2


def test_a_refused_event_waits_at_least_as_long_as_havenz_asked(cfg):
    clock = Clock()
    queue = agent.EventQueue(cfg["event_queue_path"], clock=clock)
    rec = queue.put(DOOR_A, "dao", b"scan-1")

    outcome = agent.deliver_one(cfg, queue, queue.take(timeout=0),
                                post=lambda cfg, rec, age_ms, attempt: ("retry", "HTTP 429", 20))

    assert outcome == "retry"
    assert rec["next_try"] - clock() >= 20


# ---------------------------------------------------------------------------
# A slow disk must not make a reader wait past its five seconds
# ---------------------------------------------------------------------------

def test_twenty_doors_posting_at_once_share_a_disk_write_instead_of_queueing_for_one_each(cfg, monkeypatch):
    """
    Measured in the rehearsal at a quarter of a second a write: twenty doors in the same second
    waited for twenty writes in a row, and the last reader had hung up before it was answered.
    """
    real_fsync = os.fsync

    def slow_fsync(fd):
        time.sleep(0.25)
        real_fsync(fd)

    monkeypatch.setattr(agent.os, "fsync", slow_fsync)
    queue = agent.EventQueue(cfg["event_queue_path"])
    took = []

    def post(n):
        t0 = time.monotonic()
        queue.put(DOOR_A if n % 2 else DOOR_B, "dao", f"scan-{n}".encode())
        took.append(time.monotonic() - t0)

    threads = [threading.Thread(target=post, args=(n,)) for n in range(20)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    elapsed = time.monotonic() - t0

    assert len(took) == 20
    assert elapsed < 2.0, f"20 posts took {elapsed:.2f}s; one write each in a row would be 5 s"
    monkeypatch.setattr(agent.os, "fsync", real_fsync)
    assert agent.EventQueue(cfg["event_queue_path"]).load() == 20, "and every one of them is on disk"


def test_a_disk_that_stalls_does_not_keep_the_reader_waiting_and_the_event_is_not_lost(cfg, monkeypatch):
    """
    Past the bounded wait the reader is answered anyway: the event is in memory and being
    delivered, and its line is still on its way to the disk.
    """
    monkeypatch.setattr(agent, "EVENT_ACK_WAIT_SECONDS", 0.2)
    release = threading.Event()
    real_fsync = os.fsync

    def stalled_fsync(fd):
        release.wait(30)
        real_fsync(fd)

    monkeypatch.setattr(agent.os, "fsync", stalled_fsync)
    queue = agent.EventQueue(cfg["event_queue_path"])
    try:
        t0 = time.monotonic()
        rec = queue.put(DOOR_A, "dao", b"scan-1")
        waited = time.monotonic() - t0

        assert 0.15 <= waited < 1.5, f"put() returned after {waited:.2f}s"
        assert queue.snapshot()["answered_before_durable"] == 1
        assert queue.take(timeout=0) is rec, "it can already be delivered"
    finally:
        release.set()
    queue.flush()
    assert agent.EventQueue(cfg["event_queue_path"]).load() == 1, "once the disk answers, the event is on it"


# ---------------------------------------------------------------------------
# Odds and ends that only show up under load
# ---------------------------------------------------------------------------

def test_a_command_that_finishes_while_the_journal_is_being_compacted_is_not_lost(cfg, monkeypatch):
    """The copy is written without holding up the doors; what they append meanwhile is carried over."""
    for n in range(3):
        agent._remember(cfg, f"command-{n}", None, {"n": n})
    real_fsync = os.fsync
    slipped_in = []

    def fsync_with_a_door_finishing_meanwhile(fd):
        if not slipped_in:
            slipped_in.append(True)
            agent._remember(cfg, "command-late", "tap-late", {"late": True})
        real_fsync(fd)

    monkeypatch.setattr(agent.os, "fsync", fsync_with_a_door_finishing_meanwhile)
    assert agent._executed_compact(agent.executed_store_path(cfg)) is True
    monkeypatch.setattr(agent.os, "fsync", real_fsync)

    agent._executed.clear()
    assert agent.executed_store_load(cfg) == 5
    assert agent._already_executed(None, "tap-late") == ({"late": True}, "intent")


def test_the_result_outbox_is_compacted_so_it_stays_the_size_of_what_is_waiting(cfg, monkeypatch):
    monkeypatch.setattr(agent, "RESULT_COMPACT_AFTER", 3)
    outbox = agent.ResultOutbox(cfg["result_outbox_path"])
    recs = [outbox.put(f"cmd-{n}", body(n)) for n in range(4)]
    for rec in recs[:3]:
        outbox.deliver(cfg, rec, post=Backend())

    outbox.sync()

    lines = journal(cfg["result_outbox_path"])
    assert [(r["op"], r["id"]) for r in lines] == [("put", "cmd-3")], "three delivered and compacted away"


def test_a_refused_poll_waits_as_long_as_havenz_asked_not_the_usual_backoff(cfg, monkeypatch):
    slept = []
    polls = []

    def backend_get(cfg, path, timeout=40):
        polls.append(path)
        if len(polls) == 1:
            raise http_error(429, retry_after=3)
        raise StopLoop()

    monkeypatch.setattr(agent, "backend_get", backend_get)
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: slept.append(seconds))
    with pytest.raises(StopLoop):
        agent.command_loop(cfg)

    assert 3 in slept, f"slept {slept}"
    assert agent.BACKOFF_START_SECONDS not in slept
