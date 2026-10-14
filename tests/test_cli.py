import json
import os
import sys
from pathlib import Path

import pytest

from coding_harness import cli
from coding_harness.contracts import Reply, Usage
from coding_harness.workspace import git


def call_reply(name, arguments):
    return Reply(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "same",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        Usage(input_tokens=2, output_tokens=3),
        "tool_calls",
    )


class FakeModel:
    instances = []

    def __init__(self, settings, key):
        self.settings, self.key = settings, key
        self.closed = False
        self.requests = []
        self.replies = iter(
            [
                call_reply(
                    "apply_patch",
                    {
                        "patch": "*** Begin Patch\n*** Update File: answer.py\n@@\n"
                        "-    return 0\n+    return 42\n*** End Patch",
                    },
                ),
                call_reply(
                    "execute",
                    {
                        "command": ('& "' if os.name == "nt" else '"')
                        + sys.executable
                        + '" check.py'
                    },
                ),
                call_reply("finish", {"summary": "Changed answer and ran check.py."}),
            ]
        )
        self.instances.append(self)

    def complete(self, messages, tools, **kwargs):
        self.requests.append((messages, tools, kwargs))
        return next(self.replies)

    def close(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    FakeModel.instances = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret-never-print")
    monkeypatch.setattr(cli, "DeepSeekModel", FakeModel)
    source = tmp_path / "source"
    source.mkdir()
    (source / "answer.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
    (source / "check.py").write_text("from answer import answer\nassert answer() == 42\n")
    (source / "AGENTS.md").write_text("Keep the answer function name.")
    (source / ".env").write_text("HIDDEN=do-not-copy")
    args = [
        "run",
        "--repo",
        str(source),
        "--task",
        "Make check.py pass",
        "--output",
        str(tmp_path / "run"),
        "--env-file",
        str(tmp_path / "missing.env"),
        "--backend",
        "local",
        "--allow-local",
    ]
    return source, args


def test_local_run_applies_patch_tests_and_preserves_input(setup, capsys):
    source, args = setup
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    output = Path(result["output"])
    assert result["outcome"] == "completed"
    assert "return 0" in (source / "answer.py").read_text()
    assert "return 42" in (output / "workspace" / "answer.py").read_text()
    assert not (output / "workspace" / ".env").exists()
    assert "+    return 42" in (output / "changes.patch").read_text()
    observations = [
        json.loads(line) for line in (output / "trajectory.jsonl").read_text().splitlines()
    ]
    execute = next(
        event
        for event in observations
        if event["event"] == "tool_result" and event["name"] == "execute"
    )
    assert execute["result"]["exit_code"] == 0
    assert FakeModel.instances[-1].closed
    assert "Keep the answer function name." in FakeModel.instances[-1].requests[0][0][0]["content"]


def test_local_requires_explicit_opt_in_before_creating_outputs(setup, capsys):
    source, args = setup
    args.remove("--allow-local")
    assert cli.main(args) == 2
    assert "--allow-local" in capsys.readouterr().out
    assert not source.parent.joinpath("run").exists()
    assert not FakeModel.instances


def test_setup_failure_still_produces_result_and_never_echoes_secret(setup, monkeypatch, capsys):
    source, args = setup

    def unavailable(*_):
        raise RuntimeError("offline-secret-never-print")

    monkeypatch.setattr(cli, "_environment", unavailable)
    assert cli.main(args) == 1
    raw = capsys.readouterr().out
    assert "offline-secret-never-print" not in raw
    result = json.loads(raw)
    assert result["stop_reason"] == "environment_setup"
    assert result["errors"] == [{"stage": "environment_setup", "error_type": "RuntimeError"}]
    assert source.parent.joinpath("run/result.json").exists()
    assert source.parent.joinpath("run/changes.patch").read_text() == ""


def test_primary_agent_failure_and_cleanup_failures_are_separate(setup, monkeypatch, capsys):
    _, args = setup

    class BrokenModel(FakeModel):
        def complete(self, *_, **__):
            raise RuntimeError(self.key)

    class BrokenEnvironment:
        description = "offline"
        closed = False

        def collect_patch(self):
            raise OSError("offline-secret-never-print")

        def close(self):
            self.closed = True
            raise OSError("offline-secret-never-print")

    environment = BrokenEnvironment()
    monkeypatch.setattr(cli, "DeepSeekModel", BrokenModel)
    monkeypatch.setattr(cli, "_environment", lambda *_: environment)
    assert cli.main(args) == 1
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert result["agent_outcome"] == "failed"
    assert result["stop_reason"] == "runtime_error"
    assert [error["stage"] for error in result["errors"]] == ["patch_export", "environment_close"]
    assert environment.closed and FakeModel.instances[-1].closed
    assert "offline-secret-never-print" not in raw


def test_keyboard_interrupt_still_exports_and_closes(setup, monkeypatch, capsys):
    _, args = setup

    class Interrupted(FakeModel):
        def complete(self, *_, **__):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "DeepSeekModel", Interrupted)
    assert cli.main(args) == 130
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "cancelled"
    assert Path(result["output"], "changes.patch").exists()
    assert FakeModel.instances[-1].closed


def test_probe_redacts_known_key_and_uses_small_request(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret-never-print")

    class Probe(FakeModel):
        def complete(self, messages, tools, **kwargs):
            assert tools == []
            assert kwargs["purpose"] == "summary"
            assert kwargs["max_output_tokens"] == 128
            return Reply({"role": "assistant", "content": self.key}, Usage(output_tokens=1))

    monkeypatch.setattr(cli, "DeepSeekModel", Probe)
    output = tmp_path / "probe"
    assert (
        cli.main(
            [
                "probe",
                "--env-file",
                str(tmp_path / "missing.env"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    raw = capsys.readouterr().out
    assert "offline-secret-never-print" not in raw
    assert "[REDACTED]" in raw
    assert "offline-secret-never-print" not in (output / "result.json").read_text()
    assert "offline-secret-never-print" not in (output / "trajectory.jsonl").read_text()


def test_settings_file_and_explicit_overrides(setup, capsys):
    source, args = setup
    settings = source.parent / "settings.json"
    settings.write_text(json.dumps({"model": "configured", "max_steps": 9}))
    args += ["--settings", str(settings), "--model", "explicit", "--reasoning-effort", "low"]
    assert cli.main(args) == 0
    capsys.readouterr()
    assert FakeModel.instances[-1].settings.model == "explicit"
    assert FakeModel.instances[-1].settings.reasoning_effort == "low"
    assert FakeModel.instances[-1].settings.max_steps == 9


def test_existing_output_is_not_overwritten(setup, capsys):
    source, args = setup
    output = source.parent / "run"
    output.mkdir()
    marker = output / "result.json"
    marker.write_text("existing artifact")
    assert cli.main(args) == 1
    capsys.readouterr()
    assert marker.read_text() == "existing artifact"


def test_exact_env_file_is_loaded_without_parent_search(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    parent_env = tmp_path / ".env"
    parent_env.write_text("DEEPSEEK_API_KEY=must-not-be-discovered\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert cli.main(["probe", "--env-file", str(child / "absent.env")]) == 2
    assert "must-not-be-discovered" not in capsys.readouterr().out


def test_baseline_one_request_no_tools_and_does_not_apply_patch(setup, monkeypatch, capsys):
    source, _ = setup
    git(source, "init", "--quiet")
    git(source, "add", "answer.py", "check.py")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    head = git(source, "rev-parse", "HEAD").stdout.decode().strip()
    tasks = source.parent / "tasks.jsonl"
    tasks.write_text(
        json.dumps(
            {
                "instance_id": "repo__repo-1",
                "repo": "repo/repo",
                "base_commit": head,
                "problem_statement": "answer must return 42",
                "version": "1",
            }
        )
        + "\n"
    )

    class Baseline(FakeModel):
        def complete(self, messages, tools, **kwargs):
            assert tools == [] and kwargs["purpose"] == "baseline"
            self.requests.append((messages, tools, kwargs))
            return Reply(
                {
                    "role": "assistant",
                    "content": "diff --git a/answer.py b/answer.py\n"
                    "--- a/answer.py\n+++ b/answer.py\n@@ -1,2 +1,2 @@\n"
                    " def answer():\n-    return 0\n+    return 42\n",
                },
                Usage(input_tokens=10, output_tokens=20),
            )

    monkeypatch.setattr(cli, "DeepSeekModel", Baseline)
    output = source.parent / "baseline"
    assert (
        cli.main(
            [
                "baseline",
                "--tasks",
                str(tasks),
                "--instance-id",
                "repo__repo-1",
                "--repo",
                str(source),
                "--output",
                str(output),
                "--env-file",
                str(source.parent / "missing.env"),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "generated" and result["requests"] == 1
    assert "return 0" in (source / "answer.py").read_text()
    assert len(FakeModel.instances[-1].requests) == 1
    assert FakeModel.instances[-1].closed


def test_dataset_and_grade_commands_do_not_need_credentials(tmp_path, monkeypatch, capsys):
    from coding_harness import benchmark

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(benchmark, "prepare_dataset", lambda *_args, **_kwargs: {"planned": 300})
    assert cli.main(["dataset", "--output", str(tmp_path / "tasks")]) == 0
    assert json.loads(capsys.readouterr().out)["planned"] == 300
    monkeypatch.setattr(benchmark, "evaluation_preflight", lambda: {"ready": False})
    assert (
        cli.main(
            [
                "grade-command",
                "--predictions",
                str(tmp_path / "predictions.jsonl"),
                "--grading-data",
                str(tmp_path / "grading.json"),
                "--run-id",
                "offline-check",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["executed"] is False
    assert result["preflight"]["ready"] is False
