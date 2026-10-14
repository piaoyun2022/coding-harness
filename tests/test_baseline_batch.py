import importlib.util
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from coding_harness.benchmark import Task
from coding_harness.config import Settings
from coding_harness.contracts import Reply, Usage
from coding_harness.workspace import git

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_baseline_batch.py"
SPEC = importlib.util.spec_from_file_location("baseline_batch", SCRIPT)
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)
PATCH = (
    "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)
KEY = "fake-key-must-stay-private"


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "upstream"
    path.mkdir()
    git(path, "init", "--quiet")
    (path / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n\n"
        "raise RuntimeError('repository code must never execute')\n"
    )
    git(path, "add", "calc.py")
    git(path, "-c", "user.name=Test", "-c", "user.email=test@localhost",
        "commit", "--quiet", "-m", "fixture")
    base = git(path, "rev-parse", "HEAD").stdout.decode().strip()
    return path, base


def task(instance_id, base, problem="Fix calc add subtraction"):
    return Task(instance_id, "fixture/project", base, problem, "1")


class FakeModel:
    closed = []
    requests = []
    lock = threading.Lock()

    def __init__(self, settings, api_key):
        assert api_key == KEY

    def complete(self, messages, tools, **kwargs):
        assert tools == [] and kwargs["purpose"] == "baseline"
        with self.lock:
            self.requests.append(messages)
        if "MODEL_ERROR" in messages[-1]["content"]:
            raise RuntimeError(KEY)
        return Reply({"role": "assistant", "content": PATCH}, Usage(100, 20), "stop")

    def close(self):
        self.closed.append(True)


def test_real_bare_cache_batch_keeps_failures_and_never_executes_code(
    tmp_path, repository, monkeypatch, capsys,
):
    source, base = repository
    tasks = [task("fixture-pass", base), task("fixture-fail", base, "MODEL_ERROR calc")]
    calls = []
    original_git = batch.RepositoryCache._git

    def record(path, *args, **kwargs):
        if args and args[0] == "fetch":
            calls.append(args)
        return original_git(path, *args, **kwargs)

    monkeypatch.setattr(batch.RepositoryCache, "_git", staticmethod(record))
    FakeModel.closed, FakeModel.requests = [], []
    output = tmp_path / "run"
    report = batch.run_batch(
        tasks, output, Settings(), api_key=KEY, workers=2, model_factory=FakeModel,
        origin_for_repo=lambda repo: str(source),
    )
    assert report["planned"] == report["completed"] == 2
    assert report["statuses"] == {"generated": 1, "model_error": 1}
    assert report["graded"] is False and report["resolved_rate"] is None
    assert report["usage"]["total_tokens"] == 120
    assert len(calls) == 1
    assert "--depth=1" in calls[0]
    assert len(FakeModel.closed) == 2
    predictions = [
        json.loads(line) for line in (output / "predictions.jsonl").read_text().splitlines()
    ]
    assert [entry["instance_id"] for entry in predictions] == [entry.instance_id for entry in tasks]
    assert predictions[0]["model_patch"] == PATCH and predictions[1]["model_patch"] == ""
    failure = json.loads((output / "runs/fixture-fail/result.json").read_text())
    assert failure["error_type"] == "RuntimeError"
    assert KEY not in capsys.readouterr().out
    for path in output.rglob("*.json"):
        assert KEY not in path.read_text()
    cache = next((output / "repositories").glob("*.git"))
    assert git(cache, "rev-parse", "--is-bare-repository").stdout.strip() == b"true"
    assert not (cache / "calc.py").exists()
    assert "return a - b" in (source / "calc.py").read_text()
    plan = json.loads((output / "plan.json").read_text())
    assert plan["context_chars"] == 64_000 and plan["tools"] == []
    assert plan["settings"] == Settings().model_dump()


def test_unfetchable_commit_has_empty_prediction_without_model_call(tmp_path, repository):
    source, _ = repository

    def forbidden_model(*args):
        pytest.fail("A missing source commit must fail before model construction")

    output = tmp_path / "run"
    report = batch.run_batch(
        [task("fixture-missing", "f" * 40)], output, Settings(), api_key=KEY,
        model_factory=forbidden_model, origin_for_repo=lambda repo: str(source),
    )
    assert report["statuses"] == {"setup_error": 1}
    failure = json.loads((output / "runs/fixture-missing/result.json").read_text())
    assert failure["error_type"] == "CalledProcessError"
    assert json.loads((output / "predictions.jsonl").read_text())["model_patch"] == ""


def test_cache_preserves_multiple_immutable_base_commits(tmp_path, repository):
    source, first = repository
    (source / "calc.py").write_text("value = 2\n", newline="\n")
    git(source, "add", "calc.py")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@localhost",
        "commit", "--quiet", "-m", "second")
    second = git(source, "rev-parse", "HEAD").stdout.decode().strip()
    cache = batch.RepositoryCache(tmp_path / "cache", origin_for_repo=lambda repo: str(source))
    first_path = cache.ensure(task("fixture-first", first))
    second_path = cache.ensure(task("fixture-second", second))
    assert first_path == second_path
    assert b"return a - b" in git(first_path, "show", first + ":calc.py").stdout
    assert git(first_path, "show", second + ":calc.py").stdout == b"value = 2\n"


def test_main_uses_only_exact_dotenv_without_environment_mutation(
    tmp_path, repository, monkeypatch,
):
    _, base = repository
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(json.dumps(asdict(task("fixture-pass", base))) + "\n")
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    secrets = tmp_path / "private.env"
    secrets.write_text("DEEPSEEK_API_KEY=" + KEY + "\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "different-environment-value")
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return {"completed": 1}

    monkeypatch.setattr(batch, "run_batch", fake_run)
    result = batch.main([
        "--tasks", str(tasks), "--settings", str(settings), "--env-file", str(secrets),
        "--output", str(tmp_path / "output"),
    ])
    assert result == 0 and captured["api_key"] == KEY and captured["workers"] == 2
    assert os.environ["DEEPSEEK_API_KEY"] == "different-environment-value"


def test_main_error_output_contains_only_type(tmp_path, capsys):
    status = batch.main([
        "--tasks", str(tmp_path / "tasks"), "--settings", str(tmp_path / "settings"),
        "--env-file", str(tmp_path / "missing"), "--output", str(tmp_path / "output"),
    ])
    assert status == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "batch_error", "error_type": "FileNotFoundError",
    }


def test_existing_output_and_invalid_worker_count_rejected(tmp_path, repository):
    _, base = repository
    with pytest.raises(FileExistsError):
        batch.run_batch([task("fixture-pass", base)], tmp_path, Settings(), api_key=KEY)
    with pytest.raises(ValueError, match="workers"):
        batch.run_batch([task("fixture-pass", base)], tmp_path / "new", Settings(),
                        api_key=KEY, workers=5)
