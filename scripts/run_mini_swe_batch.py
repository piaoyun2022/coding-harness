"""Run pinned mini-SWE native SWE configuration; wrapper owns only lifecycle and artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import re
import signal
import subprocess
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

PIN = "04d809ceab9df28f9adaed044884180159172930"
SAFE_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "version",
    "environment_setup_commit",
)
LABEL = "deepseek-flash-mini-swe-2.4.6"


def save(path, value, secret=""):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    path.write_text(text + "\n", encoding="utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def docker(*args, timeout=60, check=True):
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=check,
    )


def inspect_image(tag):
    result = docker("image", "inspect", tag, check=False)
    if result.returncode:
        return None
    row = json.loads(result.stdout)[0]
    return {"id": row["Id"], "repo_digests": row.get("RepoDigests") or []}


def load_tasks(path):
    tasks = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        task = {key: str(row.get(key) or "") for key in SAFE_FIELDS}
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task["instance_id"]):
            raise ValueError("Invalid task identifier")
        if not re.fullmatch("[0-9a-f]{40}", task["base_commit"]) or not task["problem_statement"]:
            raise ValueError("Need immutable source and problem statement")
        tasks.append(task)
    if len(tasks) != 3 or len({t["instance_id"] for t in tasks}) != 3:
        raise ValueError("This experiment requires exactly three unique pinned tasks")
    return tasks


class HostDeadline(KeyboardInterrupt):
    """Escape native retry and exception handlers without changing its loop."""


@contextmanager
def deadline(seconds):
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(HostDeadline()))
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def protected_classes(agent_base, model_base, environment_base):
    class PrivateModel(model_base):
        def __init__(self, *, api_key, **kwargs):
            self._api_key = api_key
            self.adapter_attempts = 0
            super().__init__(**kwargs)

        def _query(self, messages, **kwargs):
            self.adapter_attempts += 1
            return super()._query(messages, api_key=self._api_key, **kwargs)

    class RedactedAgent(agent_base):
        def __init__(self, *args, secret, **kwargs):
            self._secret = secret
            super().__init__(*args, **kwargs)

        def save(self, path, *extra_dicts):
            data = self.serialize(*extra_dicts)
            if path:
                save(Path(path), data, self._secret)
            return data

    class OwnedDocker(environment_base):
        def __init__(self, *, owner, **kwargs):
            self.owner = owner
            self.cleaned = False
            try:
                super().__init__(**kwargs)
            except BaseException:
                self.cleanup()
                raise

        def cleanup(self):
            if getattr(self, "cleaned", True):
                return
            # Find only this invocation's label, including unknown docker-run outcomes.
            candidates = docker("ps", "-aq", "--filter", "label=mini-bench.owner=" + self.owner)
            for container in candidates.stdout.splitlines():
                label = docker(
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "mini-bench.owner" }}',
                    container,
                )
                if label.stdout.strip() != self.owner:
                    raise RuntimeError("Container ownership mismatch")
                docker("rm", "-f", container)
            self.cleaned = True
            self.container_id = None

    return RedactedAgent, PrivateModel, OwnedDocker


def verify_environment(env, requested):
    def command(text):
        result = env.execute({"command": text}, timeout=60)
        if result["returncode"]:
            raise ValueError("Cannot verify official image source")
        return result["output"].strip()

    head = command("git rev-parse HEAD")
    parents, subject = [], ""
    if not re.fullmatch("[0-9a-f]{40}", head):
        raise ValueError("Invalid image HEAD")
    if head != requested:
        parents = command("git show -s --format=%P HEAD").split()
        subject = command("git show -s --format=%s HEAD")
        if parents != [requested] or subject != "SWE-bench":
            raise ValueError("Unexpected setup history")
    if command("git status --porcelain --untracked-files=normal"):
        raise ValueError("Image must begin clean")
    return {
        "task_base_commit": requested,
        "patch_base_commit": head,
        "head_parents": parents,
        "head_subject": subject,
        "clean_verified": True,
    }


def usage(data):
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0}
    for message in data.get("messages", []):
        row = message.get("extra", {}).get("response", {})
        if not isinstance(row, dict):
            continue
        value = row.get("usage") or {}
        totals["input_tokens"] += value.get("prompt_tokens") or 0
        totals["output_tokens"] += value.get("completion_tokens") or 0
        details = value.get("prompt_tokens_details") or {}
        totals["cached_tokens"] += (
            value.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0
        )
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


def run_case(task, output, config, api_key, classes, *, host_seconds=900):
    output.mkdir(parents=True, exist_ok=False)
    save(output / "task.json", task, api_key)
    Agent, Model, Environment = classes
    image = (
        "swebench/sweb.eval.x86_64."
        + task["instance_id"].replace("__", "_1776_").lower()
        + ":latest"
    )
    owner = uuid.uuid4().hex
    env = agent = None
    original_image = current_image = None
    errors, info, diagnostic = [], {}, ""
    phase, started = "environment_setup", time.monotonic()
    agent_started = None
    agent_seconds = None
    exit_status = "SetupError"
    try:
        with deadline(host_seconds):
            original_image = inspect_image(image)
            if original_image is None:
                docker("pull", image, timeout=host_seconds)
            current_image = inspect_image(image)
            env_config = dict(config["environment"])
            env_config.pop("environment_class", None)
            env_config.update(
                image=image,
                forward_env=[],
                container_timeout=str(host_seconds + 120),
                run_args=[
                    "--rm",
                    "--label",
                    "mini-bench.owner=" + owner,
                    "--network=none",
                    "--cpus=2",
                    "--memory=4g",
                    "--pids-limit=256",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                ],
            )
            env = Environment(owner=owner, **env_config)
            source = verify_environment(env, task["base_commit"])
            save(output / "source.json", source | {"image": image, "image_identity": current_image})
            phase = "model_setup"
            model = Model(api_key=api_key, **config["model"])
            agent = Agent(
                model,
                env,
                secret=api_key,
                **(
                    config["agent"]
                    | {
                        "output_path": output / "trajectory.json",
                    }
                ),
            )
            save(
                output / "effective-config.json",
                agent.serialize()["info"]["config"]
                | {
                    "wrapper": {
                        "host_total_deadline_seconds": host_seconds,
                        "deadline_scope": "environment_setup_and_agent",
                        "cleanup_outside_deadline": True,
                    },
                },
                api_key,
            )
            phase = "agent"
            agent_started = time.monotonic()
            info = agent.run(task["problem_statement"])
            exit_status = info.get("exit_status") or "Unknown"
    except HostDeadline:
        exit_status = "HostDeadline"
    except KeyboardInterrupt:
        exit_status = "Cancelled"
    except Exception as error:
        errors.append({"stage": phase, "error_type": type(error).__name__})
        exit_status = type(error).__name__
    finally:
        if agent_started is not None:
            agent_seconds = round(time.monotonic() - agent_started, 3)
        if env:
            try:
                result = env.execute({"command": "git diff --binary HEAD"}, timeout=60)
                if result["returncode"] == 0:
                    diagnostic = result["output"]
                else:
                    errors.append({"stage": "diagnostic_patch", "error_type": "CommandFailed"})
            except Exception as error:
                errors.append({"stage": "diagnostic_patch", "error_type": type(error).__name__})
            try:
                env.cleanup()
            except Exception as error:
                errors.append({"stage": "environment_close", "error_type": type(error).__name__})
        if original_image is None and current_image:
            try:
                present = inspect_image(image)
                if present and present["id"] == current_image["id"]:
                    docker("image", "rm", image)
            except Exception as error:
                errors.append({"stage": "image_cleanup", "error_type": type(error).__name__})
    # Official batch runner uses submission, not an automatic workspace diff fallback.
    patch = info.get("submission") or ""
    patch, diagnostic = [text.replace(api_key, "[REDACTED]") for text in (patch, diagnostic)]
    if patch.strip():
        status = "generated"
    elif exit_status == "Cancelled":
        status = "agent_error"
    elif phase != "agent":
        status = "setup_error"
    elif errors:
        status = "agent_error"
    elif exit_status in {"RepeatedFormatError", "FormatError"}:
        status = "invalid_response"
    else:
        status = "empty_patch"
    outcome = (
        "completed"
        if exit_status == "Submitted"
        else (
            "budget_exhausted"
            if exit_status in {"LimitsExceeded", "TimeExceeded", "HostDeadline"}
            else "failed"
        )
    )
    if exit_status == "Cancelled":
        outcome = "cancelled"
    data = agent.save(output / "trajectory.json") if agent else {}
    result = {
        "instance_id": task["instance_id"],
        "mode": "mini-swe-agent",
        "status": status,
        "outcome": outcome,
        "stop_reason": exit_status,
        "native_submission": bool(patch.strip()),
        "diagnostic_patch_bytes": len(diagnostic.encode()),
        "patch_sha256": sha(patch.encode()),
        "steps": agent.n_calls if agent else 0,
        "usage": usage(data),
        "native_cost_usd": agent.cost if agent else 0.0,
        "adapter_attempts": agent.model.adapter_attempts if agent else 0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "agent_seconds": agent_seconds,
        "host_total_deadline_seconds": host_seconds,
        "deadline_scope": "environment_setup_and_agent",
        "cleanup_outside_deadline": True,
        "errors": errors,
    }
    (output / "changes.patch").write_bytes(patch.encode())
    (output / "diagnostic-workspace.patch").write_bytes(diagnostic.encode())
    save(
        output / "prediction.json",
        {"instance_id": task["instance_id"], "model_name_or_path": LABEL, "model_patch": patch},
        api_key,
    )
    save(output / "result.json", result, api_key)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("tasks", "output", "settings", "env-file", "vendor"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if platform.system() != "Linux":
            raise ValueError("Run this wrapper inside the dedicated WSL environment")
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        import minisweagent
        import yaml
        from dotenv import dotenv_values
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.environments.docker import DockerEnvironment
        from minisweagent.models.litellm_model import LitellmModel

        vendor = args.vendor.resolve()
        head = subprocess.run(
            ["git", "-C", str(vendor), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if head != PIN or not Path(minisweagent.__file__).resolve().is_relative_to(vendor):
            raise ValueError("mini-SWE source does not match the pinned installation")
        subprocess.run(
            ["git", "-C", str(vendor), "diff", "--exit-code", PIN], capture_output=True, check=True
        )
        tasks = load_tasks(args.tasks)
        settings = json.loads(args.settings.read_text())
        native_path = vendor / "src/minisweagent/config/benchmarks/swebench.yaml"
        config = yaml.safe_load(native_path.read_text())
        config["model"].update(settings["model"])
        config["model"]["cost_tracking"] = "default"
        config["model"]["litellm_model_registry"] = None
        config["model"].pop("model_class", None)
        if config["agent"]["step_limit"] != 250 or config["agent"]["cost_limit"] != 3.0:
            raise ValueError("Unexpected native SWE defaults")
        key = dotenv_values(args.env_file, interpolate=False).get("DEEPSEEK_API_KEY") or ""
        if not key:
            raise ValueError("Explicit key is required")
        logging.disable(logging.CRITICAL)
        args.output.mkdir(parents=True, exist_ok=False)
        save(
            args.output / "plan.json",
            {
                "mode": "mini-swe-agent",
                "planned": len(tasks),
                "vendor_commit": PIN,
                "vendor_version": minisweagent.__version__,
                "native_config": config,
                "tasks_sha256": sha(args.tasks.read_bytes()),
                "settings": settings,
                "native_config_sha256": sha(native_path.read_bytes()),
                "host_wall_seconds": 900,
                "cost_map_source": "installed LiteLLM local cost map",
                "wrapper_sha256": sha(Path(__file__).read_bytes()),
                "native_step_limit": 250,
                "native_cost_limit_usd": 3.0,
                "primary_patch_policy": "native submission only; workspace diff is diagnostic",
                "dependency_versions": {
                    name: importlib.metadata.version(name)
                    for name in ["mini-swe-agent", "litellm", "openai"]
                },
            },
        )
        classes = protected_classes(DefaultAgent, LitellmModel, DockerEnvironment)
        rows = []
        for task in tasks:
            save(
                args.output / "progress.json",
                {"planned": 3, "completed": len(rows), "active_instance": task["instance_id"]},
            )
            result = run_case(
                task, args.output / "runs" / task["instance_id"], config, key, classes
            )
            rows.append(result)
            print(
                json.dumps(
                    {k: result[k] for k in ["instance_id", "status", "stop_reason", "steps"]}
                ),
                flush=True,
            )
            if result["stop_reason"] == "Cancelled" or any(
                x["stage"] in {"environment_close", "image_cleanup"} for x in result["errors"]
            ):
                break
        predictions = []
        for task in tasks:
            path = args.output / "runs" / task["instance_id"] / "prediction.json"
            predictions.append(
                json.loads(path.read_text())
                if path.exists()
                else {
                    "instance_id": task["instance_id"],
                    "model_name_or_path": LABEL,
                    "model_patch": "",
                }
            )
        (args.output / "predictions.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in predictions),
            encoding="utf-8",
        )
        report = {
            "mode": "mini-swe-agent",
            "planned": 3,
            "completed": len(rows),
            "statuses": dict(Counter(row["status"] for row in rows)),
            "graded": False,
        }
        save(args.output / "batch-result.json", report)
        save(args.output / "progress.json", report | {"active_instance": None})
        return 0 if len(rows) == 3 else 1
    except Exception as error:
        print(json.dumps({"status": "batch_error", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
