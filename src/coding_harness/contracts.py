"""Small shared contracts; no component owns another component's lifecycle."""
from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return asdict(self) | {"total_tokens": self.total_tokens}

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens

@dataclass
class Reply:
    message: dict[str, Any]
    usage: Usage
    finish_reason: str = "stop"

@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    truncated: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

class Model(Protocol):
    def complete(self, messages: list[dict], tools: list[dict], *,
                 timeout: float, max_output_tokens: int | None = None,
                 purpose: str = "action") -> Reply: ...

class Environment(Protocol):
    description: str
    def execute(self, command: str, timeout: float) -> CommandResult: ...
    def apply_patch(self, patch: str, timeout: float) -> dict: ...
    def collect_patch(self) -> str: ...
    def close(self) -> None: ...
