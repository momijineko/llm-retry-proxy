"""Streaming, byte-preserving migration of oversized JSONL files."""
import hashlib
import json
import os
import shutil
from pathlib import Path

CHUNK_BYTES = 1024 * 1024


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def split_file(source, stage, target_bytes):
    stage.mkdir(parents=True, exist_ok=False)
    prefix = source.name[:-6]
    shards = []
    digest = hashlib.sha256()
    position = 0
    rows = 0
    current = None
    current_size = 0
    boundary = True
    attributes = source.stat()

    def close_current():
        nonlocal current
        if current is not None:
            current.flush()
            os.fsync(current.fileno())
            current.close()
            current = None
            shards[-1]['size'] = current_size

    try:
        with source.open('rb') as original:
            while True:
                chunk = original.read(CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                rows += chunk.count(b'\n')
                cursor = 0
                while cursor < len(chunk):
                    if current is None or (current_size >= target_bytes and boundary):
                        close_current()
                        index = len(shards)
                        name = source.name if index == 0 else f'{prefix}_{index:06d}.jsonl'
                        current = (stage / name).open('xb')
                        os.chmod(stage / name, attributes.st_mode & 0o777)
                        shards.append({'name': name, 'start': position, 'size': 0})
                        current_size = 0
                    wanted = cursor + max(0, target_bytes - current_size)
                    newline = chunk.find(b'\n', wanted) if wanted < len(chunk) else -1
                    end = newline + 1 if newline >= 0 else len(chunk)
                    payload = chunk[cursor:end]
                    current.write(payload)
                    position += len(payload)
                    current_size += len(payload)
                    boundary = payload.endswith(b'\n')
                    cursor = end
        close_current()
    finally:
        if current is not None:
            current.close()
    after = source.stat()
    if after.st_size != attributes.st_size or after.st_mtime_ns != attributes.st_mtime_ns:
        raise RuntimeError('Source changed during splitting')
    verified = hashlib.sha256()
    copied = 0
    copied_rows = 0
    for shard in shards:
        with (stage / shard['name']).open('rb') as handle:
            while True:
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    break
                verified.update(chunk)
                copied += len(chunk)
                copied_rows += chunk.count(b'\n')
    if copied != position or copied_rows != rows or verified.digest() != digest.digest():
        raise RuntimeError('Split checksum verification failed')
    return {'source': source.name, 'bytes': position, 'rows': rows,
            'sha256': digest.hexdigest(), 'shards': shards}


def migrate(log_dir, backup, target_bytes=32 * 1024 * 1024):
    log_dir = Path(log_dir)
    backup = Path(backup)
    originals = backup / 'originals'
    originals.mkdir(parents=True, mode=0o700, exist_ok=False)
    summary_path = log_dir / '_summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    if summary.get('version', 0) < 7:
        raise RuntimeError('Summary must contain durable log offsets')
    before = json.loads(json.dumps(summary))
    shutil.copy2(summary_path, backup / '_summary.before.json')
    os.chmod(backup / '_summary.before.json', 0o600)
    candidates = sorted(path for path in log_dir.glob('retry_*.jsonl')
                        if path.stat().st_size > target_bytes)
    plans = []
    for source in candidates:
        # Migrating a daily base file is safe only before it already has shards.
        if len(source.name) != len('retry_2000-01-01.jsonl'):
            raise RuntimeError('Oversized existing shard requires manual inspection')
        if list(log_dir.glob(source.stem + '_*.jsonl')):
            raise RuntimeError('Daily file already has shards')
        offset = summary['log_offsets'].get(source.name)
        if not isinstance(offset, int) or not 0 <= offset <= source.stat().st_size:
            raise RuntimeError('Missing or invalid durable offset')
        plan = split_file(source, backup / 'stage' / source.stem, target_bytes)
        plans.append(plan)
        for shard in plan['shards']:
            summary['log_offsets'][shard['name']] = max(0, min(offset - shard['start'], shard['size']))
        print(json.dumps({'file': source.name, 'bytes': plan['bytes'],
                          'rows': plan['rows'], 'shards': len(plan['shards']),
                          'checksum_verified': True}), flush=True)
    unchanged_before = {k: v for k, v in before.items() if k != 'log_offsets'}
    unchanged_after = {k: v for k, v in summary.items() if k != 'log_offsets'}
    assert unchanged_before == unchanged_after
    atomic_json(backup / 'manifest.json', {'state': 'prepared', 'plans': plans})
    moved = []
    try:
        for plan in plans:
            source = log_dir / plan['source']
            os.replace(source, originals / source.name)
            moved.append(plan)
            stage = backup / 'stage' / source.stem
            for shard in plan['shards']:
                os.replace(stage / shard['name'], log_dir / shard['name'])
        atomic_json(summary_path, summary)
        atomic_json(backup / 'manifest.json', {'state': 'complete', 'plans': plans})
    except BaseException:
        for plan in reversed(moved):
            for shard in plan['shards']:
                path = log_dir / shard['name']
                if path.exists():
                    path.unlink()
            os.replace(originals / plan['source'], log_dir / plan['source'])
        atomic_json(summary_path, before)
        raise
    print(json.dumps({'migration_complete': True, 'files': len(plans),
                      'shards': sum(len(p['shards']) for p in plans),
                      'summary_totals_unchanged': True}), flush=True)
    return plans
