"""The policy is checked against real Git snapshots, not mocked filenames."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_docs_sync.py"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def write(root: Path, path: str, content: str) -> None:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def mapping(root: Path, values: dict) -> None:
    write(root, "docs/code-map.json", json.dumps(values))


def run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args], cwd=root,
        capture_output=True, text=True, encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "core.autocrlf", "false")
    write(tmp_path, "src/a.py", "value = 1\n")
    write(tmp_path, "docs/a.md", "A stores an integer.\n")
    mapping(tmp_path, {"src/a.py": "docs/a.md"})
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def test_unchanged_repository_passes(repo):
    assert run(repo).returncode == 0


def test_code_without_documentation_fails(repo):
    write(repo, "src/a.py", "value = 2\n")
    result = run(repo)
    assert result.returncode == 1
    assert "Documentation must change" in result.stderr


def test_worktree_document_does_not_count_for_staged_change(repo):
    write(repo, "src/a.py", "value = 2\n")
    git(repo, "add", "src/a.py")
    write(repo, "docs/a.md", "A now defaults to two.\n")
    result = run(repo, "--staged")
    assert result.returncode == 1
    assert "docs/a.md" in result.stderr
    assert run(repo).returncode == 0
    git(repo, "add", "docs/a.md")
    assert run(repo, "--staged").returncode == 0


def test_staged_snapshot_ignores_later_worktree_reversion(repo):
    write(repo, "src/a.py", "value = 2\n")
    write(repo, "docs/a.md", "A now defaults to two.\n")
    git(repo, "add", "src/a.py", "docs/a.md")
    write(repo, "docs/a.md", "A stores an integer.\n")
    assert run(repo, "--staged").returncode == 0


def test_unknown_new_code_fails_even_when_other_document_changes(repo):
    write(repo, "src/new.py", "other = True\n")
    write(repo, "docs/a.md", "A stores an integer; another module exists.\n")
    result = run(repo)
    assert result.returncode == 1
    assert "Unmapped code/config file: src/new.py" in result.stderr


def test_index_mapping_must_be_staged(repo):
    write(repo, "src/new.py", "other = True\n")
    write(repo, "docs/new.md", "New module owns a flag.\n")
    mapping(repo, {"src/a.py": "docs/a.md", "src/new.py": "docs/new.md"})
    git(repo, "add", "src/new.py", "docs/new.md")
    assert run(repo, "--staged").returncode == 1
    git(repo, "add", "docs/code-map.json")
    assert run(repo, "--staged").returncode == 0


def test_empty_mapping_cannot_bypass_existing_code(repo):
    mapping(repo, {})
    result = run(repo)
    assert result.returncode == 1
    assert "Unmapped" in result.stderr


def test_whitespace_only_document_change_fails(repo):
    write(repo, "src/a.py", "value = 2\n")
    write(repo, "docs/a.md", "  A  stores an integer.\n\n")
    assert run(repo).returncode == 1


def test_deleted_code_requires_its_historical_document(repo):
    git(repo, "rm", "src/a.py")
    mapping(repo, {})
    git(repo, "add", "docs/code-map.json")
    result = run(repo, "--staged")
    assert result.returncode == 1
    assert "docs/a.md" in result.stderr
    write(repo, "docs/a.md", "The integer module has been removed.\n")
    git(repo, "add", "docs/a.md")
    assert run(repo, "--staged").returncode == 0


def test_deleting_component_and_its_document_is_allowed(repo):
    git(repo, "rm", "src/a.py", "docs/a.md")
    mapping(repo, {})
    git(repo, "add", "docs/code-map.json")
    assert run(repo, "--staged").returncode == 0


def test_remapping_cannot_abandon_old_document(repo):
    write(repo, "src/a.py", "value = 2\n")
    write(repo, "docs/new.md", "A now defaults to two.\n")
    mapping(repo, {"src/a.py": "docs/new.md"})
    result = run(repo)
    assert result.returncode == 1
    assert "docs/a.md" in result.stderr
    write(repo, "docs/a.md", "Moved the A contract to new.md.\n")
    assert run(repo).returncode == 0


def test_initial_commit_supported_without_parent(repo):
    assert run(repo, "--base", "EMPTY").returncode == 0
    assert run(repo, "--base", "0" * 40).returncode == 0


def test_base_mode_reads_commits_not_worktree(repo):
    baseline = git(repo, "rev-parse", "HEAD")
    write(repo, "src/a.py", "value = 2\n")
    git(repo, "add", "src/a.py")
    git(repo, "commit", "-m", "missing docs")
    write(repo, "docs/a.md", "Uncommitted explanation must not repair a bad commit.\n")
    assert run(repo, "--base", baseline).returncode == 1
    git(repo, "add", "docs/a.md")
    git(repo, "commit", "-m", "explain behavior")
    assert run(repo, "--base", baseline).returncode == 0


def test_new_repository_staged_initial_snapshot(tmp_path):
    git(tmp_path, "init")
    write(tmp_path, "src/a.py", "value = 1\n")
    write(tmp_path, "docs/a.md", "A stores an integer.\n")
    mapping(tmp_path, {"src/a.py": "docs/a.md"})
    git(tmp_path, "add", ".")
    assert run(tmp_path, "--staged").returncode == 0


@pytest.mark.parametrize("path", [
    "tests/test_new.py", "scripts/new.py", ".github/workflows/new.yml",
    ".githooks/pre-commit", "pyproject.toml", ".env.example", "requirements-lock.txt",
])
def test_all_required_code_and_configuration_surfaces_are_covered(repo, path):
    write(repo, path, "changed")
    result = run(repo)
    assert result.returncode == 1
    assert path in result.stderr
