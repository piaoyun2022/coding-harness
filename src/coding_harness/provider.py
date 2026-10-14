"""DeepSeek transport only: preserve tool-call and reasoning fields for replay."""

from typing import Any

from openai import OpenAI

from .config import Settings
from .contracts import Reply, Usage


class DeepSeekModel:
    def __init__(self, settings: Settings, api_key: str, *, client: Any = None):
        self.settings = settings
        self.client = client or OpenAI(
            api_key=api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            max_retries=2,
        )

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        timeout: float,
        max_output_tokens: int | None = None,
        purpose: str = "action",
    ) -> Reply:
        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "max_tokens": max_output_tokens or self.settings.max_output_tokens,
            "reasoning_effort": "none" if purpose == "summary" else self.settings.reasoning_effort,
            "timeout": timeout,
        }
        if tools:
            request["tools"] = tools
        response = self.client.chat.completions.create(**request)
        if not response.choices:
            raise ValueError("Model response contains no choice")
        choice = response.choices[0]
        raw = choice.message.model_dump(exclude_none=True)
        if raw.get("role") != "assistant":
            raise ValueError("Model response must be an assistant message")
        message = {
            key: value
            for key, value in raw.items()
            if key in {"role", "content", "tool_calls", "reasoning_content"}
        }
        reported = response.usage
        usage = Usage()
        if reported:
            usage.input_tokens = reported.prompt_tokens or 0
            usage.output_tokens = reported.completion_tokens or 0
            details = getattr(reported, "prompt_tokens_details", None)
            usage.cached_tokens = (
                getattr(reported, "prompt_cache_hit_tokens", None)
                or getattr(details, "cached_tokens", 0)
                or 0
            )
        return Reply(message=message, usage=usage, finish_reason=choice.finish_reason or "stop")

    def close(self) -> None:
        self.client.close()
