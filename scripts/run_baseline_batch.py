"""Generate a reproducible one-shot baseline batch; never run repository code or grade here."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from dotenv import dotenv_values

from coding_harness.benchmark import (
    RETRIEVAL_VERSION,
    Task,
    generate_baseline,
    load_tasks,
    write_predictions,
)
from coding_harness.config import Settings
from coding_harness.provider import DeepSeekModel

CONTEXT_CHARS = 64_000


def _json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _public_origin(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
        raise ValueError("Expected a public GitHub owner/repository")
    return f"https://github.com/{repo}.git"


class RepositoryCache:
    """One bare repository per project; serialize its fetches and keep immutable commit refs."""

    def __init__(self, root: Path, *, origin_for_repo: Callable[[str], str] = _public_origin):
        self.root = root
        self.origin_for_repo = origin_for_repo
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @staticmethod
    def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "LANG", "LC_ALL"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_TERMINAL_PROMPT="0")
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=",
             "-c", "core.fsmonitor=false", "-C", str(path), *args],
            env=env, capture_output=True, timeout=300, check=check,
        )

    def ensure(self, task: Task) -> Path:
        with self._guard:
            lock = self._locks.setdefault(task.repo, threading.Lock())
        name = task.repo.replace("/", "__") + "-" + hashlib.sha256(
            task.repo.encode()).hexdigest()[:12] + ".git"
        path = self.root / name
        with lock:
            if not path.exists():
                path.mkdir(parents=True)
                self._git(path, "init", "--bare", "--quiet")
            exists = self._git(path, "cat-file", "-e", task.base_commit + "^{commit}", check=False)
            if exists.returncode:
                self._git(
                    path, "fetch", "--quiet", "--no-tags", "--depth=1",
                    self.origin_for_repo(task.repo),
                    f"{task.base_commit}:refs/harness/{task.base_commit}",
                )
            self._git(path, "cat-file", "-e", task.base_commit + "^{commit}")
        return path


def _failed(task: Task, settings: Settings, output: Path, error: Exception) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    result = {"instance_id": task.instance_id, "mode": "baseline", "model": settings.model,
              "status": "setup_error", "requests": 0, "error_type": type(error).__name__}
    _json(output / "task.json", asdict(task))
    _json(output / "prediction.json", {
        "instance_id": task.instance_id, "model_name_or_path": settings.model + "-baseline",
        "model_patch": "",
    })
    (output / "changes.patch").write_text("", encoding="utf-8")
    _json(output / "result.json", result)
    return result


def run_batch(
    tasks: list[Task], output: Path, settings: Settings, *, api_key: str, workers: int = 2,
    model_factory=DeepSeekModel, origin_for_repo: Callable[[str], str] = _public_origin,
) -> dict:
    """Generate each planned task once; failed cases remain in the final predictions."""
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    if not tasks or len({task.instance_id for task in tasks}) != len(tasks):
        raise ValueError("Expected nonempty unique planned tasks")
    if not api_key:
        raise ValueError("Model API key is missing")
    output.mkdir(parents=True, exist_ok=False)
    planned = "".join(json.dumps(asdict(task), ensure_ascii=False) + "\n" for task in tasks)
    (output / "tasks.jsonl").write_text(planned, encoding="utf-8", newline="\n")
    _json(output / "settings.json", settings.model_dump())
    _json(output / "plan.json", {
        "mode": "one-shot-baseline", "planned": len(tasks), "workers": workers,
        "context_chars": CONTEXT_CHARS, "retrieval_version": RETRIEVAL_VERSION,
        "tasks_sha256": hashlib.sha256(planned.encode()).hexdigest(),
        "settings": settings.model_dump(), "tools": [], "test_feedback": False,
    })
    cache = RepositoryCache(output / "repositories", origin_for_repo=origin_for_repo)
    results: dict[str, dict] = {}
    run_dir = output / "runs"
    started = time.monotonic()

    def one(task: Task) -> dict:
        start = time.monotonic()
        task_output = run_dir / task.instance_id
        model = None
        result = None
        try:
            source = cache.ensure(task)
            model = model_factory(settings, api_key)
            result = generate_baseline(
                task, source, model, settings, task_output, context_chars=CONTEXT_CHARS
            )
        except Exception as error:
            result = _failed(task, settings, task_output, error)
        finally:
            if model is not None:
                try:
                    model.close()
                except Exception as error:
                    if result is not None:
                        result["cleanup_error_type"] = type(error).__name__
        if result is None:
            raise RuntimeError("Task did not produce a result")
        result["total_elapsed_seconds"] = round(time.monotonic() - start, 3)
        _json(task_output / "result.json", result)
        return result

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="baseline") as executor:
        futures = {executor.submit(one, task): task for task in tasks}
        try:
            for future in as_completed(futures):
                task = futures[future]
                results[task.instance_id] = future.result()
                print(json.dumps({
                    "completed": len(results), "planned": len(tasks),
                    "instance_id": task.instance_id, "status": results[task.instance_id]["status"],
                }), flush=True)
        except BaseException:
            for future in futures:
                future.cancel()
            raise

    predictions = write_predictions(
        tasks, run_dir, output / "predictions.jsonl", model_name=settings.model + "-baseline"
    )
    usage = {
        field: sum(result.get("usage", {}).get(field, 0) for result in results.values())
        for field in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")
    }
    report = {
        "planned": len(tasks), "completed": len(results),
        "statuses": dict(Counter(result["status"] for result in results.values())),
        "usage": usage, "elapsed_seconds": round(time.monotonic() - started, 3),
        "predictions": predictions.name, "graded": False, "resolved_rate": None,
    }
    _json(output / "batch-result.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True, help="JSON Settings object")
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    args = parser.parse_args(argv)
    try:
        if not args.env_file.is_file():
            raise FileNotFoundError("Explicit environment file is missing")
        settings = Settings.model_validate_json(args.settings.read_text(encoding="utf-8-sig"))
        tasks = load_tasks(args.tasks)
        # No dotenv search, interpolation, os.environ mutation or credential logging.
        values = dotenv_values(args.env_file, interpolate=False)
        key = values.get("DEEPSEEK_API_KEY") or ""
        report = run_batch(tasks, args.output, settings, api_key=key, workers=args.workers)
    except Exception as error:
        print(json.dumps({"status": "batch_error", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
