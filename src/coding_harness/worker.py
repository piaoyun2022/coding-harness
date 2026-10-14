"""Small standalone sandbox worker: bounded observations and process-tree timeout cleanup."""
from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

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


def _read_output(handle, limit: int) -> tuple[str, bool]:
    handle.flush()
    size = handle.tell()
    handle.seek(0)
    # At most four UTF-8 bytes per character, with bounded reads for huge logs.
    byte_limit = max(limit, 1) * 4
    if size <= byte_limit:
        text = handle.read(byte_limit).decode("utf-8", errors="replace")
    else:
        head = handle.read(byte_limit // 2)
        handle.seek(-byte_limit // 2, os.SEEK_END)
        text = head.decode("utf-8", errors="replace") + handle.read().decode(
            "utf-8", errors="replace"
        )
    truncated = size > byte_limit or len(text) > limit
    if truncated:
        marker = "\n... output truncated ...\n"
        allowance = max(limit - len(marker), 0)
        text = text[:allowance // 2] + marker + text[-(allowance - allowance // 2):]
        text = text[:limit]
    return text, truncated


def run_command(workspace: Path, command: str, timeout: float, output_limit: int = 12000,
                *, activate_testbed: bool = False) -> dict:
    """Execute a fresh shell, preserving neither shell cwd nor environment between calls."""
    if timeout <= 0 or output_limit < 1:
        raise ValueError("Timeout and output limit must be positive")
    if activate_testbed and os.name != "nt":
        command = (
            "if [ -f /opt/miniconda3/etc/profile.d/conda.sh ] && "
            "[ -d /opt/miniconda3/envs/testbed ]; then "
            "source /opt/miniconda3/etc/profile.d/conda.sh; conda activate testbed; fi\n"
            + command
        )
    argv = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            if os.name == "nt" else ["bash", "--noprofile", "--norc", "-c", command])
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            argv, cwd=workspace, env=safe_environment(), stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(process)
        except BaseException:
            _kill(process)
            raise
        out, cut_out = _read_output(stdout, output_limit)
        err, cut_err = _read_output(stderr, output_limit)
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
