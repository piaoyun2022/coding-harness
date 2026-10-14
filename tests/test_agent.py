import json

import pytest

from coding_harness.agent import Agent
from coding_harness.config import Settings
from coding_harness.contracts import CommandResult, Reply, Usage
from coding_harness.trace import Trace


def call(name="execute", arguments=None, call_id="same"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments if arguments is not None else {"command": "inspect"}),
        },
    }


def reply(*calls, tokens=1, reason="tool_calls"):
    return Reply(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "retained",
            "tool_calls": list(calls),
        },
        Usage(input_tokens=tokens),
        reason,
    )


def finish():
    return reply(call("finish", {"summary": "Implemented; tests reported."}))


class Model:
    def __init__(self, replies, summary=None):
        self.replies = iter(replies)
        self.requests = []
        self.summary = summary or Reply(
            {"role": "assistant", "content": "Inspected files. Continue with requested fix."},
            Usage(output_tokens=10),
        )

    def complete(self, messages, tools, **kwargs):
        self.requests.append((messages, tools, kwargs))
        if kwargs["purpose"] == "summary":
            if isinstance(self.summary, Exception):
                raise self.summary
            return self.summary
        return next(self.replies)


class Environment:
    description = "Offline test environment"

    def __init__(self, output="observed", on_execute=None):
        self.commands = []
        self.output = output
        self.on_execute = on_execute

    def execute(self, command, timeout):
        self.commands.append(command)
        if self.on_execute:
            self.on_execute()
        return CommandResult(stdout=self.output)

    def apply_patch(self, patch, timeout):
        raise AssertionError("This test does not request a patch")


def run(tmp_path, model, environment=None, **overrides):
    environment = environment or Environment()
    settings = Settings(**overrides)
    trace = Trace(tmp_path)
    result = Agent(model, environment, settings, trace).run("Fix the bug", "Keep public APIs.")
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    return result, environment, events


def test_tool_results_pair_and_ids_can_repeat_across_turns(tmp_path):
    model = Model([reply(call()), reply(call()), finish()])
    result, environment, _ = run(tmp_path, model)
    assert result["outcome"] == "completed"
    assert environment.commands == ["inspect", "inspect"]
    history = model.requests[2][0]
    assert history[-2]["reasoning_content"] == "retained"
    assert history[-1]["role"] == "tool"
    assert history[-1]["tool_call_id"] == "same"
    assert result["usage"]["total_tokens"] == 3


@pytest.mark.parametrize(
    "bad_reply",
    [
        reply(call(), call()),
        reply(call(), reason="length"),
        reply(call(), reason="content_filter"),
    ],
)
def test_invalid_or_truncated_replies_never_execute_tools(tmp_path, bad_reply):
    result, environment, _ = run(tmp_path, Model([bad_reply]))
    assert result["outcome"] == "failed"
    assert environment.commands == []


def test_bad_arguments_and_unknown_tools_are_recoverable_observations(tmp_path):
    malformed = call()
    malformed["function"]["arguments"] = "{broken"
    model = Model([reply(malformed, call("missing", {}, "other")), finish()])
    result, environment, events = run(tmp_path, model)
    assert result["outcome"] == "completed"
    assert environment.commands == []
    observations = model.requests[1][0][-2:]
    assert [item["tool_call_id"] for item in observations] == ["same", "other"]
    assert len([event for event in events if event["event"] == "tool_result"]) == 3


def test_finish_cannot_execute_with_other_tools_or_accept_extra_arguments(tmp_path):
    model = Model(
        [
            reply(call("finish", {"summary": "premature"}), call(call_id="other")),
            reply(call("finish", {"summary": "extra", "unexpected": True})),
            finish(),
        ]
    )
    result, environment, _ = run(tmp_path, model)
    assert result["outcome"] == "completed"
    assert result["steps"] == 3
    assert environment.commands == []


def test_text_only_reply_gets_reminder_and_stops_at_step_budget(tmp_path):
    model = Model([Reply({"role": "assistant", "content": "I am done"}, Usage())])
    result, _, _ = run(tmp_path, model, max_steps=1)
    assert result["outcome"] == "budget_exhausted"
    assert result["stop_reason"] == "step_budget"


def test_budget_after_model_response_prevents_tool_execution(tmp_path):
    result, environment, _ = run(
        tmp_path,
        Model([reply(call(), tokens=10)]),
        max_total_tokens=10,
    )
    assert result["stop_reason"] == "token_budget"
    assert environment.commands == []


def test_time_budget_mid_batch_returns_observations_without_replaying(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("coding_harness.agent.time.monotonic", lambda: clock[0])
    environment = Environment(on_execute=lambda: clock.__setitem__(0, 20.0))
    result, _, events = run(
        tmp_path,
        Model([reply(call(call_id="first"), call(call_id="second"))]),
        environment,
        max_seconds=10,
    )
    assert result["stop_reason"] == "time_budget"
    assert environment.commands == ["inspect"]
    results = [event for event in events if event["event"] == "tool_result"]
    assert [event["tool_call_id"] for event in results] == ["first", "second"]
    assert "not executed" in results[-1]["result"]["error"]


def test_compaction_is_accounted_and_raw_log_retains_removed_history(tmp_path):
    model = Model([reply(call()) for _ in range(5)] + [finish()])
    result, _, events = run(
        tmp_path,
        model,
        Environment("observed " * 120),
        context_tokens=2000,
        keep_recent_turns=1,
    )
    assert result["outcome"] == "completed"
    assert result["compactions"] > 0
    assert result["usage"]["total_tokens"] == 6 + 10 * result["compactions"]
    summary_requests = [item for item in model.requests if item[2]["purpose"] == "summary"]
    assert summary_requests
    assert all(item[1] == [] for item in summary_requests)
    assert len([event for event in events if event["event"] == "tool_result"]) == 6


@pytest.mark.parametrize(
    "summary",
    [
        RuntimeError("transport unavailable"),
        Reply({"role": "assistant", "content": "truncated"}, Usage(output_tokens=4), "length"),
    ],
)
def test_compaction_failure_stops_without_discarding_history(tmp_path, summary):
    model = Model([reply(call()) for _ in range(6)], summary=summary)
    result, _, events = run(
        tmp_path,
        model,
        Environment("observed " * 120),
        context_tokens=2000,
        keep_recent_turns=1,
    )
    assert result["stop_reason"] == "compaction_failed"
    compact = next(event for event in events if event["event"] == "compaction")
    assert not compact["applied"]
    assert compact["before_estimated_tokens"] == compact["after_estimated_tokens"]


def test_summary_tokens_can_exhaust_budget_before_next_action(tmp_path):
    summary = Reply(
        {"role": "assistant", "content": "Inspected code, tests still needed."},
        Usage(output_tokens=100),
    )
    model = Model([reply(call()) for _ in range(6)], summary=summary)
    result, _, _ = run(
        tmp_path,
        model,
        Environment("observed " * 120),
        context_tokens=2000,
        keep_recent_turns=1,
        max_total_tokens=50,
    )
    assert result["stop_reason"] == "token_budget"
    assert model.requests[-1][2]["purpose"] == "summary"
    assert model.requests[-1][2]["max_output_tokens"] < 50
