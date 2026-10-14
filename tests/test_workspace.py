import pytest

from coding_harness.workspace import collect_patch, git, prepare_workspace


def test_snapshot_excludes_secrets_cache_and_gitignore(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--quiet")
    (source / ".gitignore").write_text("ignored.txt\n")
    (source / "ignored.txt").write_text("ignored")
    (source / ".env").write_text("SECRET=fixture")
    (source / ".env.example").write_text("SECRET=")
    (source / ".env.production").write_text("SECRET=fixture")
    (source / "module.py").write_text("value = 1\n")
    (source / "artifacts").mkdir()
    (source / "artifacts/log.txt").write_text("excluded")
    destination = tmp_path / "copy"
    result = prepare_workspace(source, destination)
    assert "module.py" in result["files"]
    assert (destination / ".env.example").exists()
    assert not (destination / ".env").exists()
    assert not (destination / ".env.production").exists()
    assert not (destination / "ignored.txt").exists()
    assert not (destination / "artifacts").exists()
    assert (source / "module.py").read_text() == "value = 1\n"
    assert git(source, "diff", "--cached").stdout == b""


def test_patch_round_trip_includes_new_and_deleted_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.py").write_text("old\n")
    (source / "delete.py").write_text("gone\n")
    workspace = tmp_path / "work"
    replay = tmp_path / "replay"
    result = prepare_workspace(source, workspace)
    prepare_workspace(source, replay)
    (workspace / "old.py").write_text("new\n")
    (workspace / "delete.py").unlink()
    (workspace / "add.py").write_text("added\n")
    text = collect_patch(workspace, result["base_commit"])
    patch_file = tmp_path / "changes.patch"
    patch_file.write_text(text, encoding="utf-8", newline="\n")
    git(replay, "apply", str(patch_file))
    assert (replay / "old.py").read_text() == "new\n"
    assert (replay / "add.py").read_text() == "added\n"
    assert not (replay / "delete.py").exists()
    assert (source / "old.py").read_text() == "old\n"
    assert (source / "delete.py").exists()


def test_existing_destination_rejected(tmp_path):
    with pytest.raises(ValueError, match="must not exist"):
        prepare_workspace(tmp_path, tmp_path)


def test_symlink_excluded(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "secret"
    target.write_text("do not copy")
    (source / "safe.py").write_text("safe = True\n")
    try:
        (source / "link").symlink_to(target)
    except OSError:
        pytest.skip("Host does not permit creating symlinks")
    result = prepare_workspace(source, tmp_path / "copy")
    assert "link" not in result["files"]
    assert not (tmp_path / "copy/link").exists()



def test_tracked_ignored_file_remains_tracked_in_snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--quiet")
    (source / ".gitignore").write_text("tracked.txt\n")
    (source / "tracked.txt").write_text("before\n")
    git(source, "add", "-f", "tracked.txt")
    workspace = tmp_path / "copy"
    manifest = prepare_workspace(source, workspace)
    (workspace / "tracked.txt").write_text("after\n")
    patch = collect_patch(workspace, manifest["base_commit"])
    assert "-before" in patch and "+after" in patch



def test_ignored_directory_inside_parent_repo_is_an_explicit_source(tmp_path):
    parent = tmp_path / "parent-repository"
    parent.mkdir()
    git(parent, "init", "--quiet")
    (parent / ".gitignore").write_text("artifacts/\n")
    source = parent / "artifacts" / "fixture"
    source.mkdir(parents=True)
    (source / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (source / ".env").write_text("TEST_SECRET=never-copy\n")
    destination = tmp_path / "workspace"
    manifest = prepare_workspace(source, destination)
    assert manifest["files"] == ["calc.py"]
    assert manifest["bytes"] > 0
    assert (destination / "calc.py").read_text() == (source / "calc.py").read_text()
    assert not (destination / ".env").exists()
    assert git(parent, "diff", "--cached").stdout == b""


@pytest.mark.parametrize("secret_only", [False, True])
def test_empty_filtered_snapshot_fails_before_creating_destination(tmp_path, secret_only):
    source = tmp_path / "source"
    source.mkdir()
    if secret_only:
        (source / ".env").write_text("TEST_SECRET=never-copy\n")
    destination = tmp_path / "workspace"
    with pytest.raises(ValueError, match="no eligible files"):
        prepare_workspace(source, destination)
    assert not destination.exists()


def test_export_excludes_new_caches_and_secrets_even_when_already_staged(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("before\n")
    workspace = tmp_path / "work"
    manifest = prepare_workspace(source, workspace)
    (workspace / "main.py").write_text("after\n")
    (workspace / "new.py").write_text("new code\n")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "main.pyc").write_bytes(b"cache")
    (workspace / ".env").write_text("FIXTURE_SECRET=do-not-export\n")
    git(workspace, "add", "-A")
    patch = collect_patch(workspace, manifest["base_commit"])
    assert "+after" in patch and "+new code" in patch
    assert "__pycache__" not in patch and ".env" not in patch
    assert "do-not-export" not in patch
    assert (workspace / ".env").exists()
    assert collect_patch(workspace, manifest["base_commit"]) == patch
