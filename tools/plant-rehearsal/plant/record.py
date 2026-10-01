"""
What a run leaves behind: the timeline the screen half follows live, and the per-assertion results
the report is built from.

timeline.jsonl   one JSON object per line, appended as things happen (contract: plant-rehearsal)
results.json     every scenario's assertions, metrics and findings; rewritten after each scenario
"""

import json
import os
import threading
from pathlib import Path

from . import config
from .util import iso

_LOCK = threading.Lock()


def current_run_dir(explicit=None):
    if explicit:
        return Path(explicit)
    pointer = config.output_root() / "current.json"
    try:
        return Path(json.loads(pointer.read_text(encoding="utf-8"))["runDir"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        raise SystemExit("no current run folder - run `python rehearsal.py seed` first") from None


def set_current_run_dir(run_dir):
    config.output_root().mkdir(parents=True, exist_ok=True)
    (config.output_root() / "current.json").write_text(
        json.dumps({"runDir": str(run_dir), "setAt": iso()}, indent=2), encoding="utf-8")


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class Timeline:
    def __init__(self, run_dir):
        self.path = Path(run_dir) / "timeline.jsonl"

    def write(self, scenario, step, detail, expect=None, **extra):
        line = {"t": iso(), "scenario": scenario, "step": step,
                "expect": expect or {"walls": {}, "doors": {}}, "detail": detail}
        line.update(extra)
        with _LOCK, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"  [{line['t'][11:19]}] {scenario} / {step}: {detail}", flush=True)
        return line


class ScenarioRecord:
    """One scenario's run: steps go to the timeline, assertions and timings to the results."""

    def __init__(self, name, title, simulated, run_dir, timeline):
        self.name, self.title, self.simulated = name, title, simulated
        self.run_dir = Path(run_dir)
        self.timeline = timeline
        self.assertions = []
        self.metrics = {}
        self.notes = []
        self.finding_ids = []
        self.started_at = None
        self.ended_at = None
        self.error = None

    # -- timeline -------------------------------------------------------------

    def start(self):
        self.started_at = iso()
        self.timeline.write(self.name, "start", self.title)

    def step(self, step, detail, expect=None, within=None):
        extra = {"withinSeconds": within} if within else {}
        return self.timeline.write(self.name, step, detail, expect, **extra)

    def end(self):
        self.ended_at = iso()
        self.timeline.write(self.name, "end", f"{self.passed_count()} of {len(self.assertions)} assertions passed",
                            result=self.result(),
                            assertions=[{"name": a["name"], "passed": a["passed"]} for a in self.assertions])

    # -- results --------------------------------------------------------------

    def check(self, name, passed, detail=None, evidence=None, finding=None):
        """
        One hard assertion. `detail` says what was seen, in numbers, whether it passed or not.
        `finding` names the finding this failure is evidence for, when it is a known product gap.
        """
        passed = bool(passed)
        row = {"name": name, "passed": passed, "detail": detail, "at": iso()}
        if evidence is not None:
            row["evidence"] = evidence
        if finding and not passed:
            row["finding"] = finding
            if finding not in self.finding_ids:
                self.finding_ids.append(finding)
        self.assertions.append(row)
        print(f"      {'PASS' if passed else 'FAIL'}  {name} - {detail}", flush=True)
        return passed

    def metric(self, name, value, unit, note=None):
        self.metrics[name] = {"value": value, "unit": unit, **({"note": note} if note else {})}
        shown = "n/a" if value is None else (f"{value:.3f}" if isinstance(value, float) else value)
        print(f"      ....  {name}: {shown} {unit}" + (f" ({note})" if note else ""), flush=True)

    def note(self, text):
        self.notes.append(text)
        print(f"      note  {text}", flush=True)

    def observe_finding(self, finding_id):
        if finding_id not in self.finding_ids:
            self.finding_ids.append(finding_id)

    def passed_count(self):
        return sum(1 for a in self.assertions if a["passed"])

    def result(self):
        if self.error:
            return "fail"
        return "pass" if self.assertions and all(a["passed"] for a in self.assertions) else "fail"

    def to_json(self):
        return {"name": self.name, "title": self.title, "simulated": self.simulated,
                "startedAt": self.started_at, "endedAt": self.ended_at, "result": self.result(),
                "assertions": self.assertions, "metrics": self.metrics, "notes": self.notes,
                "findings": self.finding_ids, "error": self.error}


def save_scenario(run_dir, record):
    path = Path(run_dir) / "results.json"
    with _LOCK:
        results = load_json(path, {"scenarios": {}, "history": []})
        previous = results["scenarios"].get(record.name)
        if previous:
            results["history"].append({"name": record.name, "startedAt": previous.get("startedAt"),
                                       "result": previous.get("result")})
        results["scenarios"][record.name] = record.to_json()
        results["updatedAt"] = iso()
        save_json(path, results)
