"""Per-task image ownership for the public evaluation pipeline; no model logic."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from run_agent_batch import BenchmarkDocker, DockerEnvironment, image_name

GIB = 1024 ** 3


class ResourceError(RuntimeError):
    """Safe, user-actionable capacity error; contains no external exception text."""


def inspect_image(tag: str) -> dict | None:
    probe = DockerEnvironment._docker(["image", "inspect", tag], timeout=30, check=False)
    if probe.returncode:
        return None
    value = json.loads(probe.stdout)[0]
    return {"id": value["Id"], "tag": tag, "repo_digests": value.get("RepoDigests", []),
            "layers": value.get("RootFS", {}).get("Layers", [])}


def available_memory() -> int | None:
    path = Path("/proc/meminfo")
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return None


def check_resources(disk: Path, *, min_disk_gib: float, min_memory_gib: float = 0) -> dict:
    free = shutil.disk_usage(disk).free
    memory = available_memory()
    if free < min_disk_gib * GIB or (memory is not None and memory < min_memory_gib * GIB):
        raise ResourceError(
            f"Need {min_disk_gib:g} GiB free on {disk} and {min_memory_gib:g} GiB "
            "available Linux memory. Free space/memory or lower --workers; "
            "--docker-disk must name the Docker backing disk.")
    return {"disk_free_bytes": free, "memory_available_bytes": memory}


def remove_exact(tag: str, identity: str) -> dict:
    current = inspect_image(tag)
    if current is None:
        return {"tag": tag, "state": "absent"}
    if current["id"] != identity:
        return {"tag": tag, "state": "changed_id_retained"}
    result = DockerEnvironment._docker(
        ["image", "rm", "--no-prune", tag], timeout=120, check=False)
    return {"tag": tag, "state": "removed" if result.returncode == 0 else "remove_failed"}


class SourceImage:
    """Keep a source through generation and grading; remove only a proven new, idle tag."""

    def __init__(self, task):
        self.tag = image_name(task)
        self.metadata = {"tag": self.tag, "pulled": False}
        self.owned_id: str | None = None
        self.info: dict | None = None

    def acquire(self):
        if self.info is not None:
            return self.info
        before = inspect_image(self.tag)
        self.metadata["present_before"] = before is not None
        if before is None:
            # An image ID already present with another tag remains user-owned.
            prior = DockerEnvironment._docker(["images", "--no-trunc", "--quiet"], timeout=30)
            prior_ids = set(prior.stdout.splitlines())
            # Record the before-state before pulling, including on a failed pull.
            self.metadata["prior_image_ids"] = sorted(prior_ids)
            DockerEnvironment._docker(["pull", self.tag], timeout=1200)
            self.metadata["pulled"] = True
        self.info = inspect_image(self.tag)
        if self.info is None:
            raise RuntimeError("Official source image is unavailable")
        self.metadata["image"] = self.info
        if before is None and self.info["id"] not in prior_ids:
            self.owned_id = self.info["id"]
        return self.info

    def verify(self) -> str:
        current = inspect_image(self.tag)
        if self.info is None or current is None or current["id"] != self.info["id"]:
            raise RuntimeError("Source image identity changed before grading")
        return current["id"]

    def close(self) -> dict:
        if self.owned_id is None:
            return self.metadata | {"cleanup": "preserved_preexisting_or_unconfirmed"}
        current = inspect_image(self.tag)
        if current is None or current["id"] != self.owned_id:
            return self.metadata | {"cleanup": "absent_or_changed_id_retained"}
        # An overlay may reference this source without Config.Image naming its tag.
        listed = DockerEnvironment._docker(["ps", "-aq"], timeout=30)
        layers = current["layers"]
        for container in listed.stdout.splitlines():
            probe = DockerEnvironment._docker(["inspect", container], timeout=30, check=False)
            if probe.returncode:
                continue  # Container disappeared between list and inspect.
            image = inspect_image(json.loads(probe.stdout)[0]["Image"])
            if image is None or not layers or image["layers"][:len(layers)] == layers:
                return self.metadata | {"cleanup": "retained_container_reference"}
        receipt = remove_exact(self.tag, self.owned_id)
        return self.metadata | {"cleanup": receipt["state"]}


def private_environment(source: SourceImage, run_id: str):
    """A unique FROM alias makes each overlay private while sharing immutable Docker layers."""

    class PrivateDocker(BenchmarkDocker):
        def __init__(self, task, output, settings):
            suffix = hashlib.sha256(task.instance_id.encode()).hexdigest()[:12]
            self.alias = "coding-harness-eval:" + run_id + "-" + suffix
            self.private: dict[str, str] = {}
            self.private_closed = False
            super().__init__(task, output, settings)

        def _runtime_image(self, image):
            info = source.acquire()
            if inspect_image(self.alias) is not None:
                raise ValueError("Private image alias already exists")
            self._docker(["tag", info["id"], self.alias], timeout=30)
            self.private[self.alias] = info["id"]
            self._candidates.discard(image)
            self.source_metadata.update(
                base_image=info, private_base_alias=self.alias, image_policy="private_overlay",
                shared_source_cleanup="pipeline_after_grading",
            )
            try:
                runtime = DockerEnvironment._runtime_image(self, self.alias)
                runtime_info = inspect_image(runtime)
                if runtime_info is None:
                    raise RuntimeError("Runtime image is missing")
                self.private[runtime] = runtime_info["id"]
                self.source_metadata["runtime_image"] = runtime_info
                return runtime_info["id"]
            finally:
                for candidate in self._candidates:
                    if candidate.startswith("coding-harness-tools:"):
                        found = inspect_image(candidate)
                        if found:
                            self.private[candidate] = found["id"]
                self.source_metadata["private_owned_tags"] = dict(self.private)

        def close(self):
            DockerEnvironment.close(self)
            if self.private_closed:
                return
            self.private_closed = True
            for tag in sorted(self.private, key=lambda value: value == self.alias):
                try:
                    receipt = remove_exact(tag, self.private[tag])
                    if receipt["state"] not in {"removed", "absent"}:
                        self.cleanup_errors.append(
                            {"stage": "image_cleanup", "error_type": receipt["state"]})
                except Exception as error:
                    self.cleanup_errors.append(
                        {"stage": "image_cleanup", "error_type": type(error).__name__})

    return PrivateDocker

