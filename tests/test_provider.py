import json

import httpx
import pytest
from openai import OpenAI

from coding_harness.config import Settings
from coding_harness.provider import DeepSeekModel


def transport_client(capture, response):
    def handle(request):
        capture.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    return OpenAI(
        api_key="offline-test-key",
        base_url="https://example.invalid",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )


def response(message):
    return {
        "id": "offline",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
            "prompt_cache_hit_tokens": 5,
        },
    }


def test_sdk_roundtrip_preserves_reasoning_tool_calls_and_usage():
    capture = []
    message = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "retained reasoning",
        "tool_calls": [
            {
                "id": "tool-1",
                "type": "function",
                "function": {"name": "execute", "arguments": '{"command":"pwd"}'},
            }
        ],
    }
    model = DeepSeekModel(
        Settings(),
        "unused",
        client=transport_client(capture, response(message)),
    )
    try:
        first = model.complete([{"role": "user", "content": "task"}], [], timeout=2)
        model.complete([first.message], [], timeout=2, max_output_tokens=123)
        assert capture[1]["messages"][0]["reasoning_content"] == "retained reasoning"
        assert capture[1]["messages"][0]["tool_calls"] == message["tool_calls"]
        assert capture[1]["reasoning_effort"] == "high"
        assert capture[1]["max_tokens"] == 123
        assert first.usage.as_dict() == {
            "input_tokens": 12,
            "output_tokens": 7,
            "cached_tokens": 5,
            "total_tokens": 19,
        }
    finally:
        model.close()


def test_summary_disables_reasoning_and_omits_empty_tools():
    capture = []
    model = DeepSeekModel(
        Settings(),
        "unused",
        client=transport_client(capture, response({"role": "assistant", "content": "summary"})),
    )
    try:
        model.complete([], [], timeout=2, max_output_tokens=2048, purpose="summary")
        assert capture[0]["reasoning_effort"] == "none"
        assert "tools" not in capture[0]
    finally:
        model.close()


def test_empty_choices_are_protocol_error():
    capture = []
    payload = response({"role": "assistant", "content": "unused"})
    payload["choices"] = []
    model = DeepSeekModel(Settings(), "unused", client=transport_client(capture, payload))
    try:
        with pytest.raises(ValueError, match="no choice"):
            model.complete([], [], timeout=2)
    finally:
        model.close()
