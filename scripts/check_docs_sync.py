"""Enforce code/design documentation updates against the same Git snapshot."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

MAP = "docs/code-map.json"
PREFIXES = ("src/", "tests/", "scripts/", ".github/", ".githooks/")
ROOT_FILES = {".env.example", ".gitignore", ".gitattributes", "Dockerfile", "Makefile"}
CONFIG_SUFFIXES = {".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".lock", ".ps1", ".sh"}


def git(root: Path, *args: str, allow_failure: bool = False) -> bytes | None:
    process = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30)
    if process.returncode:
        if allow_failure:
            return None
        raise ValueError(process.stderr.decode(errors="replace").strip())
    return process.stdout


def paths(raw: bytes | None) -> set[str]:
    return {x.decode("utf-8") for x in (raw or b"").split(b"\0") if x}


def managed(path: str) -> bool:
    if path.startswith(PREFIXES):
        return True
    if "/" in path:
        return False
    return (
        path in ROOT_FILES or PurePosixPath(path).suffix in CONFIG_SUFFIXES
        or path.startswith("requirements") and path.endswith(".txt")
    )


def valid_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and not pure.is_absolute() and ".." not in pure.parts and "\\" not in path


class Snapshot:
    """Read paths and content from worktree, Git index, a commit, or the empty tree."""

    def __init__(self, root: Path, kind: str, revision: str | None = None):
        self.root, self.kind, self.revision = root, kind, revision
        if kind == "empty":
            self.files: set[str] = set()
        elif kind == "commit":
            self.files = paths(git(root, "ls-tree", "-r", "--name-only", "-z", revision))
        else:
            self.files = paths(git(root, "ls-files", "--cached", "-z"))
            if kind == "worktree":
                self.files |= paths(git(root, "ls-files", "--others", "--exclude-standard", "-z"))
                self.files = {p for p in self.files if (root / p).is_file()}

    def read(self, path: str) -> bytes | None:
        if path not in self.files:
            return None
        if self.kind == "worktree":
            return (self.root / path).read_bytes()
        prefix = "" if self.kind == "index" else self.revision
        return git(self.root, "show", f"{prefix}:{path}")

    def mapping(self, *, required: bool) -> dict[str, list[str]]:
        raw = self.read(MAP)
        if raw is None:
            if required:
                raise ValueError(f"Missing {MAP} in {self.kind}")
            return {}
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"{MAP} must be an object mapping exact code paths to documents")
        mapping = {}
        for code, docs in value.items():
            if not isinstance(code, str) or not valid_path(code) or not managed(code):
                raise ValueError(f"Invalid code path in {MAP}: {code!r}")
            docs = [docs] if isinstance(docs, str) else docs
            if not isinstance(docs, list) or not docs or any(
                not isinstance(doc, str) or not valid_path(doc)
                or not doc.startswith("docs/") or not doc.endswith(".md") for doc in docs
            ):
                raise ValueError(f"Invalid documentation mapping for {code}")
            mapping[code] = docs
        return mapping


def substantive(raw: bytes | None) -> str:
    return re.sub(r"\s+", "", (raw or b"").decode("utf-8-sig"))


def check(root: Path, *, staged: bool = False, base: str | None = None) -> list[str]:
    head = git(root, "rev-parse", "--verify", "HEAD", allow_failure=True)
    head_ref = head.decode().strip() if head else None
    if base is not None:
        if head_ref is None:
            raise ValueError("--base requires a committed HEAD")
        current = Snapshot(root, "commit", head_ref)
        if base.upper() == "EMPTY" or re.fullmatch(r"0{40,64}", base):
            previous = Snapshot(root, "empty")
        else:
            previous_ref = git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
            previous = Snapshot(root, "commit", previous_ref.decode().strip())
    else:
        previous = Snapshot(root, "commit", head_ref) if head_ref else Snapshot(root, "empty")
        current = Snapshot(root, "index" if staged else "worktree")
        if staged and git(root, "ls-files", "--unmerged", "-z"):
            raise ValueError("Resolve unmerged index entries before committing")

    old_map = previous.mapping(required=False)
    new_map = current.mapping(required=any(managed(p) for p in current.files))
    errors = []
    for code in sorted(p for p in current.files if managed(p)):
        if code not in new_map:
            errors.append(f"Unmapped code/config file: {code}")
    for code, docs in sorted(new_map.items()):
        if code not in current.files:
            errors.append(f"Stale mapping for missing code file: {code}")
        for doc in docs:
            if not substantive(current.read(doc)):
                errors.append(f"Missing or empty document in {current.kind}: {doc} (for {code})")

    for code in sorted(p for p in previous.files | current.files if managed(p)):
        if previous.read(code) == current.read(code):
            continue
        docs = set(old_map.get(code, [])) | set(new_map.get(code, []))
        if not docs:
            errors.append(f"Changed code has no old or new documentation mapping: {code}")
            continue
        if code in previous.files and code not in current.files and code not in old_map:
            errors.append(f"Deleted file lacks a historical documentation mapping: {code}")
        for doc in sorted(docs):
            if substantive(previous.read(doc)) == substantive(current.read(doc)):
                errors.append(f"Documentation must change with {code}: {doc}")

    # Redirecting a mapping cannot silently abandon an existing design document.
    for code in sorted(old_map.keys() & new_map.keys()):
        if set(old_map[code]) != set(new_map[code]):
            for doc in sorted(set(old_map[code]) | set(new_map[code])):
                if substantive(previous.read(doc)) == substantive(current.read(doc)):
                    errors.append(f"Remapping {code} requires updating: {doc}")
    return list(dict.fromkeys(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="Check HEAD versus the Git index only")
    mode.add_argument("--base", help="Compare REF with HEAD; EMPTY handles an initial commit")
    args = parser.parse_args(argv)
    try:
        root_raw = git(Path.cwd(), "rev-parse", "--show-toplevel")
        root = Path(root_raw.decode().strip())
        errors = check(root, staged=args.staged, base=args.base)
    except (ValueError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        print(f"Documentation check failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("Documentation check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Documentation check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
