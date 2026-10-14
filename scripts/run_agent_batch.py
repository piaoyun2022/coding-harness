"""Sequential coding-agent generation; official grading stays in a separate process."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from dotenv import dotenv_values

from coding_harness.agent import Agent
from coding_harness.benchmark import Task, load_tasks, write_predictions
from coding_harness.config import Settings
from coding_harness.environment import DockerEnvironment
from coding_harness.provider import DeepSeekModel
from coding_harness.trace import Trace
from coding_harness.worker import safe_environment

ROOT = Path(__file__).resolve().parents[1]
RULES = "Fix the supplied issue. Do not modify tests to hide the bug. Do not commit or push."


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_manifest(source_commit: str, root: Path = ROOT) -> dict:
    """Require the live core to match its frozen commit; wrapper has its own hash/HEAD."""
    if not re.fullmatch("[0-9a-f]{40}", source_commit):
        raise ValueError("Source commit must be a full Git SHA")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=True,
            timeout=30,
            env=safe_environment(),
        ).stdout

    files = sorted((root / "src/coding_harness").glob("*.py"))
    if not files:
        raise ValueError("No harness source found")
    hashes = {}
    for file in [*files, root / "pyproject.toml"]:
        relative = file.relative_to(root).as_posix()
        committed = git("show", f"{source_commit}:{relative}").replace(b"\r\n", b"\n")
        live = file.read_bytes().replace(b"\r\n", b"\n")
        if committed != live:
            raise ValueError("Harness source differs from frozen commit")
        hashes[relative] = digest(live)
    return {
        "source_commit": source_commit,
        "source_sha256": hashes,
        "wrapper_commit": git("rev-parse", "HEAD").decode().strip(),
        "wrapper_sha256": digest((root / "scripts/run_agent_batch.py").read_bytes()),
    }


def image_name(task: Task) -> str:
    return (
        "swebench/sweb.eval.x86_64." + task.instance_id.lower().replace("__", "_1776_") + ":latest"
    )


class BenchmarkDocker(DockerEnvironment):
    """Prepare only this newly owned container; never relax generic Environment checks."""

    def __init__(self, task: Task, output: Path, settings: Settings):
        self.source_metadata = {
            "requested_base_commit": task.base_commit,
            "image": image_name(task),
        }
        self.cleanup_errors: list[dict] = []
        self._candidates: set[str] = {image_name(task)}
        self._owned_images: dict[str, str] = {}
        self._images_closed = False
        existing = DockerEnvironment._docker(["images", "--no-trunc", "--quiet"], timeout=30)
        self._prior_ids = set(existing.stdout.splitlines())
        super().__init__(
            output,
            task.base_commit,
            output_limit=settings.max_output_chars,
            capture_limit=settings.execution_capture_bytes,
            lifetime=settings.max_seconds + 120,
            allow_network=False,
            image=image_name(task),
            workdir="/testbed",
        )

    def _docker(self, args, **kwargs):
        if args[0] == "build" and "--tag" in args:
            self._candidates.add(args[args.index("--tag") + 1])
        return DockerEnvironment._docker(args, **kwargs)

    def _inspect_image(self, tag: str) -> dict | None:
        probe = DockerEnvironment._docker(["image", "inspect", tag], timeout=30, check=False)
        if probe.returncode:
            return None
        data = json.loads(probe.stdout)[0]
        return {"id": data["Id"], "repo_digests": data.get("RepoDigests") or [], "tag": tag}

    def _runtime_image(self, image):
        try:
            if self._inspect_image(image) is None:
                self._docker(["pull", image], timeout=1200)
            self.source_metadata["base_image"] = self._inspect_image(image)
            runtime = super()._runtime_image(image)
            self.source_metadata["runtime_image"] = self._inspect_image(runtime)
            return runtime
        finally:
            for tag in self._candidates:
                info = self._inspect_image(tag)
                if info and info["id"] not in self._prior_ids:
                    self._owned_images[tag] = info["id"]

    def _verify_base(self):
        requested = self.base_commit
        if not re.fullmatch("[0-9a-f]{40}", requested):
            raise ValueError("Invalid base commit")

        def inspect(command):
            result = self.execute(command, 60)
            if result.exit_code or result.timed_out or result.truncated:
                raise ValueError("Cannot inspect image baseline")
            return result.stdout.strip()

        head = inspect("git rev-parse HEAD")
        if not re.fullmatch("[0-9a-f]{40}", head):
            raise ValueError("Invalid image HEAD")
        self.source_metadata["initial_head"] = head
        if head != requested:
            parents = inspect("git show -s --format=%P HEAD").split()
            subject = inspect("git show -s --format=%s HEAD")
            self.source_metadata.update(head_parents=parents, head_subject=subject)
            if parents != [requested] or subject != "SWE-bench":
                raise ValueError("Image is not the expected single setup commit")
        self.source_metadata["setup_changes"] = inspect(
            f"git diff --name-status {requested} {head}"
        )
        # Official installation edits belong to the image, not to the agent's patch.
        self.base_commit = head
        super()._verify_base()
        self.source_metadata.update(patch_base_commit=head, clean_base_verified=True)

    def close(self):
        super().close()
        if self._images_closed:
            return
        self._images_closed = True
        # Overlay first; never force-delete, prune, or touch preexisting image IDs.
        for tag in sorted(self._owned_images, key=lambda name: name.startswith("swebench/")):
            try:
                current = self._inspect_image(tag)
                if current and current["id"] == self._owned_images[tag]:
                    result = self._docker(["image", "rm", tag], timeout=60, check=False)
                    if result.returncode:
                        self.cleanup_errors.append(
                            {"stage": "image_cleanup", "error_type": "DockerError"}
                        )
            except Exception as error:
                self.cleanup_errors.append(
                    {"stage": "image_cleanup", "error_type": type(error).__name__}
                )


def run_one(
    task: Task,
    output: Path,
    settings: Settings,
    api_key: str,
    *,
    model_factory=DeepSeekModel,
    environment_factory=BenchmarkDocker,
) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    trace = Trace(output, secrets=(api_key,))
    trace.write_json("task.json", asdict(task))
    environment = model = None
    phase, patch, exported = "environment_setup", "", False
    errors = []
    result = {"outcome": "failed", "stop_reason": phase, "usage": {}}
    started = time.monotonic()
    try:
        environment = environment_factory(task, output, settings)
        trace.write_json("source.json", getattr(environment, "source_metadata", {}))
        phase = "repository_rules"
        rules = environment.execute("if [ -f AGENTS.md ]; then cat AGENTS.md; fi", 60)
        if rules.exit_code or rules.timed_out or rules.truncated:
            raise ValueError("Repository rules are incomplete")
        phase = "model_setup"
        model = model_factory(settings, api_key)
        phase = "agent"
        result = Agent(model, environment, settings, trace).run(
            task.problem_statement,
            RULES + "\n" + rules.stdout,
        )
    except KeyboardInterrupt:
        result = {"outcome": "cancelled", "stop_reason": phase, "usage": {}}
    except Exception as error:
        errors.append({"stage": phase, "error_type": type(error).__name__})
        result = {"outcome": "failed", "stop_reason": phase, "usage": {}}
    finally:
        if environment is not None:
            try:
                patch = trace.clean(environment.collect_patch())
                exported = True
            except Exception as error:
                errors.append({"stage": "patch_export", "error_type": type(error).__name__})
            try:
                environment.close()
                errors.extend(getattr(environment, "cleanup_errors", []))
            except Exception as error:
                errors.append({"stage": "environment_close", "error_type": type(error).__name__})
        if model is not None:
            try:
                model.close()
            except Exception as error:
                errors.append({"stage": "model_close", "error_type": type(error).__name__})
        if exported and patch.strip():
            status = "generated"  # Candidate exported, regardless of agent finish/budget status.
        elif errors:
            status = "setup_error"
        elif result.get("stop_reason") in {
            "incomplete_response",
            "invalid_model_reply",
            "invalid_tool_calls",
        }:
            status = "invalid_response"
        elif result["outcome"] == "failed":
            status = "agent_error"
        else:
            status = "empty_patch"
        result.update(
            instance_id=task.instance_id,
            mode="agent",
            model=settings.model,
            status=status,
            errors=errors,
            patch_sha256=digest(patch.encode()),
            total_elapsed_seconds=round(time.monotonic() - started, 3),
        )
        (output / "changes.patch").write_bytes(patch.encode("utf-8"))
        trace.write_json(
            "prediction.json",
            {
                "instance_id": task.instance_id,
                "model_name_or_path": settings.model + "-harness-v2",
                "model_patch": patch,
            },
        )
        # Publish result last: a grader can now trust that the complete candidate exists.
        trace.result(result)
    return result


def run_batch(
    tasks: list[Task],
    output: Path,
    settings: Settings,
    *,
    api_key: str,
    source_commit: str,
    model_factory=DeepSeekModel,
    environment_factory=BenchmarkDocker,
    input_tasks_sha256: str | None = None,
) -> dict:
    if not tasks or len({task.instance_id for task in tasks}) != len(tasks) or not api_key:
        raise ValueError("Need unique tasks and an explicit API key")
    source = source_manifest(source_commit)
    output.mkdir(parents=True, exist_ok=False)
    planned = "".join(json.dumps(asdict(task), ensure_ascii=False) + "\n" for task in tasks)
    (output / "tasks.jsonl").write_bytes(planned.encode("utf-8"))
    save(output / "settings.json", settings.model_dump())
    save(
        output / "plan.json",
        {
            "mode": "agent",
            "planned": len(tasks),
            "workers": 1,
            "tasks_sha256": digest(planned.encode()),
            "input_tasks_sha256": input_tasks_sha256,
            "settings_sha256": digest(json.dumps(settings.model_dump(), sort_keys=True).encode()),
            "settings": settings.model_dump(),
            "source": source,
            "test_feedback": True,
            "grading_included": False,
            "selection": "input order, every planned task once",
            "benchmark_rules": RULES,
        },
    )
    results = []
    started = time.monotonic()
    stop_reason = None
    try:
        for task in tasks:
            current = source_manifest(source_commit)
            if current["source_sha256"] != source["source_sha256"]:
                raise ValueError("Source changed during batch")
            save(
                output / "progress.json",
                {
                    "mode": "agent",
                    "planned": len(tasks),
                    "completed": len(results),
                    "active_instance": task.instance_id,
                },
            )
            result = run_one(
                task,
                output / "runs" / task.instance_id,
                settings,
                api_key,
                model_factory=model_factory,
                environment_factory=environment_factory,
            )
            results.append(result)
            print(
                json.dumps(
                    {
                        "completed": len(results),
                        "planned": len(tasks),
                        "instance_id": task.instance_id,
                        "status": result["status"],
                        "outcome": result["outcome"],
                    }
                ),
                flush=True,
            )
            if result["outcome"] == "cancelled" or any(
                error["stage"] in {"environment_close", "image_cleanup"}
                for error in result["errors"]
            ):
                stop_reason = "cancelled_or_cleanup_failed"
                break
    except KeyboardInterrupt:
        stop_reason = "cancelled"
    except Exception as error:
        stop_reason = "batch_error:" + type(error).__name__
    finally:
        write_predictions(
            tasks,
            output / "runs",
            output / "predictions.jsonl",
            model_name=settings.model + "-harness-v2",
        )
        usage = {
            key: sum(row.get("usage", {}).get(key, 0) for row in results)
            for key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")
        }
        report = {
            "mode": "agent",
            "planned": len(tasks),
            "completed": len(results),
            "statuses": dict(Counter(row["status"] for row in results)),
            "outcomes": dict(Counter(row["outcome"] for row in results)),
            "usage": usage,
            "stop_reason": stop_reason,
            "graded": False,
            "resolved_rate": None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        save(output / "batch-result.json", report)
        save(output / "progress.json", report | {"active_instance": None})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("tasks", "output", "env-file", "settings"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args(argv)
    try:
        settings = Settings.model_validate_json(args.settings.read_text(encoding="utf-8-sig"))
        tasks = load_tasks(args.tasks)
        if not args.env_file.is_file():
            raise FileNotFoundError("Explicit environment file is missing")
        key = dotenv_values(args.env_file, interpolate=False).get("DEEPSEEK_API_KEY") or ""
        report = run_batch(
            tasks,
            args.output,
            settings,
            api_key=key,
            source_commit=args.source_commit,
            input_tasks_sha256=digest(args.tasks.read_bytes()),
        )
    except Exception as error:
        print(json.dumps({"status": "batch_error", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(report))
    return 0 if report["completed"] == report["planned"] and not report["stop_reason"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
