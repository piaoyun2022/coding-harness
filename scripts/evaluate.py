"""One command: pinned SWE-bench tasks, generation, official grading, and a bound report."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import run_agent_batch as agents
import run_baseline_batch as baselines
import run_official_batch as official
from dotenv import dotenv_values
from evaluation_images import ResourceError, SourceImage, check_resources, private_environment

from coding_harness import benchmark
from coding_harness.config import Settings
from coding_harness.provider import DeepSeekModel

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationError(ValueError):
    """Messages authored here are safe to show; provider errors are never interpolated."""


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    official.write_json(path, value)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def default_settings() -> Settings:
    # Same per-task C profile as the published first-30 run. Baseline is still one request.
    return Settings(max_steps=300, max_total_tokens=5_000_000, max_seconds=900,
                    max_output_tokens=32768, request_timeout=180)


def read_key(path: Path) -> str:
    if not path.is_file():
        raise ConfigurationError("Create the explicitly selected env file from .env.example")
    key = dotenv_values(path, interpolate=False).get("DEEPSEEK_API_KEY") or ""
    if not key.strip() or key.strip().lower() in {"your-api-key", "replace-me"}:
        raise ConfigurationError("Set DEEPSEEK_API_KEY in the selected env file")
    return key


def frozen_source(commit: str | None = None) -> dict:
    if commit is None:
        commit = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
            check=True, timeout=30).stdout.strip()
    # editable installs in a different checkout must never silently supply the agent.
    for name, module in list(sys.modules.items()):
        if name == "coding_harness" or name.startswith("coding_harness."):
            path = getattr(module, "__file__", None)
            if path and not Path(path).resolve().is_relative_to(ROOT / "src"):
                raise ConfigurationError("coding_harness was imported from a different checkout")
    result = agents.source_manifest(commit, ROOT)
    result["evaluation_scripts"] = {
        str(path.relative_to(ROOT)): digest(path)
        for path in sorted((ROOT / "scripts").glob("*.py"))
    }
    result["packages"] = {
        name: importlib.metadata.version(name)
        for name in ("coding-harness", "openai", "openai-agents", "swebench", "pyarrow")
    }
    return result


def preflight(docker_disk: Path, artifact_disk: Path, *, min_disk_gib: float) -> dict:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ConfigurationError("Use Linux x86_64 (Windows: WSL2) with Docker Linux containers")
    checked = benchmark.evaluation_preflight()
    if not checked["ready"]:
        raise ConfigurationError(checked["reason"])
    import docker
    client = docker.from_env(timeout=30)
    try:
        client.ping()
        info = client.info()
        if info.get("OSType") != "linux":
            raise ConfigurationError("Docker must use Linux containers")
    finally:
        client.close()
    resources = check_resources(docker_disk, min_disk_gib=min_disk_gib)
    check_resources(artifact_disk, min_disk_gib=2)
    return checked | resources | {
        "docker_disk": str(docker_disk), "artifact_disk": str(artifact_disk),
        "docker_memory_bytes": info.get("MemTotal"), "docker_cpus": info.get("NCPU"),
        "interpreter": sys.executable, "python_version": platform.python_version(),
    }


def prepare_data(root: Path, limit: int) -> tuple[list, Path, dict]:
    safe = root / "tasks"
    manifest = benchmark.prepare_dataset(safe, limit=limit)
    tasks = benchmark.load_tasks(safe / "tasks.jsonl")
    full = benchmark.prepare_grading_data(root / "grader-only" / "download.json",
                                          safe / "manifest.json")
    selected = {task.instance_id for task in tasks}
    rows = official.read_json(full)
    grading = {row["instance_id"]: row for row in rows if row["instance_id"] in selected}
    grader_path = root / "grader-only" / "tasks.json"
    save(grader_path, [grading[task.instance_id] for task in tasks])
    full.unlink()  # Only the selected official rows need to persist.
    official.validate_plan(safe / "tasks.jsonl", grader_path)
    return tasks, grader_path, manifest


class Pipeline:
    """Bounded task lanes; each task is generated once, then graded without feedback."""

    def __init__(self, tasks, output, settings, api_key, source, grader_path, *, workers=2,
                 docker_disk=Path("/"), min_disk_gib=20, min_memory_gib=8,
                 test_timeout=1800, process_timeout=3600):
        self.tasks, self.output, self.settings = tasks, output, settings
        self.api_key, self.source, self.grader_path = api_key, source, grader_path
        self.workers, self.docker_disk = workers, docker_disk
        self.min_disk_gib, self.min_memory_gib = min_disk_gib, min_memory_gib
        self.test_timeout, self.process_timeout = test_timeout, process_timeout
        self.grading = {row["instance_id"]: row for row in official.read_json(grader_path)}
        self.setup_lock = threading.Lock()
        self.stop = threading.Event()
        self.run_id = uuid.uuid4().hex
        self.cache = baselines.RepositoryCache(output / "repositories")

    def resources(self, *, setup=False):
        return check_resources(self.docker_disk, min_disk_gib=self.min_disk_gib,
                               min_memory_gib=self.min_memory_gib if setup else 0)

    def baseline(self, task, directory):
        model = None
        try:
            source = self.cache.ensure(task)
            model = DeepSeekModel(self.settings, self.api_key)
            return benchmark.generate_baseline(
                task, source, model, self.settings, directory,
                context_chars=baselines.CONTEXT_CHARS)
        except Exception as error:
            return baselines._failed(task, self.settings, directory, error)
        finally:
            if model is not None:
                model.close()

    def generate(self, mode, task, directory, lease):
        if mode == "baseline":
            return self.baseline(task, directory)
        private = private_environment(lease, self.run_id)

        def environment(task, output, settings):
            # Agent's own 900s starts only after this constructor returns.
            with self.setup_lock:
                self.resources(setup=True)
                return private(task, output, settings)

        return agents.run_one(task, directory, self.settings, self.api_key,
                              environment_factory=environment)

    def one(self, mode, task, generation, grading_output):
        lease, grader = SourceImage(task), None
        started = time.monotonic()
        row = {"instance_id": task.instance_id, "resolved": False}
        cleanup = None
        try:
            self.resources()
            current = frozen_source(self.source["source_commit"])
            if current["source_sha256"] != self.source["source_sha256"]:
                raise ValueError("Frozen source changed during the run")
            generated = self.generate(mode, task, generation / "runs" / task.instance_id, lease)
            loaded = official.generation_input(generation, task.instance_id)
            if loaded is None:
                raise RuntimeError("Generation did not publish a final result")
            row, prediction = loaded
            cleanup_errors = [error for error in generated.get("errors", [])
                              if error.get("stage") in {"environment_close", "image_cleanup"}]
            if cleanup_errors:
                row["generation_cleanup_errors"] = cleanup_errors
                self.stop.set()
            if generated.get("outcome") == "cancelled":
                row.update(status="infra_cancelled")
                self.stop.set()
            elif prediction is not None:
                # Keep the exact official source until both generation and grading finish.
                with self.setup_lock:
                    self.resources()
                    lease.acquire()
                expected_image = lease.verify()
                row["expected_source_image_id"] = expected_image
                grader = official.Grader(
                    grading_output, self.grader_path, self.grading,
                    test_timeout=self.test_timeout, process_timeout=self.process_timeout,
                    docker_disk=self.docker_disk, artifact_disk=self.output)
                row = grader.grade(row, prediction)
                cleanup_issues = {name: row[name] for name in
                                  ("container_cleanup", "image_cleanup")
                                  if row.get(name) in {"error", "ownership_not_verified"}}
                if cleanup_issues:
                    row["official_cleanup_issues"] = cleanup_issues
                    self.stop.set()  # Keep the bound score; the overall run is incomplete.
                if row.get("status") in {"graded", "patch_rejected"} and (
                    row.get("evaluated_image_id") != expected_image
                    or row.get("image_before", {}).get("image_id") != expected_image
                ):
                    row.update(status="infra_image_identity", resolved=False)
        except Exception as error:
            row.update(status="infra_pipeline", error_type=type(error).__name__)
            if isinstance(error, ResourceError):
                row["resource_message"] = str(error)
        finally:
            if grader is not None:
                try:
                    grader.client.close()
                except Exception as error:
                    row["grader_close_error_type"] = type(error).__name__
                    self.stop.set()
            try:
                cleanup = lease.close()
            except Exception as error:
                cleanup = {"cleanup": "error", "error_type": type(error).__name__}
            save(generation / "cleanup" / (task.instance_id + ".json"), cleanup)
        if cleanup["cleanup"] in {"error", "remove_failed", "retained_container_reference"}:
            row["cleanup_issue"] = cleanup["cleanup"]
            self.stop.set()
        if row.get("status") not in official.COMPLETE_STATUSES:
            # Do not spend more calls after an infrastructure/API fault.
            self.stop.set()
        row["pipeline_seconds"] = round(time.monotonic() - started, 3)
        return row

    def run_mode(self, mode):
        generation, grading = self.output / mode, self.output / ("official-" + mode)
        generation.mkdir()
        grading.mkdir()
        tasks_text = "".join(json.dumps(asdict(task), ensure_ascii=False) + "\n"
                             for task in self.tasks)
        (generation / "tasks.jsonl").write_text(tasks_text, encoding="utf-8", newline="\n")
        save(generation / "settings.json", self.settings.model_dump())
        plan = {"mode": mode, "planned": len(self.tasks), "workers": self.workers,
                "source": self.source, "settings": self.settings.model_dump(),
                "tasks_sha256": digest(generation / "tasks.jsonl"),
                "grading_data_sha256": digest(self.grader_path),
                "selection": "pinned dataset sorted instance_id, first N",
                "test_feedback": mode == "agent", "model_reruns": False,
                "provider_transport_max_retries": 2,
                "baseline_context_chars": baselines.CONTEXT_CHARS if mode == "baseline" else None}
        save(generation / "plan.json", plan)
        rows, results, active = {}, [], {}
        index = 0
        ids = [task.instance_id for task in self.tasks]

        def publish():
            report = official.summarize(ids, rows) | {
                "mode": mode, "rows": [rows[i] for i in ids if i in rows],
                "active": [task.instance_id for task in active.values()],
                "fingerprint": {"source": self.source,
                                "generation_plan_sha256": digest(generation / "plan.json"),
                                "grading_data_sha256": digest(self.grader_path),
                                "swebench_version": "4.1.0"}}
            save(grading / "progress.json", report)
            return report

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="evaluation") as pool:
            while index < len(self.tasks) or active:
                try:
                    while not self.stop.is_set() and index < len(self.tasks) and (
                        len(active) < self.workers
                    ):
                        task = self.tasks[index]
                        index += 1
                        future = pool.submit(self.one, mode, task, generation, grading)
                        active[future] = task
                    publish()
                    if not active:
                        break
                    done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        task = active.pop(future)
                        try:
                            rows[task.instance_id] = future.result()
                        except Exception as error:
                            rows[task.instance_id] = {
                                "instance_id": task.instance_id, "status": "infra_worker",
                                "resolved": False, "error_type": type(error).__name__}
                            self.stop.set()
                        print(json.dumps({"mode": mode, "completed": len(rows), "planned": len(ids),
                                          "instance_id": task.instance_id,
                                          "status": rows[task.instance_id]["status"]}), flush=True)
                except KeyboardInterrupt:
                    # Running lanes finish naturally; no model call or mutation is replayed.
                    self.stop.set()
        for task in self.tasks:
            path = generation / "runs" / task.instance_id / "result.json"
            if path.exists():
                results.append(official.read_json(path))
        benchmark.write_predictions(self.tasks, generation / "runs",
                                    generation / "predictions.jsonl",
                                    model_name=self.settings.model + "-" + mode)
        save(generation / "batch-result.json", {
            "planned": len(ids), "completed": len(results),
            "statuses": dict(Counter(row["status"] for row in results)),
            "interrupted_or_infrastructure_stop": self.stop.is_set()})
        report = publish()
        save(grading / "run.json", report)
        return report


def report_markdown(reports: dict, *, requested_modes=None, complete=True) -> str:
    lines = ["# Evaluation results", "",
             "Complete: " + str(complete) + "; requested modes: " +
             ", ".join(requested_modes or reports), "",
             "| Mode | Resolved / planned | Official rate | Unknown | Pending | Total tokens |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for mode, row in reports.items():
        rate = row["official_resolved_rate"]
        lines.append(f"| {mode} | {row['resolved']}/{row['planned']} | "
                     + (f"{rate:.1%}" if rate is not None else "provisional") +
                     f" | {row['unresolved_unknown']} | {row['pending']} | "
                     f"{row['generation_usage']['total_tokens']} |")
    lines += ["", "Baseline is one request with 64,000 characters of retrieved source. "
              "Agent uses tools and multiple requests; compute budgets are different.",
              "Only official reports bound to the exact generated patch count as solved.",
              "No task regeneration or best-of selection is performed; "
              "SDK transport retries may occur.", ""]
    return "\n".join(lines)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env-file", type=Path, default=ROOT / ".env")
    result.add_argument("--mode", choices=("both", "agent", "baseline"), default="both")
    result.add_argument("--limit", type=int, choices=range(1, 301), default=30, metavar="1..300")
    result.add_argument("--workers", type=int, choices=range(1, 9), default=2)
    result.add_argument("--output", type=Path)
    result.add_argument("--settings", type=Path, help="Optional complete Settings JSON")
    result.add_argument("--source-commit", help="Full SHA; defaults to current committed HEAD")
    result.add_argument("--docker-disk", type=Path, default=Path("/"),
                        help="Host mount containing Docker data (Desktop: backing disk mount)")
    result.add_argument("--min-disk-gib", type=float, default=20)
    result.add_argument("--min-memory-gib", type=float, default=8,
                        help="Available Linux memory required before one cold agent setup")
    result.add_argument("--test-timeout", type=int, default=1800)
    result.add_argument("--process-timeout", type=int, default=3600)
    result.add_argument("--preflight", action="store_true", help="No download, model, or container")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    output = args.output or ROOT / "artifacts" / (
        "evaluation-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    output = output.resolve()
    try:
        if args.min_disk_gib < 1 or args.min_memory_gib < 0:
            raise ConfigurationError("Resource limits must be nonnegative, disk at least 1 GiB")
        if args.test_timeout < 1 or args.process_timeout <= args.test_timeout:
            raise ConfigurationError("Process timeout must exceed positive test timeout")
        if output.exists():
            raise ConfigurationError(
                "Use a new --output directory; existing runs are never overwritten")
        settings = (Settings.model_validate_json(args.settings.read_text(encoding="utf-8-sig"))
                    if args.settings else default_settings())
        key = read_key(args.env_file.resolve())
        source = frozen_source(args.source_commit)
        parent = output.parent
        while not parent.exists():
            parent = parent.parent
        checked = preflight(args.docker_disk.resolve(), parent,
                            min_disk_gib=args.min_disk_gib)
        if args.preflight:
            print(json.dumps({"ready": True, "source": source, "environment": checked,
                              "mode": args.mode, "limit": args.limit, "workers": args.workers,
                              "model_called": False, "settings": settings.model_dump()}))
            return 0
        output.mkdir(parents=True, exist_ok=False)
        save(output / "preflight.json", checked)
        save(output / "source.json", source)
        save(output / "settings.json", settings.model_dump())
        tasks, grader_path, manifest = prepare_data(output / "data", args.limit)
        save(output / "plan.json", {
            "source": source, "dataset": manifest, "mode": args.mode, "workers": args.workers,
            "settings": settings.model_dump(), "setup_workers": 1,
            "docker_disk": str(args.docker_disk.resolve()), "min_disk_gib": args.min_disk_gib,
            "min_memory_gib": args.min_memory_gib, "test_timeout": args.test_timeout,
            "process_timeout": args.process_timeout, "model_reruns": False})
        pipeline = Pipeline(
            tasks, output, settings, key, source, grader_path, workers=args.workers,
            docker_disk=args.docker_disk.resolve(), min_disk_gib=args.min_disk_gib,
            min_memory_gib=args.min_memory_gib, test_timeout=args.test_timeout,
            process_timeout=args.process_timeout)
        reports = {}
        requested_modes = ["baseline", "agent"] if args.mode == "both" else [args.mode]
        signal.signal(signal.SIGTERM, lambda *_: pipeline.stop.set())
        for mode in requested_modes:
            reports[mode] = pipeline.run_mode(mode)
            complete = (list(reports) == requested_modes and not pipeline.stop.is_set()
                        and all(not row["provisional"] for row in reports.values()))
            summary = {"requested_modes": requested_modes, "completed_modes": list(reports),
                       "not_run_modes": [name for name in requested_modes if name not in reports],
                       "overall_complete": complete, "stopped": pipeline.stop.is_set(),
                       "modes": reports}
            save(output / "report.json", summary)
            (output / "report.md").write_text(
                report_markdown(reports, requested_modes=requested_modes, complete=complete),
                encoding="utf-8")
            if pipeline.stop.is_set():
                break
        print(json.dumps({"output": str(output), "reports": {
            name: {key: value for key, value in row.items() if key not in {"rows", "fingerprint"}}
            for name, row in reports.items()}}))
        return 0 if complete else 2
    except (ConfigurationError, ResourceError) as error:
        print(json.dumps({"status": "configuration_error", "message": str(error),
                          "output": str(output)}))
        return 1
    except Exception as error:
        # Exception text from HTTP/Docker/provider code may contain private data.
        print(json.dumps({"status": "evaluation_error", "error_type": type(error).__name__,
                          "output": str(output),
                          "hint": "Check committed source, dependency installation, Docker and "
                          "network access; no provider error text is printed."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

