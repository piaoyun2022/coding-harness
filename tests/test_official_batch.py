import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/run_official_batch.py'
SPEC = importlib.util.spec_from_file_location('official_batch', SCRIPT)
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def generated(root, instance_id='fixture-1', status='generated', patch='diff --git a/a b/a\n'):
    directory = root / 'runs' / instance_id
    dump(directory / 'prediction.json', {'instance_id': instance_id, 'model_name_or_path': 'model',
                                         'model_patch': patch})
    (directory / 'changes.patch').write_bytes(patch.encode())
    dump(directory / 'result.json', {'instance_id': instance_id, 'status': status,
                                     'usage': {'total_tokens': 100}})
    return directory


def official(directory, patch, resolved=True):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'patch.diff').write_bytes(patch.encode())
    dump(directory / 'report.json', {'fixture-1': {
        'resolved': resolved, 'tests_status': {
            'FAIL_TO_PASS': {'success': ['a'], 'failure': ['b']},
            'PASS_TO_PASS': {'success': ['c'], 'failure': []},
        },
    }})


def rejection(instance_id='fixture-1'):
    prefix = '2026-10-14 17:05:13,740 - INFO - '
    return (prefix + '>>>>> Patch Apply Failed:\nHunk FAILED\n' + prefix
            + 'Traceback (most recent call last):\n'
            + '  File "/opt/eval/swebench/harness/run_evaluation.py", line 182, in run_instance\n'
            + '    raise EvaluationError(\n'
            + f'swebench.harness.utils.EvaluationError: {instance_id}: '
            + '>>>>> Patch Apply Failed:\nHunk FAILED\n')


def test_plan_requires_same_task_ids_and_baselines(tmp_path):
    row = {'instance_id': 'fixture-1', 'repo': 'a/b', 'base_commit': 'abc', 'version': '1'}
    tasks, grading = tmp_path / 'tasks.jsonl', tmp_path / 'grading.json'
    dump(tasks, row)
    dump(grading, [row | {'patch': 'gold stays with grader'}])
    ids, rows = batch.validate_plan(tasks, grading)
    assert ids == ['fixture-1'] and rows['fixture-1']['patch'] == 'gold stays with grader'
    dump(grading, [row | {'base_commit': 'wrong'}])
    with pytest.raises(ValueError, match='baseline'):
        batch.validate_plan(tasks, grading)
    dump(grading, [row, row])
    with pytest.raises(ValueError, match='IDs'):
        batch.validate_plan(tasks, grading)


@pytest.mark.parametrize(('status', 'expected'), [
    ('invalid_response', 'generation_invalid'), ('empty_or_invalid_patch', 'generation_invalid'),
    ('model_error', 'api_error'), ('setup_error', 'generation_setup_error'),
    ('unexpected', 'infra_unknown_generation_status'),
])
def test_generation_failures_are_distinct_without_needing_images(tmp_path, status, expected):
    generated(tmp_path, status=status)
    row, prediction = batch.generation_input(tmp_path, 'fixture-1')
    assert row['status'] == expected and prediction is None


def test_prediction_and_changes_patch_must_match_before_grading(tmp_path):
    directory = generated(tmp_path)
    row, prediction = batch.generation_input(tmp_path, 'fixture-1')
    assert row['patch_sha256'] == batch.sha(prediction['model_patch'].encode())
    (directory / 'changes.patch').write_text('old patch')
    row, prediction = batch.generation_input(tmp_path, 'fixture-1')
    assert row['status'] == 'infra_generation_artifacts' and prediction is None


def test_concurrent_partial_result_is_pending(tmp_path):
    directory = generated(tmp_path)
    (directory / 'result.json').write_text('{')
    assert batch.generation_input(tmp_path, 'fixture-1') is None


def test_old_official_report_is_not_bound_to_new_prediction(tmp_path):
    official(tmp_path, 'old patch')
    result = batch.classify_official(tmp_path, 'fixture-1', batch.sha(b'new patch'))
    assert result['status'] == 'infra_unbound_official_patch' and not result['resolved']


def test_bound_report_uses_official_boolean_and_test_counts(tmp_path):
    official(tmp_path, 'patch')
    result = batch.classify_official(tmp_path, 'fixture-1', batch.sha(b'patch'))
    assert result['status'] == 'graded' and result['resolved'] is True
    assert result['tests']['FAIL_TO_PASS'] == {'passed': 1, 'failed': 1}
    official(tmp_path, 'patch', resolved='true')
    assert batch.classify_official(tmp_path, 'fixture-1', batch.sha(b'patch'))[
        'status'] == 'infra_invalid_report'


def test_patch_rejection_requires_bound_patch_and_framework_traceback(tmp_path):
    (tmp_path / 'patch.diff').write_bytes(b'patch')
    log = tmp_path / 'run_instance.log'
    log.write_text(rejection())
    result = batch.classify_official(tmp_path, 'fixture-1', batch.sha(b'patch'))
    assert result['status'] == 'patch_rejected' and result['patch_binding_verified']
    for content in ['>>>>> Patch Apply Failed:', rejection('other-instance'),
                    '2026-10-14 17:05:13,740 - INFO - >>>>> Patch Apply Failed:\n']:
        log.write_text(content)
        assert batch.classify_official(tmp_path, 'fixture-1', batch.sha(b'patch'))[
            'status'] == 'infra_missing_report'
    log.write_text(rejection())
    assert batch.classify_official(tmp_path, 'fixture-1', batch.sha(b'other'))[
        'status'] == 'infra_unbound_official_patch'


def test_fixed_denominator_distinguishes_observed_lower_bound_from_complete_score():
    ids = [f'fixture-{i}' for i in range(300)]
    rows = {ids[0]: {'status': 'graded', 'resolved': True,
                    'tests': {'FAIL_TO_PASS': {'passed': 1, 'failed': 1}}},
            ids[1]: {'status': 'generation_invalid'}, ids[2]: {'status': 'api_error'}}
    result = batch.summarize(ids, rows)
    assert result['planned'] == 300 and result['pending'] == 297
    assert result['official_resolved_rate'] is None and result['provisional']
    assert result['observed_resolved_lower_bound'] == 1 / 300
    assert result['diagnostic']['FAIL_TO_PASS'] == {'coverage': 1, 'mean_task_fraction': .5}
    for instance_id in ids[2:]:
        rows[instance_id] = {'status': 'patch_rejected'}
    complete = batch.summarize(ids, rows)
    assert not complete['provisional'] and complete['official_resolved_rate'] == 1 / 300


class Images:
    def __init__(self, image):
        self.image, self.removed = image, []

    def get(self, name):
        return self.image

    def remove(self, name, **kwargs):
        self.removed.append((name, kwargs))


@pytest.mark.parametrize(('before', 'expected'), [
    ({'tag_existed': True, 'all_image_ids': []}, 'preserved_preexisting_tag'),
    ({'tag_existed': False, 'all_image_ids': ['original']}, 'preserved_preexisting_image_id'),
    ({'tag_existed': False, 'all_image_ids': []}, 'removed_exact_new_tag'),
])
def test_cleanup_preserves_preexisting_images_and_only_untags_exact_new_image(before, expected):
    images = Images(SimpleNamespace(id='original', tags=['swebench/task:latest']))
    result = batch.cleanup_image(SimpleNamespace(images=images), 'swebench/task:latest', before)
    assert result['image_cleanup'] == expected
    assert images.removed == ([('swebench/task:latest', {'force': False, 'noprune': True})]
                              if expected == 'removed_exact_new_tag' else [])


def test_container_cleanup_requires_exact_unique_name_and_expected_image():
    removed = []
    container = SimpleNamespace(name='owned-run', id='id', attrs={'Config': {'Image': 'image'}},
                                remove=lambda **kwargs: removed.append(kwargs))
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: container))
    assert batch.cleanup_container(client, 'different', 'image')[
        'container_cleanup'] == 'ownership_not_verified'
    assert batch.cleanup_container(client, 'owned-run', 'other-image')[
        'container_cleanup'] == 'ownership_not_verified'
    assert not removed
    assert batch.cleanup_container(client, 'owned-run', 'image')[
        'container_cleanup'] == 'removed_exact_owned'
    assert removed == [{'force': True, 'v': True}]


def test_timeout_terminates_only_owned_process_group_and_does_not_forward_secrets(
    tmp_path, monkeypatch,
):
    signals, invocation = [], {}
    calls = 0

    class Process:
        pid = 98765

        def poll(self):
            return None

        def wait(self, timeout):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise subprocess.TimeoutExpired('owned', timeout)
            return -9

    def popen(*args, **kwargs):
        invocation.update(kwargs)
        return Process()

    monkeypatch.setattr(batch.subprocess, 'Popen', popen)
    monkeypatch.setattr(batch.os, 'killpg', lambda pid, sig: signals.append((pid, sig)),
                        raising=False)
    monkeypatch.setattr(batch.signal, 'SIGKILL', 9, raising=False)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'never-forward')
    result = batch.run_process(['official'], tmp_path, tmp_path / 'log', 20)
    assert result == {'runner_timed_out': True}
    assert invocation['start_new_session'] is True
    assert 'DEEPSEEK_API_KEY' not in invocation['env']
    assert [pid for pid, _ in signals] == [98765, 98765]
    assert [sig for _, sig in signals] == [batch.signal.SIGTERM, batch.signal.SIGKILL]


class FakeGrader:
    def __init__(self):
        self.calls = []

    def grade(self, row, prediction):
        self.calls.append(row['instance_id'])
        return row | {'status': 'graded', 'resolved': True, 'patch_binding_verified': True}


def test_batch_waits_for_concurrent_generation_without_hour_deadline(tmp_path, monkeypatch):
    generation, output = tmp_path / 'generation', tmp_path / 'output'
    generated(generation, 'fixture-failed', status='invalid_response')
    generated(generation, 'fixture-api', status='model_error')
    ticks = 0

    def later(seconds):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            generated(generation, 'fixture-ready')

    monkeypatch.setattr(batch.time, 'sleep', later)
    grader = FakeGrader()
    ids = ['fixture-failed', 'fixture-api', 'fixture-ready']
    result = batch.run_batch(ids, generation, output, grader, {'version': 1}, poll_seconds=1)
    assert ticks == 2 and grader.calls == ['fixture-ready']
    assert result['planned'] == result['completed'] == 3
    assert result['statuses'] == {'generation_invalid': 1, 'api_error': 1, 'graded': 1}
    assert result['official_resolved_rate'] is None
    assert batch.read_json(output / 'progress.json')['completed'] == 3


def test_missing_results_after_generation_done_are_infra_and_keep_denominator(tmp_path):
    generation = tmp_path / 'generation'
    dump(generation / 'batch-result.json', {'completed': 1})
    grader = FakeGrader()
    result = batch.run_batch(['missing'], generation, tmp_path / 'output', grader, {})
    assert result['statuses'] == {'infra_generation_missing': 1}
    assert result['planned'] == 1 and result['provisional'] and not grader.calls


def test_resume_only_regrades_unknown_results_and_rejects_changed_inputs(tmp_path):
    generation, output = tmp_path / 'generation', tmp_path / 'output'
    for name in ['good', 'unknown', 'invalid']:
        generated(generation, name, status='invalid_response' if name == 'invalid' else 'generated')
    fingerprint = {'input': 'pinned'}
    dump(output / 'progress.json', {'fingerprint': fingerprint, 'rows': [
        {'instance_id': 'good', 'status': 'graded', 'resolved': True},
        {'instance_id': 'unknown', 'status': 'infra_missing_report', 'resolved': False},
        {'instance_id': 'invalid', 'status': 'generation_invalid', 'resolved': False},
    ]})
    grader = FakeGrader()
    result = batch.run_batch(['good', 'unknown', 'invalid'], generation, output, grader,
                             fingerprint, resume=True, retry_infra=True)
    assert grader.calls == ['unknown'] and result['resolved'] == 2 and not result['provisional']
    with pytest.raises(ValueError, match='changed'):
        batch.run_batch(['good'], generation, output, grader, {'input': 'other'}, resume=True)


@pytest.mark.parametrize(('patch_changed', 'data_changed', 'expected'), [
    (False, False, True), (True, False, False), (False, True, False),
])
def test_reuse_requires_current_patch_and_exact_official_task_data(
    tmp_path, patch_changed, data_changed, expected,
):
    generation, source, output = tmp_path / 'generation', tmp_path / 'source', tmp_path / 'new'
    generated(generation, patch='new' if patch_changed else 'patch')
    relative = Path('instances/fixture-1/original-run')
    attempt = source / relative
    official(attempt / 'logs/run_evaluation/original-run/model/fixture-1', 'patch')
    grading_path = tmp_path / 'grader.json'
    data = {'instance_id': 'fixture-1', 'patch': 'gold', 'base_commit': 'same'}
    dump(grading_path, [data])
    dump(attempt / 'invocation.json', {'argv': ['grader', '--dataset_name', str(grading_path)]})
    dump(attempt / 'predictions.jsonl', {'instance_id': 'fixture-1',
                                        'model_name_or_path': 'model', 'model_patch': 'patch'})
    dump(source / 'progress.json', {'fingerprint': {'swebench_version': '4.1.0'}, 'rows': [{
        'instance_id': 'fixture-1', 'status': 'graded', 'patch_sha256': batch.sha(b'patch'),
        'attempt': str(relative), 'grader_run_id': 'original-run',
    }]})
    output.mkdir()
    current_data = data | {'patch': 'changed gold'} if data_changed else data
    rows, audit = batch.reuse_verified(['fixture-1'], generation, output, source,
                                       {'fixture-1': current_data})
    assert bool(rows) is expected and audit[0]['reused'] is expected
    if expected:
        assert rows['fixture-1']['resolved'] and rows['fixture-1']['reused']
        assert rows['fixture-1']['reuse_source'] == str(attempt)
        assert (output / rows['fixture-1']['attempt'] / 'predictions.jsonl').is_file()


def test_agent_budget_outcome_is_preserved_but_nonempty_patch_is_still_graded(tmp_path):
    directory = generated(tmp_path)
    dump(directory / 'result.json', {'instance_id': 'fixture-1', 'mode': 'agent',
                                     'status': 'generated', 'outcome': 'budget_exhausted',
                                     'stop_reason': 'max_steps'})
    row, prediction = batch.generation_input(tmp_path, 'fixture-1')
    assert prediction is not None and row['generation_mode'] == 'agent'
    assert row['agent_outcome'] == 'budget_exhausted' and row['stop_reason'] == 'max_steps'
