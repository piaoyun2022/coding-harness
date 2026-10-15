import importlib.util
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_harness.benchmark import Task
from coding_harness.config import Settings
from coding_harness.contracts import CommandResult, Reply, Usage
from coding_harness.workspace import git

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("public_evaluate", SCRIPTS / "evaluate.py")
ev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ev)
KEY = "fake-secret-never-serialize"
BASE = "a" * 40
PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-bad\n+good\n"


def task(name="fixture-001", base=BASE):
    return Task(name, "fixture/project", base, "Fix x.py", "1")


def test_default_profile_and_cli():
    settings = ev.default_settings()
    assert (settings.max_steps, settings.max_total_tokens, settings.max_seconds) == (
        300, 5_000_000, 900)
    assert settings.reasoning_effort == "high"
    assert settings.max_output_tokens == 32768 and settings.request_timeout == 180
    args = ev.parser().parse_args([])
    assert (args.mode, args.limit, args.workers) == ("both", 30, 2)
    assert ev.parser().parse_args(["--limit", "300"]).limit == 300


def test_explicit_key_no_environment_mutation_or_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "wrong-parent-key")
    path = tmp_path / "chosen.env"
    path.write_text("DEEPSEEK_API_KEY=${SHOULD_NOT_INTERPOLATE}\n")
    assert ev.read_key(path) == "${SHOULD_NOT_INTERPOLATE}"
    assert os.environ["DEEPSEEK_API_KEY"] == "wrong-parent-key"
    path.write_text("DEEPSEEK_API_KEY=\n")
    with pytest.raises(ev.ConfigurationError, match="Set DEEPSEEK_API_KEY"):
        ev.read_key(path)
    with pytest.raises(ev.ConfigurationError, match="explicitly selected"):
        ev.read_key(tmp_path / "missing.env")


def test_prepare_pinned_safe_first30_and_separate_official_rows(tmp_path, monkeypatch):
    rows = [asdict(task(f"fixture-{i:03}")) | {"patch": "GOLD", "test_patch": "HIDDEN"}
            for i in reversed(range(300))]
    provenance = {"revision": ev.benchmark.REVISION, "source_sha256": "fixed-dataset-sha"}
    monkeypatch.setattr(ev.benchmark, "_parquet", lambda *a, **k: (rows, provenance))
    tasks, grader, manifest = ev.prepare_data(tmp_path, 30)
    assert [t.instance_id for t in tasks] == [f"fixture-{i:03}" for i in range(30)]
    safe = (tmp_path / "tasks/tasks.jsonl").read_text()
    assert "GOLD" not in safe and "HIDDEN" not in safe
    official = json.loads(grader.read_text())
    assert len(official) == 30 and official[0]["test_patch"] == "HIDDEN"
    assert manifest["planned"] == 30
    assert not (tmp_path / "grader-only/download.json").exists()


class FakeEnvironment:
    description = "Offline trusted fixture"
    instances = []
    lock = threading.Lock()
    constructing = 0
    peak = 0
    fail = False

    def __init__(self, task, output, settings):
        with self.lock:
            type(self).constructing += 1
            type(self).peak = max(type(self).peak, type(self).constructing)
        time.sleep(0.005)
        with self.lock:
            type(self).constructing -= 1
        if self.fail:
            raise RuntimeError(KEY)
        self.patch, self.closed = "", False
        self.source_metadata = {"requested_base_commit": task.base_commit}
        self.cleanup_errors = []
        self.instances.append(self)

    def execute(self, command, timeout):
        return CommandResult(stdout="")

    def apply_patch(self, patch, timeout):
        self.patch = PATCH
        return {"ok": True, "files": ["x.py"]}

    def collect_patch(self):
        return self.patch

    def close(self):
        self.closed = True


class FakeModel:
    def __init__(self, settings, key):
        assert key == KEY
        self.calls = 0

    def complete(self, messages, tools, **kwargs):
        self.calls += 1
        if not tools:
            return Reply({"role": "assistant", "content": PATCH}, Usage(100, 20), "stop")
        name = "apply_patch" if self.calls == 1 else "finish"
        args = {"patch": "*** Begin Patch\n*** Add File: x.py\n+good\n*** End Patch"}
        if name == "finish":
            args = {"summary": "Done"}
        return Reply({"role": "assistant", "content": None, "tool_calls": [{
            "id": str(self.calls), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]},
            Usage(100, 20), "tool_calls")

    def close(self):
        pass


class FakeLease:
    def __init__(self, task):
        self.task = task

    def acquire(self):
        return {}

    def verify(self):
        return "sha256:fixture"

    def close(self):
        return {"cleanup": "preserved_preexisting_or_unconfirmed"}


class FakeGrader:
    graded = []

    def __init__(self, output, *args, **kwargs):
        self.output = output
        self.client = SimpleNamespace(close=lambda: None)

    def grade(self, row, prediction):
        case = self.output / "instances" / row["instance_id"]
        case.mkdir(parents=True)
        (case / "patch.diff").write_bytes(prediction["model_patch"].encode())
        ev.save(case / "report.json", {row["instance_id"]: {
            "resolved": True, "tests_status": {
                "FAIL_TO_PASS": {"success": ["one"], "failure": []},
                "PASS_TO_PASS": {"success": ["two"], "failure": []}}}})
        self.graded.append(row["instance_id"])
        # Exercise the production three-way binding path, not a fake score boolean.
        return row | ev.official.classify_official(
            case, row["instance_id"], row["patch_sha256"]) | {
                "evaluated_image_id": "sha256:fixture",
                "image_before": {"image_id": "sha256:fixture"}}


@pytest.fixture
def offline(monkeypatch):
    FakeEnvironment.instances = []
    FakeEnvironment.peak = FakeEnvironment.constructing = 0
    FakeEnvironment.fail = False
    FakeGrader.graded = []
    monkeypatch.setattr(ev, "frozen_source", lambda *a: {
        "source_commit": BASE, "source_sha256": {"agent.py": "fixed"}})
    monkeypatch.setattr(ev, "check_resources", lambda *a, **k: {})
    monkeypatch.setattr(ev, "SourceImage", FakeLease)
    monkeypatch.setattr(ev, "private_environment", lambda *a: FakeEnvironment)
    monkeypatch.setattr(ev, "DeepSeekModel", FakeModel)
    monkeypatch.setattr(ev.official, "Grader", FakeGrader)
    original = ev.agents.run_one
    monkeypatch.setattr(ev.agents, "run_one",
                        lambda *a, **k: original(*a, **k, model_factory=FakeModel))


def pipeline(tmp_path, tasks, workers=2):
    root = tmp_path / "output"
    root.mkdir()
    grader = tmp_path / "official-data.json"
    ev.save(grader, [asdict(t) | {"patch": "GOLD", "test_patch": "HIDDEN"} for t in tasks])
    return ev.Pipeline(tasks, root, Settings(max_steps=3, max_output_tokens=128),
                       KEY, ev.frozen_source(), grader, workers=workers)


def test_agent_pipeline_exactly_once_pairing_grading_and_setup_lock(tmp_path, offline):
    tasks = [task(f"fixture-{i}") for i in range(6)]
    run = pipeline(tmp_path, tasks, workers=3)
    result = run.run_mode("agent")
    assert result["official_resolved_rate"] == 1
    assert result["planned"] == result["completed"] == 6
    assert len(FakeEnvironment.instances) == 6 and all(e.closed for e in FakeEnvironment.instances)
    assert FakeEnvironment.peak == 1
    assert sorted(FakeGrader.graded) == sorted(t.instance_id for t in tasks)
    assert result["generation_usage"]["total_tokens"] == 6 * 240
    assert all(row["patch_binding_verified"] for row in result["rows"])
    for file in run.output.rglob("*"):
        if file.is_file():
            assert KEY not in file.read_text()
    assert (run.output / "agent/predictions.jsonl").read_text().count("\n") == 6


def test_baseline_pipeline_real_read_only_bare_git_and_bound_grade(tmp_path, offline):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--quiet")
    (repo / "x.py").write_text("bad\nraise RuntimeError('must never run')\n")
    git(repo, "add", "x.py")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "--quiet", "-m", "fixture")
    base = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    tasks = [task("fixture-first", base), task("fixture-second", base)]
    run = pipeline(tmp_path, tasks)
    run.cache = ev.baselines.RepositoryCache(run.output / "repos",
                                            origin_for_repo=lambda _: str(repo))
    result = run.run_mode("baseline")
    assert result["resolved"] == 2 and result["generation_usage"]["total_tokens"] == 240
    assert (repo / "x.py").read_text().startswith("bad")
    assert all(row["generation_mode"] == "baseline" for row in result["rows"])


def test_setup_error_stops_unscheduled_tasks_without_changing_denominator(tmp_path, offline):
    FakeEnvironment.fail = True
    run = pipeline(tmp_path, [task(f"fixture-{i}") for i in range(4)], workers=1)
    result = run.run_mode("agent")
    assert result["planned"] == 4 and result["completed"] == 1 and result["pending"] == 3
    assert result["unresolved_unknown"] == 1
    assert result["official_resolved_rate"] is None
    assert len(list((run.output / "agent/runs").iterdir())) == 1


def test_main_reports_missing_mode_and_cleanup_stop_nonzero(tmp_path, monkeypatch, offline):
    monkeypatch.setattr(ev, "read_key", lambda _: KEY)
    monkeypatch.setattr(ev, "preflight", lambda *a, **k: {})
    monkeypatch.setattr(ev, "prepare_data", lambda *a: ([task()], tmp_path / "grader", {}))

    class StoppedPipeline:
        def __init__(self, *args, **kwargs):
            self.stop = threading.Event()

        def run_mode(self, mode):
            assert mode == "baseline"
            self.stop.set()  # Cleanup or a SIGTERM after the last baseline row.
            return ev.official.summarize(["fixture-001"], {
                "fixture-001": {"status": "graded", "resolved": True}})

    monkeypatch.setattr(ev, "Pipeline", StoppedPipeline)
    output = tmp_path / "result"
    assert ev.main(["--output", str(output)]) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["not_run_modes"] == ["agent"]
    assert report["completed_modes"] == ["baseline"] and not report["overall_complete"]


def test_preflight_never_prepares_data_or_constructs_pipeline(tmp_path, monkeypatch, offline):
    monkeypatch.setattr(ev, "read_key", lambda _: KEY)
    monkeypatch.setattr(ev, "preflight", lambda *a, **k: {})
    monkeypatch.setattr(ev, "prepare_data", lambda *a: pytest.fail("must not download"))
    assert ev.main(["--output", str(tmp_path / "unused"), "--preflight"]) == 0
    assert not (tmp_path / "unused").exists()


def test_missing_key_actionable_and_existing_output_rejected(tmp_path, capsys):
    assert ev.main(["--env-file", str(tmp_path / "absent"),
                    "--output", str(tmp_path / "fresh")]) == 1
    assert "explicitly selected" in capsys.readouterr().out
    assert ev.main(["--output", str(tmp_path)]) == 1
    assert "never overwritten" in capsys.readouterr().out


def test_external_exception_not_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ev, "read_key", lambda _: KEY)
    monkeypatch.setattr(ev, "frozen_source",
                        lambda *a: (_ for _ in ()).throw(RuntimeError(KEY)))
    assert ev.main(["--output", str(tmp_path / "new")]) == 1
    text = capsys.readouterr().out
    assert KEY not in text and "RuntimeError" in text


def test_mismatched_official_image_is_unknown_not_solved(tmp_path, offline, monkeypatch):
    original = FakeGrader.grade

    def changed(self, row, prediction):
        return original(self, row, prediction) | {"evaluated_image_id": "sha256:changed"}

    monkeypatch.setattr(FakeGrader, "grade", changed)
    run = pipeline(tmp_path, [task()], workers=1)
    report = run.run_mode("agent")
    assert report["resolved"] == 0 and report["unresolved_unknown"] == 1
    assert report["rows"][0]["status"] == "infra_image_identity"
    assert report["official_resolved_rate"] is None


def test_foreign_editable_import_rejected(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "coding_harness.foreign",
                        SimpleNamespace(__file__=str(tmp_path / "old_checkout.py")))
    with pytest.raises(ev.ConfigurationError, match="different checkout"):
        ev.frozen_source(BASE)


def test_provider_transport_retry_disclosure():
    text = ev.report_markdown({})
    assert "SDK transport retries may occur" in text
    assert "No model retries" not in text


@pytest.mark.parametrize("field,value", [
    ("container_cleanup", "error"), ("container_cleanup", "ownership_not_verified"),
    ("image_cleanup", "error"),
])
def test_official_cleanup_failure_stops_with_preexisting_source(
    tmp_path, offline, monkeypatch, field, value,
):
    original = FakeGrader.grade
    monkeypatch.setattr(FakeGrader, "grade",
                        lambda self, row, prediction: original(self, row, prediction) | {
                            field: value})
    run = pipeline(tmp_path, [task()], workers=1)
    result = run.run_mode("agent")
    assert result["resolved"] == 1  # Bound score remains valid; resource completion does not.
    assert run.stop.is_set()
    assert result["rows"][0]["official_cleanup_issues"] == {field: value}
