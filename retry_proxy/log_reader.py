"""Bounded reverse JSONL reads for diagnostic and analysis endpoints."""
import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timedelta

from .config import logger, settings
from .routes import is_excluded_path
from .stats import _normalize_provider

READ_CHUNK_BYTES = 64 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
_READ_LOCK = threading.Lock()


class LogRecords(list):
    def __init__(self):
        super().__init__()
        self.truncated = False
        self.skipped_records = 0
        self.scanned_bytes = 0


def _reverse_lines(handle, result):
    handle.seek(0, os.SEEK_END)
    position = handle.tell()
    pending = b''
    dropping = False
    tail = True
    while position and result.scanned_bytes < MAX_SCAN_BYTES:
        size = min(READ_CHUNK_BYTES, position,
                   MAX_SCAN_BYTES - result.scanned_bytes)
        position -= size
        handle.seek(position)
        chunk = handle.read(size)
        result.scanned_bytes += len(chunk)
        parts = chunk.split(b'\n')
        for part in reversed(parts[1:]):
            if not dropping:
                line = part + pending
                if len(line) > MAX_LINE_BYTES:
                    result.skipped_records += 1
                    result.truncated = True
                elif not tail and line:
                    yield line
            else:
                result.skipped_records += 1
                result.truncated = True
            pending = b''
            dropping = False
            tail = False
        if not dropping:
            pending = parts[0] + pending
            if len(pending) > MAX_LINE_BYTES:
                pending = b''
                dropping = True
    if position:
        result.truncated = True
    elif dropping:
        result.skipped_records += 1
        result.truncated = True
    elif pending and not tail:
        yield pending


def _record_bytes(record):
    total = 0
    pending = [record]
    while pending:
        value = pending.pop()
        total += sys.getsizeof(value)
        if total > MAX_RESULT_BYTES:
            return total
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return total


def load_records(days=1, max_records=None, include_body=True, log_dir=None):
    result = LogRecords()
    log_dir = settings.log_dir if log_dir is None else log_dir
    if not os.path.isdir(log_dir):
        return result
    limit = max(1, int(max_records or 50000))
    today = datetime.now()
    selected_dates = ({(today - timedelta(days=i)).strftime("%Y-%m-%d")
                       for i in range(days)} if days > 0 else None)
    names = sorted((name for name in os.listdir(log_dir)
                    if name.startswith("retry_") and name.endswith(".jsonl")
                    and (selected_dates is None or name[6:16] in selected_dates)),
                   reverse=True)
    retained = 0
    stop = False
    for name in names:
        if not name.startswith('retry_') or not name.endswith('.jsonl'):
            continue
        path = os.path.join(log_dir, name)
        try:
            with open(path, 'rb') as handle:
                for line in _reverse_lines(handle, result):
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        result.skipped_records += 1
                        continue
                    if not isinstance(record, dict):
                        continue
                    if is_excluded_path(record.get('path', '')) or not record.get('model'):
                        continue
                    if not include_body:
                        record.pop('request_body', None)
                    cost = _record_bytes(record)
                    if retained + cost > MAX_RESULT_BYTES:
                        result.truncated = True
                        stop = True
                        break
                    record['provider'] = _normalize_provider(record.get('provider', ''))
                    result.append(record)
                    retained += cost
                    if len(result) >= limit:
                        result.truncated = True
                        stop = True
                        break
        except OSError as exc:
            logger.warning(f'读取日志文件 {name} 失败: {exc}')
        if stop or result.scanned_bytes >= MAX_SCAN_BYTES:
            result.truncated = True
            break
    result.reverse()
    return result


async def load_async(store, days, max_records, include_body=True):
    # The thread owns this lock even when its awaiting request is cancelled.
    def read():
        with _READ_LOCK:
            from .log_store import RetryLogStore
            if isinstance(store, RetryLogStore):
                if not include_body:
                    return store.load_stats(days, max_records=max_records)
                return store.load(days, max_records=max_records, include_body=include_body)
            return store.load(days, max_records=max_records)
    return await asyncio.to_thread(read)
