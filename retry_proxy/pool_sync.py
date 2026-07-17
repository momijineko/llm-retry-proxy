import asyncio
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

from .config import logger, settings
from .key_pool import KEY_POOLS, KeyEntry, KeyPool, replace_key_pool
from .sync_adapters import ADAPTERS, PoolSyncError


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _source_id(adapter, base_url):
    value = f"{adapter}:{base_url.rstrip('/')}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


class PoolSyncManager:
    """Schedules provider adapters and atomically applies their normalized key sets."""

    def __init__(self, pools=None, config=settings, client=None, adapters=None):
        self.pools = pools if pools is not None else KEY_POOLS
        self.config = config
        self.client = client
        self.adapters = adapters if adapters is not None else ADAPTERS
        self.sources = {}
        self.operations = {}
        self._lock = asyncio.Lock()
        self._task = None

    @property
    def state_file(self):
        return self.config.key_pool_sync_state_file

    @property
    def default_url(self):
        configured = self.config.key_pool_sync_default_url.rstrip("/")
        if configured in self.pools or len(self.pools) != 1:
            return configured
        return next(iter(self.pools))

    def _adapter(self, name):
        adapter = self.adapters.get(name)
        if adapter is None:
            raise PoolSyncError(f"未知号池同步适配器: {name}")
        return adapter

    def _pool_from_source(self, source):
        pool = KeyPool([], source.get("provider") or self.config.provider)
        for item in source.get("entries", []):
            pool.entries.append(KeyEntry(
                item["key"], item.get("label", ""), item.get("models", ()),
                item.get("paths", ()), item.get("sort", ""),
            ))
        pool.finalize_entries()
        return pool

    def _activate(self, source):
        replace_key_pool(source["base_url"], self._pool_from_source(source), self.pools)

    def _persistent_sources(self):
        sources = []
        for source in self.sources.values():
            item = dict(source)
            item["session"] = dict(item.get("session") or {})
            item["session"].pop("access_token", None)
            sources.append(item)
        return sources

    def load_state(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("interval") is not None:
                try:
                    interval = int(state["interval"])
                    if interval >= 0:
                        object.__setattr__(self.config, "key_pool_sync_interval", interval)
                except (TypeError, ValueError):
                    logger.warning("号池同步状态中的周期无效，继续使用环境配置")
            for source in state.get("sources") or []:
                adapter = source.get("adapter", "")
                if adapter not in self.adapters or not source.get("base_url"):
                    continue
                source["base_url"] = source["base_url"].rstrip("/")
                self.sources[source["id"]] = source
                # A successful sync with zero keys is authoritative too. Retain
                # the entries check for state files written by older versions.
                if source.get("entries") or source.get("last_sync_at"):
                    self._activate(source)
            if self.sources:
                logger.info(f"号池同步状态已恢复: {len(self.sources)} 个上游连接")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning(f"号池同步状态加载失败: {exc}")

    def _save_state(self):
        if not self.state_file:
            return
        directory = os.path.dirname(os.path.abspath(self.state_file))
        os.makedirs(directory, exist_ok=True)
        state = {"version": 2, "interval": self.config.key_pool_sync_interval,
                 "sources": self._persistent_sources()}
        fd, temp_path = tempfile.mkstemp(prefix=".pool_sync_", suffix=".json", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.state_file)
            os.chmod(self.state_file, 0o600)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _merge_local_rules(self, source, entries):
        rules = {}
        current = self.pools.get(source["base_url"])
        if current:
            rules.update({entry.key: (list(entry.models), list(entry.paths)) for entry in current.entries})
        for item in source.get("entries") or []:
            rules[item.get("key", "")] = (item.get("models", []), item.get("paths", []))
        for item in entries:
            item["models"], item["paths"] = rules.get(item.get("key", ""), ([], []))
        return entries

    async def connect(self, adapter_name, base_url, provider, credentials):
        adapter_name = (adapter_name or self.config.key_pool_sync_default_adapter).strip().lower()
        base_url = (base_url or self.default_url).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise PoolSyncError("上游地址必须以 http:// 或 https:// 开头")
        adapter = self._adapter(adapter_name)
        source_id = _source_id(adapter_name, base_url)
        async with self._lock:
            conflict = next((source for sid, source in self.sources.items()
                             if sid != source_id and source.get("base_url") == base_url), None)
            if conflict is not None and self._adapter(conflict["adapter"]).connected(
                    conflict.get("session") or {}):
                raise PoolSyncError("同一上游地址已由另一个连接接管")
            source = self.sources.get(source_id, {
                "id": source_id, "adapter": adapter_name, "base_url": base_url,
                "provider": (provider or self.config.provider).strip(), "session": {}, "entries": [],
                "last_sync_at": "", "last_attempt_at": "", "last_error": "",
            })
            source["provider"] = (provider or source.get("provider") or self.config.provider).strip()
            try:
                source["session"] = await adapter.connect(self.client, source, credentials or {})
            except PoolSyncError:
                raise
            except Exception as exc:
                raise PoolSyncError(f"连接上游失败: {exc}") from exc
            if conflict is not None:
                self.sources.pop(conflict["id"], None)
            self.sources[source_id] = source
            self._save_state()
            result = await self._sync_source_locked(source_id)
        await self.start()
        return result

    async def _sync_source_locked(self, source_id):
        source = self.sources.get(source_id)
        if source is None:
            raise PoolSyncError("号池同步连接不存在")
        adapter = self._adapter(source["adapter"])
        if not adapter.connected(source.get("session") or {}):
            raise PoolSyncError("该连接尚未登录")
        source["last_attempt_at"] = _now_iso()
        try:
            session, entries = await adapter.fetch(self.client, source, source.get("session") or {})
            source["session"] = session
            source["entries"] = self._merge_local_rules(source, entries)
            self._activate(source)
            source["last_sync_at"] = _now_iso()
            source["last_error"] = ""
            self._save_state()
            logger.info(
                f"号池同步完成: adapter={source['adapter']} upstream={source['base_url']} "
                f"keys={len(entries)}"
            )
            return self.status()
        except Exception as exc:
            source["last_error"] = str(exc)
            self._save_state()
            if isinstance(exc, PoolSyncError):
                raise
            raise PoolSyncError(f"同步上游失败: {exc}") from exc

    async def sync_now(self, source_id=None):
        async with self._lock:
            if source_id:
                return await self._sync_source_locked(source_id)
            connected = [
                sid for sid, source in self.sources.items()
                if self._adapter(source["adapter"]).connected(source.get("session") or {})
            ]
            if not connected:
                raise PoolSyncError("没有已连接的号池同步来源")
            errors = []
            for sid in connected:
                try:
                    await self._sync_source_locked(sid)
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise PoolSyncError("; ".join(errors))
            return self.status()

    async def catalog(self, source_id):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            adapter = self._adapter(source["adapter"])
            if not adapter.connected(source.get("session") or {}):
                raise PoolSyncError("该连接尚未登录")
            try:
                session, groups = await adapter.catalog(
                    self.client, source, source.get("session") or {},
                )
                source["session"] = session
                self._save_state()
                return {"source_id": source_id, "groups": groups}
            except PoolSyncError:
                self._save_state()
                raise
            except Exception as exc:
                self._save_state()
                raise PoolSyncError(f"读取分组失败: {exc}") from exc

    async def create_keys(self, source_id, group_ids=None, only_missing=False, options=None):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            adapter = self._adapter(source["adapter"])
            if not adapter.connected(source.get("session") or {}):
                raise PoolSyncError("该连接尚未登录")
            try:
                operation = {"kind": "create", "done": 0, "total": 0, "created": 0,
                             "failed": 0, "running": True}
                self.operations[source_id] = operation

                async def progress(done, total, created, failed):
                    operation.update(done=done, total=total, created=created, failed=failed)

                create_options = dict(options or {})
                create_options.setdefault("delay_seconds", getattr(self.config, "key_pool_create_delay", 1.5))
                create_options["_progress"] = progress
                session, result = await adapter.create_keys(
                    self.client, source, source.get("session") or {}, group_ids or [],
                    bool(only_missing), create_options,
                )
                operation.update(done=operation.get("total", 0),
                                created=len(result.get("created") or []),
                                failed=len(result.get("errors") or []), running=False)
                source["session"] = session
                self._save_state()
                state = await self._sync_source_locked(source_id)
                return {"creation": result, "state": state}
            except PoolSyncError:
                if source_id in self.operations:
                    self.operations[source_id]["running"] = False
                self._save_state()
                raise
            except Exception as exc:
                if source_id in self.operations:
                    self.operations[source_id]["running"] = False
                self._save_state()
                raise PoolSyncError(f"创建 Key 失败: {exc}") from exc

    async def clear_keys(self, source_id, group_ids=None, options=None):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            adapter = self._adapter(source["adapter"])
            if not adapter.connected(source.get("session") or {}):
                raise PoolSyncError("该连接尚未登录")
            try:
                session, result = await adapter.delete_keys(
                    self.client, source, source.get("session") or {}, group_ids or [], options or {},
                )
                source["session"] = session
                self._save_state()
                state = await self._sync_source_locked(source_id)
                return {"deletion": result, "state": state}
            except PoolSyncError:
                self._save_state()
                raise
            except Exception as exc:
                self._save_state()
                raise PoolSyncError(f"清空 Key 失败: {exc}") from exc

    async def set_interval(self, value):
        try:
            interval = int(value)
        except (TypeError, ValueError) as exc:
            raise PoolSyncError("同步周期必须是整数秒") from exc
        if interval < 0 or interval > 86400:
            raise PoolSyncError("同步周期必须在 0 到 86400 秒之间")
        async with self._lock:
            object.__setattr__(self.config, "key_pool_sync_interval", interval)
            self._save_state()
            task = self._task
            self._task = None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if interval > 0 and self._has_connected_sources():
                self._task = asyncio.create_task(self._run(), name="key-pool-sync")
            return self.status()

    async def disconnect(self, source_id):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            adapter = self._adapter(source["adapter"])
            try:
                await adapter.disconnect(self.client, source, source.get("session") or {})
            except Exception as exc:
                logger.warning(f"上游会话撤销失败，已清除本地连接: {exc}")
            source["session"] = {}
            source["last_error"] = ""
            self._save_state()
            result = self.status()
        if not self._has_connected_sources():
            await self.stop()
        return result

    def status(self, source_id=None):
        selected = [self.sources[source_id]] if source_id in self.sources else list(self.sources.values())
        public_sources = []
        now = datetime.now().timestamp()
        for source in selected:
            adapter = self._adapter(source["adapter"])
            pool = self.pools.get(source["base_url"])
            runtime = {entry.key: entry for entry in pool.entries} if pool else {}
            visible_entries = []
            for item in source.get("entries") or []:
                raw_key = item.get("key", "")
                entry = runtime.get(raw_key)
                visible_entries.append({
                    "source_key_id": item.get("source_key_id"),
                    "key_masked": raw_key[:7] + "..." + raw_key[-4:],
                    "label": item.get("label", ""), "sort": item.get("sort", ""),
                    "key_name": item.get("key_name", ""), "group_name": item.get("group_name", ""),
                    "platform": item.get("platform", ""), "models": item.get("models", []),
                    "paths": item.get("paths", []),
                    "cooled": bool(entry and entry.cooldown_until > now),
                })
            public_sources.append({
                "id": source["id"], "adapter": source["adapter"], "adapter_label": adapter.label,
                "base_url": source["base_url"], "provider": source.get("provider", ""),
                "connected": adapter.connected(source.get("session") or {}),
                "account": adapter.public_session(source.get("session") or {}),
                "last_sync_at": source.get("last_sync_at", ""),
                "last_attempt_at": source.get("last_attempt_at", ""),
                "last_error": source.get("last_error", ""),
                "key_count": len(visible_entries), "keys": visible_entries,
                "operation": dict(self.operations.get(source["id"]) or {}),
            })
        return {
            "interval": self.config.key_pool_sync_interval,
            "defaults": {"adapter": self.config.key_pool_sync_default_adapter,
                         "base_url": self.default_url,
                         "provider": self.config.provider},
            "adapters": [{"name": item.name, "label": item.label,
                          "credential_fields": item.credential_fields,
                          "capabilities": item.capabilities}
                         for item in self.adapters.values()],
            "sources": public_sources,
        }

    def _has_connected_sources(self):
        return any(self._adapter(source["adapter"]).connected(source.get("session") or {})
                   for source in self.sources.values())

    async def start(self):
        if self._task is None and self._has_connected_sources() and self.config.key_pool_sync_interval > 0:
            self._task = asyncio.create_task(self._run(), name="key-pool-sync")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self):
        while True:
            await asyncio.sleep(self.config.key_pool_sync_interval)
            try:
                await self.sync_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"号池自动同步失败，继续使用上次配置: {exc}")
