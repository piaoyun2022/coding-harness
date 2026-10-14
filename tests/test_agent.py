import json

import pytest

from coding_harness.agent import Agent
from coding_harness.config import Settings
from coding_harness.context import Context
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
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "model_notes": [
                            {
                                "text": "Inspected files; tests remain unresolved.",
                                "evidence_ids": [],
                            }
                        ],
                        "next_steps": ["Continue the requested fix."],
                        "evidence_ids": [],
                    }
                ),
            },
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
    model = Model([reply(call()) for _ in range(8)] + [finish()])
    result, _, events = run(
        tmp_path,
        model,
        Environment("observed " * 200),
        context_tokens=4500,
        keep_recent_turns=1,
    )
    assert result["outcome"] == "completed"
    assert result["compactions"] > 0
    assert result["usage"]["total_tokens"] == 9 + 10 * result["compactions"]
    summary_requests = [item for item in model.requests if item[2]["purpose"] == "summary"]
    assert summary_requests
    assert all(item[1] == [] for item in summary_requests)
    assert len([event for event in events if event["event"] == "tool_result"]) == 9


@pytest.mark.parametrize(
    "summary",
    [
        RuntimeError("transport unavailable"),
        Reply({"role": "assistant", "content": "truncated"}, Usage(output_tokens=4), "length"),
        Reply({"role": "assistant", "content": "bad JSON"}, Usage()),
        Reply({"role": "assistant", "content": "x" * 12001}, Usage()),
        Reply({"role": "assistant", "content": json.dumps({
            "model_notes": [{"text": "unsupported", "evidence_ids": ["ev_" + "f" * 32]}],
            "next_steps": [], "evidence_ids": [],
        })}, Usage()),
    ],
)
def test_compaction_failure_stops_without_discarding_history(tmp_path, summary, monkeypatch):
    contexts = []

    class CapturingContext(Context):
        def plan_compaction(self):
            contexts.append((self, json.dumps(self.messages(), ensure_ascii=False)))
            return super().plan_compaction()

    monkeypatch.setattr("coding_harness.agent.Context", CapturingContext)
    model = Model([reply(call()) for _ in range(6)], summary=summary)
    result, _, events = run(
        tmp_path,
        model,
        Environment("observed " * 200),
        context_tokens=4500,
        keep_recent_turns=1,
    )
    assert result["stop_reason"] == "compaction_failed"
    compact = next(event for event in events if event["event"] == "compaction")
    assert not compact["applied"]
    assert compact["before_estimated_tokens"] == compact["after_estimated_tokens"]
    assert len(contexts) == 1
    context, before = contexts[0]
    assert json.dumps(context.messages(), ensure_ascii=False) == before
    assert context.checkpoint is None
    assert not any(event["event"] == "context_cleanup" for event in events)
    executed = [event for event in events if event["event"] == "tool_result"]
    assert len(executed) == len([request for request in model.requests if request[1]])


def test_summary_tokens_can_exhaust_budget_before_next_action(tmp_path):
    summary = Reply(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "model_notes": [
                        {"text": "Inspected code; tests still needed.", "evidence_ids": []}
                    ],
                    "next_steps": [],
                    "evidence_ids": [],
                }
            ),
        },
        Usage(output_tokens=100),
    )
    model = Model([reply(call()) for _ in range(6)], summary=summary)
    result, _, _ = run(
        tmp_path,
        model,
        Environment("observed " * 200),
        context_tokens=4500,
        keep_recent_turns=1,
        max_total_tokens=50,
    )
    assert result["stop_reason"] == "token_budget"
    assert model.requests[-1][2]["purpose"] == "summary"
    assert model.requests[-1][2]["max_output_tokens"] < 50


def test_full_result_and_arguments_stored_before_preview_and_read_without_recursion(tmp_path):
    class ReadingModel:
        def __init__(self):
            self.requests = []
            self.evidence_id = None

        def complete(self, messages, tools, **kwargs):
            self.requests.append((messages, tools, kwargs))
            if len(self.requests) == 1:
                return reply(call(arguments={"command": "read full log"}))
            if len(self.requests) == 2:
                observation = json.loads(messages[-1]["content"])
                self.evidence_id = observation["evidence"]["id"]
                assert observation["preview_truncated"]
                assert "LAST-LINE" not in observation["preview"]
                assert observation["tool_facts"]["observed"]["truncated"] is False
                return reply(call("read_evidence", {"id": self.evidence_id, "limit": 16000}))
            reread = json.loads(messages[-1]["content"])
            assert reread["ok"] and reread["next_start"] is None
            document = json.loads(reread["content"])
            assert json.loads(document["arguments_json"]) == {"command": "read full log"}
            assert document["tool_name"] == "execute"
            assert document["result"]["stdout"].endswith("LAST-LINE")
            assert document["result"]["truncated"] is False
            return finish()

    model = ReadingModel()
    result, environment, events = run(
        tmp_path,
        model,
        Environment("early output " * 100 + "LAST-LINE"),
        max_output_chars=200,
    )
    assert result["outcome"] == "completed"
    assert environment.commands == ["read full log"]
    assert len(list((tmp_path / "evidence").glob("*.txt"))) == 1
    assert len([event for event in events if event["event"] == "evidence_stored"]) == 1


def test_evidence_exhaustion_after_patch_stops_remaining_calls_without_replay(tmp_path):
    class PatchEnvironment(Environment):
        def __init__(self):
            super().__init__()
            self.patches = []

        def apply_patch(self, patch, timeout):
            self.patches.append(patch)
            return {"ok": True, "changed_files": ["fixed.py"]}

    environment = PatchEnvironment()
    model = Model(
        [
            reply(
                call("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"}, "edit"),
                call(call_id="next"),
            )
        ]
    )
    result, _, events = run(tmp_path, model, environment, evidence_max_total_bytes=1)
    assert result["stop_reason"] == "evidence_budget"
    assert len(environment.patches) == 1
    assert environment.commands == []
    results = [event for event in events if event["event"] == "tool_result"]
    assert results[0]["result"]["changed_files"] == ["fixed.py"]
    assert "not executed" in results[1]["result"]["error"]
    assert not list((tmp_path / "evidence").glob("*.txt"))
    assert len(model.requests) == 1


def test_partial_evidence_is_not_claimed_as_full_and_stops(tmp_path):
    result, environment, events = run(
        tmp_path,
        Model([reply(call())]),
        Environment("original result" * 100),
        evidence_max_item_bytes=100,
    )
    assert result["stop_reason"] == "evidence_budget"
    assert environment.commands == ["inspect"]
    assert not any(event["event"] == "evidence_stored" for event in events)
    raw = next(event for event in events if event["event"] == "tool_result")
    assert raw["result"]["stdout"] == "original result" * 100


def test_known_secrets_are_redacted_in_evidence_preview_and_reread(tmp_path):
    secret = "test-only-sensitive-value"
    trace = Trace(tmp_path, secrets=(secret,))
    model = Model([reply(call()), finish()])
    result = Agent(model, Environment("prefix " + secret + " suffix"), Settings(), trace).run("Fix")
    assert result["outcome"] == "completed"
    assert secret not in json.dumps(model.requests[1][0])
    evidence_path = next((tmp_path / "evidence").glob("*.txt"))
    assert secret not in evidence_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in evidence_path.read_text(encoding="utf-8")
    assert secret not in trace.path.read_text(encoding="utf-8")


def test_schema_and_output_reservation_can_prevent_any_model_call(tmp_path, monkeypatch):
    huge_schema = [
        {"type": "function", "function": {"name": "execute", "description": "x" * 10000}}
    ]
    monkeypatch.setattr("coding_harness.agent.TOOLS", huge_schema)
    model = Model([])
    result, environment, _ = run(tmp_path, model, context_tokens=3000)
    assert result["stop_reason"] == "context_budget"
    assert model.requests == [] and environment.commands == []
    settings = Settings(
        model_context_tokens=12000, max_output_tokens=8192, context_margin_tokens=1024
    )
    assert settings.input_budget(settings.max_output_tokens) == 2784
    with pytest.raises(ValueError, match="leave space"):
        Settings(model_context_tokens=9000)


def test_checkpoint_call_respects_hard_window_and_does_not_retry(tmp_path):
    model = Model([reply(call()) for _ in range(10)])
    result, _, events = run(
        tmp_path,
        model,
        Environment("observed " * 200),
        context_tokens=4500,
        keep_recent_turns=1,
        model_context_tokens=6500,
        max_output_tokens=512,
        checkpoint_output_tokens=2048,
    )
    assert result["stop_reason"] == "compaction_failed"
    assert len([event for event in events if event["event"] == "compaction"]) == 1
    assert not any(request[2]["purpose"] == "summary" for request in model.requests)


def test_summary_request_contains_old_preview_before_any_history_is_removed(tmp_path):
    marker = "CURRENT-LOCATION: parser.py:42; assertion still failing"
    model = Model([reply(call()) for _ in range(5)] + [finish()])
    result, _, events = run(
        tmp_path, model, Environment("diagnostic " * 230 + marker),
        context_tokens=4500, keep_recent_turns=1,
    )
    assert result["outcome"] == "completed"
    summaries = [request for request in model.requests if request[2]["purpose"] == "summary"]
    assert summaries
    data = json.loads(summaries[0][0][1]["content"])
    previews = [json.loads(item["content"])["preview"]
                for group in data["complete_earlier_turns"] for item in group[1:]]
    assert any(marker in preview for preview in previews)
    assert not any(event["event"] == "context_cleanup" for event in events)


def test_repeated_results_hint_once_and_never_skip_or_unpair_execution(tmp_path):
    model = Model([reply(call(call_id=str(i))) for i in range(5)] + [finish()])
    result, environment, events = run(tmp_path, model)
    assert result["outcome"] == "completed"
    assert environment.commands == ["inspect"] * 5
    notices = [event for event in events if event["event"] == "repetition_notice"]
    assert len(notices) == 1 and notices[0]["tool_call_id"] == "2"
    stored = [event for event in events if event["event"] == "evidence_stored"]
    assert notices[0]["evidence_id"] == stored[0]["evidence"]["id"]
    history = model.requests[-1][0]
    calls = [call["id"] for item in history for call in item.get("tool_calls", [])]
    observations = [item for item in history if item["role"] == "tool"]
    assert calls == [item["tool_call_id"] for item in observations] == list(map(str, range(5)))
    assert sum("repetition_notice" in json.loads(item["content"]) for item in observations) == 1
    assert len(list((tmp_path / "evidence").glob("*.txt"))) == 5


def test_repetition_hint_window_resets_after_patch_and_requires_matching_results(tmp_path):
    class ChangingEnvironment(Environment):
        def apply_patch(self, patch, timeout):
            return {"ok": True, "changed_files": ["fixed.py"]}

    # A successful edit invalidates prior repetition counts even if printed output is identical.
    model = Model([
        reply(call()), reply(call()),
        reply(call("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"})),
        reply(call()), reply(call()), finish(),
    ])
    result, environment, events = run(tmp_path / "patched", model, ChangingEnvironment())
    assert result["outcome"] == "completed" and environment.commands == ["inspect"] * 4
    assert not any(event["event"] == "repetition_notice" for event in events)

    class FreshOutput(Environment):
        def execute(self, command, timeout):
            self.commands.append(command)
            return CommandResult(stdout=str(len(self.commands)))

    model = Model([reply(call()) for _ in range(4)] + [finish()])
    result, environment, events = run(tmp_path / "changed", model, FreshOutput())
    assert result["outcome"] == "completed" and len(environment.commands) == 4
    assert not any(event["event"] == "repetition_notice" for event in events)


@pytest.mark.parametrize("commands", [
    ["read target"] * 2 + [f"read different-{i}" for i in range(7)] + ["read target"],
    ["read target"] + [f"read different-{i}" for i in range(6)] + ["read target"] * 2,
])
def test_repetition_hint_forgets_commands_outside_bounded_window(tmp_path, commands):
    model = Model(
        [reply(call(arguments={"command": command})) for command in commands] + [finish()]
    )
    result, environment, events = run(tmp_path, model)
    assert result["outcome"] == "completed" and environment.commands == commands
    assert not any(event["event"] == "repetition_notice" for event in events)
