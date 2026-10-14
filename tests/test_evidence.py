import hashlib
import json
import uuid
from pathlib import Path

import pytest

from coding_harness.evidence import MAX_READ_CHARS, EvidenceError, EvidenceStore


def make_store(tmp_path, **kwargs):
    return EvidenceStore(tmp_path / 'evidence', redactor=lambda text: text, **kwargs)


def test_redaction_precedes_storage_hash_and_paged_read(tmp_path):
    store = EvidenceStore(tmp_path, redactor=lambda text: text.replace('secret', '[REDACTED]'))
    clean = '你好[REDACTED] done'
    metadata = store.store('你好secret done')
    assert metadata['sha256'] == hashlib.sha256(clean.encode()).hexdigest()
    assert metadata['bytes'] == len(clean.encode())
    assert metadata['chars'] == len(clean)
    assert not metadata['truncated']
    pages = []
    start = 0
    while True:
        page = store.read(metadata['id'], start, 3)
        assert page['ok'] and page['start'] == start
        assert len(page['content']) <= 3
        pages.append(page['content'])
        start = page['next_start']
        if start is None:
            assert not page['truncated']
            break
        assert page['truncated']
    assert ''.join(pages) == clean
    assert all(b'secret' not in path.read_bytes() for path in tmp_path.iterdir())
    assert str(tmp_path) not in json.dumps(metadata)


def test_item_limit_retains_valid_utf8_and_explicit_truncation(tmp_path):
    store = make_store(tmp_path, max_item_bytes=5)
    metadata = store.store('你好 world')
    assert metadata['bytes'] == 3 and metadata['chars'] == 1 and metadata['truncated']
    page = store.read(metadata['id'])
    assert page['content'] == '你'
    assert page['truncated'] and page['next_start'] is None


def test_total_limit_rejects_atomically_and_does_not_consume_remaining_quota(tmp_path):
    store = make_store(tmp_path, max_item_bytes=10, max_total_bytes=6)
    first = store.store('abcd')
    before = set((tmp_path / 'evidence').iterdir())
    with pytest.raises(EvidenceError, match='total byte limit'):
        store.store('123')
    assert set((tmp_path / 'evidence').iterdir()) == before
    assert store.contains(first['id']) and store.read(first['id'])['content'] == 'abcd'
    second = store.store('xy')
    assert store.read(second['id'])['content'] == 'xy'


def test_only_registered_ids_are_readable_and_old_runs_are_not_reopened(tmp_path):
    store = make_store(tmp_path)
    known = store.store('valid')['id']
    secret_file = tmp_path / 'private.txt'
    secret_file.write_text('must not leak', encoding='utf-8')
    for candidate in ['../private.txt', str(secret_file), 'ev_' + uuid.uuid4().hex, '', None, []]:
        assert not store.contains(candidate)
        assert store.read(candidate) == {'ok': False, 'error': 'Unknown evidence id'}
    another_run = make_store(tmp_path)
    assert not another_run.contains(known)
    assert not another_run.read(known)['ok']


@pytest.mark.parametrize(('start', 'limit'), [
    (-1, 10), (True, 10), (1.5, 10), (0, 0), (0, True), (0, '1'),
    (0, MAX_READ_CHARS + 1), (4, 10),
])
def test_invalid_read_range_is_recoverable(tmp_path, start, limit):
    store = make_store(tmp_path)
    evidence_id = store.store('abc')['id']
    result = store.read(evidence_id, start, limit)
    assert not result['ok'] and 'content' not in result


def test_exact_end_and_empty_content_have_no_next_page(tmp_path):
    store = make_store(tmp_path)
    for content in ['', 'abc']:
        metadata = store.store(content)
        page = store.read(metadata['id'], len(content))
        assert page['ok'] and page['content'] == '' and page['next_start'] is None


def test_metadata_mutation_and_duplicate_content_cannot_rewrite_old_evidence(tmp_path):
    store = make_store(tmp_path)
    metadata = store.store('original')
    first_id = metadata['id']
    metadata.update(id='fake', sha256='fake', chars=0, bytes=0)
    second = store.store('original')
    assert second['id'] != first_id
    assert store.read(first_id)['content'] == 'original'
    assert len(list((tmp_path / 'evidence').iterdir())) == 2


def test_id_collision_never_overwrites_or_spends_quota(tmp_path, monkeypatch):
    same_uuid = uuid.uuid4()
    monkeypatch.setattr('coding_harness.evidence.uuid.uuid4', lambda: same_uuid)
    store = make_store(tmp_path, max_total_bytes=6)
    first = store.store('abc')
    with pytest.raises(EvidenceError, match='Cannot write evidence'):
        store.store('xyz')
    assert store.read(first['id'])['content'] == 'abc'
    monkeypatch.setattr('coding_harness.evidence.uuid.uuid4', lambda: uuid.UUID(int=1))
    assert store.store('def')['bytes'] == 3


@pytest.mark.parametrize('damage', ['same_length', 'larger', 'missing'])
def test_disk_tampering_is_detected_without_leaking_paths(tmp_path, damage):
    store = make_store(tmp_path)
    evidence_id = store.store('original')['id']
    path = next((tmp_path / 'evidence').iterdir())
    if damage == 'missing':
        path.unlink()
    else:
        path.write_text('modified' if damage == 'same_length' else 'x' * 100_000)
    result = store.read(evidence_id)
    assert not result['ok'] and 'content' not in result
    assert str(tmp_path) not in json.dumps(result)


def test_symlink_evidence_is_rejected_before_read(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    evidence_id = store.store('original')['id']
    monkeypatch.setattr(Path, 'is_symlink', lambda path: True)
    assert store.read(evidence_id) == {'ok': False, 'error': 'Evidence unavailable'}


def test_redactor_error_is_safe_and_does_not_write(tmp_path):
    def broken(text):
        raise RuntimeError('secret from ' + str(tmp_path))

    store = EvidenceStore(tmp_path, redactor=broken)
    with pytest.raises(EvidenceError) as caught:
        store.store('secret')
    assert 'secret' not in str(caught.value) and str(tmp_path) not in str(caught.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('kwargs', [
    {'max_item_bytes': 0}, {'max_item_bytes': True}, {'max_total_bytes': -1},
    {'max_total_bytes': 1.5},
])
def test_invalid_storage_limits_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        make_store(tmp_path, **kwargs)
