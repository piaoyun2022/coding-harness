import base64
import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from coding_harness.environment import DockerEnvironment, LocalEnvironment, ModalEnvironment
from coding_harness.worker import DEFAULT_CAPTURE_LIMIT, run_command
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
    result = run_command(tmp_path, command, 10, 400, capture_limit=400)
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


def test_local_returns_middle_evidence_before_preview(tmp_path):
    env = LocalEnvironment(tmp_path, BASE, output_limit=100, capture_limit=200_000)
    command = python_command(
        "import sys; print('a'*30000 + 'MIDDLE_EVIDENCE' + 'z'*30000); "
        "sys.stderr.write('b'*10000 + 'STDERR_EVIDENCE' + 'y'*10000)"
    )
    try:
        result = env.execute(command, 20)
        assert result.exit_code == 0 and not result.truncated
        assert len(result.stdout) > 60_000 and 'MIDDLE_EVIDENCE' in result.stdout
        assert len(result.stderr) > 20_000 and 'STDERR_EVIDENCE' in result.stderr
        assert 'output truncated' not in result.stdout
    finally:
        env.close()


def test_capture_drains_both_streams_after_cap(tmp_path):
    # The final file proves both much-larger-than-pipe writes completed without blocking.
    command = python_command(
        "import sys; from pathlib import Path; "
        "sys.stdout.write('o' * (4*1024*1024)); sys.stdout.flush(); "
        "sys.stderr.write('e' * (4*1024*1024)); sys.stderr.flush(); "
        "Path('completed').write_text('done')"
    )
    result = run_command(tmp_path, command, 30, capture_limit=4096)
    assert result['exit_code'] == 0 and not result['timed_out']
    assert result['truncated']
    assert 0 < len(result['stdout']) <= 4096
    assert 0 < len(result['stderr']) <= 4096
    assert (tmp_path / 'completed').read_text() == 'done'


def test_default_capture_cap_is_independent_of_preview(tmp_path):
    command = python_command(f"print('x' * {DEFAULT_CAPTURE_LIMIT + 1000})")
    result = run_command(tmp_path, command, 20, output_limit=100)
    assert result['exit_code'] == 0 and result['truncated']
    assert len(result['stdout']) == DEFAULT_CAPTURE_LIMIT


def test_timeout_preserves_output_and_kills_descendant(tmp_path):
    child_code = "import time; time.sleep(30)"
    code = (
        "import subprocess, sys, time; from pathlib import Path; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "Path('child.pid').write_text(str(child.pid)); "
        "print('OUT_BEFORE_TIMEOUT', flush=True); "
        "print('ERR_BEFORE_TIMEOUT', file=sys.stderr, flush=True); time.sleep(30)"
    )
    result = run_command(tmp_path, python_command(code), 2, capture_limit=10_000)
    assert result['timed_out'] and result['exit_code'] != 0
    assert 'OUT_BEFORE_TIMEOUT' in result['stdout']
    assert 'ERR_BEFORE_TIMEOUT' in result['stderr']
    assert not result['truncated']
    child_pid = int((tmp_path / 'child.pid').read_text())
    if os.name == 'nt':
        probe = subprocess.run(
            ['tasklist', '/FI', f'PID eq {child_pid}', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, check=True, timeout=10,
        )
        assert f'"{child_pid}"' not in probe.stdout
    else:
        # A dead descendant can briefly remain a zombie awaiting its new parent's reap.
        proc_status = __import__('pathlib').Path(f'/proc/{child_pid}/stat')
        if proc_status.exists():
            assert proc_status.read_text().split(') ', 1)[1].startswith('Z ')
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)


@pytest.mark.parametrize('backend', ['docker', 'modal'])
@pytest.mark.parametrize('capture_limit', [256, 100_000])
def test_remote_capture_rpc_preserves_worker_result(tmp_path, monkeypatch, backend, capture_limit):
    original = fake_result

    def execute_locally(request, **kwargs):
        if request.get('command', '').startswith(('git rev-parse HEAD', 'git status ')):
            return original(request, **kwargs)
        assert request['output_limit'] == 100
        assert request['capture_limit'] == capture_limit
        return run_command(tmp_path, request['command'], request['timeout'],
                           request['output_limit'], capture_limit=request['capture_limit'])

    monkeypatch.setattr(sys.modules[__name__], 'fake_result', execute_locally)
    if backend == 'docker':
        install_fake_docker(monkeypatch)
        cls = DockerEnvironment
    else:
        install_fake_modal(monkeypatch)
        cls = ModalEnvironment
    env = cls(tmp_path, BASE, output_limit=100, capture_limit=capture_limit)
    try:
        result = env.execute(python_command("print('a'*20000 + 'MIDDLE' + 'z'*20000)"), 20)
        assert result.exit_code == 0 and not result.timed_out
        assert result.truncated == (capture_limit == 256)
        if capture_limit == 256:
            assert len(result.stdout) <= capture_limit
        else:
            assert len(result.stdout) > 40_000 and 'MIDDLE' in result.stdout
    finally:
        env.close()


@pytest.mark.parametrize('capture_limit', [0, -1, True, 1.5])
def test_invalid_capture_limit_fails_before_execution(tmp_path, monkeypatch, capture_limit):
    calls, _ = install_fake_docker(monkeypatch)
    modal = install_fake_modal(monkeypatch)
    for cls in (LocalEnvironment, DockerEnvironment, ModalEnvironment):
        with pytest.raises(ValueError, match='Capture limit'):
            cls(tmp_path, BASE, capture_limit=capture_limit)
    with pytest.raises(ValueError, match='Capture limit'):
        run_command(tmp_path, 'must not execute', 1, capture_limit=capture_limit)
    assert not calls and 'create' not in modal


def test_standalone_worker_rpc_retains_middle(tmp_path):
    from coding_harness import worker

    request = {
        'operation': 'execute', 'workspace': str(tmp_path), 'timeout': 20,
        'command': python_command("print('a'*20000 + 'MIDDLE_RPC' + 'z'*20000)"),
        'output_limit': 100, 'capture_limit': 100_000,
    }
    result = subprocess.run(
        [sys.executable, worker.__file__, '-'],
        input=base64.b64encode(json.dumps(request).encode()).decode(),
        text=True, capture_output=True, timeout=30, check=True,
    )
    response = json.loads(result.stdout)
    assert response['exit_code'] == 0 and not response['truncated']
    assert 'MIDDLE_RPC' in response['stdout'] and len(response['stdout']) > 40_000
