"""Run-scoped, redacted evidence with opaque IDs and bounded reads."""
from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from pathlib import Path

MAX_READ_CHARS = 16_000
EVIDENCE_ID_PATTERN = r"^ev_[0-9a-f]{32}$"


class EvidenceError(ValueError):
    """A safe-to-display storage failure; never includes content or host paths."""


class EvidenceStore:
    """Keep immutable evidence for this instance, without reopening earlier runs."""

    def __init__(self, directory: Path, *, redactor: Callable[[str], str],
                 max_item_bytes: int = 1_048_576, max_total_bytes: int = 33_554_432):
        if any(type(value) is not int or value < 1
               for value in (max_item_bytes, max_total_bytes)):
            raise ValueError("Evidence byte limits must be positive integers")
        if not callable(redactor):
            raise ValueError("Evidence redactor must be callable")
        self._directory = Path(directory)
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise EvidenceError("Cannot create evidence storage") from error
        self._redactor = redactor
        self._max_item_bytes = max_item_bytes
        self._max_total_bytes = max_total_bytes
        self._total_bytes = 0
        self._items: dict[str, dict] = {}

    def store(self, text: str) -> dict:
        """Redact before hashing/writing; truncate one item or reject total overflow."""
        if not isinstance(text, str):
            raise EvidenceError("Evidence content must be text")
        try:
            clean = self._redactor(text)
            if not isinstance(clean, str):
                raise TypeError
            encoded = clean.encode("utf-8")
        except Exception as error:
            raise EvidenceError("Cannot redact or encode evidence") from error
        truncated = len(encoded) > self._max_item_bytes
        if truncated:
            clean = encoded[:self._max_item_bytes].decode("utf-8", errors="ignore")
            encoded = clean.encode("utf-8")
        if self._total_bytes + len(encoded) > self._max_total_bytes:
            raise EvidenceError("Evidence total byte limit exceeded")
        evidence_id = "ev_" + uuid.uuid4().hex
        metadata = {"id": evidence_id, "sha256": hashlib.sha256(encoded).hexdigest(),
                    "chars": len(clean), "bytes": len(encoded), "truncated": truncated}
        path = self._directory / (evidence_id + ".txt")
        created = False
        try:
            with path.open("xb") as stream:
                created = True
                stream.write(encoded)
        except OSError as error:
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise EvidenceError("Cannot write evidence") from error
        self._items[evidence_id] = metadata
        self._total_bytes += len(encoded)
        return metadata.copy()

    def contains(self, evidence_id: str) -> bool:
        """Only IDs registered by this store instance are valid references."""
        return (isinstance(evidence_id, str)
                and re.fullmatch(EVIDENCE_ID_PATTERN, evidence_id) is not None
                and evidence_id in self._items)

    def read(self, evidence_id: str, start: int = 0, limit: int = 4000) -> dict:
        """Page by character offset; verify immutable stored bytes before returning."""
        if not self.contains(evidence_id):
            return {"ok": False, "error": "Unknown evidence id"}
        if (type(start) is not int or start < 0 or type(limit) is not int
                or not 1 <= limit <= MAX_READ_CHARS):
            return {"ok": False, "error": "Invalid evidence read range"}
        metadata = self._items[evidence_id]
        if start > metadata["chars"]:
            return {"ok": False, "error": "Evidence start exceeds stored length"}
        path = self._directory / (evidence_id + ".txt")
        try:
            if path.is_symlink() or not path.is_file():
                return {"ok": False, "error": "Evidence unavailable"}
            with path.open("rb") as stream:
                encoded = stream.read(metadata["bytes"] + 1)
            if (len(encoded) != metadata["bytes"]
                    or hashlib.sha256(encoded).hexdigest() != metadata["sha256"]):
                return {"ok": False, "error": "Evidence integrity check failed"}
            content = encoded.decode("utf-8")
        except (OSError, UnicodeError):
            return {"ok": False, "error": "Evidence unavailable"}
        end = min(start + limit, len(content))
        return {"ok": True, "id": evidence_id, "content": content[start:end],
                "start": start, "next_start": end if end < len(content) else None,
                "total_chars": len(content),
                "truncated": metadata["truncated"] or end < len(content)}
