"""Persistent compact statistics, independent of large request-body shards."""
import json
import os
import sqlite3
from contextlib import closing

from .log_reader import LogRecords, _record_bytes
from .stats import _normalize_provider

FIELDS = ('ts', 'method', 'path', 'provider', 'model', 'key_id', 'key_pool',
          'key_attempts', 'upstream_status', 'final_status', 'stream_status',
          'stream_error_status', 'succeeded', 'first_ok', 'attempts', 'retries',
          'retry_codes', 'duration_s', 'prompt_tokens', 'completion_tokens',
          'total_tokens', 'cached_tokens', 'mode')
MAX_RECOVERY_LINE = 16 * 1024 * 1024
MAX_STATS_OBJECT_BYTES = 64 * 1024 * 1024


class StatsIndex:
    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.path = os.path.join(log_dir, '_stats.sqlite3')
        with closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    file TEXT NOT NULL, offset INTEGER NOT NULL,
                    ts TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(file, offset));
                CREATE INDEX IF NOT EXISTS stats_timestamp ON records(ts);
                CREATE TABLE IF NOT EXISTS progress (
                    file TEXT PRIMARY KEY, offset INTEGER NOT NULL);
            """)
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA cache_size=-8192')
        db.execute('PRAGMA synchronous=NORMAL')
        return db

    @staticmethod
    def compact(record):
        return {key: record[key] for key in FIELDS if key in record}

    def append(self, file, offset, record):
        payload = json.dumps(self.compact(record), ensure_ascii=False)
        with closing(self.connect()) as db:
            with db:
                db.execute('INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?)',
                           (file, offset, record.get('ts', '') or '', payload))
                db.execute('INSERT OR REPLACE INTO progress VALUES (?, ?)', (file, offset))

    def recover(self, names, include):
        recovered = 0
        skipped = 0
        with closing(self.connect()) as db:
            progress = dict(db.execute('SELECT file, offset FROM progress'))
            changed_dates = {name[6:16] for name in names
                             if progress.get(name, 0) > os.path.getsize(os.path.join(self.log_dir, name))}
            # A split/truncation changes offsets within that date. Rebuild only
            # that date rather than serving duplicates or disabling the index.
            for date in changed_dates:
                with db:
                    db.execute("DELETE FROM records WHERE substr(file, 7, 10) = ?", (date,))
                    db.execute("DELETE FROM progress WHERE substr(file, 7, 10) = ?", (date,))
                progress = {name: offset for name, offset in progress.items() if name[6:16] != date}

            for name in names:
                path = os.path.join(self.log_dir, name)
                size = os.path.getsize(path)
                offset = progress.get(name, 0)
                if offset > size:
                    raise ValueError('Statistics index offset exceeds shard size')
                if offset == size:
                    continue
                with open(path, 'rb') as handle:
                    handle.seek(offset)
                    batch = []
                    while True:
                        line = handle.readline(MAX_RECOVERY_LINE + 1)
                        if not line:
                            break
                        if len(line) > MAX_RECOVERY_LINE:
                            # Consume the rest without materializing a giant row.
                            while line and not line.endswith(b'\n'):
                                line = handle.readline(64 * 1024)
                            offset = handle.tell()
                            skipped += 1
                            continue
                        if not line.endswith(b'\n'):
                            break
                        offset = handle.tell()
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            skipped += 1
                            continue
                        if isinstance(record, dict) and include(record):
                            compact = self.compact(record)
                            batch.append((name, offset, compact.get('ts', '') or '',
                                          json.dumps(compact, ensure_ascii=False)))
                            recovered += 1
                        if len(batch) >= 100:
                            with db:
                                db.executemany('INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?)', batch)
                                db.execute('INSERT OR REPLACE INTO progress VALUES (?, ?)', (name, offset))
                            batch.clear()
                    with db:
                        if batch:
                            db.executemany('INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?)', batch)
                        db.execute('INSERT OR REPLACE INTO progress VALUES (?, ?)', (name, offset))
        return {'indexed': recovered, 'skipped': skipped}

    def load(self, cutoff, max_records):
        result = LogRecords()
        retained = 0
        with closing(self.connect()) as db:
            query = 'SELECT payload FROM records WHERE ts >= ? ORDER BY ts DESC, file DESC, offset DESC LIMIT ?'
            for (payload,) in db.execute(query, (cutoff, max_records + 1)):
                record = json.loads(payload)
                cost = _record_bytes(record)
                if len(result) >= max_records or retained + cost > MAX_STATS_OBJECT_BYTES:
                    result.truncated = True
                    break
                record['provider'] = _normalize_provider(record.get('provider', ''))
                result.append(record)
                retained += cost
        result.reverse()
        return result

    @staticmethod
    def _write_json(path, value):
        temp = path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(temp, path)

    def snapshot(self, summary):
        with closing(self.connect()) as db:
            row = db.execute('SELECT file FROM progress ORDER BY file DESC LIMIT 1').fetchone()
            count = db.execute('SELECT count(*) FROM records').fetchone()[0]
        if row is None:
            return
        name = row[0] + '.stats.json'
        self._write_json(os.path.join(self.log_dir, name), {
            'version': 1, 'shard': row[0], 'index': '_stats.sqlite3',
            'indexed_requests': count,
            'cumulative': {key: value for key, value in summary.items() if key != 'log_offsets'},
        })
        self._write_json(os.path.join(self.log_dir, '_stats_latest.json'), {
            'version': 1, 'statistics_file': name, 'index': '_stats.sqlite3',
        })
