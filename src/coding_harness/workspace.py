"""Copy a filtered source tree, then track its changes against a private Git baseline."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".ssh", ".aws", "artifacts", ".harness",
}
SECRET_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "credentials.json",
                "id_rsa", "id_ed25519"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TREE_BYTES = 100 * 1024 * 1024
MAX_PATCH_BYTES = 10 * 1024 * 1024


def is_link(path: Path) -> bool:
    """Include Windows junctions, not only POSIX symbolic links."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    )


def git(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Use fixed Git behavior without inheriting caller hooks, aliases or credentials."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false",
         "-c", "core.quotepath=false", "-c", "core.fsmonitor=false",
         "-c", "core.untrackedCache=false", "-c", "core.safecrlf=false", *args],
        cwd=workspace, env=env, capture_output=True, check=check, timeout=60,
    )


def _excluded(relative: Path) -> bool:
    name = relative.name.lower()
    return (
        any(part.lower() in SKIP_DIRS for part in relative.parts)
        or name in SECRET_NAMES
        or (name.startswith(".env.") and name != ".env.example")
        or relative.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
    )


def _candidates(source: Path) -> list[Path]:
    probe = git(source, "rev-parse", "--show-toplevel", check=False)
    # An explicitly selected directory owns its input semantics. Parent ignore rules
    # must not silently erase a fixture/subproject that is not itself a Git root.
    is_git_root = (
        probe.returncode == 0
        and Path(os.fsdecode(probe.stdout).strip()).resolve() == source.resolve()
    )
    if is_git_root:
        raw = git(source, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        return sorted({Path(os.fsdecode(p)) for p in raw.stdout.split(b"\0") if p})
    result = []
    for parent, directories, names in os.walk(source, followlinks=False):
        directories[:] = [d for d in directories if d.lower() not in SKIP_DIRS
                          and not is_link(Path(parent) / d)]
        result.extend((Path(parent) / name).relative_to(source) for name in names)
    return sorted(result)


def prepare_workspace(source: Path, destination: Path) -> dict:
    """Create a new filtered copy. Never modify the original, including its Git index."""
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if not source.is_dir() or destination.exists() or is_link(destination):
        raise ValueError("Source must be a directory and destination must not exist")
    destination = destination.resolve()
    if source == destination or source.is_relative_to(destination):
        raise ValueError("Destination cannot contain the source")
    selected, excluded, total = [], [], 0
    for relative in _candidates(source):
        candidate = source / relative
        parents = [candidate, *candidate.parents]
        if (_excluded(relative) or any(is_link(p) for p in parents if p != source)
                or not candidate.is_file() or not candidate.resolve().is_relative_to(source)
                or candidate.resolve().is_relative_to(destination)):
            excluded.append(relative.as_posix())
            continue
        size = candidate.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_TREE_BYTES:
            raise ValueError("Workspace exceeds the configured snapshot size limit")
        total += size
        selected.append(relative)
    if not selected:
        raise ValueError("Source contains no eligible files after workspace filtering")
    destination.mkdir(parents=True)
    for relative in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    git(destination, "init", "--quiet")
    git(destination, "add", "-f", "-A")
    git(destination, "-c", "user.name=Coding Harness", "-c",
        "user.email=harness@localhost", "commit", "--quiet", "--allow-empty",
        "-m", "Input snapshot")
    base_commit = git(destination, "rev-parse", "HEAD").stdout.decode().strip()
    return {"base_commit": base_commit, "files": [p.as_posix() for p in selected],
            "excluded": excluded, "bytes": total}


def collect_patch(workspace: Path, base_commit: str) -> str:
    """Include tracked edits and nonignored new files; return a portable binary Git patch."""
    if not base_commit or any(c not in "0123456789abcdefABCDEF" for c in base_commit):
        raise ValueError("Expected a commit hash")
    # Rebuild only the disposable index, so earlier agent staging cannot smuggle
    # generated caches/secrets into the export. The working tree is untouched.
    git(workspace, "read-tree", base_commit)
    git(workspace, "add", "-u")
    untracked = git(workspace, "ls-files", "--others", "--exclude-standard", "-z").stdout
    for raw in untracked.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        path = workspace / relative
        if (_excluded(relative) or relative.suffix.lower() in {".pyc", ".pyo"}
                or not path.is_file() or is_link(path)
                or any(is_link(parent) for parent in path.parents if parent != workspace)):
            continue
        git(workspace, "add", "--", relative.as_posix())
    patch = git(workspace, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
                base_commit, "--").stdout
    if len(patch) > MAX_PATCH_BYTES:
        raise ValueError("Patch export exceeds 10 MiB")
    return patch.decode("utf-8", errors="strict")
