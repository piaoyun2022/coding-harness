"""Serial, auditable SWE-bench grading; never generates or retries model answers."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

GIB = 1024 ** 3
KNOWN_FAILURES = {'generation_invalid', 'empty_patch', 'patch_rejected'}
COMPLETE_STATUSES = KNOWN_FAILURES | {'graded'}
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
LOGGER = re.compile(r'(?m)^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - (?:INFO|ERROR) - ')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def validate_plan(tasks_path: Path, grader_path: Path) -> tuple[list[str], dict[str, dict]]:
    tasks = [json.loads(line) for line in tasks_path.read_text(encoding='utf-8-sig').splitlines()
             if line.strip()]
    ids = [row['instance_id'] for row in tasks]
    if not ids or len(ids) != len(set(ids)) or not all(SAFE_ID.fullmatch(i) for i in ids):
        raise ValueError('Task IDs must be unique safe names')
    grader_rows = read_json(grader_path)
    if not isinstance(grader_rows, list):
        raise ValueError('Expected official grading JSON list')
    grading = {row['instance_id']: row for row in grader_rows}
    if len(grading) != len(grader_rows) or set(grading) != set(ids):
        raise ValueError('Grading data and planned task IDs differ')
    for task in tasks:
        if any(task[key] != grading[task['instance_id']][key]
               for key in ('repo', 'base_commit', 'version')):
            raise ValueError('Grading data does not match task baseline')
    return ids, grading


def generation_input(root: Path, instance_id: str) -> tuple[dict, dict | None] | None:
    directory = root / 'runs' / instance_id
    try:
        result = read_json(directory / 'result.json')
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # The concurrent producer may still be writing its final result.
    row = {'instance_id': instance_id, 'generation_status': result.get('status'),
           'generation_mode': result.get('mode', 'baseline'),
           'agent_outcome': result.get('outcome'), 'stop_reason': result.get('stop_reason'),
           'generation_usage': result.get('usage'), 'resolved': False}
    if result.get('instance_id') != instance_id:
        return row | {'status': 'infra_generation_identity'}, None
    status = result.get('status')
    if status in {'invalid_response', 'empty_or_invalid_patch'}:
        return row | {'status': 'generation_invalid'}, None
    if status == 'empty_patch':
        return row | {'status': 'empty_patch'}, None
    if status == 'model_error':
        return row | {'status': 'api_error'}, None
    if status == 'setup_error':
        return row | {'status': 'generation_setup_error'}, None
    if status != 'generated':
        return row | {'status': 'infra_unknown_generation_status'}, None
    try:
        prediction_path = directory / 'prediction.json'
        prediction = read_json(prediction_path)
        patch = prediction['model_patch']
        model = prediction['model_name_or_path']
        if (prediction['instance_id'] != instance_id or not isinstance(patch, str)
                or not isinstance(model, str) or not SAFE_ID.fullmatch(model)):
            raise ValueError('Invalid prediction identity')
        if not patch.strip():
            return row | {'status': 'empty_patch'}, None
        digest = sha(patch.encode('utf-8'))
        changes_digest = sha((directory / 'changes.patch').read_bytes())
        if changes_digest != digest:
            raise ValueError('Prediction and changes.patch differ')
        row.update(patch_sha256=digest, changes_patch_sha256=changes_digest,
                   prediction_file_sha256=sha(prediction_path.read_bytes()), model=model)
    except (OSError, ValueError, KeyError, TypeError):
        return row | {'status': 'infra_generation_artifacts'}, None
    return row, prediction


def framework_patch_rejection(text: str, instance_id: str) -> bool:
    """Require a framework log record and its final matching EvaluationError traceback."""
    records = LOGGER.split(text)[1:]
    rejected = any(record.startswith('>>>>> Patch Apply Failed:\n') for record in records)
    tracebacks = [record for record in records
                  if record.startswith('Traceback (most recent call last):\n')]
    if not rejected or not tracebacks:
        return False
    final = tracebacks[-1]
    expected = f'swebench.harness.utils.EvaluationError: {instance_id}: >>>>> Patch Apply Failed:'
    return (re.search(r'(?m)^  File "[^"\n]*/swebench/harness/run_evaluation.py", '
                      r'line \d+, in run_instance\n    raise EvaluationError\(', final) is not None
            and expected in final.splitlines())


def classify_official(directory: Path, instance_id: str, digest: str) -> dict:
    patch_path = directory / 'patch.diff'
    if not patch_path.is_file() or sha(patch_path.read_bytes()) != digest:
        return {'status': 'infra_unbound_official_patch', 'resolved': False}
    binding = {'official_patch_sha256': digest, 'patch_binding_verified': True}
    report_path = directory / 'report.json'
    if report_path.exists():
        try:
            report = read_json(report_path)[instance_id]
            if type(report.get('resolved')) is not bool:
                raise ValueError('Missing official boolean result')
            groups = report.get('tests_status', {})
            tests = {}
            for name in ('FAIL_TO_PASS', 'PASS_TO_PASS'):
                group = groups.get(name, {})
                passed, failed = group.get('success', []), group.get('failure', [])
                if not isinstance(passed, list) or not isinstance(failed, list):
                    raise ValueError('Invalid official test group')
                tests[name] = {'passed': len(passed), 'failed': len(failed)}
            return binding | {'status': 'graded', 'resolved': report['resolved'], 'tests': tests,
                              'report_sha256': sha(report_path.read_bytes())}
        except (OSError, ValueError, KeyError, TypeError):
            return binding | {'status': 'infra_invalid_report', 'resolved': False}
    log_path = directory / 'run_instance.log'
    text = log_path.read_text(encoding='utf-8', errors='replace') if log_path.exists() else ''
    if framework_patch_rejection(text, instance_id):
        return binding | {'status': 'patch_rejected', 'resolved': False,
                          'diagnostic_source': 'bound_framework_EvaluationError'}
    return binding | {'status': 'infra_missing_report', 'resolved': False}


def summarize(ids: list[str], rows: dict[str, dict]) -> dict:
    selected = [rows[i] for i in ids if i in rows]
    counts = Counter(row['status'] for row in selected)
    resolved = sum(row.get('resolved') is True and row['status'] == 'graded' for row in selected)
    unknown = sum(row['status'] not in COMPLETE_STATUSES for row in selected)
    complete = len(selected) == len(ids)
    provisional = not complete or unknown > 0
    diagnostic = {}
    for group in ('FAIL_TO_PASS', 'PASS_TO_PASS'):
        fractions = []
        for row in selected:
            values = row.get('tests', {}).get(group, {}) if row['status'] == 'graded' else {}
            total = values.get('passed', 0) + values.get('failed', 0)
            if total:
                fractions.append(values['passed'] / total)
        diagnostic[group] = {'coverage': len(fractions),
                             'mean_task_fraction': sum(fractions) / len(fractions)
                             if fractions else None}
    usage = {name: sum((row.get('generation_usage') or {}).get(name, 0) for row in selected)
             for name in ('input_tokens', 'output_tokens', 'cached_tokens', 'total_tokens')}
    return {'planned': len(ids), 'completed': len(selected), 'pending': len(ids) - len(selected),
            'statuses': dict(counts), 'graded': counts['graded'], 'resolved': resolved,
            'unresolved_unknown': unknown, 'provisional': provisional,
            'official_resolved_rate': None if provisional else resolved / len(ids),
            'observed_resolved_lower_bound': resolved / len(ids), 'diagnostic': diagnostic,
            'generation_usage': usage, 'generation_complete': complete}



def reuse_verified(ids: list[str], generation: Path, output: Path, source: Path,
                   grading: dict[str, dict]) -> tuple[dict, list]:
    """Reuse grading evidence only after rechecking task data and all patch hashes."""
    previous_path = source / 'progress.json'
    previous = read_json(previous_path)
    if previous.get('fingerprint', {}).get('swebench_version') != '4.1.0':
        raise ValueError('Cannot reuse a different official grader version')
    rows, audit, datasets = {}, [], {}
    for old in previous['rows']:
        instance_id = old['instance_id']
        if instance_id not in ids or old['status'] not in {'graded', 'patch_rejected'}:
            continue
        try:
            loaded = generation_input(generation, instance_id)
            if loaded is None or loaded[1] is None:
                raise ValueError('Current prediction is unavailable')
            current, prediction = loaded
            if old['patch_sha256'] != current['patch_sha256']:
                raise ValueError('Current patch differs')
            relative = Path(old['attempt'])
            attempt = (source / relative).resolve()
            if relative.is_absolute() or not attempt.is_relative_to(source.resolve()):
                raise ValueError('Invalid source attempt path')
            invocation = read_json(attempt / 'invocation.json')
            argv = invocation['argv']
            original_data = Path(argv[argv.index('--dataset_name') + 1])
            if original_data not in datasets:
                datasets[original_data] = {
                    row['instance_id']: row for row in read_json(original_data)
                }
            if datasets[original_data][instance_id] != grading[instance_id]:
                raise ValueError('Official task data differs')
            original_prediction = read_json(attempt / 'predictions.jsonl')
            if (original_prediction['instance_id'] != instance_id
                    or sha(original_prediction['model_patch'].encode()) != current['patch_sha256']):
                raise ValueError('Old prediction is not bound')
            run_id, model = old['grader_run_id'], original_prediction['model_name_or_path']
            logs = attempt / 'logs/run_evaluation' / run_id / model / instance_id
            checked = classify_official(logs, instance_id, current['patch_sha256'])
            if checked['status'] not in {'graded', 'patch_rejected'}:
                raise ValueError('Old official evidence is not conclusive')
            if any(path.is_symlink() for path in attempt.rglob('*')):
                raise ValueError('Cannot copy linked evaluation artifacts')
            target = output / 'reused' / instance_id / run_id
            shutil.copytree(attempt, target)
            rows[instance_id] = current | checked | {
                'grader_run_id': run_id, 'attempt': str(target.relative_to(output)),
                'reused': True, 'reuse_source': str(attempt),
                'reuse_source_progress_sha256': sha(previous_path.read_bytes()),
                'reuse_source_fingerprint': previous['fingerprint'],
            }
            audit.append({'instance_id': instance_id, 'reused': True, 'source': str(attempt)})
        except (OSError, ValueError, KeyError, TypeError) as error:
            audit.append({'instance_id': instance_id, 'reused': False,
                          'error_type': type(error).__name__})
    return rows, audit


def registry_size(image: str) -> dict:
    """Read public Docker Hub manifest size without pulling an image or saving auth tokens."""
    repository, tag = image.rsplit(':', 1)
    query = urllib.parse.urlencode({'service': 'registry.docker.io',
                                   'scope': f'repository:{repository}:pull'})
    with urllib.request.urlopen('https://auth.docker.io/token?' + query, timeout=30) as response:
        token = json.load(response)['token']
    headers = {'Authorization': 'Bearer ' + token,
               'Accept': 'application/vnd.docker.distribution.manifest.v2+json, '
                         'application/vnd.oci.image.manifest.v1+json, '
                         'application/vnd.docker.distribution.manifest.list.v2+json, '
                         'application/vnd.oci.image.index.v1+json'}

    def fetch(reference):
        request = urllib.request.Request(
            f'https://registry-1.docker.io/v2/{repository}/manifests/{reference}', headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response), response.headers.get('Docker-Content-Digest')

    manifest, digest = fetch(tag)
    if 'manifests' in manifest:
        candidates = [item for item in manifest['manifests']
                      if item.get('platform', {}).get('os') == 'linux'
                      and item.get('platform', {}).get('architecture') == 'amd64']
        if len(candidates) != 1:
            raise ValueError('Expected one linux/amd64 image manifest')
        manifest, digest = fetch(candidates[0]['digest'])
    sizes = [layer['size'] for layer in manifest['layers']]
    if not sizes or any(type(size) is not int or size < 0 for size in sizes):
        raise ValueError('Invalid registry layer size')
    return {'compressed_bytes': sum(sizes), 'registry_manifest_digest': digest}


def image_snapshot(client, image: str) -> dict:
    identities = sorted(item.id for item in client.images.list(all=True))
    try:
        current = client.images.get(image)
        return {'tag_existed': True, 'image_id': current.id, 'all_image_ids': identities}
    except Exception as error:
        if type(error).__name__ != 'ImageNotFound':
            raise
        return {'tag_existed': False, 'image_id': None, 'all_image_ids': identities}


def cleanup_image(client, image: str, before: dict) -> dict:
    if before['tag_existed']:
        return {'image_cleanup': 'preserved_preexisting_tag'}
    try:
        current = client.images.get(image)
        if current.id in before['all_image_ids']:
            return {'image_cleanup': 'preserved_preexisting_image_id', 'image_id': current.id}
        if image not in current.tags:
            return {'image_cleanup': 'preserved_unconfirmed_tag'}
        identity = current.id
        client.images.remove(image, force=False, noprune=True)
        return {'image_cleanup': 'removed_exact_new_tag', 'image_id': identity}
    except Exception as error:
        if type(error).__name__ == 'ImageNotFound':
            return {'image_cleanup': 'not_present'}
        return {'image_cleanup': 'error', 'image_cleanup_error_type': type(error).__name__}


def cleanup_container(client, name: str, image: str) -> dict:
    try:
        container = client.containers.get(name)
        if container.name != name or container.attrs.get('Config', {}).get('Image') != image:
            return {'container_cleanup': 'ownership_not_verified'}
        identity = container.id
        container.remove(force=True, v=True)
        return {'container_cleanup': 'removed_exact_owned', 'container_id': identity}
    except Exception as error:
        if type(error).__name__ == 'NotFound':
            return {'container_cleanup': 'not_present'}
        return {'container_cleanup': 'error', 'container_cleanup_error_type': type(error).__name__}


def stop_process_group(process) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    except ProcessLookupError:
        process.wait(timeout=10)


def run_process(argv: list[str], cwd: Path, log: Path, timeout: int) -> dict:
    allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR'}
    environment = {name: value for name, value in os.environ.items() if name in allowed}
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return {'grader_returncode': process.wait(timeout=timeout)}
        except subprocess.TimeoutExpired:
            stop_process_group(process)
            return {'runner_timed_out': True}
        except BaseException:
            stop_process_group(process)
            raise


class Grader:
    def __init__(self, output: Path, grader_data: Path, grading: dict, *,
                 test_timeout: int = 1800, process_timeout: int = 3600,
                 docker_disk: Path = Path('/mnt/d'), artifact_disk: Path = Path('/mnt/c')):
        import docker
        from swebench.harness.test_spec.test_spec import make_test_spec

        self.client = docker.from_env(timeout=60)
        self.client.ping()
        self.output, self.grader_data, self.grading = output, grader_data, grading
        self.test_timeout, self.process_timeout = test_timeout, process_timeout
        self.docker_disk, self.artifact_disk = docker_disk, artifact_disk
        self.make_test_spec = make_test_spec

    def grade(self, row: dict, prediction: dict) -> dict:
        instance_id = row['instance_id']
        run_id = 'official-' + uuid.uuid4().hex
        attempt = self.output / 'instances' / instance_id / run_id
        attempt.mkdir(parents=True, exist_ok=False)
        spec = self.make_test_spec(self.grading[instance_id], namespace='swebench', arch='x86_64')
        image = spec.instance_image_key
        container_name = spec.get_instance_container_name(run_id)
        row = row | {'grader_run_id': run_id, 'attempt': str(attempt.relative_to(self.output)),
                     'image': image, 'container_name': container_name}
        predictions = attempt / 'predictions.jsonl'
        predictions.write_text(json.dumps(prediction) + '\n', encoding='utf-8')
        argv = [sys.executable, '-m', 'swebench.harness.run_evaluation',
                '--dataset_name', str(self.grader_data), '--split', 'test',
                '--predictions_path', str(predictions), '--instance_ids', instance_id,
                '--max_workers', '1', '--run_id', run_id, '--timeout', str(self.test_timeout),
                '--cache_level', 'instance', '--clean', 'False', '--namespace', 'swebench',
                '--report_dir', str(attempt)]
        started = time.monotonic()
        before = None
        owned = False
        pull_started = False
        interrupted = None
        try:
            before = image_snapshot(self.client, image)
            row['image_before'] = before
            try:
                self.client.containers.get(container_name)
                raise ValueError('Unique container name unexpectedly exists')
            except Exception as error:
                if type(error).__name__ != 'NotFound':
                    raise
            owned = True
            size = registry_size(image) if not before['tag_existed'] else {'compressed_bytes': 0}
            free = shutil.disk_usage(self.docker_disk).free
            artifact_free = shutil.disk_usage(self.artifact_disk).free
            required = max(4 * GIB, size['compressed_bytes'] * 3 + 2 * GIB)
            row.update(**size, docker_free_bytes=free, artifact_free_bytes=artifact_free,
                       required_docker_headroom_bytes=required)
            write_json(attempt / 'invocation.json', row | {'argv': argv,
                       'process_timeout_seconds': self.process_timeout})
            if free < required or artifact_free < 2 * GIB:
                row['status'] = 'infra_disk_headroom'
                return row
            if not before['tag_existed']:
                pull_started = True
                pulled = run_process(['docker', 'pull', image], attempt,
                                     attempt / 'image-pull.log', min(1200, self.process_timeout))
                row['image_pull'] = pulled
                if pulled.get('grader_returncode') != 0:
                    row['status'] = 'infra_image_pull'
                    return row
            current = self.client.images.get(image)
            row['evaluated_image_id'] = current.id
            row['evaluated_image_digests'] = current.attrs.get('RepoDigests', [])
            row.update(run_process(argv, attempt, attempt / 'grader.stdout.log',
                                   self.process_timeout))
            log_dir = attempt / 'logs/run_evaluation' / run_id / prediction['model_name_or_path']
            log_dir /= instance_id
            row.update(classify_official(log_dir, instance_id, row['patch_sha256']))
            if row.get('runner_timed_out'):
                row.update(status='infra_supervisor_timeout', resolved=False)
        except KeyboardInterrupt as error:
            row.update(status='infra_interrupted', resolved=False)
            interrupted = error
        except Exception as error:
            row.update(status='infra_grader_exception', resolved=False,
                       runner_error_type=type(error).__name__)
        finally:
            if owned:
                row.update(cleanup_container(self.client, container_name, image))
            if before is not None and (before['tag_existed'] or pull_started):
                row.update(cleanup_image(self.client, image, before))
            row['grading_seconds'] = round(time.monotonic() - started, 3)
            write_json(attempt / 'outcome.json', row)
        if interrupted is not None:
            raise interrupted
        return row


def run_batch(ids: list[str], generation: Path, output: Path, grader, fingerprint: dict, *,
              resume: bool = False, retry_infra: bool = False, poll_seconds: float = 5,
              reuse_from: Path | None = None, grading: dict | None = None) -> dict:
    if resume:
        previous = read_json(output / 'progress.json')
        if previous['fingerprint'] != fingerprint:
            raise ValueError('Cannot resume with changed task, grader, or generation inputs')
        rows = {row['instance_id']: row for row in previous['rows']}
        if retry_infra:
            rows = {key: value for key, value in rows.items()
                    if value['status'] in COMPLETE_STATUSES or value['status'] == 'api_error'}
    else:
        output.mkdir(parents=True, exist_ok=False)
        rows = {}
        if reuse_from is not None:
            rows, audit = reuse_verified(ids, generation, output, reuse_from, grading or {})
            write_json(output / 'reuse-audit.json', {'source': str(reuse_from), 'rows': audit})
    started = time.monotonic()
    active = None

    def update():
        state = summarize(ids, rows) | {'fingerprint': fingerprint,
                'rows': [rows[i] for i in ids if i in rows], 'active_instance': active,
                'supervisor_elapsed_seconds': round(time.monotonic() - started, 3),
                'updated_unix': time.time()}
        write_json(output / 'progress.json', state)
        return state

    update()
    try:
        while len(rows) < len(ids):
            ready = False
            for instance_id in ids:
                if instance_id in rows:
                    continue
                loaded = generation_input(generation, instance_id)
                if loaded is None:
                    continue
                ready = True
                row, prediction = loaded
                if prediction is not None:
                    active = instance_id
                    update()
                    print(json.dumps({'stage': 'grading_started', 'instance_id': instance_id,
                                      'completed': len(rows), 'planned': len(ids)}), flush=True)
                    row = grader.grade(row, prediction)
                rows[instance_id] = row
                active = None
                state = update()
                print(json.dumps({'stage': 'task_finished', 'instance_id': instance_id,
                                  'status': row['status'], 'completed': len(rows),
                                  'resolved': state['resolved'], 'planned': len(ids)}), flush=True)
            if not ready:
                if (generation / 'batch-result.json').exists():
                    for instance_id in ids:
                        if instance_id not in rows:
                            rows[instance_id] = {'instance_id': instance_id,
                                                 'status': 'infra_generation_missing',
                                                 'resolved': False}
                    break
                time.sleep(poll_seconds)  # No total-batch or one-hour generation deadline.
                update()
    finally:
        active = None
        state = update()
        write_json(output / 'run.json', state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks', type=Path, required=True)
    parser.add_argument('--generation', type=Path, required=True)
    parser.add_argument('--grading-data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-count', type=int, default=300)
    parser.add_argument('--test-timeout', type=int, default=1800)
    parser.add_argument('--process-timeout', type=int, default=3600)
    parser.add_argument('--reuse-from', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--retry-infra', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        raise RuntimeError('Official grading requires Linux Python and a Docker Linux engine')
    if args.test_timeout < 1 or args.process_timeout <= args.test_timeout:
        raise ValueError('Process timeout must exceed positive test timeout')
    if args.retry_infra and not args.resume:
        raise ValueError('Retrying grading requires --resume; model generation is never retried')
    if importlib.metadata.version('swebench') != '4.1.0':
        raise RuntimeError('This supervisor is verified with swebench 4.1.0')
    args.tasks, args.generation = args.tasks.resolve(), args.generation.resolve()
    args.grading_data, args.output = args.grading_data.resolve(), args.output.resolve()
    ids, grading = validate_plan(args.tasks, args.grading_data)
    if len(ids) != args.expected_count:
        raise ValueError('Unexpected planned denominator')
    fingerprint = {'tasks_sha256': sha(args.tasks.read_bytes()),
                   'grading_data_sha256': sha(args.grading_data.read_bytes()),
                   'generation_plan_sha256': sha((args.generation / 'plan.json').read_bytes()),
                   'generation_directory': str(args.generation), 'supervisor_sha256': sha(
                       Path(__file__).read_bytes()), 'interpreter': sys.executable,
                   'python_version': sys.version.split()[0], 'swebench_version': '4.1.0',
                   'test_timeout': args.test_timeout, 'process_timeout': args.process_timeout,
                   'model_reruns': False, 'cache_level': 'instance', 'clean': False}
    grader = Grader(args.output, args.grading_data, grading, test_timeout=args.test_timeout,
                    process_timeout=args.process_timeout)
    # Termination follows the same owned-child cleanup path as Ctrl-C.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        result = run_batch(ids, args.generation, args.output, grader, fingerprint,
                           resume=args.resume, retry_infra=args.retry_infra,
                           reuse_from=args.reuse_from.resolve() if args.reuse_from else None,
                           grading=grading)
        public = {key: value for key, value in result.items() if key != 'rows'}
        print(json.dumps(public), flush=True)
        return 0 if not result['provisional'] else 2
    except KeyboardInterrupt:
        print(json.dumps({'stage': 'supervisor_interrupted', 'model_reruns': False}), flush=True)
        return 130
    finally:
        grader.client.close()


if __name__ == '__main__':
    raise SystemExit(main())
