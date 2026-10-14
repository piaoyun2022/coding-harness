"""Schemas and recoverable dispatch for the two execution tools."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from .contracts import Environment


class ExecuteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Annotated[StrictStr, Field(min_length=1)]
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=600)] | None = None


class PatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch: Annotated[StrictStr, Field(min_length=1, max_length=256 * 1024)]


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: Annotated[StrictStr, Field(min_length=1)]


TOOLS = [
    {"type": "function", "function": {
        "name": "execute",
        "description": "Run a fresh shell in the workspace to read/search files and run tests. "
                       "Shell state does not persist. Use apply_patch for code edits.",
        "parameters": ExecuteArgs.model_json_schema(),
    }},
    {"type": "function", "function": {
        "name": "apply_patch",
        "description": "Edit UTF-8 files using Codex patch syntax: *** Begin Patch, "
                       "*** Add File: path (+ lines), *** Update File: path (@@ hunks), "
                       "*** Delete File: path, optional *** Move to: path after Update, "
                       "then *** End Patch. Use relative workspace paths.",
        "parameters": PatchArgs.model_json_schema(),
    }},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Declare completion and summarize edits and tests. Call alone. "
                       "This does not certify that independent evaluation passes.",
        "parameters": FinishArgs.model_json_schema(),
    }},
]


def dispatch(environment: Environment, name: str, args_json: str, timeout: float) -> dict:
    """No automatic retry: a failed write may already have executed outside our process."""
    try:
        if timeout <= 0:
            return {"ok": False, "error": "Execution time budget exhausted"}
        if name == "execute":
            args = ExecuteArgs.model_validate_json(args_json)
            budget = min(timeout, args.timeout_seconds or timeout)
            result = environment.execute(args.command, budget).as_dict()
            return {"ok": True, **result}
        if name == "apply_patch":
            args = PatchArgs.model_validate_json(args_json)
            return environment.apply_patch(args.patch, timeout)
        return {"ok": False, "error": "Unknown execution tool"}
    except ValidationError as error:
        issues = error.errors(include_url=False, include_context=False, include_input=False)
        return {"ok": False, "error": "Invalid tool arguments", "details": issues}
    except (ValueError, OSError) as error:
        return {"ok": False, "error": str(error)[:1000]}
    except Exception as error:
        return {"ok": False, "error": f"Tool execution failed: {type(error).__name__}"}
