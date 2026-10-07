import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from retry_proxy.log_split import migrate, split_file
from retry_proxy.log_store import RetryLogStore


class LogSplitTests(unittest.TestCase):
    def test_streaming_split_preserves_bytes_and_complete_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'retry_2026-10-07.jsonl'
            data = b'first\n' + b'x' * 1000 + b'\n' + '中文\nlast\n'.encode()
            source.write_bytes(data)
            with patch('retry_proxy.log_split.CHUNK_BYTES', 13):
                plan = split_file(source, Path(tmp) / 'stage', 100)
            copies = [(Path(tmp) / 'stage' / shard['name']).read_bytes()
                      for shard in plan['shards']]
            self.assertEqual(b''.join(copies), data)
            self.assertTrue(all(part.endswith(b'\n') for part in copies))
            self.assertEqual(plan['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(plan['rows'], 4)

    def test_migration_maps_durable_offsets_and_preserves_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / 'logs'
            logs.mkdir()
            source = logs / 'retry_2026-10-07.jsonl'
            lines = [json.dumps({'model': 'test', 'ts': f'2026-10-07T00:00:0{i}',
                                 'retries': 0, 'final_status': 200}).encode() + b'\n'
                     for i in range(5)]
            data = b''.join(lines)
            source.write_bytes(data)
            offset = len(b''.join(lines[:3]))
            summary = {'version': 7, 'total_requests': 3, 'by_model': {'test': {'requests': 3}},
                       'log_offsets': {source.name: offset}}
            (logs / '_summary.json').write_text(json.dumps(summary))
            plans = migrate(logs, Path(tmp) / 'backup', 100)
            updated = json.loads((logs / '_summary.json').read_text())
            self.assertEqual(updated['total_requests'], 3)
            self.assertEqual(updated['by_model'], summary['by_model'])
            tail = []
            for shard in plans[0]['shards']:
                payload = (logs / shard['name']).read_bytes()
                tail.append(payload[updated['log_offsets'][shard['name']]:])
            self.assertEqual(b''.join(tail), b''.join(lines[3:]))
            self.assertEqual((Path(tmp) / 'backup/originals' / source.name).read_bytes(), data)

    def test_failed_commit_restores_original_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / 'logs'
            logs.mkdir()
            source = logs / 'retry_2026-10-07.jsonl'
            data = b'{}\n' * 200
            source.write_bytes(data)
            summary = {'version': 7, 'total_requests': 200, 'log_offsets': {source.name: len(data)}}
            (logs / '_summary.json').write_text(json.dumps(summary))
            original_replace = os.replace
            failed = [False]
            def replace(src, dst):
                if 'stage' in str(src) and not failed[0]:
                    failed[0] = True
                    raise OSError('synthetic commit failure')
                return original_replace(src, dst)
            with patch('retry_proxy.log_split.os.replace', side_effect=replace):
                with self.assertRaises(OSError):
                    migrate(logs, Path(tmp) / 'backup', 100)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(json.loads((logs / '_summary.json').read_text()), summary)
            self.assertEqual(len(list(logs.glob('retry_*.jsonl'))), 1)


class LogRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_rotates_and_restart_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(log_dir=tmp, summary_file=os.path.join(tmp, '_summary.json'),
                                     legacy_log_file=os.path.join(tmp, 'legacy.jsonl'),
                                     log_retention_days=0)
            with patch('retry_proxy.log_store.settings', config), patch('retry_proxy.log_store.MAX_LOG_FILE_BYTES', 200):
                store = RetryLogStore()
                store.initialize()
                for i in range(6):
                    await store.write({'model': 'test', 'ts': f'{datetime.now():%Y-%m-%d}T00:00:0{i}',
                                       'n': i, 'retries': 0, 'final_status': 200})
                store.flush()
                names = store._log_files()
                self.assertGreater(len(names), 1)
                self.assertTrue(all(os.path.getsize(os.path.join(tmp, n)) <= 200 for n in names))
                recovered = RetryLogStore()
                recovered.initialize()
                self.assertEqual(recovered.summary['total_requests'], 6)
                self.assertEqual([r['n'] for r in recovered.load(1, 20)], list(range(6)))
                # Resume on the final numbered shard after process restart.
                await recovered.write({'model': 'test', 'ts': f'{datetime.now():%Y-%m-%d}T00:00:06',
                                       'n': 6, 'retries': 0, 'final_status': 200})
                self.assertEqual(recovered.summary['total_requests'], 7)
                self.assertEqual([r['n'] for r in recovered.load(1, 20)], list(range(7)))

    async def test_cleanup_covers_numbered_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            date = datetime.now() - timedelta(days=90)
            for suffix in ('', '_000001'):
                (Path(tmp) / f'retry_{date:%Y-%m-%d}{suffix}.jsonl').write_bytes(b'{}\n')
            with patch('retry_proxy.log_store.settings', SimpleNamespace(log_dir=tmp, log_retention_days=30)):
                RetryLogStore()._cleanup()
            self.assertEqual(list(Path(tmp).glob('retry_*.jsonl')), [])

class SplitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_replays_only_unflushed_tail_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / 'logs'
            logs.mkdir()
            config = SimpleNamespace(log_dir=str(logs), summary_file=str(logs / '_summary.json'),
                                     legacy_log_file=str(logs / 'legacy.jsonl'), log_retention_days=0)
            with patch('retry_proxy.log_store.settings', config):
                store = RetryLogStore()
                store.initialize()
                for i in range(5):
                    await store.write({'model': 'test', 'ts': f'{datetime.now():%Y-%m-%d}T00:00:0{i}',
                                       'retries': 0, 'final_status': 200})
                    if i == 2:
                        store.flush()
                migrate(logs, Path(tmp) / 'backup', 150)
                restored = RetryLogStore()
                restored.initialize()
                self.assertEqual(restored.summary['total_requests'], 5)
                self.assertEqual(len(restored.load(1, 10)), 5)
                again = RetryLogStore()
                again.initialize()
                self.assertEqual(again.summary['total_requests'], 5)
