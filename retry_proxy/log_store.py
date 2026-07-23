import asyncio
import json
import os
from datetime import datetime, timedelta

from .config import logger, settings
from .routes import is_excluded_path
from .stats import _model_key, _normalize_provider, _req_succeeded


class RetryLogStore:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.summary_cache = None

    def _new_summary(self):
        return {"version": 5, "total_requests": 0, "total_retries": 0, "total_succeeded": 0,
                "total_failed": 0, "total_first_ok": 0, "by_provider": {}, "by_model": {},
                "by_key": {}, "by_status": {}, "first_ts": None, "last_ts": None}

    def _update(self, summary, r):
        summary["total_requests"] += 1
        summary["total_retries"] += r.get("retries", 0)
        if _req_succeeded(r):
            summary["total_succeeded"] += 1
            if r.get("first_ok", r.get("retries", 0) == 0): summary["total_first_ok"] += 1
        else: summary["total_failed"] += 1
        if r.get("ts"):
            summary["first_ts"] = summary["first_ts"] or r["ts"]
            summary["last_ts"] = r["ts"]
        for field, key in (("by_provider", _normalize_provider(r.get("provider", "") or "(unknown)")),
                           ("by_model", _model_key(r)), ("by_key", r.get("key_id", ""))):
            if not key: continue
            b = summary[field].setdefault(key, {"requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0, "failed": 0, "max_retries": 0})
            b["requests"] += 1; b["retries"] += r.get("retries", 0)
            if _req_succeeded(r):
                b["succeeded"] += 1
                if r.get("first_ok", r.get("retries", 0) == 0): b["first_ok"] += 1
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
        except Exception as e: logger.warning(f"写累计汇总失败: {e}")

    def _rebuild(self):
        summary = self._new_summary()
        if not os.path.isdir(settings.log_dir): return summary
        for name in sorted(os.listdir(settings.log_dir)):
            if not name.startswith("retry_") or not name.endswith(".jsonl"): continue
            try:
                with open(os.path.join(settings.log_dir, name), encoding="utf-8") as f:
                    for line in f:
                        try:
                            record = json.loads(line)
                            if not is_excluded_path(record.get("path", "")) and record.get("model"): self._update(summary, record)
                        except json.JSONDecodeError: pass
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
        if self.summary_cache.get("version", 1) < 4:
            logger.info("累计汇总格式过旧，从日志重建...")
            self.summary_cache = self._rebuild()
            if self.summary_cache.get("total_requests", 0) > 0: self._save()
        self.summary_cache.setdefault("version", 5)
        for key in ("total_requests", "total_retries", "total_succeeded", "total_failed", "total_first_ok"): self.summary_cache.setdefault(key, 0)
        for key in ("by_provider", "by_model", "by_key", "by_status"): self.summary_cache.setdefault(key, {})
        self.summary_cache.setdefault("first_ts", None); self.summary_cache.setdefault("last_ts", None)
        self._cleanup()
        if self.summary_cache.get("total_requests", 0): self._save()

    async def write(self, record):
        if not record.get("model"): return
        date_str = record.get("ts", "")[:10] or datetime.now().strftime("%Y-%m-%d")
        async with self.lock:
            try:
                os.makedirs(settings.log_dir, exist_ok=True)
                with open(os.path.join(settings.log_dir, f"retry_{date_str}.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e: logger.warning(f"写重试日志失败: {e}")
            if self.summary_cache is not None:
                self._update(self.summary_cache, record); self._save()

    def load(self, days=1):
        records = []
        if not os.path.isdir(settings.log_dir): return records
        today = datetime.now()
        files = sorted(os.listdir(settings.log_dir)) if days <= 0 else [f"retry_{(today - timedelta(days=i)).strftime('%Y-%m-%d')}.jsonl" for i in range(days)]
        for fname in files:
            if not fname.startswith("retry_") or not fname.endswith(".jsonl"): continue
            fpath = os.path.join(settings.log_dir, fname)
            if not os.path.exists(fpath): continue
            try:
                with open(os.path.join(settings.log_dir, fname), encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            if not is_excluded_path(rec.get("path", "")) and rec.get("model"):
                                rec["provider"] = _normalize_provider(rec.get("provider", "")); records.append(rec)
                        except json.JSONDecodeError: pass
            except Exception as e: logger.warning(f"读取日志文件 {fname} 失败: {e}")
        return records

    @property
    def summary(self):
        if self.summary_cache is None: self.initialize()
        return self.summary_cache
