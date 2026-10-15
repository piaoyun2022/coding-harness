"""Exercise the real shell path-selection function without creating a venv or running pip."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/evaluate.sh"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Bootstrap executes in Linux/WSL")


def selected(repo, kernel, *, cache, override=None):
    env = os.environ | {"XDG_CACHE_HOME": str(cache)}
    env.pop("HARNESS_EVAL_VENV", None)
    if override is not None:
        env["HARNESS_EVAL_VENV"] = override
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; select_eval_venv "$2" "$3" "$4"',
         "--", str(SCRIPT), repo, kernel, sys.executable],
        check=True, capture_output=True, text=True, env=env,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("repo,kernel", [
    ("/work/repository", "6.8.0-linux"),
    ("/mnt/c/work/repository", "6.8.0-linux"),
    ("/work/repository", "6.6.0-microsoft-standard-WSL2"),
])
def test_native_linux_or_linux_filesystem_uses_checkout(repo, kernel, tmp_path):
    assert selected(repo, kernel, cache=tmp_path) == repo + "/.harness/eval-venv"
    assert not list(tmp_path.iterdir())


def test_wsl_windows_checkout_uses_distinct_linux_cache(tmp_path):
    for repo in ("/mnt/c/My Work/repo", "/mnt/d/My Work/repo"):
        expected = hashlib.sha256(repo.encode()).hexdigest()[:20]
        actual = selected(repo, "6.6.0-microsoft-standard-WSL2", cache=tmp_path)
        assert actual == str(tmp_path / "coding-harness/eval-venvs" / expected)
    assert not list(tmp_path.iterdir())


def test_explicit_override_wins_without_shell_expansion(tmp_path):
    override = "/tmp/custom environment/$(should-not-run)"
    assert selected("/mnt/c/repo", "microsoft", cache=tmp_path, override=override) == override


def test_shell_parses_and_sourcing_does_not_install():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    result = subprocess.run(["bash", "-c", 'source "$1"', "--", str(SCRIPT)],
                            check=True, capture_output=True, text=True)
    assert result.stdout == ""

