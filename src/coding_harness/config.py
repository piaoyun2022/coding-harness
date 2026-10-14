"""Validated per-task limits; secrets are never part of settings."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    reasoning_effort: Literal["none", "low", "high", "max"] = "high"
    max_steps: int = Field(default=30, ge=1, le=1000)
    max_seconds: int = Field(default=900, ge=1)
    max_total_tokens: int = Field(default=100_000, ge=1)
    context_tokens: int = Field(default=24_000, ge=1000)
    model_context_tokens: int = Field(default=128_000, ge=1000)
    context_margin_tokens: int = Field(default=1024, ge=0)
    recent_context_tokens: int = Field(default=6000, ge=1)
    keep_recent_turns: int = Field(default=2, ge=1)
    checkpoint_output_tokens: int = Field(default=2048, ge=128)
    max_output_tokens: int = Field(default=8192, ge=128)
    command_timeout: int = Field(default=60, ge=1, le=600)
    request_timeout: int = Field(default=90, ge=1)
    max_output_chars: int = Field(default=12_000, ge=200)
    execution_capture_bytes: int = Field(default=1_048_576, ge=1, le=4_194_304)
    evidence_max_item_bytes: int = Field(default=16_777_216, ge=1)
    evidence_max_total_bytes: int = Field(default=67_108_864, ge=1)

    @model_validator(mode="after")
    def usable_window(self):
        reserve = max(self.max_output_tokens, self.checkpoint_output_tokens)
        if reserve + self.context_margin_tokens >= self.model_context_tokens:
            raise ValueError("Model window must leave space for input after output and margin")
        return self

    def input_budget(self, output_tokens: int, *, action: bool = True) -> int:
        """Action soft limit; summaries may use the remaining configured hard window."""
        available = self.model_context_tokens - output_tokens - self.context_margin_tokens
        return min(self.context_tokens, available) if action else available
