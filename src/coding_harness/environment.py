"""Execution backends; only trusted fixtures may use the unisolated local backend."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import tempfile
import uuid
from importlib.metadata import version
from pathlib import Path

from .contracts import CommandResult
from .patching import apply_patch
from .worker import DEFAULT_CAPTURE_LIMIT, run_command, validate_capture_limit
from .workspace import collect_patch

WORKER_DIR = "/opt/coding-harness"
IMAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")


class LocalEnvironment:
    """Unisolated backend for explicit trusted local use; CLI must enforce the opt-in."""

    def __init__(self, workspace: Path, base_commit: str, output_limit: int = 12000,
                 capture_limit: int = DEFAULT_CAPTURE_LIMIT):
        validate_capture_limit(capture_limit)
        self.workspace = Path(workspace).resolve(strict=True)
        self.base_commit = base_commit
        self.output_limit = output_limit
        self.capture_limit = capture_limit
        self.closed = False
        self.description = (
            f"Trusted LOCAL workspace {self.workspace}; fresh shell each call. "
            "Use relative paths. This backend is not an OS sandbox."
        )

    def _open(self) -> None:
        if self.closed:
            raise RuntimeError("Environment is closed")

    def execute(self, command: str, timeout: float) -> CommandResult:
        self._open()
        return CommandResult(**run_command(
            self.workspace, command, timeout, self.output_limit, capture_limit=self.capture_limit,
        ))

    def apply_patch(self, patch: str, timeout: float) -> dict:
        self._open()
        if timeout <= 0:
            raise ValueError("Execution time budget exhausted")
        return apply_patch(self.workspace, patch)

    def collect_patch(self) -> str:
        self._open()
        return collect_patch(self.workspace, self.base_commit)

    def close(self) -> None:
        self.closed = True


class _RemoteEnvironment:
    workdir: str
    base_commit: str
    output_limit: int
    capture_limit: int
    closed: bool
    prebuilt: bool

    def _rpc(self, operation: str, timeout: float, **values) -> dict:
        raise NotImplementedError

    def _request(self, operation: str, **values) -> str:
        if self.closed:
            raise RuntimeError("Environment is closed")
        return base64.b64encode(json.dumps(
            {"operation": operation, "workspace": self.workdir, **values}
        ).encode("utf-8")).decode("ascii")

    def _response(self, output: str) -> dict:
        result = json.loads(output)
        if result.get("ok") is False:
            raise ValueError(str(result.get("error", "Worker operation failed"))[:1000])
        return result

    def _verify_base(self) -> None:
        if not re.fullmatch("[0-9a-fA-F]{40}", self.base_commit):
            raise ValueError("Expected a full Git base commit hash")
        result = self.execute("git rev-parse HEAD", 60)
        if (result.exit_code or result.timed_out or result.truncated
                or result.stdout.strip() != self.base_commit):
            raise ValueError("Sandbox repository HEAD does not match the requested base commit")
        status = self.execute("git status --porcelain --untracked-files=normal", 60)
        if status.exit_code or status.timed_out or status.truncated or status.stdout.strip():
            raise ValueError("Sandbox repository must start from a verified clean base")

    def execute(self, command: str, timeout: float) -> CommandResult:
        return CommandResult(**self._rpc(
            "execute", timeout, command=command, timeout=timeout,
            output_limit=self.output_limit, capture_limit=self.capture_limit,
            activate_testbed=self.prebuilt,
        ))

    def apply_patch(self, patch: str, timeout: float) -> dict:
        return self._rpc("apply_patch", timeout, patch=patch)

    def collect_patch(self) -> str:
        return self._rpc("collect_patch", 90, base_commit=self.base_commit)["patch"]


def _validate_remote(workdir: str, image: str | None, lifetime: int) -> None:
    if not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", workdir):
        raise ValueError("Workdir must be a simple absolute Linux directory")
    if image is not None and not IMAGE_PATTERN.fullmatch(image):
        raise ValueError("Invalid image reference")
    if lifetime < 1:
        raise ValueError("Sandbox lifetime must be positive")


class ModalEnvironment(_RemoteEnvironment):
    def __init__(self, workspace: Path, base_commit: str, output_limit: int = 12000,
                 lifetime: int = 1200, allow_network: bool = False,
                 image: str | None = None, workdir: str = "/workspace",
                 capture_limit: int = DEFAULT_CAPTURE_LIMIT):
        import modal

        _validate_remote(workdir, image, lifetime)
        validate_capture_limit(capture_limit)
        self.workdir, self.base_commit, self.output_limit = workdir, base_commit, output_limit
        self.capture_limit = capture_limit
        self.prebuilt, self.closed, self.sandbox = image is not None, False, None
        self.description = f"Modal Linux sandbox; fresh Bash; repository at {workdir}."
        try:
            runtime = (
                modal.Image.from_registry(image, add_python="3.12") if image
                else modal.Image.debian_slim(python_version="3.12").apt_install("git", "ripgrep")
            )
            runtime = runtime.pip_install(f"openai-agents=={version('openai-agents')}")
            for name in ("worker.py", "patching.py", "workspace.py"):
                runtime = runtime.add_local_file(
                    Path(__file__).with_name(name), f"{WORKER_DIR}/{name}", copy=True
                )
            if image is None:
                runtime = runtime.add_local_dir(Path(workspace), workdir, copy=True, ignore=[])
            app = modal.App.lookup("coding-harness", create_if_missing=True)
            self.sandbox = modal.Sandbox.create(
                "sleep", str(lifetime), app=app, image=runtime, workdir=workdir,
                timeout=lifetime, block_network=not allow_network, cpu=2, memory=4096,
            )
            self._verify_base()
        except BaseException:
            self.close()
            raise

    def _rpc(self, operation: str, rpc_timeout: float, **values) -> dict:
        request = self._request(operation, **values)
        if rpc_timeout <= 0:
            raise ValueError("Execution time budget exhausted")
        process = self.sandbox.exec(
            "python", f"{WORKER_DIR}/worker.py", "-", timeout=math.ceil(rpc_timeout) + 20
        )
        process.stdin.write(request)
        process.stdin.write_eof()
        process.stdin.drain()
        stdout, stderr = process.stdout.read(), process.stderr.read()
        process.wait()
        if process.returncode:
            raise RuntimeError("Sandbox worker failed" + (" with diagnostics" if stderr else ""))
        return self._response(stdout)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.sandbox is not None:
                self.sandbox.terminate(wait=True)


class DockerEnvironment(_RemoteEnvironment):
    """Own one disposable container; no host bind mounts, Docker socket or host credentials."""

    def __init__(self, workspace: Path, base_commit: str, output_limit: int = 12000,
                 lifetime: int = 1200, allow_network: bool = False,
                 image: str | None = None, workdir: str = "/workspace",
                 cpus: float = 2.0, memory: str = "4g",
                 capture_limit: int = DEFAULT_CAPTURE_LIMIT):
        _validate_remote(workdir, image, lifetime)
        validate_capture_limit(capture_limit)
        if cpus <= 0 or not re.fullmatch(r"[1-9][0-9]*[kmgKMG]?", memory):
            raise ValueError("Invalid Docker CPU/memory limits")
        self.workdir, self.base_commit, self.output_limit = workdir, base_commit, output_limit
        self.capture_limit = capture_limit
        self.prebuilt, self.closed = image is not None, False
        self.name = "coding-harness-" + uuid.uuid4().hex
        self.created = False
        self.description = f"Docker Linux sandbox; fresh Bash; repository at {workdir}."
        try:
            runtime = self._runtime_image(image)
            self.created = True
            self._docker(
                ["run", "-d", "--name", self.name, "--label", "coding-harness.owner=" + self.name,
                 "--init", "--cpus", str(cpus), "--memory", memory, "--pids-limit", "256",
                 "--network", "bridge" if allow_network else "none",
                 "--cap-drop=ALL", "--security-opt=no-new-privileges",
                 "--entrypoint", "/bin/sleep", runtime, str(lifetime)],
                timeout=120,
            )
            self._docker(["exec", self.name, "mkdir", "-p", WORKER_DIR], timeout=30)
            for name in ("worker.py", "patching.py", "workspace.py"):
                self._docker(["cp", str(Path(__file__).with_name(name)),
                              f"{self.name}:{WORKER_DIR}/{name}"], timeout=60)
            if image is None:
                self._docker(["exec", self.name, "mkdir", "-p", workdir], timeout=30)
                self._docker(["cp", str(Path(workspace).resolve()) + "/.",
                              f"{self.name}:{workdir}"], timeout=120)
            self._verify_base()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _docker(args: list[str], *, timeout: float, data: str | None = None,
                check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", *args], input=data, text=True, encoding="utf-8", capture_output=True,
            timeout=timeout, check=check,
        )

    def _runtime_image(self, image: str | None) -> str:
        package = f"openai-agents=={version('openai-agents')}"
        if image is None:
            dockerfile = (
                "FROM python:3.12-slim\n"
                "RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep "
                "&& rm -rf /var/lib/apt/lists/*\n"
                "RUN python -m venv /opt/harness-tools\n"
            )
        else:
            # SWE-bench's standard testbed images provide this conda installation.
            dockerfile = (
                f"FROM {image}\nUSER root\n"
                "RUN /opt/miniconda3/bin/conda create -y -p /opt/harness-tools "
                "--override-channels -c conda-forge python=3.12 pip\n"
            )
        dockerfile += f"RUN /opt/harness-tools/bin/pip install --no-cache-dir {package}\n"
        tag = "coding-harness-tools:" + hashlib.sha256(dockerfile.encode()).hexdigest()[:16]
        probe = self._docker(["image", "inspect", tag], timeout=30, check=False)
        if probe.returncode:
            with tempfile.TemporaryDirectory(prefix="harness-image-") as temporary:
                Path(temporary, "Dockerfile").write_text(dockerfile, encoding="utf-8")
                self._docker(["build", "--tag", tag, temporary], timeout=1200)
        return tag

    def _rpc(self, operation: str, rpc_timeout: float, **values) -> dict:
        request = self._request(operation, **values)
        if rpc_timeout <= 0:
            raise ValueError("Execution time budget exhausted")
        try:
            process = self._docker(
                ["exec", "-i", "-w", self.workdir, self.name,
                 "/opt/harness-tools/bin/python", f"{WORKER_DIR}/worker.py", "-"],
                timeout=rpc_timeout + 20, data=request,
            )
        except subprocess.TimeoutExpired:
            # The client timing out does not stop docker exec. Kill this owned container.
            self.close()
            raise
        return self._response(process.stdout)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.created:
                # A run timeout has an unknown outcome; only remove our exact ownership label.
                probe = self._docker(
                    ["inspect", "--format", '{{ index .Config.Labels "coding-harness.owner" }}',
                     self.name], timeout=30, check=False,
                )
                if probe.returncode == 0 and probe.stdout.strip() == self.name:
                    self._docker(["rm", "-f", self.name], timeout=30)
