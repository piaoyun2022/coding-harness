import copy
import json

import pytest

from coding_harness.context import Context, ToolFact, estimate_tokens
from coding_harness.contracts import Reply, Usage
from coding_harness.evidence import EvidenceStore


def turn(prefix, payload, reasoning="reasoning preserved"):
    calls = [
        {
            "id": prefix + str(index),
            "type": "function",
            "function": {"name": "execute", "arguments": "{}"},
        }
        for index in range(2)
    ]
    assistant = {"role": "assistant", "reasoning_content": reasoning, "tool_calls": calls}
    observations = [
        {"role": "tool", "tool_call_id": call["id"], "content": payload} for call in calls
    ]
    return assistant, observations


def summary(notes=None, ids=None):
    return Reply(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "model_notes": [
                        {"text": note, "evidence_ids": ids or []}
                        for note in (notes or ["Changed parser; tests failed."])
                    ],
                    "next_steps": ["Inspect failure."],
                    "evidence_ids": ids or [],
                }
            ),
        },
        Usage(),
    )


def filled_context():
    context = Context(
        "rules stay\n逐字不变", "original task\n不要丢失", token_limit=1000, keep_recent_turns=1
    )
    context.add_turn(*turn("old", "old observations " * 100))
    context.add_turn(*turn("recent", "recent observation"))
    return context


def evidence_turn(context, store, prefix, *, output="observed", exit_code=1, reasoning=""):
    metadata = store.store(json.dumps({"exit_code": exit_code, "stdout": output}))
    fact = ToolFact(
        evidence_id=metadata["id"],
        tool_name="execute",
        tool_call_id=prefix + "0",
        observed={"ok": True, "exit_code": exit_code, "timed_out": False},
    )
    context.register_fact(fact)
    payload = json.dumps(
        {
            "evidence": metadata,
            "tool_facts": fact.model_dump(),
            "preview": output,
            "preview_truncated": False,
        }
    )
    context.add_turn(*turn(prefix, payload, reasoning=reasoning))
    return metadata["id"]


def test_compaction_retains_task_rules_and_complete_recent_turn():
    context = filled_context()
    before = context.messages()
    plan = context.plan_compaction()
    assert plan is not None
    assert context.apply_compaction(plan, summary())
    after = context.messages()
    assert after[:2] == before[:2]
    assert after[-3:] == before[-3:]
    assert after[-3]["reasoning_content"] == "reasoning preserved"
    assert [item["tool_call_id"] for item in after[-2:]] == ["recent0", "recent1"]
    after[0]["content"] = "caller mutated copy"
    assert context.messages()[0]["content"] == "rules stay\n逐字不变"


@pytest.mark.parametrize(
    "message,reason",
    [
        ({"role": "assistant", "content": ""}, "stop"),
        ({"role": "assistant", "content": summary().message["content"]}, "length"),
        ({"role": "assistant", "content": "x" * 12001}, "stop"),
        ({"role": "assistant", "content": "bad", "tool_calls": [{}]}, "stop"),
        ({"role": "assistant", "content": '{"model_notes": [42]}'}, "stop"),
        ({"role": "assistant", "content": '{"model_notes": [], "next_steps": []}'}, "stop"),
        ({"role": "assistant", "content": '{"model_notes": ["passed"], "tool_facts": []}'}, "stop"),
    ],
)
def test_invalid_compaction_never_destroys_original_history(message, reason):
    context = filled_context()
    before = copy.deepcopy(context.messages())
    plan = context.plan_compaction()
    assert plan is not None
    assert not context.apply_compaction(plan, Reply(message, Usage(), reason))
    assert context.messages() == before
    assert context.checkpoint is None


def test_cannot_compact_only_recent_turns_or_unmatched_observations():
    context = Context("rules", "task", token_limit=1, keep_recent_turns=1)
    assistant, observations = turn("call", "large output")
    with pytest.raises(ValueError, match="Incomplete"):
        context.add_turn(assistant, observations[:1])
    with pytest.raises(ValueError, match="Incomplete"):
        context.add_turn(assistant, list(reversed(observations)))
    with pytest.raises(ValueError, match="matching"):
        context.add_turn({"role": "assistant"}, observations)
    context.add_turn(assistant, observations)
    assert context.plan_compaction() is None


def test_repeated_checkpoint_carries_previous_facts_and_anchor(tmp_path):
    store = EvidenceStore(tmp_path, redactor=lambda value: value)
    context = Context(
        "Pinned system\n第二行", "Exact task", token_limit=2000, keep_recent_turns=1, evidence=store
    )
    first = evidence_turn(context, store, "old", output="large output" * 500)
    context.add_turn(*turn("recent", "pending next action"))
    pinned = context.messages()[:2]
    plan = context.plan_compaction()
    assert context.apply_compaction(plan, summary(["First unresolved issue"]))
    context.add_turn(*turn("newold", "new discovery " * 500))
    context.add_turn(*turn("newrecent", "latest observation"))
    plan = context.plan_compaction()
    data = json.loads(plan.messages[1]["content"])
    assert data["previous_checkpoint"]["model_notes"] == [
        {"text": "First unresolved issue", "evidence_ids": []}
    ]
    assert first in data["previous_checkpoint"]["evidence_ids"]
    assert context.apply_compaction(plan, summary(["Retained issue plus new discovery"]))
    assert context.messages()[:2] == pinned
    assert context.checkpoint.evidence_ids == [first]
    # A model statement cannot overwrite actual failed command status.
    assert context.checkpoint.tool_facts[0].observed["exit_code"] == 1
    detached = context.checkpoint
    detached.tool_facts[0].observed["exit_code"] = 0
    assert context.checkpoint.tool_facts[0].observed["exit_code"] == 1


def test_forged_or_missing_evidence_rejects_checkpoint(tmp_path):
    store = EvidenceStore(tmp_path, redactor=lambda value: value)
    context = Context("rules", "task", token_limit=1000, keep_recent_turns=1, evidence=store)
    known = evidence_turn(context, store, "old", output="failed test " * 500)
    context.add_turn(*turn("recent", "small"))
    plan = context.plan_compaction()
    before = context.messages()
    assert not context.apply_compaction(plan, summary(ids=["ev_" + "0" * 32]))
    assert context.messages() == before
    (tmp_path / (known + ".txt")).unlink()
    assert not context.apply_compaction(plan, summary(ids=[known]))
    assert context.messages() == before


def test_cleanup_preserves_evidence_and_recent_whole_turn(tmp_path):
    store = EvidenceStore(tmp_path, redactor=lambda value: value)
    context = Context("rules", "task", token_limit=2000, keep_recent_turns=1, evidence=store)
    known = evidence_turn(context, store, "old", output="full output " * 1000)
    context.add_turn(*turn("recent", "small latest"))
    recent = context.messages()[-3:]
    before = context.estimated_tokens
    assert context.clean_old_previews() == 2
    assert context.estimated_tokens < before
    assert context.messages()[-3:] == recent
    old = json.loads(context.messages()[3]["content"])
    assert "preview" not in old and old["preview_omitted"]
    assert old["evidence"]["id"] == known
    assert "full output" in store.read(known, limit=16000)["content"]
    assert context.plan_compaction() is None


def test_recent_turns_are_selected_by_tokens_without_splitting_pair():
    context = Context("rules", "task", token_limit=1, keep_recent_turns=10, recent_token_limit=400)
    context.add_turn(*turn("old", "large" * 100))
    context.add_turn(*turn("middle", "large" * 100))
    context.add_turn(*turn("latest", "oversized latest" * 500))
    plan = context.plan_compaction()
    assert plan.older_turns == 2
    # An oversized newest turn is retained rather than silently dropped to fit.
    before = context.messages()
    assert not context.apply_compaction(plan, summary())
    assert context.messages() == before
    assert context.estimated_tokens > context.token_limit
    assert [item["tool_call_id"] for item in context.messages()[-2:]] == ["latest0", "latest1"]


def test_tool_schemas_count_toward_budget_and_stale_plans_rejected():
    messages = [{"role": "user", "content": "tiny"}]
    tools = [{"type": "function", "function": {"description": "x" * 5000}}]
    assert estimate_tokens(messages, tools) > estimate_tokens(messages) + 2000
    context = Context("rules", "task", token_limit=1000, tools=tools)
    assert context.estimated_tokens > 1000
    assert context.plan_compaction() is None
    context = filled_context()
    plan = context.plan_compaction()
    context.add_turn(*turn("arrived", "new"))
    before = context.messages()
    assert not context.apply_compaction(plan, summary())
    assert context.messages() == before


def test_individual_note_cannot_reference_forged_evidence(tmp_path):
    store = EvidenceStore(tmp_path, redactor=lambda value: value)
    context = Context("rules", "task", token_limit=2000, keep_recent_turns=1, evidence=store)
    known = evidence_turn(context, store, "old", output="failing details " * 1000)
    context.add_turn(*turn("recent", "small"))
    plan = context.plan_compaction()
    payload = json.loads(summary(ids=[known]).message["content"])
    payload["model_notes"][0]["evidence_ids"] = ["ev_" + "f" * 32]
    before = context.messages()
    assert not context.apply_compaction(
        plan,
        Reply({"role": "assistant", "content": json.dumps(payload)}, Usage()),
    )
    assert context.messages() == before


def test_summary_receives_bounded_evidence_excerpts_after_preview_cleanup(tmp_path):
    store = EvidenceStore(tmp_path, redactor=lambda value: value)
    context = Context("rules", "task", token_limit=2000, keep_recent_turns=1, evidence=store)
    evidence_turn(
        context,
        store,
        "old",
        output="Failure detail: line 42\n" * 1000,
        reasoning="Prior investigation " * 400,
    )
    context.add_turn(*turn("latest", "small"))
    assert context.clean_old_previews() == 2
    plan = context.plan_compaction()
    data = json.loads(plan.messages[1]["content"])
    assert sum(len(page["content"]) for page in data["evidence_excerpts"]) <= 8000
    assert "Failure detail: line 42" in data["evidence_excerpts"][0]["content"]
    assert data["evidence_excerpts"][0]["truncated"]


def test_checkpoint_budget_accounts_for_fixed_state_and_requests_deduplicated_current_state():
    from coding_harness.context import SUMMARY_PROMPT

    def plan_with(tools):
        context = Context(
            "unchanged rules", "original goal", token_limit=2400, keep_recent_turns=1, tools=tools
        )
        context.add_turn(*turn("old", "the same failed attempt " * 600))
        context.add_turn(*turn("latest", "current status"))
        return json.loads(context.plan_compaction().messages[1]["content"])

    normal = plan_with([])
    larger_schema = plan_with([{"type": "function", "description": "schema words " * 50}])
    assert 0 < normal["checkpoint_budget"]["target_model_state_json_utf8_bytes"] <= 3000
    assert (
        larger_schema["checkpoint_budget"]["remaining_model_state_tokens_estimate"]
        < normal["checkpoint_budget"]["remaining_model_state_tokens_estimate"]
    )
    assert normal["checkpoint_budget"]["target_max_next_steps"] == 3
    assert "not a chronological recap" in SUMMARY_PROMPT
    assert "ONE current unresolved issue" in SUMMARY_PROMPT
    assert "Never repeat the Anchor" in SUMMARY_PROMPT
