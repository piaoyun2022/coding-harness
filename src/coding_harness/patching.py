"""Codex patch envelope, backed by the SDK's exported text-diff implementation."""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

MAX_PATCH_BYTES = 256 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
HEADER = re.compile(r"\*\*\* (Add|Update|Delete) File: (.+)")
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
}


def _is_link(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    )


def _path(root: Path, name: str) -> Path:
    normalized = name.replace("\\", "/")
    parts = normalized.split("/")
    if (not name or normalized.startswith("/") or any(
            part in {"", ".", ".."} or part.lower() == ".git" or ":" in part
            or part.rstrip(" .") != part or part.split(".")[0].upper() in RESERVED
            or any(ord(c) < 32 for c in part) for part in parts)):
        raise ValueError("Patch paths must be ordinary relative paths inside the workspace")
    current = root
    for index, part in enumerate(parts):
        current = current / part
        if _is_link(current):
            raise ValueError("Patch paths cannot cross symlinks or reparse points")
        if current.exists() and index < len(parts) - 1 and not current.is_dir():
            raise ValueError("A patch parent is not a directory")
    if not current.resolve().is_relative_to(root):
        raise ValueError("Patch path escapes the workspace")
    if current.exists() and not current.is_file():
        raise ValueError("Patch target is not a regular file")
    return current


def _parse(patch: str) -> list[tuple[str, str, str | None, str]]:
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise ValueError("Patch exceeds 256 KiB")
    lines = patch.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Expected a complete Begin Patch / End Patch envelope")
    operations, index = [], 1
    while index < len(lines) - 1:
        match = HEADER.fullmatch(lines[index])
        if match is None:
            raise ValueError("Expected Add File, Update File or Delete File")
        kind, name = match.groups()
        index += 1
        move_to = None
        if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
            if kind != "Update":
                raise ValueError("Only Update File can use Move to")
            move_to = lines[index][len("*** Move to: "):]
            index += 1
        body = []
        while index < len(lines) - 1 and not HEADER.fullmatch(lines[index]):
            line = lines[index]
            if line.startswith("***") and line != "*** End of File":
                raise ValueError("Unexpected patch marker")
            if body and body[-1] == "*** End of File":
                raise ValueError("End of File must be the final line in its update")
            body.append(line)
            index += 1
        if kind == "Delete" and body:
            raise ValueError("Delete File cannot contain a diff")
        if kind == "Add" and any(not line.startswith("+") for line in body):
            raise ValueError("Add File lines must start with +")
        if kind == "Update":
            if not body and move_to is None:
                raise ValueError("Update File requires a diff")
            if any(line and line[0] not in " +-" and line != "@@"
                   and not line.startswith("@@ ") and line != "*** End of File" for line in body):
                raise ValueError("Invalid update diff line")
        operations.append((kind, name, move_to, "\n".join(body)))
        if len(operations) > 100:
            raise ValueError("Too many patch operations")
    return operations


def _replace(path: Path, data: bytes, mode: int | None) -> None:
    """Replace one file without exposing a half-written file to readers."""
    descriptor, temporary = tempfile.mkstemp(prefix=".harness-patch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_patch(workspace: Path, patch: str) -> dict:
    """Validate every operation before writing; return status and changed relative paths."""
    from agents import apply_diff

    workspace = Path(workspace)
    if _is_link(workspace):
        raise ValueError("Workspace cannot be a symlink or reparse point")
    root = workspace.resolve(strict=True)
    operations = _parse(patch)
    if not operations:
        raise ValueError("Empty patch")
    planned: dict[Path, bytes | None] = {}
    originals: dict[Path, tuple[bytes | None, int | None]] = {}
    claimed: set[Path] = set()
    for kind, name, move_to, diff in operations:
        source = _path(root, name)
        target = _path(root, move_to) if move_to is not None else source
        paths = {source, target}
        if claimed & paths or any(
                p != q and (p.is_relative_to(q) or q.is_relative_to(p))
                for p in paths for q in claimed | paths):
            raise ValueError("Duplicate or conflicting patch paths")
        claimed.update(paths)
        for path in paths:
            if path.exists():
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError("Patch target exceeds 5 MiB")
                originals[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            else:
                originals[path] = (None, None)
        old = originals[source][0]
        if kind == "Add":
            if old is not None:
                raise ValueError("Add File target already exists")
            content = apply_diff("", diff, mode="create")
            planned[source] = (content + "\n" if diff else "").encode("utf-8")
        else:
            if old is None:
                raise ValueError("Update/Delete source does not exist")
            if kind == "Delete":
                planned[source] = None
            else:
                if target != source and originals[target][0] is not None:
                    raise ValueError("Move destination already exists")
                content = apply_diff(old.decode("utf-8"), diff) if diff else old.decode("utf-8")
                if target != source:
                    planned[source] = None
                    originals[target] = (None, originals[source][1])
                planned[target] = content.encode("utf-8")
        if any(data is not None and len(data) > MAX_FILE_BYTES for data in planned.values()):
            raise ValueError("Resulting file exceeds 5 MiB")

    # Recheck against concurrent changes before the first write. This is not an OS sandbox.
    for path, (before, _) in originals.items():
        _path(root, path.relative_to(root).as_posix())
        now = path.read_bytes() if path.exists() else None
        if now != before:
            raise ValueError("Workspace changed while validating the patch")
    created_directories: list[Path] = []
    try:
        for path, after in planned.items():
            if after is None:
                path.unlink()
                continue
            missing, parent = [], path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)
            _replace(path, after, originals[path][1])
    except BaseException:
        for path, (before, mode) in originals.items():
            if before is None:
                if path.exists():
                    path.unlink()
            else:
                _replace(path, before, mode)
        for directory in reversed(created_directories):
            directory.rmdir()
        raise
    return {"ok": True, "changed_files": [p.relative_to(root).as_posix() for p in planned]}
