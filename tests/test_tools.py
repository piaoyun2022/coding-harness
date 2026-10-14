import json

import pytest

from coding_harness.contracts import CommandResult
from coding_harness.evidence import EvidenceStore
from coding_harness.tools import TOOLS, dispatch


class Environment:
    def __init__(self):
        self.calls = []

    def execute(self, command, timeout):
        self.calls.append((command, timeout))
        return CommandResult(stdout="output", exit_code=1)

    def apply_patch(self, patch, timeout):
        self.calls.append((patch, timeout))
        return {"ok": True, "changed_files": ["a.py"]}


def test_schemas_and_bounded_timeout():
    env = Environment()
    result = dispatch(env, "execute", json.dumps({"command": "test", "timeout_seconds": 60}), 2)
    assert env.calls == [("test", 2)]
    assert result["ok"] and result["exit_code"] == 1
    assert {t["function"]["name"] for t in TOOLS} == {
        "execute", "apply_patch", "finish", "read_evidence",
    }


@pytest.mark.parametrize("name,args", [
    ("execute", "not json"), ("execute", '{"command": 1}'),
    ("execute", '{"command":"x","timeout_seconds":true}'),
    ("execute", '{"command":"x","extra":1}'), ("apply_patch", '{"patch":""}'),
    ("unknown", "{}"), ("finish", '{"summary":"done"}'),
])
def test_invalid_tools_never_execute(name, args):
    env = Environment()
    assert dispatch(env, name, args, 2)["ok"] is False
    assert not env.calls


def test_patch_dispatch_and_error_no_retry():
    env = Environment()
    assert dispatch(env, "apply_patch", '{"patch":"patch"}', 3)["ok"]
    assert env.calls == [("patch", 3)]

    def broken(patch, timeout):
        env.calls.append(("failed", timeout))
        raise ValueError("Context not found")

    env.apply_patch = broken
    assert dispatch(env, "apply_patch", '{"patch":"bad"}', 3)["error"] == "Context not found"
    assert len(env.calls) == 2


def test_validation_details_do_not_echo_input():
    result = dispatch(Environment(), "execute", '{"command":{"secret":"do-not-echo"}}', 2)
    assert "do-not-echo" not in json.dumps(result)



def test_evidence_tool_reads_saved_text_without_running_environment(tmp_path):
    env = Environment()
    evidence = EvidenceStore(tmp_path, redactor=lambda value: value.replace("secret", "safe"))
    metadata = evidence.store("secret abcdef")
    result = dispatch(env, "read_evidence", json.dumps({
        "id": metadata["id"], "start": 2, "limit": 4,
    }), 3, evidence=evidence)
    assert result["ok"] and result["content"] == "fe a" and result["next_start"] == 6
    assert not env.calls


@pytest.mark.parametrize("extra", [
    {"start": True}, {"start": -1}, {"limit": False}, {"limit": 0},
    {"limit": 16001}, {"limit": "4"}, {"extra": "secret"},
    {"id": "../secret"}, {"id": "C:/private/secret"},
])
def test_evidence_tool_rejects_invalid_or_path_arguments(tmp_path, extra):
    env = Environment()
    evidence = EvidenceStore(tmp_path, redactor=lambda value: value)
    evidence_id = evidence.store("saved")["id"]
    result = dispatch(env, "read_evidence", json.dumps({"id": evidence_id, **extra}),
                      3, evidence=evidence)
    assert not result["ok"] and result["error"] == "Invalid tool arguments"
    assert "secret" not in json.dumps(result)
    assert not env.calls


@pytest.mark.parametrize("enabled", [True, False])
def test_evidence_tool_unknown_or_unavailable_is_recoverable(tmp_path, enabled):
    env = Environment()
    evidence = EvidenceStore(tmp_path, redactor=lambda value: value) if enabled else None
    result = dispatch(env, "read_evidence", json.dumps({"id": "ev_" + "1" * 32}),
                      3, evidence=evidence)
    assert not result["ok"] and "content" not in result and not env.calls


def test_execution_outputs_are_unchanged_when_evidence_is_supplied(tmp_path):
    env = Environment()
    evidence = EvidenceStore(tmp_path, redactor=lambda value: value)
    result = dispatch(env, "execute", '{"command":"test"}', 3, evidence=evidence)
    assert result["stdout"] == "output" and result["exit_code"] == 1
    assert not list(tmp_path.iterdir())
