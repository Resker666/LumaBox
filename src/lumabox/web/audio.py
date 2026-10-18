"""Persistent, optional audio attachments processed by the existing single worker."""
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from ..audio import extract_audio, audio_staging_path
from ..gallery_music import download_gallery_music
from ..errors import LumaBoxError
from ..diagnostics import public_diagnostics
from .config import WebError

ACTIVE_AUDIO = frozenset({'pending', 'running', 'cancelling'})
AUDIO_LIMIT = 200 * 1024**2
AUDIO_ERRORS = frozenset({'NO_AUDIO', 'AUDIO_FAILED', 'INTERRUPTED', 'SIZE_LIMIT',
                          'DEPENDENCY_MISSING', 'TIMEOUT', 'VERIFY_FAILED', 'STORAGE_FULL',
                          'UNSAFE_TARGET', 'AUTH_REQUIRED', 'CHALLENGE_REQUIRED', 'BLOCKED',
                          'RATE_LIMITED', 'NOT_FOUND', 'DOWNLOAD_FAILED', 'UNSUPPORTED_MEDIA'})


class AudioJobs:
    def __init__(self, store):
        self.store = store

    def request(self, job_id):
        with self.store.lock:
            job = self.store.get(job_id)
            if job['status'] != 'success' or job['kind'] not in ('video', 'gallery') or job['storage_state'] != 'present':
                raise WebError('AUDIO_UNAVAILABLE', 409)
            if job['audio']['status'] in ACTIVE_AUDIO:
                return job
            with self.store.connection() as db:
                for file in db.execute("SELECT * FROM files WHERE job_id=? AND mime IN ('audio/mp4','audio/mpeg')", (job_id,)):
                    try:
                        self.store.file_path(dict(file))
                        return job
                    except (WebError, OSError):
                        pass
                if db.execute("SELECT count(*) FROM jobs WHERE audio_state IN ('pending','running','cancelling')").fetchone()[0] >= 10:
                    raise WebError('QUEUE_FULL', 409)
                row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row['audio_token'] and not self._clean(row):
                    raise WebError('AUDIO_UNAVAILABLE', 409)
                self.store.file_path(self._source(row))
                # Retain invalid/missing attachment indexes so explicit purge still
                # knows their ownership. Public views omit unusable optional files.
                db.execute("UPDATE jobs SET include_audio=1,audio_state='pending',audio_error=NULL,audio_error_detail='{}',audio_token=NULL,audio_root=NULL,audio_manifest='[]' WHERE id=?", (job_id,))
            self.store.storage.invalidate()
            return self.store.get(job_id)

    def cancel(self, job_id):
        with self.store.lock:
            job = self.store.get(job_id)
            if job['audio']['status'] not in ACTIVE_AUDIO | {'cancelled'}:
                raise WebError('NOT_CANCELLABLE', 409)
            with self.store.connection() as db:
                db.execute("UPDATE jobs SET audio_state=CASE WHEN audio_state='pending' THEN 'cancelled' WHEN audio_state='running' THEN 'cancelling' ELSE audio_state END WHERE id=?", (job_id,))
            self.store.storage.invalidate()
            return self.store.get(job_id)

    def _source(self, row):
        with self.store.connection() as db:
            if row['kind'] == 'gallery':
                file = db.execute("SELECT * FROM files WHERE job_id=? AND mime IN ('image/jpeg','image/webp') ORDER BY rowid LIMIT 1", (row['id'],)).fetchone()
            else:
                file = db.execute("SELECT * FROM files WHERE job_id=? AND mime='video/mp4'", (row['id'],)).fetchone()
        if not file:
            raise WebError('AUDIO_UNAVAILABLE', 409)
        file = dict(file)
        relative = Path(file['relative_path'])
        if (len(relative.parts) != 3 or relative.parts[0] != row['id'] or
                not re.fullmatch(fr"{re.escape(row['work_id'])}-[0-9a-f]{{32}}", relative.parts[1]) or
                (not re.fullmatch(r'[0-9]{2}\.(?:jpg|webp)', relative.name) if row['kind'] == 'gallery'
                 else relative.name != f"{row['work_id']}.mp4")):
            raise WebError('VERIFY_FAILED')
        return file

    def _paths(self, row, root=None):
        token = row['audio_token']
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
            raise WebError('VERIFY_FAILED')
        source = self._source(row)
        root = self.store.config.output_dir if root is None else root
        parent = Path(source['relative_path']).parent
        directory = self.store.safe_path(str(parent), root=root)
        extension = 'mp3' if row['kind'] == 'gallery' else 'm4a'
        temporary = directory / f'.audio-{token}.{extension}'
        final = directory / f"{row['work_id']}-audio-{token}.{extension}"
        for path in (temporary, final, audio_staging_path(temporary)):
            if os.path.lexists(path):
                self.store.safe_path(str(path.relative_to(root)), root=root)
        return source, temporary, final

    def _clean(self, row, root=None):
        root = self.store.config.output_dir if root is None else root
        if row['audio_root'] != str(root):
            return False
        try:
            _, temporary, final = self._paths(row, root)
            allowed = {str(p.relative_to(root)) for p in (temporary, final, audio_staging_path(temporary))}
            manifest = json.loads(row['audio_manifest'] or '[]')
            if (not isinstance(manifest, list) or len(manifest) > 3 or
                    any(not isinstance(item, dict) or set(item) != {'path', 'device', 'inode'} or
                        item['path'] not in allowed or type(item['device']) is not int or
                        type(item['inode']) is not int for item in manifest)):
                return False
            with self.store.connection() as db:
                indexed = {f[0] for f in db.execute('SELECT relative_path FROM files WHERE job_id=?', (row['id'],))}
            complete = True
            for item in manifest:
                if item['path'] in indexed:
                    continue
                path = root / item['path']
                if not os.path.lexists(path):
                    continue
                path = self.store.safe_path(item['path'], root=root)
                stat = path.stat(follow_symlinks=False)
                if (stat.st_dev, stat.st_ino) != (item['device'], item['inode']):
                    complete = False
                    continue
                path.unlink()
            return complete
        except (OSError, WebError, ValueError, TypeError, KeyError):
            return False

    def recover(self, root=None):
        with self.store.lock:
            with self.store.connection() as db:
                rows = db.execute("SELECT * FROM jobs WHERE audio_state IN ('running','cancelling') OR audio_error='CLEANUP_FAILED'").fetchall()
            for row in rows:
                if root is not None and row['audio_root'] != str(root):
                    continue
                clean = self._clean(row, root)
                status = 'cancelled' if row['audio_state'] in ('cancelling', 'cancelled') else 'failed'
                with self.store.connection() as db:
                    db.execute("UPDATE jobs SET audio_state=?,audio_error=?,audio_error_detail='{}' WHERE id=?",
                               (status, (None if status == 'cancelled' else 'INTERRUPTED') if clean else 'CLEANUP_FAILED', row['id']))
                    if clean:
                        db.execute("UPDATE jobs SET audio_token=NULL,audio_root=NULL,audio_manifest='[]' WHERE id=?", (row['id'],))
            self.store.storage.invalidate()

    def _record_created(self, row, path, identity):
        source, temporary, final = self._paths(row)
        if path not in (temporary, final, audio_staging_path(temporary)):
            raise WebError('VERIFY_FAILED')
        stat = path.stat(follow_symlinks=False)
        if (stat.st_dev, stat.st_ino) != identity:
            raise WebError('VERIFY_FAILED')
        entry = {'path': str(path.relative_to(self.store.config.output_dir)), 'device': identity[0], 'inode': identity[1]}
        with self.store.connection() as db:
            current = db.execute('SELECT audio_token,audio_state,audio_manifest FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if current is None or current['audio_token'] != row['audio_token'] or current['audio_state'] not in ('running', 'cancelling'):
                raise WebError('INTERRUPTED')
            manifest = json.loads(current['audio_manifest'] or '[]')
            if len(manifest) >= 3 or any(item['path'] == entry['path'] for item in manifest):
                raise WebError('VERIFY_FAILED')
            manifest.append(entry)
            db.execute('UPDATE jobs SET audio_manifest=? WHERE id=?', (json.dumps(manifest), row['id']))

    def _owned_path(self, row, path):
        with self.store.connection() as db:
            manifest = json.loads(db.execute('SELECT audio_manifest FROM jobs WHERE id=?', (row['id'],)).fetchone()[0])
        relative = str(path.relative_to(self.store.config.output_dir))
        entry = next((item for item in manifest if item['path'] == relative), None)
        path = self.store.safe_path(relative)
        stat = path.stat(follow_symlinks=False)
        if entry is None or (stat.st_dev, stat.st_ino) != (entry['device'], entry['inode']):
            raise WebError('VERIFY_FAILED')
        return path

    def _unlink_created(self, row, path):
        self._owned_path(row, path).unlink()

    def run_once(self, stopping):
        if stopping.is_set():
            return False
        with self.store.connection() as db:
            if db.execute("SELECT 1 FROM jobs WHERE audio_state IN ('running','cancelling')").fetchone():
                return False
            row = db.execute("SELECT * FROM jobs WHERE audio_state='pending' AND status='success' AND hidden_at IS NULL AND storage_state='present' ORDER BY updated_at,rowid LIMIT 1").fetchone()
            if not row:
                return False
            token = uuid.uuid4().hex
            db.execute("UPDATE jobs SET audio_state='running',audio_token=?,audio_root=?,audio_manifest='[]',audio_error_detail='{}' WHERE id=?",
                       (token, str(self.store.config.output_dir), row['id']))
            row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
        self.store.storage.invalidate()
        published = False
        error = 'AUDIO_FAILED'
        detail = {}

        def cancelled():
            if stopping.is_set():
                return True
            with self.store.connection() as db:
                current = db.execute('SELECT audio_state FROM jobs WHERE id=?', (row['id'],)).fetchone()
            return current is None or current['audio_state'] != 'running'

        try:
            source, temporary, final = self._paths(row)
            if temporary.exists() or final.exists():
                raise WebError('VERIFY_FAILED')
            reserve = max(self.store.config.reserve_bytes, 2 * AUDIO_LIMIT + 64 * 1024**2)
            if (shutil.disk_usage(self.store.config.output_dir).free < reserve or
                    (self.store.config.capacity_bytes is not None and self.store.usage() + reserve > self.store.config.capacity_bytes)):
                raise WebError('STORAGE_FULL')
            with self.store.open_file(source['id']) as (_, stream):
                digest = hashlib.sha256()
                while block := stream.read(1024**2):
                    if cancelled():
                        raise WebError('INTERRUPTED')
                    digest.update(block)
                if digest.hexdigest() != source['sha256']:
                    raise WebError('VERIFY_FAILED')
                record = lambda path, identity: self._record_created(row, path, identity)
                if row['kind'] == 'gallery':
                    data = download_gallery_music(row['work_id'], temporary, headless=self.store.config.headless,
                                                  cancelled=cancelled, max_bytes=AUDIO_LIMIT, on_created=record)
                    mime = 'audio/mpeg'
                else:
                    data = extract_audio(self.store.file_path(source), temporary, cancelled=cancelled, max_bytes=AUDIO_LIMIT, on_created=record)
                    mime = 'audio/mp4'
                if cancelled():
                    raise WebError('INTERRUPTED')
                if final.exists():
                    raise WebError('VERIFY_FAILED')
                digest, count = hashlib.sha256(), 0
                with final.open('x+b') as destination:
                    stat = os.fstat(destination.fileno())
                    record(final, (stat.st_dev, stat.st_ino))
                    with temporary.open('rb') as original:
                        while block := original.read(1024**2):
                            if cancelled():
                                raise WebError('INTERRUPTED')
                            count += len(block); digest.update(block); destination.write(block)
                            if count > AUDIO_LIMIT:
                                raise WebError('SIZE_LIMIT')
                    if count != data['bytes'] or digest.hexdigest() != data['sha256']:
                        raise WebError('VERIFY_FAILED')
                    destination.flush()
                    destination.seek(0)
                    digest = hashlib.sha256()
                    while block := destination.read(1024**2):
                        if cancelled():
                            raise WebError('INTERRUPTED')
                        digest.update(block)
                    if digest.hexdigest() != data['sha256']:
                        raise WebError('VERIFY_FAILED')
                    self._unlink_created(row, temporary)
                    with self.store.lock:
                        if cancelled():
                            raise WebError('INTERRUPTED')
                        self._owned_path(row, final)
                        if self.store.config.capacity_bytes is not None and self.store.usage() > self.store.config.capacity_bytes:
                            raise WebError('STORAGE_FULL')
                        with self.store.connection() as db:
                            db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)', (
                                uuid.uuid4().hex, row['id'], str(final.relative_to(self.store.config.output_dir)), final.name,
                                data['bytes'], data['sha256'], mime, None, None, data['duration']))
                            db.execute("UPDATE jobs SET audio_state='success',audio_error=NULL,audio_error_detail='{}',updated_at=? WHERE id=?", (time.time(), row['id']))
                        published = True
        except (LumaBoxError, WebError) as exc:
            error = exc.code if exc.code in AUDIO_ERRORS else 'AUDIO_FAILED'
            if isinstance(exc, LumaBoxError) and exc.code in AUDIO_ERRORS:
                detail = public_diagnostics(exc.diagnostics)
        except Exception:
            error = 'AUDIO_FAILED'
        finally:
            if not published:
                with self.store.lock:
                    with self.store.connection() as db:
                        row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
                    clean = self._clean(row)
                    with self.store.connection() as db:
                        current = db.execute('SELECT audio_state FROM jobs WHERE id=?', (row['id'],)).fetchone()
                        status = 'cancelled' if current and current['audio_state'] == 'cancelling' else 'failed'
                        db.execute('UPDATE jobs SET audio_state=?,audio_error=?,audio_error_detail=? WHERE id=?',
                                   (status, None if status == 'cancelled' and clean else error if clean else 'CLEANUP_FAILED',
                                    json.dumps(detail if status == 'failed' and clean else {}), row['id']))
            self.store.storage.invalidate()
        return True
