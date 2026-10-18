"""Optional audio remains an attachment of the saved video, with its own outcome."""
import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace

import pytest

from lumabox.web.app import create_app
from lumabox.web.config import WebError
from lumabox.web.store import JobStore
from fastapi.testclient import TestClient
from test_daily_api import completed, headers, ORIGIN
from web_helpers import URL, fixture_worker


@contextmanager
def client_for(store):
    from lumabox.web.worker import JobWorker
    client = TestClient(create_app(store.config, JobWorker(store.config, store)), base_url=ORIGIN)
    try:
        yield client  # Routes only: fixtures, not the real browser, own processing.
    finally:
        client.close()


def test_audio_is_opt_in_and_strictly_boolean(web_config):
    store = JobStore(web_config)
    with client_for(store) as client:
        guard = headers(client)
        response = client.post('/api/jobs', headers=guard, json={'input': URL, 'include_audio': True})
        assert response.status_code == 202
        assert response.json()['include_audio'] is True
        assert response.json()['audio']['status'] == 'waiting'
        default = client.post('/api/jobs', headers=guard, json={'input': 'https://v.douyin.com/other/'})
        assert default.status_code == 202
        assert default.json()['audio']['status'] == 'none'
        for value in (1, 'true', None, []):
            invalid = client.post('/api/jobs', headers=guard, json={'input': URL, 'include_audio': value})
            assert invalid.status_code == 400 and invalid.json()['error'] == 'INVALID_REQUEST'


def test_existing_video_audio_reuses_source_and_keeps_one_collection(web_config, media_fixtures):
    store = JobStore(web_config)
    job, source, original = completed(store, media_fixtures[0])
    before = store.storage.summary()['saved_bytes']
    with client_for(store) as client:
        endpoint = f'/api/jobs/{job["id"]}/audio'
        assert client.post(endpoint, json={}).status_code == 403
        assert client.post(endpoint, headers=headers(client), json={'url': URL}).status_code == 400
        response = client.post(endpoint, headers=headers(client), json={})
        assert response.status_code == 202
        assert response.json()['audio']['status'] == 'pending'
        assert store.storage.items()['items'][0]['audio_active'] is True
        assert client.post(endpoint, headers=headers(client), json={}).status_code == 202
        assert store.audio.run_once(threading.Event())
        saved = store.get(job['id'])
        assert saved['status'] == 'success' and saved['actual_count'] == 1
        assert saved['audio']['status'] == 'success'
        audio = next(file for file in saved['files'] if file['mime'] == 'audio/mp4')
        assert audio['name'].endswith('.m4a') and audio['duration'] > 0
        audio_path = store.file_path(store.file(audio['id']))
        assert audio_path.stat().st_size == audio['bytes']
        assert source.read_bytes() == original
        assert store.search()['total'] == store.storage.summary()['saved_count'] == 1
        assert store.storage.summary()['saved_bytes'] == before + audio['bytes']
        again = client.post(endpoint, headers=headers(client), json={}).json()
        assert again['audio']['status'] == 'success'
        assert not store.audio.run_once(threading.Event())
        assert len(store.get(job['id'])['files']) == 2
        part = client.get(f'/api/files/{audio["id"]}/preview', headers={'Range': 'bytes=0-31'})
        assert part.status_code == 206 and part.content == audio_path.read_bytes()[:32]
        assert part.headers['content-type'].startswith('audio/mp4')
        download = client.get(f'/api/files/{audio["id"]}')
        assert download.content == audio_path.read_bytes() and 'attachment' in download.headers['content-disposition']
    assert store.storage.purge(job['id'])['status'] == 'deleted'
    assert not source.exists() and not audio_path.exists()


def test_selected_audio_runs_after_publication_without_reopening_browser(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config)
    with client_for(store) as client:
        response = client.post('/api/jobs', headers=headers(client), json={'input': URL, 'include_audio': True})
        assert response.status_code == 202
        job_id = response.json()['id']
    worker = fixture_worker(web_config, store, tmp_path, media_fixtures[0])
    assert worker.run_once()
    assert store.get(job_id)['status'] == 'success'
    assert store.get(job_id)['audio']['status'] == 'pending'
    worker.mode = 'failed'  # A second browser download would fail this test.
    assert worker.run_once()
    assert store.get(job_id)['audio']['status'] == 'success'
    assert store.get(job_id)['attempt_count'] == 1


def test_pending_audio_protects_collection_until_cancelled(web_config, media_fixtures):
    store = JobStore(web_config)
    job, path, original = completed(store, media_fixtures[0])
    # The public endpoint is deliberately used before looking at internal state.
    with client_for(store) as client:
        endpoint = f'/api/jobs/{job["id"]}/audio'
        assert client.post(endpoint, headers=headers(client), json={}).status_code == 202
        with pytest.raises(WebError, match='JOB_ACTIVE'):
            store.remove(job['id'])
        with pytest.raises(WebError, match='JOB_ACTIVE'):
            store.storage.purge(job['id'])
        cancelled = client.post(endpoint + '/cancel', headers=headers(client), json={})
        assert cancelled.status_code == 200 and cancelled.json()['audio']['status'] == 'cancelled'
    assert not store.audio.run_once(threading.Event())
    assert path.read_bytes() == original
    store.remove(job['id'])


def test_failed_audio_preserves_video_and_can_retry(web_config, media_fixtures, monkeypatch):
    from lumabox.errors import LumaBoxError
    store = JobStore(web_config)
    job, path, original = completed(store, media_fixtures[0])
    store.audio.request(job['id'])
    import lumabox.web.audio as audio
    real = audio.extract_audio
    def missing_track(*args, **kwargs):
        raise LumaBoxError('NO_AUDIO')
    monkeypatch.setattr(audio, 'extract_audio', missing_track)
    assert store.audio.run_once(threading.Event())
    saved = store.get(job['id'])
    assert saved['audio'] == {'status': 'failed', 'error': 'NO_AUDIO'}
    assert saved['status'] == 'success' and len(saved['files']) == 1
    assert path.read_bytes() == original
    monkeypatch.setattr(audio, 'extract_audio', real)
    store.audio.request(job['id'])
    assert store.audio.run_once(threading.Event())
    assert store.get(job['id'])['audio']['status'] == 'success'


def test_audio_rejects_source_tampering(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config)
    job, path, original = completed(store, media_fixtures[0])
    store.audio.request(job['id'])
    path.write_bytes(b'x' * len(original))
    store.audio.run_once(threading.Event())
    assert store.get(job['id'])['audio']['error'] == 'VERIFY_FAILED'
    assert len(store.get(job['id'])['files']) == 1


def test_audio_cancel_while_processing_protects_lease_and_cleans_only_own_temp(web_config, media_fixtures, monkeypatch):
    from lumabox.errors import LumaBoxError
    import lumabox.web.audio as audio
    store = JobStore(web_config)
    job, path, original = completed(store, media_fixtures[0])
    entered, release = threading.Event(), threading.Event()
    def blocked(source, target, *, cancelled, max_bytes, on_created):
        target.write_bytes(b'partial')
        stat = target.stat()
        on_created(target, (stat.st_dev, stat.st_ino))
        entered.set()
        assert release.wait(5)
        assert cancelled()
        raise LumaBoxError('INTERRUPTED')
    monkeypatch.setattr(audio, 'extract_audio', blocked)
    store.audio.request(job['id'])
    thread = threading.Thread(target=store.audio.run_once, args=(threading.Event(),))
    thread.start()
    try:
        assert entered.wait(5)
        assert store.downloads[job['id']] == 1
        store.audio.cancel(job['id'])
        with pytest.raises(WebError, match='JOB_ACTIVE'):
            store.storage.purge(job['id'])
    finally:
        release.set(); thread.join(8)
    assert not thread.is_alive()
    assert store.get(job['id'])['audio']['status'] == 'cancelled'
    assert not store.downloads and path.read_bytes() == original
    assert list(path.parent.glob('*.m4a')) == []
    assert store.storage.purge(job['id'])['status'] == 'deleted'


def test_audio_pending_survives_restart_and_interrupted_output_is_not_published(web_config, media_fixtures):
    store = JobStore(web_config)
    job, source, original = completed(store, media_fixtures[0])
    store.audio.request(job['id'])
    reopened = JobStore(web_config)
    reopened.recover()
    assert reopened.get(job['id'])['audio']['status'] == 'pending'
    assert reopened.audio.run_once(threading.Event())
    assert reopened.get(job['id'])['audio']['status'] == 'success'
    # Simulate a crash between creating a derived file and indexing it.
    other, path, data = completed(reopened, media_fixtures[0])
    token = 'b' * 32
    temporary = path.parent / f'.audio-{token}.m4a'
    temporary.write_bytes(b'partial')
    from lumabox.audio import audio_staging_path
    stage = audio_staging_path(temporary)
    stage.write_bytes(b'unpublished')
    manifest = []
    for owned in (temporary, stage):
        stat = owned.stat()
        manifest.append({'path': str(owned.relative_to(web_config.output_dir)), 'device': stat.st_dev, 'inode': stat.st_ino})
    with reopened.connection() as db:
        db.execute("UPDATE jobs SET audio_state='running',audio_token=?,audio_root=?,audio_manifest=? WHERE id=?", (token, str(web_config.output_dir), json.dumps(manifest), other['id']))
    restarted = JobStore(web_config)
    restarted.recover()
    assert restarted.get(other['id'])['audio'] == {'status': 'failed', 'error': 'INTERRUPTED'}
    assert not temporary.exists() and not stage.exists() and path.read_bytes() == data
    assert source.read_bytes() == original


def test_audio_queue_is_bounded_and_expiry_skips_active_audio(web_config, media_fixtures):
    store = JobStore(replace(web_config, retention_seconds=1))
    jobs = [completed(store, media_fixtures[0])[0] for _ in range(11)]
    for job in jobs[:10]:
        store.audio.request(job['id'])
    with pytest.raises(WebError, match='QUEUE_FULL'):
        store.audio.request(jobs[10]['id'])
    store.expire(now=10**12)
    assert all(store.get(job['id'])['status'] == 'success' for job in jobs[:10])
    assert store.get(jobs[10]['id'])['status'] == 'expired'


def test_missing_optional_audio_still_reuses_video_and_can_be_regenerated(web_config, media_fixtures):
    store = JobStore(web_config)
    job, source, original = completed(store, media_fixtures[0])
    store.audio.request(job['id']); store.audio.run_once(threading.Event())
    audio = next(file for file in store.get(job['id'])['files'] if file['mime'] == 'audio/mp4')
    store.file_path(store.file(audio['id'])).unlink()
    with client_for(store) as client:
        duplicate = client.post('/api/jobs', headers=headers(client), json={'input': URL, 'include_audio': True})
        assert duplicate.status_code == 409
        assert duplicate.json()['existing']['kind'] == 'video'
        assert duplicate.json()['existing']['id'] == job['id']
    store.audio.request(job['id']); store.audio.run_once(threading.Event())
    assert store.get(job['id'])['audio']['status'] == 'success'
    assert len(store.get(job['id'])['files']) == 2 and source.read_bytes() == original


def test_failed_download_does_not_leave_audio_waiting_and_retry_retains_choice(web_config):
    store = JobStore(web_config)
    job = store.create(URL, include_audio=True)
    store.fail(job['id'], 'AUTH_REQUIRED')
    assert store.get(job['id'])['audio']['status'] == 'none'
    retried = store.retry(job['id'])
    assert retried['include_audio'] and retried['audio']['status'] == 'waiting'


def test_audio_stale_source_rejected_before_queueing(web_config, media_fixtures):
    store = JobStore(web_config)
    job, path, _ = completed(store, media_fixtures[0])
    path.unlink()
    with pytest.raises(WebError):
        store.audio.request(job['id'])
    assert store.get(job['id'])['audio']['status'] == 'none'


def test_active_jobs_are_independent_of_history_filters_and_counts(web_config, media_fixtures):
    store = JobStore(web_config)
    saved, _, _ = completed(store, media_fixtures[0])
    failed = store.create('https://v.douyin.com/failed/')
    store.fail(failed['id'], 'AUTH_REQUIRED')
    active = store.create('https://v.douyin.com/active/')
    store.audio.request(saved['id'])
    with client_for(store) as client:
        response = client.get('/api/jobs/active')
        assert response.status_code == 200
        assert {job['id'] for job in response.json()['jobs']} == {saved['id'], active['id']}
        history = client.get('/api/jobs?scope=history').json()
        assert history['total'] == 1 and history['jobs'][0]['id'] == failed['id']
        assert client.get('/api/jobs?scope=invalid').status_code == 400
        assert client.get('/api/jobs').json()['total'] == 3
    store.audio.cancel(saved['id'])
    assert store.search(scope='history')['total'] == 2


def test_late_duplicate_transfers_audio_choice_to_saved_video(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config)
    saved, source, original = completed(store, media_fixtures[0])
    incoming = store.create('https://v.douyin.com/late-duplicate/', include_audio=True)
    worker = fixture_worker(web_config, store, tmp_path, media_fixtures[0])
    assert worker.run_once()
    duplicate = store.get(incoming['id'])
    assert duplicate['status'] == 'duplicate' and duplicate['duplicate_of'] == saved['id']
    assert duplicate['audio']['status'] == 'none'
    assert store.get(saved['id'])['audio']['status'] == 'pending'
    assert worker.run_once()
    assert store.get(saved['id'])['audio']['status'] == 'success'
    assert source.read_bytes() == original


def test_damaged_audio_regeneration_preserves_ownership_for_collection_deletion(web_config, media_fixtures):
    store = JobStore(web_config)
    job, source, original = completed(store, media_fixtures[0])
    store.audio.request(job['id']); store.audio.run_once(threading.Event())
    file = next(file for file in store.get(job['id'])['files'] if file['mime'] == 'audio/mp4')
    damaged = store.file_path(store.file(file['id']))
    damaged.write_bytes(b'broken')
    store.audio.request(job['id']); store.audio.run_once(threading.Event())
    assert store.get(job['id'])['audio']['status'] == 'success'
    assert source.read_bytes() == original
    assert store.storage.purge(job['id'])['status'] == 'deleted'
    assert not damaged.exists() and not source.exists()


def test_audio_failed_cleanup_is_retried_on_restart(web_config, media_fixtures, monkeypatch):
    from pathlib import Path
    from lumabox.errors import LumaBoxError
    import lumabox.web.audio as audio
    store = JobStore(web_config)
    job, source, original = completed(store, media_fixtures[0])
    real_unlink = Path.unlink
    failed = []
    def fail_once(path, *args, **kwargs):
        if path.name.startswith('.audio-') and not failed:
            failed.append(path)
            raise PermissionError('temporary file busy')
        return real_unlink(path, *args, **kwargs)
    def interrupted(source, target, **kwargs):
        target.write_bytes(b'partial')
        stat = target.stat()
        kwargs['on_created'](target, (stat.st_dev, stat.st_ino))
        raise LumaBoxError('INTERRUPTED')
    monkeypatch.setattr(audio, 'extract_audio', interrupted)
    monkeypatch.setattr(Path, 'unlink', fail_once)
    store.audio.request(job['id']); store.audio.run_once(threading.Event())
    assert store.get(job['id'])['audio']['error'] == 'CLEANUP_FAILED'
    assert failed[0].exists()
    restarted = JobStore(web_config)
    restarted.recover()
    assert not failed[0].exists()
    assert source.read_bytes() == original
    assert restarted.storage.purge(job['id'])['status'] == 'deleted'


def test_audio_competing_final_is_preserved_on_failed_publication(web_config, media_fixtures, monkeypatch):
    import lumabox.web.audio as audio
    store = JobStore(web_config)
    job, source, original = completed(store, media_fixtures[0])
    real = audio.extract_audio
    competing = []
    def collide(source, target, **kwargs):
        result = real(source, target, **kwargs)
        token = target.stem.removeprefix('.audio-')
        final = target.parent / f'{source.stem}-audio-{token}.m4a'
        final.write_bytes(b'keep this unrelated replacement')
        competing.append(final)
        return result
    monkeypatch.setattr(audio, 'extract_audio', collide)
    store.audio.request(job['id']); store.audio.run_once(threading.Event())
    assert store.get(job['id'])['audio']['status'] == 'failed'
    assert competing[0].read_bytes() == b'keep this unrelated replacement'
    assert source.read_bytes() == original


def test_audio_temporary_replacement_is_not_deleted_even_with_identical_bytes(web_config, media_fixtures, monkeypatch):
    import lumabox.web.audio as audio
    store = JobStore(web_config)
    job, source, original = completed(store, media_fixtures[0])
    real = audio.extract_audio
    replacement = []
    def replace_temporary(source, target, **kwargs):
        result = real(source, target, **kwargs)
        other = target.with_name('replacement.m4a')
        other.write_bytes(target.read_bytes())
        target.unlink(); other.rename(target)
        replacement.append((target, target.read_bytes()))
        return result
    monkeypatch.setattr(audio, 'extract_audio', replace_temporary)
    store.audio.request(job['id']); store.audio.run_once(threading.Event())
    assert replacement[0][0].read_bytes() == replacement[0][1]
    assert store.get(job['id'])['audio']['status'] == 'failed'
    assert source.read_bytes() == original


def test_audio_final_replaced_after_copy_is_neither_published_nor_deleted(web_config, media_fixtures, monkeypatch):
    store = JobStore(web_config)
    job, source, original = completed(store, media_fixtures[0])
    unlink = store.audio._unlink_created
    competing = []
    def replace_after_copy(row, temporary):
        unlink(row, temporary)
        _, _, final = store.audio._paths(row)
        other = final.with_name('replacement.m4a')
        other.write_bytes(b'x' * final.stat().st_size)
        competing.append(other)
        final.unlink(); other.rename(final)
        competing[-1] = final
    monkeypatch.setattr(store.audio, '_unlink_created', replace_after_copy)
    store.audio.request(job['id']); store.audio.run_once(threading.Event())
    assert store.get(job['id'])['audio']['status'] == 'failed'
    assert len(store.get(job['id'])['files']) == 1
    assert competing[0].read_bytes().startswith(b'xxx')
    assert source.read_bytes() == original


@pytest.mark.parametrize('state', ['pending', 'running', 'cancelling'])
def test_audio_output_migration_recovers_at_original_root(web_config, media_fixtures, state):
    from lumabox.audio import audio_staging_path
    store = JobStore(web_config)
    store.migrate_outputs()  # Remember this root as an existing installation.
    job, source, original = completed(store, media_fixtures[0])
    store.audio.request(job['id'])
    stage = None
    if state != 'pending':
        token = 'c' * 32
        stage = audio_staging_path(source.parent / f'.audio-{token}.m4a')
        stage.write_bytes(b'partial')
        stat = stage.stat()
        manifest = [{'path': str(stage.relative_to(web_config.output_dir)), 'device': stat.st_dev, 'inode': stat.st_ino}]
        with store.connection() as db:
            db.execute('UPDATE jobs SET audio_state=?,audio_token=?,audio_root=?,audio_manifest=? WHERE id=?',
                       (state, token, str(web_config.output_dir), json.dumps(manifest), job['id']))
    moved = JobStore(replace(web_config, output=web_config.data_dir.parent / 'Moved'))
    moved.migrate_outputs(); moved.recover()
    if stage is not None:
        assert not stage.exists()
    assert source.read_bytes() == original  # Migration retains the original copy.
    assert moved.get(job['id'])['audio']['error'] != 'CLEANUP_FAILED'
    if state != 'pending':
        moved.audio.request(job['id'])
    assert moved.audio.run_once(threading.Event())
    assert moved.get(job['id'])['audio']['status'] == 'success'
    assert moved.storage.purge(job['id'])['status'] == 'deleted'
