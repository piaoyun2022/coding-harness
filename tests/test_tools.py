import json

import pytest

from coding_harness.contracts import CommandResult
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
    assert {t["function"]["name"] for t in TOOLS} == {"execute", "apply_patch", "finish"}


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
