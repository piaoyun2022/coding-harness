import importlib.util
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from coding_harness.benchmark import Task
from coding_harness.config import Settings
from coding_harness.contracts import CommandResult, Reply, Usage
from coding_harness.environment import DockerEnvironment
from coding_harness.workspace import git

SPEC = importlib.util.spec_from_file_location(
    "agent_batch", Path(__file__).parents[1] / "scripts/run_agent_batch.py"
)
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)
KEY = "test-private-key"
BASE = "a" * 40
HEAD = "b" * 40
PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-bad\n+good\n"


def task(name="fixture-one", problem="Fix bug"):
    return Task(name, "fixture/project", BASE, problem, "1")


class FakeEnvironment:
    description = "Fake isolated test fixture"
    instances = []

    def __init__(self, task, output, settings):
        self.patch, self.closed = "", False
        self.source_metadata = {"requested_base_commit": task.base_commit}
        self.cleanup_errors = []
        self.instances.append(self)
        if task.instance_id.endswith("setup-fail"):
            raise RuntimeError(KEY)

    def execute(self, command, timeout):
        return CommandResult(stdout="Project rules")

    def apply_patch(self, patch, timeout):
        self.patch = PATCH
        return {"ok": True, "files": ["x.py"]}

    def collect_patch(self):
        return self.patch

    def close(self):
        self.closed = True


class FakeModel:
    instances = []

    def __init__(self, settings, api_key):
        assert api_key == KEY
        self.calls, self.closed = 0, False
        self.instances.append(self)

    def complete(self, messages, tools, **kwargs):
        self.calls += 1
        if any("MODEL_ERROR" in str(message) for message in messages):
            raise RuntimeError(KEY)
        name = "apply_patch" if self.calls == 1 else "finish"
        args = {"patch": "*** Begin Patch\n*** Add File: x.py\n+good\n*** End Patch"}
        if name == "finish":
            args = {"summary": "Done"}
        return Reply(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": str(self.calls),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            Usage(100, 20),
            "tool_calls",
        )

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeEnvironment.instances, FakeModel.instances = [], []


@pytest.fixture
def frozen(monkeypatch):
    manifest = {
        "source_commit": BASE,
        "source_sha256": {"agent.py": "fixed"},
        "wrapper_commit": HEAD,
        "wrapper_sha256": "wrapper",
    }
    monkeypatch.setattr(batch, "source_manifest", lambda commit: manifest)
    return manifest


def test_batch_real_agent_keeps_order_failures_patches_and_no_secrets(tmp_path, frozen, capsys):
    tasks = [task(), task("fixture-setup-fail"), task("fixture-model-fail", "MODEL_ERROR")]
    output = tmp_path / "run"
    report = batch.run_batch(
        tasks,
        output,
        Settings(),
        api_key=KEY,
        source_commit=BASE,
        model_factory=FakeModel,
        environment_factory=FakeEnvironment,
        input_tasks_sha256="original-input-hash",
    )
    assert report["planned"] == report["completed"] == 3
    assert report["statuses"] == {"generated": 1, "setup_error": 1, "agent_error": 1}
    assert report["usage"]["total_tokens"] == 240
    assert report["graded"] is False and report["resolved_rate"] is None
    predictions = [
        json.loads(line) for line in (output / "predictions.jsonl").read_text().splitlines()
    ]
    assert [entry["instance_id"] for entry in predictions] == [entry.instance_id for entry in tasks]
    assert [entry["model_patch"] for entry in predictions] == [PATCH, "", ""]
    assert all(model.closed for model in FakeModel.instances)
    assert len(FakeModel.instances) == 2
    assert FakeEnvironment.instances[0].closed and FakeEnvironment.instances[2].closed
    case = output / "runs/fixture-one"
    result = json.loads((case / "result.json").read_text())
    assert result["mode"] == "agent" and result["outcome"] == "completed"
    assert result["patch_sha256"] == batch.digest((case / "changes.patch").read_bytes())
    assert (case / "trajectory.jsonl").exists()
    assert (case / "evidence").is_dir()
    plan = json.loads((output / "plan.json").read_text())
    assert plan["source"] == frozen and plan["workers"] == 1 and plan["test_feedback"]
    assert plan["input_tasks_sha256"] == "original-input-hash"
    assert KEY not in capsys.readouterr().out
    for path in output.rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(encoding="utf-8")


def test_budget_stop_nonempty_patch_is_still_graded_candidate(tmp_path):
    result = batch.run_one(
        task(),
        tmp_path / "case",
        Settings(max_steps=1),
        KEY,
        model_factory=FakeModel,
        environment_factory=FakeEnvironment,
    )
    assert result["status"] == "generated"
    assert result["outcome"] == "budget_exhausted" and result["stop_reason"] == "step_budget"
    assert FakeModel.instances[0].calls == 1
    assert FakeModel.instances[0].closed and FakeEnvironment.instances[0].closed


def test_cleanup_failure_stops_next_task_but_preserves_all_prediction_ids(tmp_path, frozen):
    class CleanupFailure(FakeEnvironment):
        def close(self):
            super().close()
            self.cleanup_errors.append({"stage": "image_cleanup", "error_type": "DockerError"})

    tasks = [task(), task("fixture-next")]
    output = tmp_path / "run"
    result = batch.run_batch(
        tasks,
        output,
        Settings(),
        api_key=KEY,
        source_commit=BASE,
        model_factory=FakeModel,
        environment_factory=CleanupFailure,
    )
    assert result["completed"] == 1 and result["stop_reason"] == "cancelled_or_cleanup_failed"
    assert len(FakeModel.instances) == 1
    assert len((output / "predictions.jsonl").read_text().splitlines()) == 2


def test_source_change_stops_without_model_and_records_reason(tmp_path, monkeypatch):
    calls = 0

    def changing(commit):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError(KEY)
        return {"source_sha256": {"agent": "old"}}

    monkeypatch.setattr(batch, "source_manifest", changing)
    report = batch.run_batch(
        [task()],
        tmp_path / "run",
        Settings(),
        api_key=KEY,
        source_commit=BASE,
        model_factory=FakeModel,
    )
    assert report["completed"] == 0 and report["stop_reason"] == "batch_error:ValueError"
    assert not FakeModel.instances


def test_existing_output_and_duplicate_tasks_rejected(tmp_path, frozen):
    with pytest.raises(FileExistsError):
        batch.run_batch([task()], tmp_path, Settings(), api_key=KEY, source_commit=BASE)
    with pytest.raises(ValueError):
        batch.run_batch(
            [task(), task()], tmp_path / "run", Settings(), api_key=KEY, source_commit=BASE
        )


def test_source_manifest_pins_core_but_records_separate_wrapper_commit(tmp_path):
    source = tmp_path / "repo"
    (source / "src/coding_harness").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "src/coding_harness/agent.py").write_text("x = 1\n")
    (source / "pyproject.toml").write_text("[project]\n")
    (source / "scripts/run_agent_batch.py").write_text("first\n")
    git(source, "init", "--quiet")

    def commit():
        git(source, "add", ".")
        git(
            source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        )
        return git(source, "rev-parse", "HEAD").stdout.decode().strip()

    core = commit()
    (source / "scripts/run_agent_batch.py").write_text("wrapper changed\n")
    wrapper = commit()
    manifest = batch.source_manifest(core, root=source)
    assert manifest["source_commit"] == core and manifest["wrapper_commit"] == wrapper
    (source / "src/coding_harness/agent.py").write_text("x = 2\n")
    with pytest.raises(ValueError, match="differs"):
        batch.source_manifest(core, root=source)


def test_main_uses_exact_dotenv_no_environment_mutation(tmp_path, monkeypatch):
    tasks, settings, secrets = [
        tmp_path / name for name in ("tasks.jsonl", "settings.json", "key.env")
    ]
    tasks.write_text(json.dumps(asdict(task())) + "\n")
    settings.write_text("{}")
    secrets.write_text("DEEPSEEK_API_KEY=" + KEY + "\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-value")
    captured = {}

    def run(*args, **kwargs):
        captured.update(kwargs)
        return {"completed": 1, "planned": 1, "stop_reason": None}

    monkeypatch.setattr(batch, "run_batch", run)
    assert (
        batch.main(
            [
                "--tasks",
                str(tasks),
                "--settings",
                str(settings),
                "--env-file",
                str(secrets),
                "--output",
                str(tmp_path / "out"),
                "--source-commit",
                BASE,
            ]
        )
        == 0
    )
    assert captured["api_key"] == KEY
    assert captured["input_tasks_sha256"] == batch.digest(tasks.read_bytes())
    assert os.environ["DEEPSEEK_API_KEY"] == "ambient-value"


@pytest.mark.parametrize(
    "head,parents,subject,dirty,valid",
    [
        (BASE, "", "", "", True),
        (HEAD, BASE, "SWE-bench", "", True),
        (HEAD, "c" * 40, "SWE-bench", "", False),
        (HEAD, BASE + " " + "c" * 40, "SWE-bench", "", False),
        (HEAD, BASE, "other", "", False),
        (HEAD, BASE, "SWE-bench", " M file.py", False),
    ],
)
def test_official_baseline_preserves_only_verified_setup_commit(
    head, parents, subject, dirty, valid
):
    env = object.__new__(batch.BenchmarkDocker)
    env.base_commit, env.source_metadata = BASE, {}
    calls = []

    def execute(command, timeout):
        calls.append(command)
        values = {
            "git rev-parse HEAD": head,
            "git show -s --format=%P HEAD": parents,
            "git show -s --format=%s HEAD": subject,
            "git status --porcelain --untracked-files=normal": dirty,
        }
        return CommandResult(stdout=values.get(command, "M\tpyproject.toml"))

    env.execute = execute
    if valid:
        env._verify_base()
        assert env.base_commit == head and env.source_metadata["patch_base_commit"] == head
    else:
        with pytest.raises(ValueError):
            env._verify_base()
    assert not any(word in " ".join(calls) for word in ("checkout", "reset", "clean -"))


def test_cleanup_removes_only_owned_unchanged_images_overlay_first(monkeypatch):
    env = object.__new__(batch.BenchmarkDocker)
    env._images_closed, env.cleanup_errors = False, []
    env._owned_images = {
        "swebench/base:latest": "new-base",
        "coding-harness-tools:new": "new-overlay",
        "coding-harness-tools:changed": "old-id",
    }
    images = {
        "swebench/base:latest": "new-base",
        "coding-harness-tools:new": "new-overlay",
        "coding-harness-tools:changed": "different-id",
    }
    removed = []
    monkeypatch.setattr(DockerEnvironment, "close", lambda self: None)
    env._inspect_image = lambda tag: {"id": images[tag]}
    env._docker = lambda args, **kwargs: (
        removed.append(args) or type("Result", (), {"returncode": 0})()
    )
    env.close()
    env.close()
    assert removed == [
        ["image", "rm", "coding-harness-tools:new"],
        ["image", "rm", "swebench/base:latest"],
    ]


@pytest.mark.parametrize(
    "reason",
    [
        "incomplete_response",
        "invalid_model_reply",
        "invalid_tool_calls",
    ],
)
def test_invalid_model_response_is_not_unknown_infrastructure_failure(
    tmp_path, monkeypatch, reason
):
    monkeypatch.setattr(
        batch.Agent,
        "run",
        lambda *args: {
            "outcome": "failed",
            "stop_reason": reason,
            "usage": {},
        },
    )
    result = batch.run_one(
        task(),
        tmp_path / "case",
        Settings(),
        KEY,
        model_factory=FakeModel,
        environment_factory=FakeEnvironment,
    )
    assert result["status"] == "invalid_response" and result["stop_reason"] == reason
