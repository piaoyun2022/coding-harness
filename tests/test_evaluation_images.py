import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("public_images", SCRIPTS / "evaluation_images.py")
images = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(images)


def info(identity="sha256:new"):
    return {"id": identity, "tag": "source", "repo_digests": ["sha256:digest"],
            "layers": ["first", "second"]}


def docker_fixture(monkeypatch, *, existing=False, referenced=False, preexisting_id=False):
    state = {"present": existing}
    calls = []

    def inspect(tag):
        if tag == "derived":
            return info() | {"layers": ["first", "second", "third"]}
        return info() if state["present"] else None

    def docker(args, **kwargs):
        calls.append(args)
        out = ""
        if args[0] == "images":
            out = "sha256:new\n" if preexisting_id else "sha256:other\n"
        elif args[0] == "pull":
            state["present"] = True
        elif args[:2] == ["ps", "-aq"]:
            out = "container\n" if referenced else ""
        elif args[0] == "inspect":
            out = json.dumps([{"Image": "derived"}])
        return SimpleNamespace(returncode=0, stdout=out)

    monkeypatch.setattr(images, "inspect_image", inspect)
    monkeypatch.setattr(images.DockerEnvironment, "_docker", staticmethod(docker))
    return state, calls


def source():
    return images.SourceImage(SimpleNamespace(instance_id="fixture__case-1"))


def test_source_new_only_cleanup_after_idle_and_exact_id(monkeypatch):
    _, calls = docker_fixture(monkeypatch)
    lease = source()
    lease.acquire()
    assert lease.metadata["pulled"] and lease.owned_id == "sha256:new"
    receipt = lease.close()
    assert receipt["cleanup"] == "removed"
    assert ["image", "rm", "--no-prune", lease.tag] in calls
    assert not any("prune" in args for args in calls)


@pytest.mark.parametrize("existing,preexisting_id", [(True, False), (False, True)])
def test_preexisting_tag_or_id_retained(monkeypatch, existing, preexisting_id):
    _, calls = docker_fixture(monkeypatch, existing=existing, preexisting_id=preexisting_id)
    lease = source()
    lease.acquire()
    assert lease.close()["cleanup"] == "preserved_preexisting_or_unconfirmed"
    assert not any(args[:2] == ["image", "rm"] for args in calls)


def test_overlay_container_reference_prevents_source_cleanup(monkeypatch):
    _, calls = docker_fixture(monkeypatch, referenced=True)
    lease = source()
    lease.acquire()
    assert lease.close()["cleanup"] == "retained_container_reference"
    assert not any(args[:2] == ["image", "rm"] for args in calls)


def test_changed_tag_is_never_deleted(monkeypatch):
    monkeypatch.setattr(images, "inspect_image", lambda _: info("sha256:somebody-else"))
    assert images.remove_exact("tag", "sha256:owned")["state"] == "changed_id_retained"


def test_resource_gate_reports_safe_actionable_message(monkeypatch, tmp_path):
    monkeypatch.setattr(images.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=10 * images.GIB))
    monkeypatch.setattr(images, "available_memory", lambda: 16 * images.GIB)
    with pytest.raises(images.ResourceError, match="--docker-disk"):
        images.check_resources(tmp_path, min_disk_gib=20)
    assert images.check_resources(tmp_path, min_disk_gib=5)["memory_available_bytes"] > 0


def test_memory_gate_separate_from_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(images.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=100 * images.GIB))
    monkeypatch.setattr(images, "available_memory", lambda: 4 * images.GIB)
    with pytest.raises(images.ResourceError):
        images.check_resources(tmp_path, min_disk_gib=20, min_memory_gib=8)


def test_private_overlay_pins_immutable_source_and_only_removes_owned_tags(monkeypatch, tmp_path):
    state = {}
    calls = []
    closed = []
    fixed = info()
    lease = SimpleNamespace(acquire=lambda: fixed)

    def inspect(tag):
        return state.get(tag)

    def docker(args, **kwargs):
        calls.append(args)
        if args[0] == "images":
            return SimpleNamespace(stdout="sha256:new\n", returncode=0)
        if args[0] == "tag":
            state[args[2]] = fixed | {"tag": args[2]}
        if args[:2] == ["image", "rm"]:
            state.pop(args[-1], None)
        return SimpleNamespace(stdout="", returncode=0)

    def initialize(self, workspace, base_commit, **kwargs):
        self.closed = False
        self.actual_runtime = self._runtime_image(kwargs["image"])

    def runtime(self, base):
        assert base.startswith("coding-harness-eval:unique-")
        assert state[base]["id"] == "sha256:new"
        tag = "coding-harness-tools:unique"
        self._docker(["build", "--tag", tag, "fixture"], timeout=1)
        state[tag] = info("sha256:runtime") | {"tag": tag}
        return tag

    monkeypatch.setattr(images, "inspect_image", inspect)
    monkeypatch.setattr(images.DockerEnvironment, "_docker", staticmethod(docker))
    monkeypatch.setattr(images.DockerEnvironment, "__init__", initialize)
    monkeypatch.setattr(images.DockerEnvironment, "_runtime_image", runtime)
    monkeypatch.setattr(images.DockerEnvironment, "close", lambda self: closed.append(self))
    from coding_harness.config import Settings
    task = SimpleNamespace(instance_id="fixture__case-1", base_commit="a" * 40)
    environment = images.private_environment(lease, "unique")(task, tmp_path, Settings())
    assert environment.actual_runtime == "sha256:runtime"
    assert environment.source_metadata["base_image"]["id"] == "sha256:new"
    environment.close()
    removed = [args[-1] for args in calls if args[:2] == ["image", "rm"]]
    assert removed == ["coding-harness-tools:unique", environment.alias]
    assert not any(tag.startswith("swebench/") for tag in removed)
    assert closed and not state


def test_source_verify_refuses_changed_id_before_grading(monkeypatch):
    docker_fixture(monkeypatch)
    lease = source()
    lease.acquire()
    assert lease.verify() == "sha256:new"
    monkeypatch.setattr(images, "inspect_image", lambda _: info("sha256:changed"))
    with pytest.raises(RuntimeError, match="identity changed"):
        lease.verify()

