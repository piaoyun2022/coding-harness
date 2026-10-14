import copy

import pytest

from coding_harness.context import Context
from coding_harness.contracts import Reply, Usage


def turn(prefix, payload):
    calls = [
        {
            "id": prefix + str(index),
            "type": "function",
            "function": {"name": "execute", "arguments": "{}"},
        }
        for index in range(2)
    ]
    assistant = {
        "role": "assistant",
        "reasoning_content": "reasoning preserved",
        "tool_calls": calls,
    }
    observations = [
        {"role": "tool", "tool_call_id": call["id"], "content": payload} for call in calls
    ]
    return assistant, observations


def filled_context():
    context = Context("rules stay", "original task", token_limit=1000, keep_recent_turns=1)
    context.add_turn(*turn("old", "old observations " * 100))
    context.add_turn(*turn("recent", "recent observation"))
    return context


def test_compaction_retains_task_rules_and_complete_recent_turn():
    context = filled_context()
    before = context.messages()
    plan = context.plan_compaction()
    assert plan is not None
    assert context.apply_compaction(
        plan,
        Reply(
            {"role": "assistant", "content": "Changed one file; tests failed; inspect parser."},
            Usage(),
        ),
    )
    after = context.messages()
    assert after[:2] == before[:2]
    assert after[-3:] == before[-3:]
    assert after[-3]["reasoning_content"] == "reasoning preserved"
    assert [item["tool_call_id"] for item in after[-2:]] == ["recent0", "recent1"]
    after[0]["content"] = "caller mutated copy"
    assert context.messages()[0]["content"] == "rules stay"


@pytest.mark.parametrize(
    "message,reason",
    [
        ({"role": "assistant", "content": ""}, "stop"),
        ({"role": "assistant", "content": "partial summary"}, "length"),
        ({"role": "assistant", "content": "x" * 12001}, "stop"),
        ({"role": "assistant", "content": "bad", "tool_calls": [{}]}, "stop"),
    ],
)
def test_invalid_compaction_never_destroys_original_history(message, reason):
    context = filled_context()
    before = copy.deepcopy(context.messages())
    plan = context.plan_compaction()
    assert plan is not None
    assert not context.apply_compaction(plan, Reply(message, Usage(), reason))
    assert context.messages() == before


def test_cannot_compact_only_recent_turns_or_unmatched_observations():
    context = Context("rules", "task", token_limit=1, keep_recent_turns=1)
    assistant, observations = turn("call", "large output")
    with pytest.raises(ValueError, match="Incomplete"):
        context.add_turn(assistant, observations[:1])
    context.add_turn(assistant, observations)
    assert context.plan_compaction() is None
