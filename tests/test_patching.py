
import pytest

from coding_harness import patching


def patch(body):
    return "*** Begin Patch\n" + body + "\n*** End Patch\n"


def test_add_update_delete_and_move(tmp_path):
    patching.apply_patch(tmp_path, patch("*** Add File: source.py\n+value = 1"))
    assert (tmp_path / "source.py").read_text() == "value = 1\n"
    patching.apply_patch(tmp_path, patch(
        "*** Update File: source.py\n*** Move to: sub/result.py\n@@\n-value = 1\n+value = 2"
    ))
    assert not (tmp_path / "source.py").exists()
    assert (tmp_path / "sub/result.py").read_text() == "value = 2\n"
    patching.apply_patch(tmp_path, patch("*** Delete File: sub/result.py"))
    assert not (tmp_path / "sub/result.py").exists()


def test_invalid_second_operation_does_not_write_first(tmp_path):
    (tmp_path / "old.py").write_text("old\n")
    with pytest.raises(ValueError):
        patching.apply_patch(tmp_path, patch(
            "*** Add File: new.py\n+new\n*** Update File: old.py\n@@\n-missing\n+replacement"
        ))
    assert not (tmp_path / "new.py").exists()
    assert (tmp_path / "old.py").read_text() == "old\n"


@pytest.mark.parametrize("name", [
    "../outside.py", "/outside.py", "C:/outside.py", "nested/../../outside.py",
    ".git/config", "a/.git/config", "a\\..\\outside.py", "file:stream", "NUL", "x. ",
])
def test_unsafe_paths_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        patching.apply_patch(tmp_path, patch(f"*** Add File: {name}\n+bad"))


@pytest.mark.parametrize("body", [
    "*** Add File: file.py\n+one\n*** Add File: file.py\n+two",
    "*** Add File: sub\n+one\n*** Add File: sub/file.py\n+two",
    "*** Update File: file.py\n*** Move to: target.py\n@@\n-old\n+new"
    "\n*** Add File: target.py\n+duplicate",
    "*** Delete File: file.py\n+unexpected",
    "*** Add File: new.py\nnot-prefixed",
    "*** Update File: file.py\n@@\n-old\n+new\n*** End of File\n+trailing",
])
def test_conflicting_or_malformed_patch_is_atomic(tmp_path, body):
    (tmp_path / "file.py").write_text("old\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        patching.apply_patch(tmp_path, patch(body))
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_io_failure_restores_originals(tmp_path, monkeypatch):
    (tmp_path / "one.py").write_text("one\n")
    (tmp_path / "two.py").write_text("two\n")
    original_replace = patching._replace
    count = 0

    def fail_second(path, data, mode):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("simulated write failure")
        original_replace(path, data, mode)

    monkeypatch.setattr(patching, "_replace", fail_second)
    with pytest.raises(OSError, match="simulated"):
        patching.apply_patch(tmp_path, patch(
            "*** Update File: one.py\n@@\n-one\n+ONE\n"
            "*** Update File: two.py\n@@\n-two\n+TWO"
        ))
    assert (tmp_path / "one.py").read_text() == "one\n"
    assert (tmp_path / "two.py").read_text() == "two\n"


def test_symlink_rejected(tmp_path):
    source = tmp_path / "outside"
    source.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating symlinks")
    with pytest.raises(ValueError, match="symlink|reparse"):
        patching.apply_patch(tmp_path, patch("*** Add File: link/file.py\n+bad"))
    assert not (source / "file.py").exists()


def test_reparse_point_rejected_without_host_privilege(tmp_path, monkeypatch):
    (tmp_path / "junction").mkdir()
    real = patching._is_link
    monkeypatch.setattr(patching, "_is_link", lambda path: path.name == "junction" or real(path))
    with pytest.raises(ValueError, match="symlink|reparse"):
        patching.apply_patch(tmp_path, patch("*** Add File: junction/file.py\n+bad"))


def test_patch_size_limit(tmp_path):
    with pytest.raises(ValueError, match="256 KiB"):
        patching.apply_patch(tmp_path, patch("*** Add File: file.py\n+" + "x" * 262145))


def test_sdk_keeps_crlf_for_updates(tmp_path):
    (tmp_path / "file.py").write_bytes(b"old\r\n")
    patching.apply_patch(tmp_path, patch("*** Update File: file.py\n@@\n-old\n+new"))
    assert (tmp_path / "file.py").read_bytes() == b"new\r\n"
