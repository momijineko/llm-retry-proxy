import asyncio
import json
import os
import time
from datetime import datetime, timedelta

from .config import logger, settings
from .routes import is_excluded_path
from .stats import (_model_key, _normalize_provider, _req_cancelled,
                    _req_first_ok, _req_succeeded)

# 汇总落盘的最小间隔（秒）。每条日志仍即时追加到 JSONL，但累计汇总
# 按此间隔节流，避免高 QPS 下每请求全量序列化+fsync 成为瓶颈。
SUMMARY_FLUSH_INTERVAL = 5.0
MAX_LOG_FILE_BYTES = 32 * 1024 * 1024


class RetryLogStore:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.summary_cache = None
        self._summary_dirty = False
        self._last_flush_at = 0.0
        self._active_log_files = {}
        self.stats_index = None
        self._last_stat_shard = None

    def _new_summary(self):
        return {"version": 7, "total_requests": 0, "total_retries": 0, "total_succeeded": 0,
                "total_failed": 0, "total_cancelled": 0, "total_first_ok": 0,
                "total_prompt_tokens": 0, "total_completion_tokens": 0, "total_tokens": 0,
                "total_cached_tokens": 0,
                "by_provider": {}, "by_model": {},
                "by_key": {}, "by_status": {}, "first_ts": None, "last_ts": None,
                "log_offsets": {}}

    def _update(self, summary, r):
        summary["total_requests"] += 1
        summary["total_retries"] += r.get("retries", 0)
        prompt_tokens = r.get("prompt_tokens", 0) or 0
        completion_tokens = r.get("completion_tokens", 0) or 0
        total_tokens = r.get("total_tokens", 0) or 0
        cached_tokens = r.get("cached_tokens", 0) or 0
        summary["total_prompt_tokens"] += prompt_tokens
        summary["total_completion_tokens"] += completion_tokens
        summary["total_tokens"] += total_tokens
        summary["total_cached_tokens"] += cached_tokens
        if _req_cancelled(r):
            summary["total_cancelled"] += 1
        elif _req_succeeded(r):
            summary["total_succeeded"] += 1
            if _req_first_ok(r): summary["total_first_ok"] += 1
        else: summary["total_failed"] += 1
        if r.get("ts"):
            summary["first_ts"] = summary["first_ts"] or r["ts"]
            summary["last_ts"] = r["ts"]
        for field, key in (("by_provider", _normalize_provider(r.get("provider", "") or "(unknown)")),
                           ("by_model", _model_key(r)), ("by_key", r.get("key_id", ""))):
            if not key: continue
            b = summary[field].setdefault(key, {
                "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0,
                "failed": 0, "cancelled": 0, "max_retries": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "cached_tokens": 0,
            })
            b["requests"] += 1; b["retries"] += r.get("retries", 0)
            b["prompt_tokens"] += prompt_tokens
            b["completion_tokens"] += completion_tokens
            b["total_tokens"] += total_tokens
            b["cached_tokens"] += cached_tokens
            if _req_cancelled(r):
                b["cancelled"] += 1
            elif _req_succeeded(r):
                b["succeeded"] += 1
                if _req_first_ok(r): b["first_ok"] += 1
            else: b["failed"] += 1
            b["max_retries"] = max(b["max_retries"], r.get("retries", 0))
        statuses = [r.get("upstream_status", 0), *r.get("retry_codes", [])]
        if r.get("stream_error_status"):
            statuses.append(r["stream_error_status"])
        for code in statuses:
            summary["by_status"][str(code)] = summary["by_status"].get(str(code), 0) + 1

    def _save(self):
        os.makedirs(settings.log_dir, exist_ok=True)
        tmp = settings.summary_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f: json.dump(self.summary_cache, f, ensure_ascii=False)
            os.replace(tmp, settings.summary_file)
            if self.stats_index is not None:
                self.stats_index.snapshot(self.summary_cache)
            return True
        except Exception as e: logger.warning(f"写累计汇总失败: {e}")
        return False

    @staticmethod
    def _log_files():
        if not os.path.isdir(settings.log_dir):
            return []
        return sorted(
            name for name in os.listdir(settings.log_dir)
            if name.startswith("retry_") and name.endswith(".jsonl")
        )

    def _recover_summary_tail(self):
        """Replay JSONL records appended after the last durable summary save."""
        offsets = self.summary_cache.setdefault("log_offsets", {})
        changed = False
        for name in self._log_files():
            path = os.path.join(settings.log_dir, name)
            try:
                size = os.path.getsize(path)
                offset = int(offsets.get(name, 0))
                if offset < 0 or offset > size:
                    raise ValueError("日志文件已截断或偏移无效")
                with open(path, "rb") as f:
                    f.seek(offset)
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            break
                        offset = f.tell()
                        try:
                            record = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if not is_excluded_path(record.get("path", "")) and record.get("model"):
                            self._update(self.summary_cache, record)
                            changed = True
                if offsets.get(name) != offset:
                    offsets[name] = offset
                    changed = True
            except (OSError, ValueError) as exc:
                logger.warning(f"累计汇总无法增量恢复 {name}: {exc}，将从日志重建")
                self.summary_cache = self._rebuild()
                return True
        return changed

    def _legacy_log_offsets(self):
        """Locate the durable tail of a version-6 summary without offsets."""
        last_ts = self.summary_cache.get("last_ts")
        files = []
        found = None
        for name in self._log_files():
            path = os.path.join(settings.log_dir, name)
            safe_offset = 0
            try:
                with open(path, "rb") as f:
                    while True:
                        line = f.readline()
                        if not line or not line.endswith(b"\n"):
                            break
                        safe_offset = f.tell()
                        try:
                            record = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if last_ts and record.get("ts") == last_ts:
                            found = (name, safe_offset)
                files.append((name, safe_offset))
            except OSError:
                files.append((name, 0))
        if found is None:
            # Retention may already have removed the durable tail. Starting at
            # EOF preserves historical totals without duplicating retained logs.
            return dict(files)
        found_name, found_offset = found
        after_tail = False
        offsets = {}
        for name, eof in files:
            if name == found_name:
                offsets[name] = found_offset
                after_tail = True
            else:
                offsets[name] = 0 if after_tail else eof
        return offsets

    def _rebuild(self):
        summary = self._new_summary()
        if not os.path.isdir(settings.log_dir): return summary
        for name in self._log_files():
            try:
                with open(os.path.join(settings.log_dir, name), "rb") as f:
                    safe_offset = 0
                    while True:
                        line = f.readline()
                        if not line or not line.endswith(b"\n"):
                            break
                        safe_offset = f.tell()
                        try:
                            record = json.loads(line.decode("utf-8"))
                            if not is_excluded_path(record.get("path", "")) and record.get("model"): self._update(summary, record)
                        except (UnicodeDecodeError, json.JSONDecodeError): pass
                    summary["log_offsets"][name] = safe_offset
            except Exception: pass
        return summary

    def _migrate_legacy(self):
        path = settings.legacy_log_file
        if not os.path.exists(path) or os.path.isdir(path): return
        groups = {}; migrated = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line); date = record.get("ts", "")[:10] or "unknown"
                        groups.setdefault(date, []).append(line.rstrip("\n")); migrated += 1
                    except json.JSONDecodeError: pass
        except Exception as exc:
            logger.warning(f"读取旧日志文件失败，跳过迁移: {exc}"); return
        if not migrated:
            try: os.rename(path, path + ".bak")
            except Exception: pass
            return
        os.makedirs(settings.log_dir, exist_ok=True)
        for date, records in groups.items():
            target = os.path.join(settings.log_dir, f"retry_{date}.jsonl")
            if os.path.exists(target): continue
            try:
                with open(target, "w", encoding="utf-8") as f: f.write("\n".join(records) + "\n")
            except Exception as exc: logger.warning(f"迁移写入 {target} 失败: {exc}")
        try: os.rename(path, path + ".bak")
        except Exception: pass
        logger.info(f"已迁移旧日志 {migrated} 条到 {settings.log_dir}/，旧文件重命名为 {path}.bak")

    def _cleanup(self):
        if settings.log_retention_days <= 0 or not os.path.isdir(settings.log_dir): return
        cutoff = (datetime.now() - timedelta(days=settings.log_retention_days)).strftime("%Y-%m-%d")
        removed = 0
        for name in os.listdir(settings.log_dir):
            if name.startswith("retry_") and name.endswith(".jsonl") and len(name[6:16]) == 10 and name[6:16] < cutoff:
                try: os.remove(os.path.join(settings.log_dir, name)); removed += 1
                except Exception: pass
        if removed:
            logger.info(f"已清理 {removed} 个过期日志文件 (>{settings.log_retention_days}天)")

    def initialize(self):
        os.makedirs(settings.log_dir, exist_ok=True)
        self._migrate_legacy()
        try:
            with open(settings.summary_file, encoding="utf-8") as f: self.summary_cache = json.load(f)
        except Exception as e:
            logger.warning(f"读取累计汇总失败，重新初始化: {e}")
            self.summary_cache = self._rebuild()
        if self.summary_cache.get("version", 1) < 6:
            logger.info("累计汇总格式过旧，从日志重建...")
            self.summary_cache = self._rebuild()
            if self.summary_cache.get("total_requests", 0) > 0: self._save()
        if self.summary_cache.get("version", 1) < 7:
            self.summary_cache["log_offsets"] = self._legacy_log_offsets()
        self.summary_cache["version"] = 7
        for key in ("total_requests", "total_retries", "total_succeeded", "total_failed",
                    "total_cancelled", "total_first_ok",
                    "total_prompt_tokens", "total_completion_tokens", "total_tokens",
                    "total_cached_tokens"): self.summary_cache.setdefault(key, 0)
        for key in ("by_provider", "by_model", "by_key", "by_status"): self.summary_cache.setdefault(key, {})
        # Backfill token fields on legacy per-bucket entries that predate version 6.
        for field in ("by_provider", "by_model", "by_key"):
            for bucket in self.summary_cache.get(field, {}).values():
                if isinstance(bucket, dict):
                    bucket.setdefault("prompt_tokens", 0)
                    bucket.setdefault("completion_tokens", 0)
                    bucket.setdefault("total_tokens", 0)
                    bucket.setdefault("cached_tokens", 0)
        self.summary_cache.setdefault("first_ts", None); self.summary_cache.setdefault("last_ts", None)
        recovered = self._recover_summary_tail()
        self._cleanup()
        self._summary_dirty = False
        self._last_flush_at = time.monotonic()
        if self.summary_cache.get("total_requests", 0) or recovered: self._save()
        try:
            from .stats_index import StatsIndex
            self.stats_index = StatsIndex(settings.log_dir)
            recovery = self.stats_index.recover(self._log_files(),
                lambda record: bool(record.get("model")) and not is_excluded_path(record.get("path", "")))
            if recovery["skipped"]:
                logger.warning("统计索引回填跳过 %s 条无效或超大记录", recovery["skipped"])
            self.stats_index.snapshot(self.summary_cache)
            names = self._log_files()
            self._last_stat_shard = names[-1] if names else None
        except Exception as exc:
            self.stats_index = None
            logger.warning("统计索引初始化失败，使用受限明细读取: %s", exc)


    def _write_filename(self, date_str, payload_bytes):
        prefix = f"retry_{date_str}"
        filename = self._active_log_files.get(date_str)
        if filename is None:
            names = [name for name in self._log_files()
                     if name == prefix + ".jsonl"
                     or (name.startswith(prefix + "_")
                         and name[len(prefix) + 1:-6].isdigit())]
            filename = names[-1] if names else prefix + ".jsonl"
        path = os.path.join(settings.log_dir, filename)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if size and size + payload_bytes > MAX_LOG_FILE_BYTES:
            suffix = filename[len(prefix):-6]
            index = int(suffix[1:]) if suffix.startswith("_") else 0
            filename = f"{prefix}_{index + 1:06d}.jsonl"
            # Never append to or overwrite an existing later shard.
            while os.path.exists(os.path.join(settings.log_dir, filename)):
                index += 1
                filename = f"{prefix}_{index + 1:06d}.jsonl"
        self._active_log_files[date_str] = filename
        return filename

    async def write(self, record):
        if not record.get("model"): return
        date_str = record.get("ts", "")[:10] or datetime.now().strftime("%Y-%m-%d")
        async with self.lock:
            try:
                os.makedirs(settings.log_dir, exist_ok=True)
                payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
                filename = self._write_filename(date_str, len(payload))
                with open(os.path.join(settings.log_dir, filename), "ab") as f:
                    f.write(payload)
                    offset = f.tell()
            except Exception as e: logger.warning(f"写重试日志失败: {e}")
            rotated_stats = False
            if self.stats_index is not None and "offset" in locals():
                try:
                    rotated_stats = self._last_stat_shard != filename
                    if self._last_stat_shard != filename and self.summary_cache is not None:
                        self.stats_index.snapshot(self.summary_cache)
                    self.stats_index.append(filename, offset, record)
                    self._last_stat_shard = filename
                except Exception as exc:
                    self.stats_index = None
                    logger.warning("统计索引追加失败，使用受限明细读取: %s", exc)
            if self.summary_cache is not None:
                self._update(self.summary_cache, record)
                if "offset" in locals():
                    self.summary_cache.setdefault("log_offsets", {})[filename] = offset
                self._summary_dirty = True
                self._maybe_flush()
                if rotated_stats:
                    self.flush()

    def _maybe_flush(self):
        if not self._summary_dirty:
            return
        now = time.monotonic()
        if now - self._last_flush_at < SUMMARY_FLUSH_INTERVAL:
            return
        if self._save():
            self._summary_dirty = False
            self._last_flush_at = now

    def flush(self):
        """Force-write the in-memory summary if it has pending changes."""
        if self._summary_dirty and self.summary_cache is not None:
            if self._save():
                self._summary_dirty = False
                self._last_flush_at = time.monotonic()

    def load(self, days=1, max_records=None, include_body=True):
        from .log_reader import load_records
        return load_records(days, max_records, include_body, log_dir=settings.log_dir)

    def load_stats(self, days=1, max_records=50000):
        if self.stats_index is None:
            return self.load(days, max_records, include_body=False)
        cutoff = ((datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
                  if days > 0 else "")
        return self.stats_index.load(cutoff, max_records)

    @property
    def summary(self):
        if self.summary_cache is None: self.initialize()
        return self.summary_cache
