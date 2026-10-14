"""Validated per-task limits; secrets are never part of settings."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    reasoning_effort: Literal["none", "low", "high", "max"] = "high"
    max_steps: int = Field(default=30, ge=1, le=1000)
    max_seconds: int = Field(default=900, ge=1)
    max_total_tokens: int = Field(default=100_000, ge=1)
    context_tokens: int = Field(default=24_000, ge=1000)
    keep_recent_turns: int = Field(default=2, ge=1)
    max_output_tokens: int = Field(default=8192, ge=128)
    command_timeout: int = Field(default=60, ge=1, le=600)
    request_timeout: int = Field(default=90, ge=1)
    max_output_chars: int = Field(default=12_000, ge=200)
