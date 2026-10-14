"""Append-only, secret-redacted events and human-inspectable artifacts."""
import json
import time
from pathlib import Path
from typing import Any


class Trace:
    def __init__(self, directory: Path, secrets: tuple[str, ...] = ()):
        self.directory = directory
        self.secrets = tuple(value for value in secrets if value)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "trajectory.jsonl"

    def clean(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def event(self, kind: str, **fields: Any) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(self.clean(json.dumps(
                {"time": time.time(), "event": kind, **fields}, ensure_ascii=False
            )) + "\n")

    def result(self, value: dict) -> None:
        self.write_json("result.json", value)

    def write_json(self, name: str, value: dict) -> None:
        (self.directory / name).write_text(
            self.clean(json.dumps(value, ensure_ascii=False, indent=2)) + "\n",
            encoding="utf-8",
        )
