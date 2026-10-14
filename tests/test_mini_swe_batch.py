import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "mini_batch", Path(__file__).parents[1] / "scripts/run_mini_swe_batch.py"
)
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)
KEY = "private-fake-key"
BASE = "a" * 40
PATCH = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"


class ModelBase:
    def __init__(self, **kwargs):
        self.config = kwargs

    def _query(self, messages, **kwargs):
        return kwargs


class AgentBase:
    def __init__(self, model, env, **kwargs):
        self.model, self.env, self.config = model, env, SimpleNamespace(**kwargs)
        self.n_calls, self.cost = 0, 0.0

    def run(self, task):
        self.n_calls, self.cost = 2, 0.01
        if task == "cancel":
            raise KeyboardInterrupt()
        if task == "deadline":
            raise batch.HostDeadline()
        if task == "error":
            raise RuntimeError(KEY)
        return {
            "exit_status": "Submitted" if task == "submit" else "LimitsExceeded",
            "submission": PATCH if task == "submit" else "",
        }

    def serialize(self, *extra):
        return {
            "info": {"config": {"model": self.model.config}},
            "messages": [
                {
                    "content": KEY,
                    "extra": {
                        "response": {
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 20,
                                "prompt_tokens_details": {"cached_tokens": 50},
                            }
                        }
                    },
                }
            ],
        }


class FakeEnvironment:
    instances = []

    def __init__(self, **kwargs):
        self.config, self.closed = kwargs, False
        self.instances.append(self)

    def execute(self, action, **kwargs):
        command = action["command"]
        values = {
            "git rev-parse HEAD": BASE,
            "git status --porcelain --untracked-files=normal": "",
            "git diff --binary HEAD": PATCH,
        }
        return {"returncode": 0, "output": values[command]}

    def cleanup(self):
        self.closed = True


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(batch, "deadline", lambda seconds: nullcontext())
    monkeypatch.setattr(batch, "inspect_image", lambda image: {"id": "preexisting"})
    FakeEnvironment.instances = []
    Agent, Model, _ = batch.protected_classes(AgentBase, ModelBase, object)
    return Agent, Model, FakeEnvironment


def run(tmp_path, classes, problem):
    task = {"instance_id": "fixture-one", "base_commit": BASE, "problem_statement": problem}
    return batch.run_case(
        task,
        tmp_path / "case",
        {
            "agent": {"step_limit": 250, "cost_limit": 3.0},
            "model": {"model_name": "deepseek/deepseek-flash"},
            "environment": {
                "environment_class": "docker",
                "cwd": "/testbed",
                "env": {"PAGER": "cat"},
            },
        },
        KEY,
        classes,
    )


def test_native_submission_is_primary_and_artifacts_are_private(tmp_path, offline):
    result = run(tmp_path, offline, "submit")
    assert result["status"] == "generated" and result["stop_reason"] == "Submitted"
    assert result["outcome"] == "completed" and result["steps"] == 2
    assert result["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": 50,
        "total_tokens": 120,
    }
    case = tmp_path / "case"
    assert (case / "changes.patch").read_bytes() == PATCH.encode()
    assert result["patch_sha256"] == batch.sha(PATCH.encode())
    env = FakeEnvironment.instances[0]
    assert env.closed and env.config["forward_env"] == []
    assert "--network=none" in env.config["run_args"]
    assert "--memory=4g" in env.config["run_args"]
    for path in case.iterdir():
        assert KEY not in path.read_text()


@pytest.mark.parametrize(
    "problem,reason", [("limit", "LimitsExceeded"), ("deadline", "HostDeadline")]
)
def test_stopped_native_submission_never_silently_uses_workspace_patch(
    tmp_path, offline, problem, reason
):
    result = run(tmp_path, offline, problem)
    case = tmp_path / "case"
    assert result["status"] == "empty_patch" and result["stop_reason"] == reason
    assert result["outcome"] == "budget_exhausted"
    assert (case / "changes.patch").read_bytes() == b""
    assert (case / "diagnostic-workspace.patch").read_bytes() == PATCH.encode()
    assert json.loads((case / "prediction.json").read_text())["model_patch"] == ""
    assert FakeEnvironment.instances[0].closed


def test_exception_records_type_and_closes_owned_environment(tmp_path, offline):
    result = run(tmp_path, offline, "error")
    assert result["status"] == "agent_error"
    assert result["errors"] == [{"stage": "agent", "error_type": "RuntimeError"}]
    assert KEY not in json.dumps(result)
    assert FakeEnvironment.instances[0].closed


def test_key_is_query_only_not_serialized_config(monkeypatch):
    _, Model, _ = batch.protected_classes(object, ModelBase, object)
    model = Model(api_key=KEY, model_name="deepseek/deepseek-flash")
    assert model._query([])["api_key"] == KEY
    assert KEY not in json.dumps(model.config)
    assert model.adapter_attempts == 1


def test_only_owned_label_is_removed_synchronously(monkeypatch):
    class EnvironmentBase:
        def __init__(self, **kwargs):
            self.container_id = "owned"

    _, _, Environment = batch.protected_classes(object, object, EnvironmentBase)
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="owned\n" if args[0] == "ps" else "owner\n")

    monkeypatch.setattr(batch, "docker", docker)
    env = Environment(owner="owner")
    env.cleanup()
    env.cleanup()
    assert calls[0] == ("ps", "-aq", "--filter", "label=mini-bench.owner=owner")
    assert calls[-1] == ("rm", "-f", "owned") and len(calls) == 3


def test_task_projection_discards_grader_fields_and_requires_three(tmp_path):
    rows = [
        {
            "instance_id": "fixture-" + str(i),
            "base_commit": BASE,
            "problem_statement": "issue",
            "patch": "do not pass",
            "test_patch": "hidden",
        }
        for i in range(3)
    ]
    path = tmp_path / "tasks.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert all("patch" not in row and "test_patch" not in row for row in batch.load_tasks(path))
    path.write_text(json.dumps(rows[0]))
    with pytest.raises(ValueError, match="exactly three"):
        batch.load_tasks(path)


def test_user_cancel_is_not_model_empty_patch_failure(tmp_path, offline):
    result = run(tmp_path, offline, "cancel")
    assert result["status"] == "agent_error" and result["outcome"] == "cancelled"
    assert result["stop_reason"] == "Cancelled"
    assert result["agent_seconds"] is not None
    assert result["deadline_scope"] == "environment_setup_and_agent"
    assert result["host_total_deadline_seconds"] == 900
    assert FakeEnvironment.instances[0].closed
