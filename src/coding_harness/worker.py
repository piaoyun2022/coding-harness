"""Small standalone sandbox worker: bounded observations and process-tree timeout cleanup."""
from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_CAPTURE_LIMIT = 1024 * 1024  # Raw bytes, separately for stdout and stderr.

ENV_ALLOW = {
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC",
    "LANG", "LC_ALL", "TERM",
}


def safe_environment() -> dict[str, str]:
    """Do not forward API keys or SDK/cloud credentials to executed commands."""
    result = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    result.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                  GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
                  GIT_CONFIG_GLOBAL=os.devnull)
    return result


def _kill(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, check=False, timeout=10,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def validate_capture_limit(capture_limit: int) -> None:
    if isinstance(capture_limit, bool) or not isinstance(capture_limit, int) or capture_limit < 1:
        raise ValueError("Capture limit must be a positive integer byte count")


class _Capture:
    """Drain one pipe continuously; retain only the bounded original byte prefix."""

    def __init__(self, stream, limit: int):
        self.stream, self.limit = stream, limit
        self.data = bytearray()
        self.truncated = False
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            while not self.stop.is_set():
                chunk = self.stream.read(64 * 1024)
                if not chunk:
                    break
                with self.lock:
                    remaining = self.limit - len(self.data)
                    self.data.extend(chunk[:remaining])
                    self.truncated |= len(chunk) > remaining
        except OSError:
            # A broken reader is incomplete evidence even if the cap was not reached.
            with self.lock:
                self.truncated = True
        finally:
            self.stream.close()

    def finish(self) -> tuple[str, bool]:
        # Killing the process tree normally closes both pipes immediately. A child
        # escaping that tree must not leave the controller blocked on an inherited pipe.
        self.thread.join(timeout=1)
        with self.lock:
            if self.thread.is_alive():
                self.stop.set()
                self.truncated = True
            return bytes(self.data).decode("utf-8", errors="replace"), self.truncated


def run_command(workspace: Path, command: str, timeout: float, output_limit: int = 12000,
                *, activate_testbed: bool = False,
                capture_limit: int = DEFAULT_CAPTURE_LIMIT) -> dict:
    """Return bounded evidence; the caller produces the model-facing short preview.

    output_limit remains a compatibility hint, not a capture or return-value limit.
    capture_limit counts raw bytes per stream, before UTF-8 replacement decoding.
    """
    if timeout <= 0 or output_limit < 1:
        raise ValueError("Timeout and output limit must be positive")
    validate_capture_limit(capture_limit)
    if activate_testbed and os.name != "nt":
        command = (
            "if [ -f /opt/miniconda3/etc/profile.d/conda.sh ] && "
            "[ -d /opt/miniconda3/envs/testbed ]; then "
            "source /opt/miniconda3/etc/profile.d/conda.sh; conda activate testbed; fi\n"
            + command
        )
    argv = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            if os.name == "nt" else ["bash", "--noprofile", "--norc", "-c", command])
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        argv, cwd=workspace, env=safe_environment(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    captures = []
    timed_out = False
    try:
        for stream in (process.stdout, process.stderr):
            captures.append(_Capture(stream, capture_limit))
        process.wait(timeout=max(deadline - time.monotonic(), 0))
        for capture in captures:
            capture.thread.join(timeout=max(deadline - time.monotonic(), 0))
        if any(capture.thread.is_alive() for capture in captures):
            raise subprocess.TimeoutExpired(argv, timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(process)
    except BaseException:
        _kill(process)
        raise
    finally:
        output = [capture.finish() for capture in captures]
    (out, cut_out), (err, cut_err) = output
    return {"stdout": out, "stderr": err, "exit_code": process.returncode,
            "timed_out": timed_out, "truncated": cut_out or cut_err}


def main() -> None:
    encoded = sys.stdin.read() if sys.argv[1] == "-" else sys.argv[1]
    request = json.loads(base64.b64decode(encoded).decode("utf-8"))
    operation = request.pop("operation")
    request["workspace"] = Path(request["workspace"])
    try:
        if operation == "execute":
            result = run_command(**request)
        elif operation == "apply_patch":
            from patching import apply_patch
            result = apply_patch(**request)
        elif operation == "collect_patch":
            from workspace import collect_patch
            result = {"patch": collect_patch(**request)}
        else:
            raise ValueError("Unknown worker operation")
    except (ValueError, OSError) as error:
        result = {"ok": False, "error": str(error)[:1000]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
