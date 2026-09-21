"""
A reader's event must not be lost, and must not come back as a ghost.

A reader posts an event once. The agent used to answer 200 and then hold the event in a thread that
tried Havenz three times over three seconds and gave up - so an add-on restart, or an uplink that
blinked for ten seconds, lost it. These tests cover the replacement: write it to disk, THEN answer
the reader, deliver from the disk, and pick up where we left off after a restart.

The second half is the high-water mark for reading a reader's log, so a poll every thirty seconds
stops hauling the whole history upstream - without hiding a reader whose log has started again.

Run:  python3 -m pytest havenz-agent/tests -q
"""

import base64
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402

DOOR_A = "11111111-1111-1111-1111-111111111111"
DOOR_B = "22222222-2222-2222-2222-222222222222"


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "events.jsonl")


@pytest.fixture
def cfg(tmp_path, path):
    return {"api_url": "http://havenz.invalid", "hub_key": "k", "event_queue_path": path,
            "roster_path": str(tmp_path / "roster.json")}


def journal(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class Havenz:
    """Stands in for the backend: records what it was sent, answers as scripted."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.received = []

    def __call__(self, cfg, rec, age_ms, attempt):
        self.received.append({"id": rec["id"], "terminalId": rec["terminalId"], "kind": rec["kind"],
                              "body": base64.b64decode(rec["body"]), "age_ms": age_ms,
                              "attempt": attempt})
        return self.answers.pop(0) if self.answers else ("ok", None)


# ---------------------------------------------------------------------------
# Custody: on disk before the reader is answered
# ---------------------------------------------------------------------------

def test_an_event_is_on_disk_by_the_time_put_returns(path):
    queue = agent.EventQueue(path)

    queue.put(DOOR_A, "dao", b'{"object_changes":[]}')

    lines = journal(path)
    assert len(lines) == 1
    assert lines[0]["op"] == "put" and lines[0]["terminalId"] == DOOR_A and lines[0]["kind"] == "dao"
    assert base64.b64decode(lines[0]["body"]) == b'{"object_changes":[]}', "byte for byte as the reader sent it"


def test_the_reader_is_answered_only_after_the_event_is_written(cfg, path):
    """The order IS the fix: disk first, 200 second."""
    order = []

    class Recording(agent.EventQueue):
        def put(self, terminal_id, kind, body):
            rec = super().put(terminal_id, kind, body)
            order.append(("on-disk", len(journal(path))))
            return rec

    queue = Recording(path)
    agent.STATE["readers"] = {}
    agent.STATE["roster_hosts"] = {"127.0.0.1": DOOR_A}
    gate = threading.Event()
    original = agent.event_delivery_loop
    agent.event_delivery_loop = lambda cfg, q: gate.wait()     # no delivery during this test
    try:
        server = agent.start_event_listener(cfg, 0, queue=queue)
        port = server.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/amico/notifications/dao",
                                     data=b'{"n":1}', method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            order.append(("answered", resp.status))
        server.shutdown()
    finally:
        gate.set()
        agent.event_delivery_loop = original

    assert order == [("on-disk", 1), ("answered", 200)]


def test_an_event_from_an_address_that_is_not_ours_is_refused_and_not_queued(cfg, path):
    queue = agent.EventQueue(path)
    agent.STATE["readers"] = {}
    agent.STATE["roster_hosts"] = {}
    server = agent.start_event_listener(cfg, 0, queue=queue)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/amico/notifications/dao",
                                     data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(req, timeout=5)
        assert refused.value.code == 404
    finally:
        server.shutdown()
    assert queue.snapshot()["pending"] == 0


def test_the_saved_roster_attributes_events_when_havenz_is_unreachable_after_a_restart(cfg):
    """No roster from the backend yet - the uplink is down - but the door's address is remembered."""
    agent.roster_save(cfg, {"192.168.0.41": DOOR_A})
    agent.STATE["readers"] = {}
    agent.STATE["roster_hosts"] = {}

    agent.roster_load(cfg)

    assert agent.terminal_for_source("192.168.0.41") == DOOR_A
    assert agent.terminal_for_source("192.168.0.99") is None
    assert "password" not in Path(cfg["roster_path"]).read_text(), "addresses and ids only - never a credential"


# ---------------------------------------------------------------------------
# Delivery, and the restart in the middle of it
# ---------------------------------------------------------------------------

def test_a_delivered_event_is_acknowledged_in_the_journal_and_leaves_the_queue(cfg, path):
    queue = agent.EventQueue(path)
    rec = queue.put(DOOR_A, "dao", b"scan-1")
    havenz = Havenz()

    assert agent.deliver_one(cfg, queue, queue.take(timeout=0), post=havenz) == "ok"

    assert [r["op"] for r in journal(path)] == ["put", "ack"]
    assert queue.snapshot()["pending"] == 0
    assert havenz.received[0]["id"] == rec["id"] and havenz.received[0]["attempt"] == 1


def test_a_restart_before_delivery_loses_nothing(cfg, path):
    """The add-on is restarted with three events still waiting. All three go up afterwards, in order."""
    before = agent.EventQueue(path)
    for n in (1, 2, 3):
        before.put(DOOR_A, "dao", f"scan-{n}".encode())
    del before                                     # the process dies; only the file survives

    after = agent.EventQueue(path)
    assert after.load() == 3

    havenz = Havenz()
    while (rec := after.take(timeout=0)) is not None:
        agent.deliver_one(cfg, after, rec, post=havenz)

    assert [r["body"] for r in havenz.received] == [b"scan-1", b"scan-2", b"scan-3"]
    assert after.snapshot()["pending"] == 0
    assert agent.EventQueue(path).load() == 0, "and a second restart finds nothing left to send"


def test_a_restart_mid_delivery_redelivers_the_event_under_the_same_id(cfg, path):
    """
    Havenz answered 200 and the agent died before writing that down. The event must go up again -
    losing it is the failure - and it goes up under the SAME id, so the backend (which
    de-duplicates access events on the reader's own row id) stores nothing twice.
    """
    first = agent.EventQueue(path)
    rec = first.put(DOOR_A, "dao", b"scan-1")
    taken = first.take(timeout=0)
    sent = Havenz()
    sent(cfg, taken, first.age_ms(taken), 1)       # delivered...
    del first                                       # ...and the process dies before the ack

    second = agent.EventQueue(path)
    assert second.load() == 1
    again = Havenz()
    agent.deliver_one(cfg, second, second.take(timeout=0), post=again)

    assert again.received[0]["id"] == rec["id"] == sent.received[0]["id"]
    assert again.received[0]["body"] == b"scan-1"
    assert second.snapshot()["pending"] == 0


def test_an_outage_keeps_the_event_and_retries_until_it_ends(cfg, path):
    clock = Clock()
    queue = agent.EventQueue(path, clock=clock)
    queue.put(DOOR_A, "dao", b"scan-1")
    havenz = Havenz(("retry", "timed out"), ("retry", "HTTP 503"), ("ok", None))

    assert agent.deliver_one(cfg, queue, queue.take(timeout=0), post=havenz) == "retry"
    assert queue.take(timeout=0) is None, "backing off, not hammering a dead uplink"
    assert queue.snapshot()["pending"] == 1 and queue.snapshot()["last_error"] == "timed out"

    clock.advance(2)
    assert agent.deliver_one(cfg, queue, queue.take(timeout=0), post=havenz) == "retry"
    clock.advance(5)
    assert agent.deliver_one(cfg, queue, queue.take(timeout=0), post=havenz) == "ok"

    assert [r["attempt"] for r in havenz.received] == [1, 2, 3]
    assert [r["op"] for r in journal(path)] == ["put", "ack"]


def test_the_age_sent_with_a_delivery_is_how_long_the_event_waited(cfg, path):
    """
    This number is what lets Havenz record a backlog as history instead of flashing "Welcome" on a
    door panel for someone who walked through hours ago.
    """
    clock = Clock()
    before = agent.EventQueue(path, clock=clock)
    before.put(DOOR_A, "dao", b"scan-1")
    del before

    clock.advance(2 * 3600)                         # the agent was down all afternoon
    after = agent.EventQueue(path, clock=clock)
    after.load()
    havenz = Havenz()
    agent.deliver_one(cfg, after, after.take(timeout=0), post=havenz)

    assert havenz.received[0]["age_ms"] == 2 * 3600 * 1000


def test_the_headers_carry_the_event_id_its_age_and_the_attempt(cfg):
    headers = agent.event_headers(cfg, {"id": "evt-1", "terminalId": DOOR_A}, 4200, 3)

    assert headers["X-Event-Id"] == "evt-1"
    assert headers["X-Event-Age-Ms"] == "4200"
    assert headers["X-Event-Attempt"] == "3"
    assert headers["X-Terminal-Id"] == DOOR_A and headers["X-Hub-Key"] == "k"


# ---------------------------------------------------------------------------
# Order, and one bad door not damming the site
# ---------------------------------------------------------------------------

def test_a_doors_events_go_up_in_the_order_its_reader_sent_them(cfg, path):
    queue = agent.EventQueue(path)
    queue.put(DOOR_A, "dao", b"a1")
    queue.put(DOOR_A, "dao", b"a2")

    first = queue.take(timeout=0)
    assert queue.take(timeout=0) is None, "a2 must wait until a1 is settled - two workers must not reorder a door"
    agent.deliver_one(cfg, queue, first, post=Havenz())
    assert base64.b64decode(queue.take(timeout=0)["body"]) == b"a2"


def test_a_refused_door_does_not_hold_up_the_other_doors(cfg, path):
    clock = Clock()
    queue = agent.EventQueue(path, clock=clock)
    queue.put(DOOR_A, "dao", b"a1")                 # door A was removed in Havenz: 403 from now on
    queue.put(DOOR_A, "dao", b"a2")
    queue.put(DOOR_B, "dao", b"b1")

    assert agent.deliver_one(cfg, queue, queue.take(timeout=0), post=Havenz(("refused", "HTTP 403"))) == "refused"

    nxt = queue.take(timeout=0)
    assert base64.b64decode(nxt["body"]) == b"b1", "door B carries on; door A waits in order behind its refused event"
    agent.deliver_one(cfg, queue, nxt, post=Havenz())
    assert queue.take(timeout=0) is None
    assert queue.snapshot()["pending"] == 2

    # Retried slowly, and given up on - loudly - after a day.
    clock.advance(agent.EVENT_REFUSED_RETRY_SECONDS + 1)
    assert agent.deliver_one(cfg, queue, queue.take(timeout=0), post=Havenz(("refused", "HTTP 403"))) == "refused"
    clock.advance(agent.EVENT_REFUSED_GIVE_UP_SECONDS + 1)
    assert agent.deliver_one(cfg, queue, queue.take(timeout=0), post=Havenz(("refused", "HTTP 403"))) == "dropped"

    assert queue.snapshot()["dropped"] == 1
    assert journal(path)[-1]["op"] == "drop" and "403" in journal(path)[-1]["reason"]
    dead = [json.loads(line) for line in Path(path + ".dead").read_text().splitlines()]
    assert base64.b64decode(dead[0]["body"]) == b"a1", "a dropped event is kept where someone can find it"


# ---------------------------------------------------------------------------
# A small box, a power cut, a full disk
# ---------------------------------------------------------------------------

def test_a_line_torn_by_a_power_cut_costs_only_that_line(path):
    queue = agent.EventQueue(path)
    queue.put(DOOR_A, "dao", b"whole")
    with open(path, "ab") as f:
        f.write(b'{"v":1,"op":"put","id":"half-writ')   # the power went here

    recovered = agent.EventQueue(path)
    assert recovered.load() == 1
    assert recovered.snapshot()["corrupt_lines"] == 0, "a torn LAST line is expected, not corruption"

    recovered.put(DOOR_A, "dao", b"after")               # and the next append is not glued onto it
    assert agent.EventQueue(path).load() == 2


def test_an_unreadable_line_in_the_middle_is_skipped_and_counted(path):
    queue = agent.EventQueue(path)
    queue.put(DOOR_A, "dao", b"one")
    with open(path, "ab") as f:
        f.write(b"not json at all\n")
    queue.put(DOOR_A, "dao", b"two")

    recovered = agent.EventQueue(path)
    assert recovered.load() == 2
    assert recovered.snapshot()["corrupt_lines"] == 1


def test_the_journal_is_compacted_so_it_stays_the_size_of_what_is_waiting(cfg, path, monkeypatch):
    monkeypatch.setattr(agent, "EVENT_COMPACT_AFTER", 3)
    queue = agent.EventQueue(path)
    for n in range(4):
        queue.put(DOOR_A, "dao", f"scan-{n}".encode())
    for _ in range(3):
        agent.deliver_one(cfg, queue, queue.take(timeout=0), post=Havenz())

    lines = journal(path)
    assert [r["op"] for r in lines] == ["put"], "three delivered and compacted away, one still waiting"
    assert base64.b64decode(lines[0]["body"]) == b"scan-3"


def test_a_full_queue_drops_the_oldest_and_says_so(path, monkeypatch):
    monkeypatch.setattr(agent, "EVENT_MAX_PENDING", 3)
    queue = agent.EventQueue(path)
    for n in range(5):
        queue.put(DOOR_A, "dao", f"scan-{n}".encode())

    snapshot = queue.snapshot()
    assert snapshot["pending"] == 3 and snapshot["dropped"] == 2
    assert [r["op"] for r in journal(path)].count("drop") == 2, "never silently"
    assert base64.b64decode(queue.take(timeout=0)["body"]) == b"scan-2", "the newest are the ones kept"


def test_an_event_nobody_could_deliver_for_a_week_is_dropped_not_kept_for_ever(path):
    clock = Clock()
    queue = agent.EventQueue(path, clock=clock)
    queue.put(DOOR_A, "dao", b"ancient")
    clock.advance(agent.EVENT_MAX_AGE_SECONDS + 1)
    queue.put(DOOR_A, "dao", b"fresh")

    assert base64.b64decode(queue.take(timeout=0)["body"]) == b"fresh"
    assert queue.snapshot()["dropped"] == 1


def test_a_disk_that_cannot_be_written_still_delivers_the_event_from_memory(cfg, tmp_path):
    queue = agent.EventQueue(str(tmp_path / "no-such-dir" / "events.jsonl"))

    queue.put(DOOR_A, "dao", b"scan-1")             # no worse than before there was a queue

    assert queue.snapshot()["unpersisted"] == 1
    havenz = Havenz()
    agent.deliver_one(cfg, queue, queue.take(timeout=0), post=havenz)
    assert havenz.received[0]["body"] == b"scan-1"


def test_a_keepalive_is_never_queued(cfg, path, monkeypatch):
    """Delivered an hour late it would say 'this reader spoke just now' about one that may be dead."""
    relayed = threading.Event()
    monkeypatch.setattr(agent, "relay_unqueued", lambda *a: relayed.set())
    queue = agent.EventQueue(path)
    agent.STATE["readers"] = {}
    agent.STATE["roster_hosts"] = {"127.0.0.1": DOOR_A}
    server = agent.start_event_listener(cfg, 0, queue=queue)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/amico/notifications/device_is_alive",
            data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
        assert relayed.wait(5)
    finally:
        server.shutdown()

    assert queue.snapshot()["pending"] == 0
    assert not Path(path).exists() or journal(path) == []


# ---------------------------------------------------------------------------
# Reading a reader's log: the high-water mark
# ---------------------------------------------------------------------------

def rows(*ids):
    return [{"id": i, "userId": 0, "event": 7, "time": 1_789_000_000 + i} for i in ids]


def test_only_rows_after_the_mark_go_upstream():
    result = agent.apply_log_cursor(rows(5118, 5119, 5120, 5121), 5119)

    assert [e["id"] for e in result["entries"]] == [5120, 5121]
    assert result["readerMaxLogId"] == 5121 and result["logRestarted"] is False


def test_no_mark_means_the_whole_log_as_before():
    """An older backend sends no mark; nothing changes for it."""
    assert len(agent.apply_log_cursor(rows(1, 2, 3), None)["entries"]) == 3
    assert len(agent.apply_log_cursor(rows(1, 2, 3), 0)["entries"]) == 3
    assert len(agent.apply_log_cursor(rows(1, 2, 3), "not a number")["entries"]) == 3


def test_history_is_not_replayed_after_an_agent_restart():
    """
    The mark is the backend's and arrives with every command, so there is no agent-side polling
    state for a restart to lose: a fresh process given the same mark returns the same two rows,
    not the reader's whole history.
    """
    history = rows(*range(1, 501))

    before_restart = agent.apply_log_cursor(history, 498)
    after_restart = agent.apply_log_cursor(history, 498)     # nothing carried over, nothing needed

    assert [e["id"] for e in before_restart["entries"]] == [499, 500]
    assert after_restart == before_restart


def test_a_reader_whose_log_started_again_is_not_hidden_behind_the_old_mark():
    """
    Factory reset or replaced: ids begin again at 1. 'Nothing above 5120' would hide every event it
    records until its counter climbed past 5120 - so everything is returned, and flagged.
    """
    result = agent.apply_log_cursor(rows(1, 2, 3), 5120)

    assert [e["id"] for e in result["entries"]] == [1, 2, 3]
    assert result["logRestarted"] is True and result["readerMaxLogId"] == 3


def test_the_mark_travels_in_the_command_payload(monkeypatch):
    seen = {}

    class FakeReader:
        host = "192.168.0.41"

        def access_logs(self, after_log_id=None):
            seen["after"] = after_log_id
            return {"entries": []}

    monkeypatch.setattr(agent, "readers_for", lambda cfg, force=False: {DOOR_A: FakeReader()})

    agent.execute({}, {"type": "GetAccessLogs", "terminalId": DOOR_A, "payload": json.dumps({"afterLogId": 5070})})
    assert seen["after"] == 5070

    agent.execute({}, {"type": "GetAccessLogs", "terminalId": DOOR_A, "payload": None})
    assert seen["after"] is None


def test_reads_are_not_written_to_the_executed_store(tmp_path, monkeypatch):
    """
    The access log is read every thirty seconds per door. Remembering each read - result and all -
    filled the store with copies of reader logs and rewrote it in full after every command.
    """
    cfg = {"api_url": "http://localhost", "executed_store_path": str(tmp_path / "executed.json")}
    agent._executed.clear()
    monkeypatch.setattr(agent, "execute", lambda cfg, command: {"entries": rows(*range(1, 200))})
    monkeypatch.setattr(agent, "report", lambda *a: None)

    agent.handle(cfg, {"id": "cmd-read", "type": "GetAccessLogs", "terminalId": DOOR_A,
                       "notValidAfter": "2099-01-01T00:00:00Z"})
    assert agent._executed == {} and not Path(cfg["executed_store_path"]).exists()

    agent.handle(cfg, {"id": "cmd-open", "intentId": "tap-1", "type": "OpenDoor", "terminalId": DOOR_A,
                       "notValidAfter": "2099-01-01T00:00:00Z"})
    assert set(agent._executed) == {"cmd-open", "tap-1"}, "a door is still remembered, by command and by tap"
    agent._executed.clear()


# ---------------------------------------------------------------------------
# A reader that takes the unlock and never answers
# ---------------------------------------------------------------------------

class Reports:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, command_id, success, result, error, duration_ms, outcome=None):
        self.calls.append({"success": success, "error": error, "outcome": outcome})


def unlock(command_id="cmd-1", intent_id="tap-1"):
    return {"id": command_id, "intentId": intent_id, "type": "OpenDoor", "terminalId": DOOR_A,
            "payload": json.dumps({"door": 1}), "notValidAfter": "2099-01-01T00:00:00Z"}


def test_an_unlock_the_reader_never_answered_is_reported_as_unknown_not_failed(tmp_path, monkeypatch):
    """
    Seen on the bench: the reader opened the door and answered after the agent's timeout. "Failed"
    tells the person to tap again, and tapping again is how a door opens twice.
    """
    cfg = {"api_url": "http://localhost", "executed_store_path": str(tmp_path / "executed.json")}
    agent._executed.clear()
    agent._breakers.clear()
    reports = Reports()
    monkeypatch.setattr(agent, "report", reports)

    def silent_reader(cfg, command):
        raise agent.ReaderOutcomeUnknown("execute_actions.fcgi on 10.0.0.7: timed out")

    monkeypatch.setattr(agent, "execute", silent_reader)
    agent.handle(cfg, unlock())

    assert reports.calls[0]["success"] is False
    assert reports.calls[0]["outcome"] == "unknown"
    assert "may have opened" in reports.calls[0]["error"]
    assert agent._executed == {}, "not remembered as done - nobody knows that it was"


def test_an_unlock_that_never_reached_the_reader_is_a_plain_failure(tmp_path, monkeypatch):
    cfg = {"api_url": "http://localhost", "executed_store_path": str(tmp_path / "executed.json")}
    agent._executed.clear()
    agent._breakers.clear()
    reports = Reports()
    monkeypatch.setattr(agent, "report", reports)

    def unreachable(cfg, command):
        raise agent.ReaderError("cannot reach 10.0.0.7: connection refused")

    monkeypatch.setattr(agent, "execute", unreachable)
    agent.handle(cfg, unlock())

    assert reports.calls[0]["outcome"] is None and reports.calls[0]["success"] is False


def test_only_an_unlock_is_treated_this_way(tmp_path, monkeypatch):
    """A user sync that timed out is simply retried by the backend; it needs no special outcome."""
    cfg = {"api_url": "http://localhost", "executed_store_path": str(tmp_path / "executed.json")}
    agent._breakers.clear()
    reports = Reports()
    monkeypatch.setattr(agent, "report", reports)
    monkeypatch.setattr(agent, "execute",
                        lambda cfg, command: (_ for _ in ()).throw(agent.ReaderOutcomeUnknown("timed out")))

    agent.handle(cfg, {**unlock(), "type": "CreateUser", "intentId": None})

    assert reports.calls[0]["outcome"] is None


def test_which_transport_errors_may_have_reached_the_reader():
    import socket
    import urllib.error

    maybe = agent._may_have_reached_the_reader
    assert maybe(socket.timeout("timed out"))
    assert maybe(urllib.error.URLError(socket.timeout("timed out")))
    assert maybe(ConnectionResetError())
    assert maybe(agent.http.client.RemoteDisconnected("Remote end closed connection without response"))
    assert not maybe(urllib.error.URLError(ConnectionRefusedError(10061, "refused"))), "never reached the reader"
    assert not maybe(urllib.error.URLError(socket.gaierror("name not known")))


def test_a_slow_reader_makes_the_real_client_raise_unknown():
    """End to end through Reader.call against a reader that accepts the unlock and says nothing."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    release = threading.Event()
    opened = []

    class SlowReader(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            if self.path.startswith("/hidlogin"):
                body = b'{"session":"s"}'
            else:
                opened.append(self.path)       # the door opens...
                release.wait(10)               # ...and the reader is slow to say so
                body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass                           # the agent stopped listening long ago

    server = HTTPServer(("127.0.0.1", 0), SlowReader)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reader = agent.Reader(DOOR_A, "slow door", f"127.0.0.1:{server.server_address[1]}", "admin", "admin", timeout=1)
    try:
        with pytest.raises(agent.ReaderOutcomeUnknown):
            reader.open_door(1)
        assert len(opened) == 1, "the unlock reached the reader exactly once - no silent second try"
    finally:
        release.set()
        server.shutdown()
