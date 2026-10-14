"""Small command-line entry points; no orchestration framework or hidden auto-discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from .agent import Agent
from .config import Settings
from .provider import DeepSeekModel
from .trace import Trace
from .workspace import git, prepare_workspace


class CLIError(Exception):
    """Only explicitly authored, nonsecret messages may be displayed verbatim."""


def _default_env() -> Path:
    checkout = Path(__file__).resolve().parents[2]
    return checkout / ".env" if (checkout / "pyproject.toml").is_file() else Path.cwd() / ".env"


def _model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", type=Path, default=_default_env())
    parser.add_argument("--settings", help="JSON settings object or path to a JSON file")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=["none", "low", "high", "max"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe", help="Make one small real API request")
    _model_options(probe)
    probe.add_argument("--output", type=Path)

    run = commands.add_parser("run", help="Run the agent on an isolated repository copy")
    _model_options(run)
    run.add_argument("--repo", required=True, type=Path)
    task = run.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file", type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--backend", choices=["docker", "local", "modal"], default="docker")
    run.add_argument("--allow-local", action="store_true")
    run.add_argument("--allow-network", action="store_true")
    run.add_argument("--image", help="Optional prebuilt clean repository image")
    run.add_argument("--workdir", default="/workspace")

    dataset = commands.add_parser("dataset", help="Download the task-safe SWE-bench projection")
    dataset.add_argument("--output", required=True, type=Path)
    dataset.add_argument("--revision")
    dataset.add_argument("--limit", type=int)

    baseline = commands.add_parser("baseline", help="One model request with fixed retrieval")
    _model_options(baseline)
    baseline.add_argument("--tasks", required=True, type=Path)
    baseline.add_argument("--instance-id", required=True)
    baseline.add_argument("--repo", required=True, type=Path)
    baseline.add_argument("--output", required=True, type=Path)
    baseline.add_argument("--context-chars", type=int, default=64_000)

    grade = commands.add_parser("grade-command", help="Print argv for the separate official grader")
    grade.add_argument("--predictions", required=True, type=Path)
    grade.add_argument("--grading-data", required=True, type=Path)
    grade.add_argument("--manifest", type=Path, help="Prepare grading-only rows from this manifest")
    grade.add_argument("--run-id", required=True)
    grade.add_argument("--workers", type=int, default=1)

    score = commands.add_parser("score", help="Summarize official reports without grading")
    score.add_argument("--tasks", required=True, type=Path)
    score.add_argument("--reports", required=True, type=Path)
    score.add_argument("--run-dir", type=Path)
    score.add_argument("--output", type=Path)
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    values = {}
    if args.settings:
        candidate = args.settings.strip()
        if candidate.startswith("{"):
            values = json.loads(candidate)
        else:
            values = json.loads(Path(candidate).read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict):
        raise CLIError("Settings must be a JSON object.")
    if args.model:
        values["model"] = args.model
    if args.reasoning_effort:
        values["reasoning_effort"] = args.reasoning_effort
    return Settings(**values)


def _key(env_file: Path) -> str:
    # Exact file only; never search parents or enumerate the environment.
    load_dotenv(dotenv_path=env_file, override=False)
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise CLIError("Configure DEEPSEEK_API_KEY in the selected local environment file.")
    return key


def _clean(text: str, key: str) -> str:
    return text.replace(key, "[REDACTED]") if key else text


def _emit(value: dict, key: str = "") -> None:
    print(_clean(json.dumps(value, ensure_ascii=False, indent=2), key))


def _fresh_output(path: Path) -> Path:
    output = path.resolve()
    output.mkdir(parents=True, exist_ok=False)
    return output


def _probe(args: argparse.Namespace, settings: Settings, key: str) -> tuple[dict, int]:
    output = _fresh_output(args.output or Path("artifacts") / ("probe-" + uuid.uuid4().hex[:12]))
    trace = Trace(output, secrets=(key,))
    model = None
    result = {"mode": "probe", "model": settings.model, "connected": False, "errors": []}
    try:
        model = DeepSeekModel(settings, key)
        messages = [{"role": "user", "content": "Reply with exactly OK."}]
        trace.event("model_request", purpose="probe", messages=messages)
        reply = model.complete(
            messages,
            [],
            timeout=settings.request_timeout,
            max_output_tokens=128,
            purpose="summary",
        )
        trace.event(
            "model_response",
            purpose="probe",
            message=reply.message,
            finish_reason=reply.finish_reason,
            usage=reply.usage.as_dict(),
        )
        result.update(
            connected=True,
            response=reply.message.get("content"),
            usage=reply.usage.as_dict(),
            finish_reason=reply.finish_reason,
        )
    except Exception as exc:
        result["errors"].append({"stage": "model", "error_type": type(exc).__name__})
    finally:
        if model is not None:
            try:
                model.close()
            except Exception as exc:
                result["errors"].append({"stage": "model_close", "error_type": type(exc).__name__})
        result["output"] = str(output)
        trace.result(result)
    return result, 0 if result["connected"] and not result["errors"] else 1


def _environment(args, workspace: Path, base_commit: str, settings: Settings):
    from .environment import DockerEnvironment, LocalEnvironment, ModalEnvironment

    if args.backend == "local":
        return LocalEnvironment(
            workspace, base_commit, output_limit=settings.max_output_chars,
            capture_limit=settings.execution_capture_bytes,
        )
    factory = DockerEnvironment if args.backend == "docker" else ModalEnvironment
    return factory(
        workspace,
        base_commit,
        output_limit=settings.max_output_chars,
        capture_limit=settings.execution_capture_bytes,
        lifetime=settings.max_seconds + 120,
        allow_network=args.allow_network,
        image=args.image,
        workdir=args.workdir,
    )


def _run(args: argparse.Namespace, settings: Settings, key: str) -> tuple[dict, int]:
    if args.backend == "local" and not args.allow_local:
        raise CLIError("Local execution requires --allow-local and trusted task code.")
    if args.backend == "local" and args.image:
        raise CLIError("A prebuilt image requires the docker or modal backend.")
    task = args.task if args.task is not None else args.task_file.read_text(encoding="utf-8-sig")
    if not task.strip():
        raise CLIError("Task text must not be empty.")
    image_base = None
    if args.image:
        # The image already contains its repository; a snapshot commit cannot identify it.
        if git(args.repo, "status", "--porcelain").stdout.strip():
            raise CLIError("Prebuilt image mode requires a clean input repository checkout.")
        image_base = git(args.repo, "rev-parse", "HEAD").stdout.decode().strip()
    output = _fresh_output(args.output)
    trace = Trace(output, secrets=(key,))
    environment = model = None
    errors = []
    phase = "workspace_setup"
    result = {"outcome": "failed", "stop_reason": phase, "summary": "Run setup failed."}
    patch = ""
    try:
        manifest = prepare_workspace(args.repo, output / "workspace")
        trace.write_json("workspace.json", manifest)
        rules_path = output / "workspace" / "AGENTS.md"
        instructions = rules_path.read_text(encoding="utf-8-sig") if rules_path.is_file() else ""
        phase = "environment_setup"
        environment = _environment(
            args,
            output / "workspace",
            image_base or manifest["base_commit"],
            settings,
        )
        phase = "model_setup"
        model = DeepSeekModel(settings, key)
        phase = "agent"
        result = Agent(model, environment, settings, trace).run(task, instructions)
    except KeyboardInterrupt:
        result = {"outcome": "cancelled", "stop_reason": phase, "summary": "Run interrupted."}
    except Exception as exc:
        errors.append({"stage": phase, "error_type": type(exc).__name__})
        result = {"outcome": "failed", "stop_reason": phase, "summary": "Run failed."}
    finally:
        result["agent_outcome"] = result["outcome"] if phase == "agent" else None
        result["agent_stop_reason"] = result["stop_reason"] if phase == "agent" else None
        if environment is not None:
            try:
                patch = environment.collect_patch()
            except BaseException as exc:
                errors.append({"stage": "patch_export", "error_type": type(exc).__name__})
            try:
                environment.close()
            except BaseException as exc:
                errors.append({"stage": "environment_close", "error_type": type(exc).__name__})
        if model is not None:
            try:
                model.close()
            except BaseException as exc:
                errors.append({"stage": "model_close", "error_type": type(exc).__name__})
        try:
            (output / "changes.patch").write_text(
                _clean(patch, key), encoding="utf-8", newline="\n"
            )
        except Exception as exc:
            errors.append({"stage": "patch_write", "error_type": type(exc).__name__})
        if errors and result["outcome"] == "completed":
            result.update(outcome="failed", stop_reason="artifact_or_cleanup_error")
        result.update(
            mode="agent",
            model=settings.model,
            backend=args.backend,
            output=str(output),
            errors=errors,
        )
        try:
            trace.result(result)
        except Exception as exc:
            errors.append({"stage": "result_write", "error_type": type(exc).__name__})
            if result["outcome"] == "completed":
                result.update(outcome="failed", stop_reason="artifact_or_cleanup_error")
    code = 130 if result["outcome"] == "cancelled" else int(result["outcome"] != "completed")
    return result, code


def _baseline(args: argparse.Namespace, settings: Settings, key: str) -> tuple[dict, int]:
    from .benchmark import generate_baseline, load_tasks

    if args.output.exists():
        raise CLIError("Baseline output directory must not already exist.")
    matches = [task for task in load_tasks(args.tasks) if task.instance_id == args.instance_id]
    if len(matches) != 1:
        raise CLIError("The selected instance does not exist in the task projection.")
    model = DeepSeekModel(settings, key)
    result = None
    cleanup_errors = []
    try:
        result = generate_baseline(
            matches[0],
            args.repo,
            model,
            settings,
            args.output,
            context_chars=args.context_chars,
        )
    finally:
        try:
            model.close()
        except Exception as exc:
            cleanup_errors.append({"stage": "model_close", "error_type": type(exc).__name__})
        if result is not None:
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors
                (args.output / "result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            # Only exports belonging to this completed generation are rewritten.
            for path in args.output.iterdir():
                if path.is_file() and path.suffix in {".json", ".patch"}:
                    original = path.read_text(encoding="utf-8")
                    clean = _clean(original, key)
                    if clean != original:
                        path.write_text(clean, encoding="utf-8", newline="\n")
    return result, 0 if result["status"] == "generated" and not cleanup_errors else 1


def _benchmark_command(args: argparse.Namespace) -> dict:
    from . import benchmark

    if args.command == "dataset":
        kwargs = {"limit": args.limit}
        if args.revision:
            kwargs["revision"] = args.revision
        return benchmark.prepare_dataset(args.output, **kwargs)
    if args.command == "grade-command":
        if args.manifest:
            benchmark.prepare_grading_data(args.grading_data, args.manifest)
        return {
            "argv": benchmark.evaluation_command(
                args.predictions,
                args.grading_data,
                args.run_id,
                workers=args.workers,
            ),
            "preflight": benchmark.evaluation_preflight(),
            "executed": False,
        }
    result = benchmark.summarize(
        benchmark.load_tasks(args.tasks),
        benchmark.read_official_reports(args.reports),
        args.run_dir,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    key = ""
    try:
        if args.command in {"probe", "run", "baseline"}:
            settings = _settings(args)
            key = _key(args.env_file)
            handler = {"probe": _probe, "run": _run, "baseline": _baseline}[args.command]
            result, code = handler(args, settings, key)
        else:
            result, code = _benchmark_command(args), 0
        _emit(result, key)
        return code
    except CLIError as exc:
        _emit({"outcome": "failed", "error": str(exc)}, key)
        return 2
    except KeyboardInterrupt:
        _emit({"outcome": "cancelled"}, key)
        return 130
    except Exception as exc:
        _emit({"outcome": "failed", "error_type": type(exc).__name__}, key)
        return 1


if __name__ == "__main__":
    sys.exit(main())
