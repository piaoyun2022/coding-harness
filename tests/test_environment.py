import base64
import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from coding_harness.environment import DockerEnvironment, LocalEnvironment, ModalEnvironment
from coding_harness.worker import run_command
from coding_harness.workspace import prepare_workspace

BASE = "a" * 40


def python_command(code):
    if os.name == "nt":
        return "& '" + sys.executable.replace("'", "''") + "' -c '" + code.replace("'", "''") + "'"
    return shlex.quote(sys.executable) + " -c " + shlex.quote(code)


def test_local_commands_strip_secrets_and_limit_output(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-never-forward")
    command = python_command(
        "import os; print(os.environ.get('DEEPSEEK_API_KEY','absent')); print('x'*2000)"
    )
    result = run_command(tmp_path, command, 10, 400)
    assert result["exit_code"] == 0
    assert result["truncated"] and len(result["stdout"]) <= 400
    assert "absent" in result["stdout"]
    assert "test-secret" not in result["stdout"]


def test_timeout_kills_command(tmp_path):
    result = run_command(tmp_path, python_command("import time; time.sleep(30)"), 0.2)
    assert result["timed_out"]
    assert result["exit_code"] != 0


def test_local_edits_and_cleanup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.py").write_text("old\n")
    work = tmp_path / "work"
    manifest = prepare_workspace(source, work)
    env = LocalEnvironment(work, manifest["base_commit"])
    env.apply_patch("*** Begin Patch\n*** Update File: file.py\n@@\n-old\n+new\n*** End Patch", 10)
    assert "-old" in env.collect_patch() and "+new" in env.collect_patch()
    assert (source / "file.py").read_text() == "old\n"
    env.close()
    env.close()
    with pytest.raises(RuntimeError, match="closed"):
        env.execute("echo nope", 1)


def fake_result(request, *, wrong_base=False, base_status=None):
    if request["operation"] == "execute":
        if request.get("command", "").startswith("git status "):
            return {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False,
                    "truncated": False, **(base_status or {})}
        return {"stdout": ("b" * 40 if wrong_base else BASE) + "\n", "stderr": "",
                "exit_code": 0, "timed_out": False, "truncated": False}
    if request["operation"] == "apply_patch":
        return {"ok": True, "changed_files": ["file.py"]}
    return {"patch": "diff fixture"}


def install_fake_docker(monkeypatch, *, wrong_base=False, rpc_timeout=False, base_status=None):
    calls = []
    requests = []

    def fake(args, *, timeout, data=None, check=True):
        calls.append(args)
        stdout = args[-1] if args[0] == "inspect" else ""
        if data:
            request = json.loads(base64.b64decode(data))
            requests.append(request)
            setup_command = request.get("command", "").startswith(
                ("git rev-parse HEAD", "git status "))
            if rpc_timeout and not setup_command:
                raise subprocess.TimeoutExpired(["docker"], timeout)
            stdout = json.dumps(fake_result(
                request, wrong_base=wrong_base, base_status=base_status))
        return subprocess.CompletedProcess(["docker", *args], 0, stdout, "")

    monkeypatch.setattr(DockerEnvironment, "_docker", staticmethod(fake))
    return calls, requests


def test_docker_runtime_boundary_and_json_stdin(tmp_path, monkeypatch):
    calls, requests = install_fake_docker(monkeypatch)
    env = DockerEnvironment(tmp_path, BASE)
    command = next(call for call in calls if call[0] == "run")
    assert command[command.index("--network") + 1] == "none"
    assert "--memory" in command and "--cpus" in command and "--pids-limit" in command
    assert not any(flag in command for flag in ["-v", "--mount", "-e", "--env"])
    patch = "x" * 100_000
    env.apply_patch(patch, 1)
    assert requests[-1]["patch"] == patch
    assert len(next(call for call in reversed(calls) if call[0] == "exec")) < 12
    env.close()
    env.close()
    assert sum(call[:2] == ["rm", "-f"] for call in calls) == 1


def test_docker_prebuilt_keeps_testbed_and_activates_its_python(tmp_path, monkeypatch):
    calls, requests = install_fake_docker(monkeypatch)
    env = DockerEnvironment(tmp_path, BASE, image="swebench/instance:test", workdir="/testbed")
    assert requests[0]["workspace"] == "/testbed"
    assert requests[0]["activate_testbed"] is True
    assert not any(call[0] == "cp" and call[1].endswith("/.") for call in calls)
    env.close()


def test_docker_failed_setup_cleans_container(tmp_path, monkeypatch):
    calls, _ = install_fake_docker(monkeypatch, wrong_base=True)
    with pytest.raises(ValueError, match="HEAD"):
        DockerEnvironment(tmp_path, BASE)
    assert any(call[:2] == ["rm", "-f"] for call in calls)


def test_docker_client_timeout_terminates_owned_container(tmp_path, monkeypatch):
    calls, _ = install_fake_docker(monkeypatch, rpc_timeout=True)
    env = DockerEnvironment(tmp_path, BASE)
    with pytest.raises(subprocess.TimeoutExpired):
        env.execute("blocked", 1)
    assert env.closed
    assert any(call[:2] == ["rm", "-f"] for call in calls)


def install_fake_modal(monkeypatch, *, wrong_base=False):
    record = {"images": [], "requests": [], "terminated": 0}

    class Image:
        def __getattr__(self, name):
            def chain(*args, **kwargs):
                record["images"].append((name, args, kwargs))
                return self
            return chain

        @classmethod
        def debian_slim(cls, **kwargs):
            record["images"].append(("debian_slim", (), kwargs))
            return cls()

        @classmethod
        def from_registry(cls, *args, **kwargs):
            record["images"].append(("from_registry", args, kwargs))
            return cls()

    class Process:
        returncode = 0

        def __init__(self):
            self.encoded = ""
            self.stdin = SimpleNamespace(
                write=self.write, write_eof=lambda: None, drain=lambda: None
            )
            self.stdout = SimpleNamespace(read=self.read)
            self.stderr = SimpleNamespace(read=lambda: "")

        def write(self, data):
            self.encoded += data

        def read(self):
            request = json.loads(base64.b64decode(self.encoded))
            record["requests"].append(request)
            return json.dumps(fake_result(request, wrong_base=wrong_base))

        def wait(self):
            return None

    class Sandbox:
        @classmethod
        def create(cls, *args, **kwargs):
            record["create"] = (args, kwargs)
            return cls()

        def exec(self, *args, **kwargs):
            record["exec"] = (args, kwargs)
            return Process()

        def terminate(self, **kwargs):
            record["terminated"] += 1

    module = SimpleNamespace(Image=Image, Sandbox=Sandbox,
                             App=SimpleNamespace(lookup=lambda *args, **kwargs: object()))
    monkeypatch.setitem(sys.modules, "modal", module)
    return record


def test_modal_prebuilt_and_no_host_secrets(tmp_path, monkeypatch):
    record = install_fake_modal(monkeypatch)
    env = ModalEnvironment(tmp_path, BASE, image="swebench/instance:test", workdir="/testbed")
    assert any(item[0] == "from_registry" for item in record["images"])
    assert not any(item[0] == "add_local_dir" for item in record["images"])
    create = record["create"][1]
    assert create["block_network"] is True
    assert "secrets" not in create and "env" not in create
    assert env.apply_patch("patch", 2)["ok"]
    env.close()
    env.close()
    assert record["terminated"] == 1


def test_modal_setup_failure_terminates(tmp_path, monkeypatch):
    record = install_fake_modal(monkeypatch, wrong_base=True)
    with pytest.raises(ValueError, match="HEAD"):
        ModalEnvironment(tmp_path, BASE)
    assert record["terminated"] == 1


@pytest.mark.parametrize("workdir,image", [
    ("/workspace;bad", None), ("/", None), ("/work", "bad\nRUN evil"),
])
def test_image_inputs_rejected_before_start(tmp_path, workdir, image):
    with pytest.raises(ValueError):
        DockerEnvironment(tmp_path, BASE, image=image, workdir=workdir)



def test_docker_run_unknown_outcome_cleans_only_owned_container(tmp_path, monkeypatch):
    calls, _ = install_fake_docker(monkeypatch)
    normal = DockerEnvironment._docker

    def uncertain(args, **kwargs):
        if args[0] == "run":
            calls.append(args)
            raise subprocess.TimeoutExpired(["docker", "run"], 120)
        return normal(args, **kwargs)

    monkeypatch.setattr(DockerEnvironment, "_docker", staticmethod(uncertain))
    with pytest.raises(subprocess.TimeoutExpired):
        DockerEnvironment(tmp_path, BASE)
    assert any(call[:2] == ["rm", "-f"] for call in calls)


def test_docker_does_not_remove_other_owner(tmp_path, monkeypatch):
    calls, _ = install_fake_docker(monkeypatch)
    normal = DockerEnvironment._docker

    def foreign_label(args, **kwargs):
        if args[0] == "inspect":
            calls.append(args)
            return subprocess.CompletedProcess(["docker", *args], 0, "another-owner", "")
        return normal(args, **kwargs)

    monkeypatch.setattr(DockerEnvironment, "_docker", staticmethod(foreign_label))
    env = DockerEnvironment(tmp_path, BASE)
    env.close()
    assert not any(call[:2] == ["rm", "-f"] for call in calls)


@pytest.mark.parametrize("base_status", [
    {"stdout": " M already-fixed.py\n"},
    {"stdout": "?? hidden-grading-tests.py\n"},
    {"exit_code": 128},
    {"truncated": True},
    {"timed_out": True},
])
def test_prebuilt_image_requires_clean_complete_baseline(tmp_path, monkeypatch, base_status):
    calls, requests = install_fake_docker(monkeypatch, base_status=base_status)
    with pytest.raises(ValueError, match="clean base"):
        DockerEnvironment(tmp_path, BASE, image="swebench/instance:test", workdir="/testbed")
    assert any(request.get("command", "").startswith("git status ") for request in requests)
    assert any(call[:2] == ["rm", "-f"] for call in calls)
