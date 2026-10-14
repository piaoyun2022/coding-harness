"""Regression tests for benchmark isolation, one-shot generation, and score accounting."""
import hashlib
import json
import subprocess
from dataclasses import replace

import pytest

from coding_harness import benchmark as bench
from coding_harness.config import Settings
from coding_harness.contracts import Reply, Usage


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "average.py").write_text("def average(xs):\n    return sum(xs) / len(xs)\n")
    (root / "unrelated.py").write_text("value = 42\n")
    (root / ".env").write_text("HIDDEN_VALUE=do-not-retrieve\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return root


@pytest.fixture
def task(source):
    return bench.Task("demo__project-1", "demo/project", git(source, "rev-parse", "HEAD"),
                      "average should return zero for an empty list", "1")


def test_projection_discards_gold_and_hints(task):
    row = vars(task) | {"patch": "GOLD", "test_patch": "HIDDEN_TEST",
                       "hints_text": "ORACLE", "FAIL_TO_PASS": ["secret_test"]}
    assert vars(bench.Task.from_row(row)) == vars(task)
    assert "GOLD" not in json.dumps(vars(bench.Task.from_row(row)))


@pytest.mark.parametrize("bad_id", ["../outside", "C:\\outside", "x/y", ""])
def test_unsafe_task_identifier_rejected(task, bad_id):
    with pytest.raises(ValueError):
        replace(task, instance_id=bad_id)


def test_retrieval_is_deterministic_pinned_and_secret_filtered(source, task):
    before = bench.retrieve_context(task, source, max_chars=2000)
    (source / "average.py").write_text("NEW_OR_GOLD_IMPLEMENTATION")
    after = bench.retrieve_context(task, source, max_chars=2000)
    assert before == after
    assert before["files"][0]["path"] == "average.py"
    assert "NEW_OR_GOLD_IMPLEMENTATION" not in before["context"]
    assert "do-not-retrieve" not in before["context"]
    assert len(before["context"]) <= 2000


class FakeModel:
    def __init__(self, content, finish_reason="stop", error=False):
        self.calls = []
        self.content, self.finish_reason, self.error = content, finish_reason, error

    def complete(self, messages, tools, **kwargs):
        self.calls.append((messages, tools, kwargs))
        if self.error:
            raise RuntimeError("sensitive-provider-exception")
        return Reply({"role": "assistant", "content": self.content},
                     Usage(input_tokens=10, output_tokens=20), self.finish_reason)


PATCH = ("diff --git a/average.py b/average.py\n--- a/average.py\n+++ b/average.py\n"
         "@@ -1,2 +1,2 @@\n def average(xs):\n-    return sum(xs) / len(xs)\n"
         "+    return sum(xs) / len(xs) if xs else 0\n")


def test_baseline_one_request_no_tools_no_repository_execution(source, task, tmp_path):
    model = FakeModel(PATCH)
    output = tmp_path / "run"
    before = (source / "average.py").read_text()
    result = bench.generate_baseline(task, source, model, Settings(), output)
    assert len(model.calls) == 1
    assert model.calls[0][1] == []
    assert model.calls[0][2]["purpose"] == "baseline"
    assert result["status"] == "generated"
    assert result["usage"]["total_tokens"] == 30
    assert (output / "changes.patch").read_text() == PATCH
    assert (source / "average.py").read_text() == before
    assert (output / "request.json").exists()
    with pytest.raises(FileExistsError):
        bench.generate_baseline(task, source, model, Settings(), output)
    assert len(model.calls) == 1


@pytest.mark.parametrize(("content", "reason", "error", "status"), [
    (PATCH, "length", False, "invalid_response"),
    ("It is fixed", "stop", False, "empty_or_invalid_patch"),
    ("", "stop", True, "model_error"),
])
def test_failed_generation_is_recorded_without_retry(
    source, task, tmp_path, content, reason, error, status,
):
    output = tmp_path / status
    model = FakeModel(content, reason, error)
    result = bench.generate_baseline(task, source, model, Settings(), output)
    assert result["status"] == status
    assert len(model.calls) == 1
    prediction = json.loads((output / "prediction.json").read_text())
    assert prediction["model_patch"] == ""
    assert "sensitive-provider-exception" not in (output / "result.json").read_text()


def test_prepare_full_dataset_is_projected_and_checksum_verified(tmp_path, monkeypatch, task):
    rows = [vars(replace(task, instance_id=f"demo__project-{i:03}")) | {"patch": "gold"}
            for i in range(300)]
    calls = []

    def parquet(revision, *, columns):
        calls.append(columns)
        return rows, {"revision": revision, "source_sha256": "abc"}

    monkeypatch.setattr(bench, "_parquet", parquet)
    output = tmp_path / "dataset"
    manifest = bench.prepare_dataset(output)
    assert manifest["planned"] == 300
    assert calls == [bench.SAFE_COLUMNS]
    assert "gold" not in (output / "tasks.jsonl").read_text()
    assert len(bench.load_tasks(output / "tasks.jsonl")) == 300
    with (output / "tasks.jsonl").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="checksum"):
        bench.load_tasks(output / "tasks.jsonl")


def test_grading_revision_checksum_must_match(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"revision": bench.REVISION, "source_sha256": "expected"}))
    monkeypatch.setattr(bench, "_parquet", lambda *a, **kw: ([], {"source_sha256": "wrong"}))
    with pytest.raises(ValueError, match="match"):
        bench.prepare_grading_data(tmp_path / "grade.json", manifest)


def test_prediction_aggregation_preserves_missing_tasks(tmp_path, task):
    tasks = [task, replace(task, instance_id="demo__project-2")]
    run = tmp_path / "run"
    case = run / task.instance_id
    case.mkdir(parents=True)
    (case / "prediction.json").write_text(json.dumps({
        "instance_id": task.instance_id, "model_patch": PATCH,
    }))
    path = bench.write_predictions(tasks, run, tmp_path / "predictions.jsonl", model_name="run")
    predictions = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(predictions) == 2
    assert predictions[1]["model_patch"] == ""


def test_scores_keep_planned_denominator_and_separate_infrastructure(tmp_path, task):
    tasks = [replace(task, instance_id=f"demo__project-{i}") for i in range(6)]
    reports = {
        tasks[0].instance_id: {"resolved": True, "tests_status": {
            "FAIL_TO_PASS": {"success": ["a"], "failure": []},
            "PASS_TO_PASS": {"success": ["b"], "failure": []},
        }},
        tasks[1].instance_id: {"resolved": False, "tests_status": {
            "FAIL_TO_PASS": {"success": ["a"], "failure": ["b"]},
            "PASS_TO_PASS": {"success": ["x"], "failure": ["y"]},
        }},
        tasks[2].instance_id: {"resolved": False, "infra_failure": True},
    }
    run = tmp_path / "run"
    for index in range(3):
        case = run / tasks[index].instance_id
        case.mkdir(parents=True)
        (case / "prediction.json").write_text(json.dumps({
            "instance_id": tasks[index].instance_id, "model_patch": PATCH,
        }))
        reports[tasks[index].instance_id]["_evaluation_patch_sha256"] = (
            hashlib.sha256(PATCH.encode()).hexdigest())
    for index, status in [(3, "generated"), (4, "empty_or_invalid_patch")]:
        case = run / tasks[index].instance_id
        case.mkdir(parents=True)
        (case / "result.json").write_text(json.dumps({"status": status}))
    result = bench.summarize(tasks, reports, run)
    assert result["planned"] == 6
    assert result["attempted"] == 5
    assert result["graded"] == 2
    assert result["resolved_rate_planned"] == pytest.approx(1 / 6)
    assert result["infrastructure_errors"] == 1
    assert result["missing_reports"] == 1
    assert result["empty_predictions"] == 1
    assert result["unattempted"] == 1
    assert result["provisional"] is True
    assert result["official_score_available"] is False
    assert result["diagnostic_repair_fraction"] == .75
    assert result["diagnostic_preservation_fraction"] == .75


def test_no_fake_zero_ability_score_when_nothing_was_graded(task):
    result = bench.summarize([task], {})
    assert result["official_score_available"] is False
    assert result["provisional"] is True
    assert result["graded"] == 0
    assert result["diagnostic_repair_fraction"] is None


def test_official_report_reader_rejects_mixed_runs(tmp_path, task):
    for name in ("run_a", "run_b"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "report.json").write_text(json.dumps({task.instance_id: {"resolved": False}}))
    with pytest.raises(ValueError, match="Multiple"):
        bench.read_official_reports(tmp_path)


def test_official_command_uses_pinned_local_dataset_and_bounded_workers(tmp_path):
    argv = bench.evaluation_command(tmp_path / "predictions.jsonl", tmp_path / "grading.json",
                                    "unique-run")
    assert "swebench.harness.run_evaluation" in argv
    assert argv[argv.index("--max_workers") + 1] == "1"
    assert argv[argv.index("--dataset_name") + 1] == str((tmp_path / "grading.json").resolve())
    with pytest.raises(ValueError):
        bench.evaluation_command(tmp_path / "p", tmp_path / "d", "../bad")
    with pytest.raises(ValueError):
        bench.evaluation_command(tmp_path / "p", tmp_path / "d", "run", workers=99)


def test_symlink_git_mode_is_not_retrieved(source, task):
    # Index insertion avoids Windows symlink privileges; the target is never dereferenced.
    blob = subprocess.run(["git", "-C", str(source), "hash-object", "-w", "--stdin"],
                          input="outside-secret", text=True, capture_output=True, check=True).stdout
    git(source, "update-index", "--add", "--cacheinfo", f"120000,{blob.strip()},average-link")
    git(source, "commit", "-m", "symlink")
    current = replace(task, base_commit=git(source, "rev-parse", "HEAD"))
    result = bench.retrieve_context(current, source)
    assert "average-link" not in [entry["path"] for entry in result["files"]]



def test_windows_preflight_requires_linux_python(monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    result = bench.evaluation_preflight()
    assert result["ready"] is False
    assert "WSL" in result["reason"]


def test_floating_dataset_revision_rejected_before_fetch():
    with pytest.raises(ValueError, match="immutable"):
        bench._parquet("main", columns=bench.SAFE_COLUMNS)


def test_empty_predictions_without_any_official_reports_are_not_an_official_score(tmp_path, task):
    case = tmp_path / task.instance_id
    case.mkdir()
    (case / "result.json").write_text(json.dumps({"status": "empty_or_invalid_patch"}))
    result = bench.summarize([task], {}, tmp_path)
    assert result["empty_predictions"] == 1
    assert result["official_score_available"] is False


def bound_report_fixture(tmp_path, task, generated_patch, evaluated_patch):
    run = tmp_path / "generation"
    case = run / task.instance_id
    case.mkdir(parents=True)
    (case / "result.json").write_text(json.dumps({"status": "generated"}))
    (case / "prediction.json").write_text(json.dumps({
        "instance_id": task.instance_id, "model_patch": generated_patch,
    }))
    (case / "changes.patch").write_bytes(generated_patch.encode("utf-8"))
    official = tmp_path / "official" / task.instance_id
    official.mkdir(parents=True)
    (official / "report.json").write_text(json.dumps({task.instance_id: {"resolved": True}}))
    if evaluated_patch is not None:
        (official / "patch.diff").write_bytes(evaluated_patch.encode("utf-8"))
    return run, bench.read_official_reports(tmp_path / "official")


def test_old_success_report_cannot_score_a_different_current_patch(tmp_path, task):
    run, reports = bound_report_fixture(tmp_path, task, PATCH + "invalid current change", PATCH)
    result = bench.summarize([task], reports, run)
    assert result["resolved"] == 0
    assert result["graded"] == 0
    assert result["unverified_reports"] == 1
    assert result["rows"][0]["report_binding"] == "patch_mismatch"
    assert result["provisional"] is True
    assert result["official_score_available"] is False


def test_matching_official_patch_binds_report_to_current_generation(tmp_path, task):
    run, reports = bound_report_fixture(tmp_path, task, PATCH, PATCH)
    result = bench.summarize([task], reports, run)
    assert result["resolved"] == 1
    assert result["verified_reports"] == 1
    assert result["official_score_available"] is True
    assert result["score_scope"] == "current_run_verified_patches"
    assert result["comparison_to_current_run"] is True


def test_report_without_original_evaluated_patch_remains_unverified(tmp_path, task):
    run, reports = bound_report_fixture(tmp_path, task, PATCH, None)
    result = bench.summarize([task], reports, run)
    assert result["unverified_reports"] == 1
    assert result["rows"][0]["report_binding"] == "missing_evaluation_patch"
    assert result["official_score_available"] is False


def test_standalone_reports_explicitly_do_not_claim_current_run_binding(task):
    result = bench.summarize([task], {task.instance_id: {"resolved": True}})
    assert result["resolved"] == 1
    assert result["score_scope"] == "provided_reports_only"
    assert result["comparison_to_current_run"] is False
    assert result["verified_reports"] == 0
    assert result["rows"][0]["report_binding"] == "not_checked_against_generation"


def test_conflicting_generation_artifacts_are_rejected(tmp_path, task):
    run, reports = bound_report_fixture(tmp_path, task, PATCH, PATCH)
    (run / task.instance_id / "changes.patch").write_bytes(b"a different exported patch")
    result = bench.summarize([task], reports, run)
    assert result["rows"][0]["report_binding"] == "generation_artifact_mismatch"
    assert result["official_score_available"] is False


@pytest.mark.parametrize("status", ["empty_or_invalid_patch", "invalid_response"])
def test_invalid_generation_is_not_misclassified_as_infrastructure(tmp_path, task, status):
    case = tmp_path / task.instance_id
    case.mkdir()
    (case / "result.json").write_text(json.dumps({"status": status}))
    result = bench.summarize([task], {}, tmp_path)
    assert result["empty_predictions"] == 1
    assert result["infrastructure_errors"] == 0
    assert result["missing_reports"] == 0
    assert result["planned"] == 1
