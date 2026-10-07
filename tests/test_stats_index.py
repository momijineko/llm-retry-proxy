import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from retry_proxy.log_store import RetryLogStore
from retry_proxy.stats import compute_stats
from retry_proxy.stats_index import StatsIndex


class StatsIndexTests(unittest.TestCase):
    def test_index_contains_all_prior_shards_and_omits_request_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = []
            for number in range(3):
                name = f'retry_{datetime.now():%Y-%m-%d}_{number:06d}.jsonl'
                names.append(name)
                record = {'ts': f'{datetime.now():%Y-%m-%d}T00:00:0{number}',
                          'model': 'test', 'retries': number, 'final_status': 200,
                          'request_body': {'input': 'private' * 1000}}
                (Path(tmp) / name).write_text(json.dumps(record) + '\n')
            index = StatsIndex(tmp)
            self.assertEqual(index.recover(names, lambda r: bool(r.get('model')))['indexed'], 3)
            records = index.load('', 100)
            self.assertEqual(len(records), 3)
            self.assertTrue(all('request_body' not in r for r in records))
            self.assertEqual(compute_stats(records, 'all', {})['summary']['total_retries'], 3)
            # A statistics query must not reopen historical JSONL files.
            with patch('builtins.open', side_effect=AssertionError('No raw log read allowed')):
                self.assertEqual(len(index.load('', 100)), 3)
            index.snapshot({'total_requests': 3, 'total_retries': 3, 'log_offsets': {}})
            latest = json.loads((Path(tmp) / '_stats_latest.json').read_text())
            snapshot = json.loads((Path(tmp) / latest['statistics_file']).read_text())
            self.assertEqual(snapshot['cumulative']['total_requests'], 3)
            self.assertEqual(snapshot['indexed_requests'], 3)

    def test_recovery_is_idempotent_and_replays_only_complete_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = f'retry_{datetime.now():%Y-%m-%d}.jsonl'
            first = json.dumps({'model': 'test', 'ts': f'{datetime.now():%Y-%m-%d}T00:00:00'}).encode() + b'\n'
            second = json.dumps({'model': 'test', 'ts': f'{datetime.now():%Y-%m-%d}T00:00:01'}).encode() + b'\n'
            path = Path(tmp) / name
            path.write_bytes(first + second[:-1])
            index = StatsIndex(tmp)
            index.recover([name], lambda r: bool(r.get('model')))
            self.assertEqual(len(index.load('', 100)), 1)
            with path.open('ab') as handle:
                handle.write(b'\n')
            restored = StatsIndex(tmp)
            restored.recover([name], lambda r: bool(r.get('model')))
            restored.recover([name], lambda r: bool(r.get('model')))
            self.assertEqual(len(restored.load('', 100)), 2)

    def test_timestamp_cutoff_excludes_older_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = StatsIndex(tmp)
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            today = datetime.now().strftime('%Y-%m-%d')
            index.append('old', 1, {'model': 'test', 'ts': yesterday + 'T23:59:00'})
            index.append('new', 1, {'model': 'test', 'ts': today + 'T00:01:00'})
            self.assertEqual(len(index.load(today, 10)), 1)

    def test_record_limit_explicitly_marks_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = StatsIndex(tmp)
            for i in range(5):
                index.append('file', i + 1, {'model': 'test', 'ts': str(i)})
            records = index.load('', 2)
            self.assertEqual(len(records), 2)
            self.assertTrue(records.truncated)

    def test_compact_stats_budget_is_separate_from_raw_body_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = StatsIndex(tmp)
            for i in range(3):
                index.append('file', i + 1, {'model': 'test', 'ts': str(i)})
            with patch('retry_proxy.stats_index._record_bytes', return_value=20 * 1024 * 1024):
                result = index.load('', 10)
                self.assertEqual(len(result), 3)
                self.assertFalse(result.truncated)
                index.append('file', 4, {'model': 'test', 'ts': '3'})
                result = index.load('', 10)
                self.assertEqual(len(result), 3)
                self.assertTrue(result.truncated)


class StatsIndexWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotation_carries_prior_statistics_and_restart_is_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(log_dir=tmp, summary_file=os.path.join(tmp, '_summary.json'),
                                     legacy_log_file=os.path.join(tmp, 'legacy.jsonl'), log_retention_days=0)
            with patch('retry_proxy.log_store.settings', config), patch('retry_proxy.log_store.MAX_LOG_FILE_BYTES', 200):
                store = RetryLogStore()
                store.initialize()
                for i in range(6):
                    await store.write({'model': 'test', 'ts': f'{datetime.now():%Y-%m-%d}T00:00:0{i}',
                                       'retries': 0, 'final_status': 200, 'n': i})
                store.flush()
                self.assertGreater(len(store._log_files()), 1)
                self.assertEqual(len(store.load_stats(1, 100)), 6)
                restored = RetryLogStore()
                restored.initialize()
                self.assertEqual(restored.summary['total_requests'], 6)
                self.assertEqual(len(restored.load_stats(1, 100)), 6)
                latest = json.loads((Path(tmp) / '_stats_latest.json').read_text())
                snapshot = json.loads((Path(tmp) / latest['statistics_file']).read_text())
                self.assertEqual(snapshot['cumulative']['total_requests'], 6)
                self.assertEqual(snapshot['indexed_requests'], 6)
