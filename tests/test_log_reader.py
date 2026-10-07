import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from retry_proxy import log_reader
from retry_proxy.log_store import RetryLogStore


class BoundedLogReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = patch('retry_proxy.log_store.settings', SimpleNamespace(log_dir=self.tmp.name))
        self.config.start()
        self.addCleanup(self.config.stop)
        self.excluded = patch('retry_proxy.log_reader.is_excluded_path', return_value=False)
        self.excluded.start()
        self.addCleanup(self.excluded.stop)
        self.path = os.path.join(self.tmp.name, f'retry_{datetime.now():%Y-%m-%d}.jsonl')

    def record(self, number, body='hello'):
        return json.dumps({'model': 'test', 'n': number, 'request_body': {'input': body}}).encode() + b'\n'

    def write(self, data):
        with open(self.path, 'wb') as handle:
            handle.write(data)

    def test_sparse_gigabyte_file_reads_only_tail_and_latest_records(self):
        with open(self.path, 'wb') as handle:
            handle.seek(1024 * 1024 * 1024)
            handle.write(b'\n' + self.record(1) + self.record(2))
        records = RetryLogStore().load(1, max_records=2)
        self.assertEqual([r['n'] for r in records], [1, 2])
        self.assertLessEqual(records.scanned_bytes, log_reader.READ_CHUNK_BYTES)

    def test_reverse_dates_returns_latest_days_in_chronological_order(self):
        old = os.path.join(self.tmp.name, f'retry_{datetime.now() - timedelta(days=1):%Y-%m-%d}.jsonl')
        with open(old, 'wb') as handle:
            handle.write(self.record(1))
        self.write(self.record(2) + self.record(3))
        self.assertEqual([r['n'] for r in RetryLogStore().load(0, 2)], [2, 3])

    def test_unfinished_tail_and_malformed_record_are_ignored(self):
        self.write(self.record(1) + b'bad json\n' + self.record(2)[:-1])
        records = RetryLogStore().load(1, 10)
        self.assertEqual([r['n'] for r in records], [1])
        self.assertEqual(records.skipped_records, 1)

    def test_oversized_record_crossing_chunks_is_skipped(self):
        self.write(self.record(1) + self.record(2, 'x' * 1000) + self.record(3))
        with patch.object(log_reader, 'READ_CHUNK_BYTES', 73), patch.object(log_reader, 'MAX_LINE_BYTES', 200):
            records = RetryLogStore().load(1, 10)
        self.assertEqual([r['n'] for r in records], [1, 3])
        self.assertEqual(records.skipped_records, 1)
        self.assertTrue(records.truncated)

    def test_scan_budget_stops_without_reading_whole_file(self):
        self.write(b'\n' + b'x' * 10000 + b'\n' + self.record(1))
        with patch.object(log_reader, 'MAX_SCAN_BYTES', 512):
            records = RetryLogStore().load(1, 10)
        self.assertEqual([r['n'] for r in records], [1])
        self.assertEqual(records.scanned_bytes, 512)
        self.assertTrue(records.truncated)

    def test_result_byte_budget_and_body_projection(self):
        self.write(self.record(1, 'x' * 700) + self.record(2, 'x' * 700))
        with patch.object(log_reader, 'MAX_RESULT_BYTES', 2100):
            full = RetryLogStore().load(1, 10)
            projected = RetryLogStore().load(1, 10, include_body=False)
        self.assertEqual(len(full), 1)
        self.assertTrue(full.truncated)
        self.assertEqual(len(projected), 2)
        self.assertNotIn('request_body', projected[0])

    def test_utf8_across_chunk_boundary(self):
        self.write(json.dumps({'model': 'test', 'request_body': '中文' * 20}, ensure_ascii=False).encode() + b'\n')
        with patch.object(log_reader, 'READ_CHUNK_BYTES', 7):
            records = RetryLogStore().load(1, 10)
        self.assertEqual(records[0]['request_body'], '中文' * 20)


class BackgroundReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_runs_off_event_loop(self):
        import threading
        main_thread = threading.get_ident()
        threads = []
        store = SimpleNamespace(load=lambda *args, **kwargs: threads.append(threading.get_ident()) or [])
        await log_reader.load_async(store, 1, 5)
        self.assertNotEqual(threads[0], main_thread)

    async def test_cancelled_request_does_not_allow_parallel_read(self):
        import threading
        entered = threading.Event()
        release = threading.Event()
        active = [0]
        peak = [0]
        def read(*args, **kwargs):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            entered.set()
            release.wait(2)
            active[0] -= 1
            return []
        store = SimpleNamespace(load=read)
        first = asyncio.create_task(log_reader.load_async(store, 1, 5))
        await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(log_reader.load_async(store, 1, 5))
        await asyncio.sleep(0.03)
        release.set()
        await second
        self.assertEqual(peak[0], 1)

class AnalysisEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_reads_history_once_and_filters_each_window(self):
        from unittest.mock import Mock
        from retry_proxy.api import create_handlers
        now = datetime.now()
        records = [{'ts': (now - timedelta(days=age)).isoformat(), 'model': 'test',
                    'provider': 'synthetic', 'final_status': 200, 'succeeded': True,
                    'attempts': 1, 'retries': 0, 'duration_s': 0}
                   for age in (0, 1, 8, 31)]
        store = SimpleNamespace(load=Mock(return_value=records),
                                summary=RetryLogStore()._new_summary())
        with patch('retry_proxy.api.KEY_POOLS', {}):
            result = await create_handlers(None, store)[2](range='7d')
        store.load.assert_called_once_with(30, max_records=50000)
        self.assertEqual(result['record_count'], 2)
        self.assertEqual(result['rate_counts']['month'], 3)

    async def test_busy_analysis_rejects_another_read_and_keeps_health_available(self):
        import threading
        from fastapi import HTTPException
        from retry_proxy.api import create_handlers
        entered = threading.Event()
        release = threading.Event()
        def read(*args, **kwargs):
            entered.set()
            release.wait(2)
            return []
        store = SimpleNamespace(load=read, summary=RetryLogStore()._new_summary())
        handlers = create_handlers(None, store)
        first = asyncio.create_task(handlers[2]())
        await asyncio.to_thread(entered.wait, 1)
        try:
            with self.assertRaises(HTTPException) as raised:
                await handlers[2]()
            self.assertEqual(raised.exception.status_code, 503)
            self.assertEqual(await handlers[0](), {'status': 'ok'})
        finally:
            first.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
