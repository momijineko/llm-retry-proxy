import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import logger, settings
from .key_pool import (KEY_POOLS, KEY_POOL_STRATEGIES, KeyEntry, KeyPool,
                       clone_key_pool, replace_key_pool)
from .routes import normalize_route_prefix
from .experience_data import (_EXPERIENCE_PATH_PATTERN,
                             _EXPERIENCE_TRANSFORM_DEFAULTS,
                             _experience_timestamp, _experience_value,
                             _parse_experience_payload)
from .secrets_crypto import (SENSITIVE_FIELDS, decrypt_session, derive_key,
                             encrypt_session)
from .sync_adapters import ADAPTERS, PoolSyncError


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _source_id(adapter, base_url):
    value = f"{adapter}:{base_url.rstrip('/')}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


def _mask_key(raw_key):
    """Mask a key for display without leaking short keys in full."""
    raw_key = str(raw_key or "")
    if len(raw_key) <= 11:
        return raw_key[:2] + "***" if len(raw_key) > 2 else "***"
    return raw_key[:7] + "..." + raw_key[-4:]


def _is_private_or_loopback_host(hostname):
    """Return whether an IP literal is not globally routable."""
    if not hostname:
        return False
    host = hostname.strip().rstrip(".").lower()
    if host in ("localhost",):
        return True
    # Strip IPv6 brackets for ipaddress parsing.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not addr.is_global


async def _resolve_public_url_destination(url):
    """Resolve a URL and return only globally routable destination addresses."""
    parsed = urlsplit(url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise PoolSyncError("外部数据 URL 端口无效") from exc
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise PoolSyncError(f"外部数据 URL 域名解析失败: {exc}") from exc
    resolved = tuple(dict.fromkeys(item[4][0] for item in addresses if item[4]))
    if not resolved:
        raise PoolSyncError("外部数据 URL 域名未解析到地址")
    if any(_is_private_or_loopback_host(address) for address in resolved):
        raise PoolSyncError("外部数据 URL 域名解析到私有、回环或非公网地址")
    return parsed, resolved


_CLOUD_METADATA_HOSTNAMES = (
    "metadata.google.internal", "metadata.tencentyun.com", "metadata.goog",
)


async def _validate_base_url_destination(url):
    """反 SSRF 校验号池连接的 base_url（connect / 手动添加共用）。

    默认拒绝回环、链路本地、云元数据与私有网段目标（IP 字面量与域名
    解析结果都检查），防止管理凭据被用于探测内网；自建局域网上游
    （sub2api/new-api）需设置 KEY_POOL_ALLOW_PRIVATE_BASE_URL=true。
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PoolSyncError("上游地址必须是有效的 http:// 或 https:// 地址")
    host = parsed.hostname.strip().lower().rstrip(".")
    if host == "localhost" or host in _CLOUD_METADATA_HOSTNAMES:
        raise PoolSyncError("上游地址不能指向回环或云元数据地址")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise PoolSyncError("上游地址不能指向私有、回环或链路本地地址")
        return
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise PoolSyncError("上游地址端口无效") from exc
    try:
        resolved = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise PoolSyncError(f"上游地址域名解析失败: {exc}") from exc
    addresses = {item[4][0] for item in resolved if item[4]}
    if not addresses:
        raise PoolSyncError("上游地址域名未解析到地址")
    if any(_is_private_or_loopback_host(address) for address in addresses):
        raise PoolSyncError("上游地址域名解析到私有、回环或非公网地址")


def _pinned_url(parsed, address):
    """Replace only the connection host while preserving path and query."""
    host = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))


def _original_host_header(parsed):
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return host


async def _get_pinned_public_url(url, *, params=None, headers=None, timeout=20):
    """Fetch a validated URL without allowing a second DNS lookup to change its target.

    The TCP connection uses a validated IP literal. HTTPS still authenticates the
    original hostname through SNI, and the HTTP Host header is preserved.
    """
    parsed, addresses = await _resolve_public_url_destination(url)
    request_headers = dict(headers or {})
    request_headers["Host"] = _original_host_header(parsed)
    last_error = None
    for address in addresses:
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                return await client.get(
                    _pinned_url(parsed, address), params=params, headers=request_headers,
                    timeout=timeout, extensions={"sni_hostname": parsed.hostname},
                )
        except httpx.RequestError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise PoolSyncError("外部数据 URL 域名未解析到地址")


def _experience_timestamp(value):
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _manual_source_key_id(key):
    return hashlib.sha256(f"manual-key:{key}".encode("utf-8")).hexdigest()[:12]


def _manual_text(value, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PoolSyncError(f"{field} 必须是字符串")
    return value.strip()


def _manual_patterns(value, field):
    if value in (None, ""):
        return []
    values = value.split(";") if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise PoolSyncError(f"{field} 必须是数组或分号分隔文本")
    normalized = []
    for item in values:
        if not isinstance(item, str):
            raise PoolSyncError(f"{field} 中的规则必须是字符串")
        item = item.strip()
        if item:
            normalized.append(item)
    return normalized


def _manual_auth(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PoolSyncError("auth 必须是对象")
    normalized = {}
    for field in ("header", "scheme"):
        if field not in value:
            continue
        if not isinstance(value[field], str):
            raise PoolSyncError(f"auth.{field} 必须是字符串")
        normalized[field] = value[field]
    return normalized


class PoolSyncManager:
    """Schedules provider adapters and atomically applies their normalized key sets."""

    def __init__(self, pools=None, config=settings, client=None, adapters=None, route_registry=None):
        self.pools = pools if pools is not None else KEY_POOLS
        self.config = config
        self.client = client
        self.adapters = adapters if adapters is not None else ADAPTERS
        self.route_registry = route_registry
        self.static_pools = {
            url.rstrip("/"): clone_key_pool(pool) for url, pool in self.pools.items()
        }
        self.sources = {}
        self.operations = {}
        self._lock = asyncio.Lock()
        self._task = None
        self._state_dirty = False
        self._last_state_save_at = 0.0

    # 状态文件节流落盘的最小间隔（秒）。mark_model_unsupported 等高频路径
    # 只在内存中更新并标记 dirty，按此间隔落盘，避免每次拒绝都 fsync。
    _STATE_SAVE_INTERVAL = 5.0

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
        pool.strategy = source.get("strategy", "cost")
        pool.target_ttft_s = float(source.get("target_ttft_s", 5.0))
        pool.external_retest_weight = float(source.get("external_retest_weight", 0.5))
        pool.external_ttft_prior_strength = float(
            source.get("external_ttft_prior_strength", 2.0)
        )
        pool.session_affinity = bool(source.get("session_affinity", False))
        disabled_key_ids = {
            str(value) for value in source.get("disabled_key_ids", [])
            if value not in (None, "")
        }
        for item in source.get("entries", []):
            if str(item.get("source_key_id")) in disabled_key_ids:
                continue
            pool.entries.append(KeyEntry(
                item["key"], item.get("label", ""), item.get("models", ()),
                item.get("paths", ()), item.get("sort", ""),
                item.get("group_id", ""), item.get("group_name", ""),
                self._routing_capabilities(source, item),
                item.get("auth"),
            ))
        external_items = {
            str(item.get("id")): item
            for item in source.get("experience_items", [])
            if isinstance(item, dict) and item.get("id") not in (None, "")
        }
        mappings = source.get("experience_mappings") or {}
        fetched_ts = _experience_timestamp(source.get("experience_last_sync_at"))
        for local_group_id, external_group_id in mappings.items():
            item = external_items.get(str(external_group_id))
            if not item or item.get("ttft") is None or not item.get("samples"):
                continue
            pool.prior_metrics[str(local_group_id)] = {
                "ttft": float(item["ttft"]),
                "samples": int(item["samples"]),
                "last_ts": fetched_ts,
                "observed_ts": float(item.get("last_ts") or 0.0),
                "external_group_id": str(external_group_id),
                "name": item.get("name", ""),
            }
        pool.finalize_entries()
        return pool

    @staticmethod
    def _pool_url(source):
        return (source.get("pool_url") or source["base_url"]).rstrip("/")

    def _resolve_pool_url(self, source):
        base_url = source["base_url"].rstrip("/")
        prefix = source.get("route_prefix", "")
        if self.route_registry is None or not prefix:
            return (source.get("pool_url") or base_url).rstrip("/")
        return self.route_registry.environment_upstream(
            prefix, base_url, source.get("provider", ""),
        )

    def _activate(self, source):
        replace_key_pool(self._pool_url(source), self._pool_from_source(source), self.pools)

    @staticmethod
    def _routing_capabilities(source, item):
        capabilities = dict(item.get("routing_capabilities") or {})
        group_id = str(item.get("group_id") or "")
        raw_rejections = source.get("group_model_rejections")
        rejections = raw_rejections if isinstance(raw_rejections, dict) else {}
        rejected = rejections.get(group_id, [])
        if rejected:
            capabilities["rejected_models"] = list(rejected)
        return capabilities

    def _restore_static(self, base_url):
        base_url = base_url.rstrip("/")
        static = self.static_pools.get(base_url)
        if static is None:
            self.pools.pop(base_url, None)
            return
        replace_key_pool(base_url, clone_key_pool(static), self.pools)

    def _persistent_sources(self):
        key = derive_key(getattr(self.config, "key_pool_sync_secret", "") or "")
        sources = []
        for source in self.sources.values():
            item = dict(source)
            adapter = self._adapter(item["adapter"])
            session = adapter.persistent_session(item.get("session") or {})
            item["session"] = encrypt_session(session, SENSITIVE_FIELDS, key) if key else session
            entries = item.get("entries") or []
            item["entries"] = (
                encrypt_session({"entries": entries}, ("entries",), key)
                if key else entries
            )
            sources.append(item)
        return sources

    def _decrypt_session(self, session):
        """解密状态文件中的 session；失败则清空凭据，要求重新登录。"""
        if not isinstance(session, dict) or not session.get("__encrypted__"):
            return session
        key = derive_key(getattr(self.config, "key_pool_sync_secret", "") or "")
        try:
            return decrypt_session(session, key)
        except ValueError as exc:
            logger.warning(f"号池同步凭据解密失败，已清空该连接会话: {exc}")
            return {}

    def _decrypt_entries(self, entries):
        if not isinstance(entries, dict) or not entries.get("__encrypted__"):
            return entries if isinstance(entries, list) else []
        key = derive_key(getattr(self.config, "key_pool_sync_secret", "") or "")
        try:
            restored = decrypt_session(entries, key).get("entries")
            return restored if isinstance(restored, list) else []
        except ValueError as exc:
            logger.warning(f"号池同步 Key 解密失败，已清空该连接快照: {exc}")
            return None

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
            if self.route_registry is not None:
                self.route_registry.clear_managed()
            for source in state.get("sources") or []:
                adapter = source.get("adapter", "")
                if adapter not in self.adapters or not source.get("base_url"):
                    continue
                try:
                    source["base_url"] = source["base_url"].rstrip("/")
                    source.setdefault("route_prefix", "")
                    source.setdefault("group_rules", {})
                    source.setdefault("group_model_cache", {})
                    if not isinstance(source.get("group_model_rejections"), dict):
                        source["group_model_rejections"] = {}
                    source.setdefault("strategy", "cost")
                    source.setdefault("target_ttft_s", 5.0)
                    source.setdefault("external_retest_weight", 0.5)
                    source.setdefault("external_ttft_prior_strength", 2.0)
                    source.setdefault("session_affinity", False)
                    source.setdefault("check_model", "")
                    source.setdefault("experience_source", {})
                    source.setdefault("experience_items", [])
                    source.setdefault("experience_mappings", {})
                    source.setdefault("experience_last_sync_at", "")
                    source.setdefault("experience_last_error", "")
                    source["disabled_key_ids"] = [
                        str(value) for value in source.get("disabled_key_ids", [])
                        if value not in (None, "")
                    ]
                    try:
                        source["pool_url"] = self._resolve_pool_url(source)
                    except ValueError as exc:
                        source["pool_url"] = source["base_url"]
                        logger.warning(f"号池运行地址未绑定到环境路由: {exc}")
                    source["session"] = self._decrypt_session(source.get("session") or {})
                    restored_entries = self._decrypt_entries(source.get("entries") or [])
                    if restored_entries is None:
                        source["entries"] = []
                        source["last_sync_at"] = ""
                    else:
                        source["entries"] = restored_entries
                    self.sources[source["id"]] = source
                    if self.route_registry is not None and source.get("route_prefix"):
                        try:
                            self.route_registry.register(
                                source["id"], source["route_prefix"], source["base_url"],
                                source.get("provider", ""),
                            )
                        except ValueError as exc:
                            logger.warning(f"号池代理路由未恢复: {exc}")
                    # A successful sync with zero keys is authoritative too. Retain
                    # the entries check for state files written by older versions.
                    if source.get("entries") or source.get("last_sync_at"):
                        self._activate(source)
                except (ValueError, KeyError, TypeError) as exc:
                    # Skip a single malformed source instead of aborting all restores.
                    source_id = source.get("id", source.get("base_url", "?"))
                    self.sources.pop(source.get("id"), None)
                    if self.route_registry is not None and source.get("route_prefix"):
                        self.route_registry.unregister(source_id)
                    logger.warning(f"号池同步状态条目恢复失败，已跳过 {source_id}: {exc}")
            if self.sources:
                logger.info(f"号池同步状态已恢复: {len(self.sources)} 个上游连接")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning(f"号池同步状态加载失败: {exc}")

    def _save_state(self):
        if not self.state_file:
            return
        directory = os.path.dirname(os.path.abspath(self.state_file))
        os.makedirs(directory, exist_ok=True)
        state = {"version": 5, "interval": self.config.key_pool_sync_interval,
                 "sources": self._persistent_sources()}
        fd, temp_path = tempfile.mkstemp(prefix=".pool_sync_", suffix=".json", dir=directory)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None
                json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.state_file)
            os.chmod(self.state_file, 0o600)
        finally:
            if fd is not None:
                os.close(fd)
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        self._state_dirty = False
        self._last_state_save_at = time.monotonic()

    def _save_state_throttled(self):
        """Mark state dirty and persist only if the throttle window has elapsed."""
        self._state_dirty = True
        if time.monotonic() - self._last_state_save_at >= self._STATE_SAVE_INTERVAL:
            self._save_state()

    def _flush_state(self):
        """Force-persist pending state changes; called on shutdown."""
        if self._state_dirty:
            self._save_state()

    def _merge_local_rules(self, source, entries):
        rules = {}
        current = self.pools.get(self._pool_url(source))
        if current:
            rules.update({entry.key: (list(entry.models), list(entry.paths)) for entry in current.entries})
        for item in source.get("entries") or []:
            rules[item.get("key", "")] = (item.get("models", []), item.get("paths", []))
        group_rules = source.get("group_rules") or {}
        for item in entries:
            group_rule = group_rules.get(str(item.get("group_id")))
            if group_rule is not None:
                item["models"] = list(group_rule.get("models") or [])
                item["paths"] = list(group_rule.get("paths") or [])
            else:
                item["models"], item["paths"] = rules.get(item.get("key", ""), ([], []))
        return entries

    @staticmethod
    def _normalize_group_rules(raw):
        if not isinstance(raw, dict):
            raise PoolSyncError("分组映射规则必须是对象")
        normalized = {}
        for group_id, rule in raw.items():
            if not isinstance(rule, dict):
                raise PoolSyncError("分组映射规则格式无效")
            def patterns(value):
                values = value.split(";") if isinstance(value, str) else value
                if not isinstance(values, (list, tuple)):
                    raise PoolSyncError("models/paths 必须是数组或分号分隔文本")
                return [str(item).strip().lstrip("/") for item in values if str(item).strip()]
            normalized[str(group_id)] = {
                "models": patterns(rule.get("models", [])),
                "paths": patterns(rule.get("paths", [])),
            }
        return normalized

    async def connect(self, adapter_name, base_url, provider, credentials, route_prefix=None):
        adapter_name = (adapter_name or self.config.key_pool_sync_default_adapter).strip().lower()
        if adapter_name == "manual":
            raise PoolSyncError("手动号池请使用手动添加 Key 功能")
        base_url = (base_url or self.default_url).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise PoolSyncError("上游地址必须以 http:// 或 https:// 开头")
        if not getattr(self.config, "key_pool_allow_private_base_url", False):
            await _validate_base_url_destination(base_url)
        adapter = self._adapter(adapter_name)
        source_id = _source_id(adapter_name, base_url)
        existing = self.sources.get(source_id)
        requested_provider = (
            provider or (existing or {}).get("provider") or self.config.provider
        ).strip()
        normalized_route_prefix = None
        if route_prefix is not None:
            try:
                normalized_route_prefix = normalize_route_prefix(route_prefix)
                if not normalized_route_prefix:
                    raise ValueError("代理前缀不能为空或使用根路径")
                if self.route_registry is not None:
                    self.route_registry.validate(
                        source_id, normalized_route_prefix, base_url, requested_provider,
                    )
            except ValueError as exc:
                raise PoolSyncError(str(exc)) from exc
        async with self._lock:
            conflict = next((source for sid, source in self.sources.items()
                             if sid != source_id and source.get("base_url") == base_url), None)
            if conflict is not None and self._adapter(conflict["adapter"]).connected(
                    conflict.get("session") or {}):
                raise PoolSyncError("同一上游地址已由另一个连接接管")
            source = self.sources.get(source_id, {
                "id": source_id, "adapter": adapter_name, "base_url": base_url,
                "provider": requested_provider, "session": {}, "entries": [],
                "route_prefix": "", "strategy": "cost", "target_ttft_s": 5.0,
                "external_retest_weight": 0.5,
                "external_ttft_prior_strength": 2.0,
                "session_affinity": False,
                "check_model": "", "disabled_key_ids": [], "group_model_cache": {},
                "group_model_rejections": {},
                "experience_source": {}, "experience_items": [],
                "experience_mappings": {}, "experience_last_sync_at": "",
                "experience_last_error": "",
                "last_sync_at": "", "last_attempt_at": "", "last_error": "",
            })
            source["provider"] = requested_provider
            if normalized_route_prefix is not None:
                source["route_prefix"] = normalized_route_prefix
            try:
                source["pool_url"] = self._resolve_pool_url(source)
            except ValueError as exc:
                raise PoolSyncError(str(exc)) from exc
            try:
                source["session"] = await adapter.connect(self.client, source, credentials or {})
            except PoolSyncError:
                raise
            except Exception as exc:
                raise PoolSyncError(f"连接上游失败: {exc}") from exc
            if conflict is not None:
                self.sources.pop(conflict["id"], None)
                if self.route_registry is not None:
                    self.route_registry.unregister(conflict["id"])
            self.sources[source_id] = source
            if self.route_registry is not None:
                self.route_registry.register(
                    source_id, source.get("route_prefix", ""), base_url, source["provider"],
                )
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
            if (source.get("experience_source") or {}).get("url"):
                await self._refresh_experience_locked(source, raise_errors=False)
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
                if source.get("adapter") != "manual"
                and self._adapter(source["adapter"]).connected(source.get("session") or {})
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
                for group in groups or []:
                    rule = (source.get("group_rules") or {}).get(str(group.get("id")))
                    group["models"] = list((rule or {}).get("models") or [])
                    group["paths"] = list((rule or {}).get("paths") or [])
                    group["routing_capabilities"] = self._routing_capabilities(source, {
                        **group, "group_id": group.get("id"),
                    })
                self._save_state()
                return {"source_id": source_id, "groups": groups}
            except PoolSyncError:
                self._save_state()
                raise
            except Exception as exc:
                self._save_state()
                raise PoolSyncError(f"读取分组失败: {exc}") from exc

    async def set_group_rules(self, source_id, rules):
        normalized = self._normalize_group_rules(rules)
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            source["group_rules"] = normalized
            source["entries"] = self._merge_local_rules(source, [dict(item) for item in source.get("entries") or []])
            self._activate(source)
            self._save_state()
            return self.status()

    @staticmethod
    def _normalize_experience_source(url, samples=100, sample_param="samples",
                                     transform=None, query_params=None):
        url = str(url or "").strip()
        if not url:
            return {}
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise PoolSyncError("外部数据 URL 必须是有效的 http:// 或 https:// 地址")
        if parsed.username or parsed.password or parsed.fragment:
            raise PoolSyncError("外部数据 URL 不能包含账号、密码或片段")
        if _is_private_or_loopback_host(parsed.hostname):
            raise PoolSyncError("外部数据 URL 不能指向私有、回环或链路本地地址")
        if len(url) > 2048:
            raise PoolSyncError("外部数据 URL 过长")
        normalized_params = None
        if query_params is not None:
            if not isinstance(query_params, dict):
                raise PoolSyncError("外部数据查询参数必须是对象")
            if len(query_params) > 32:
                raise PoolSyncError("外部数据查询参数不能超过 32 个")
            normalized_params = {}
            for raw_name, raw_value in query_params.items():
                name = str(raw_name or "").strip()
                if not name or len(name) > 128 or "\n" in name or "\r" in name:
                    raise PoolSyncError("外部数据查询参数名无效")
                if isinstance(raw_value, (dict, list)) or raw_value is None:
                    raise PoolSyncError(f"外部数据查询参数值无效: {name}")
                value = str(raw_value)
                if len(value) > 2048 or "\n" in value or "\r" in value:
                    raise PoolSyncError(f"外部数据查询参数值无效: {name}")
                normalized_params[name] = value
        else:
            try:
                samples = int(samples)
            except (TypeError, ValueError) as exc:
                raise PoolSyncError("外部数据样本数必须是整数") from exc
            if samples < 1 or samples > 1000:
                raise PoolSyncError("外部数据样本数必须在 1 到 1000 之间")
            sample_param = str(sample_param or "").strip()
            if sample_param and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sample_param):
                raise PoolSyncError("外部数据样本参数名只能包含字母、数字、下划线和连字符")
        normalized_transform = dict(_EXPERIENCE_TRANSFORM_DEFAULTS)
        if transform is not None:
            if not isinstance(transform, dict):
                raise PoolSyncError("外部数据转换规则必须是对象")
            for name in normalized_transform:
                if name in transform:
                    normalized_transform[name] = transform[name]
        for name in normalized_transform:
            if name == "ttft_unit":
                continue
            value = str(normalized_transform.get(name) or "").strip()
            if name in ("items_path", "id_path", "ttft_path") and not value:
                raise PoolSyncError(f"外部数据转换字段不能为空: {name}")
            if value == "$" and name != "items_path":
                raise PoolSyncError(f"外部数据字段路径无效: {value}")
            if value != "$" and value and not _EXPERIENCE_PATH_PATTERN.fullmatch(value):
                raise PoolSyncError(f"外部数据字段路径无效: {value}")
            normalized_transform[name] = value
        unit = str(normalized_transform.get("ttft_unit") or "ms").strip().lower()
        if unit not in ("ms", "s"):
            raise PoolSyncError("TTFT 单位必须是 ms 或 s")
        normalized_transform["ttft_unit"] = unit
        config = {"url": url, "transform": normalized_transform}
        if normalized_params is not None:
            config["query_params"] = normalized_params
        else:
            config.update({"samples": samples, "sample_param": sample_param})
        return config

    async def _fetch_experience_items(self, config):
        if "query_params" in config:
            params = config.get("query_params") or None
        else:
            params = ({config["sample_param"]: config["samples"]}
                      if config.get("sample_param") else None)
        if isinstance(self.client, httpx.AsyncClient):
            response = await _get_pinned_public_url(
                config["url"], params=params,
                headers={"Accept": "application/json"}, timeout=20,
            )
        else:
            # Test doubles do not open sockets; retain their simple get() contract.
            await _resolve_public_url_destination(config["url"])
            response = await self.client.get(
                config["url"], params=params,
                headers={"Accept": "application/json"}, timeout=20,
            )
        if response.status_code >= 400:
            raise PoolSyncError(f"外部数据接口请求失败 (HTTP {response.status_code})")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise PoolSyncError("外部数据接口返回了非 JSON 响应") from exc
        return _parse_experience_payload(payload, config.get("transform"))

    async def _refresh_experience_locked(self, source, raise_errors=True):
        config = source.get("experience_source") or {}
        if not config.get("url"):
            return []
        try:
            items = await self._fetch_experience_items(config)
            source["experience_items"] = items
            source["experience_last_sync_at"] = _now_iso()
            source["experience_last_error"] = ""
            return items
        except Exception as exc:
            source["experience_last_error"] = str(exc)
            if raise_errors:
                if isinstance(exc, PoolSyncError):
                    raise
                raise PoolSyncError(f"外部数据读取失败: {exc}") from exc
            logger.warning(
                f"外部数据刷新失败，继续使用原有调度: upstream={source['base_url']} "
                f"error={exc}"
            )
            return source.get("experience_items") or []

    async def set_experience_source(self, source_id, url, samples=100,
                                    sample_param="samples", transform=None,
                                    query_params=None):
        config = self._normalize_experience_source(
            url, samples, sample_param, transform, query_params,
        )
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            if not config:
                source["experience_source"] = {}
                source["experience_items"] = []
                source["experience_mappings"] = {}
                source["experience_last_sync_at"] = ""
                source["experience_last_error"] = ""
                self._activate(source)
                self._save_state()
                return self.status()
            items = await self._fetch_experience_items(config)
            previous_config = source.get("experience_source") or {}
            previous_transform = previous_config.get("transform") or {}
            identity_changed = any((
                previous_config.get("url") != config["url"],
                previous_transform.get("items_path") != config["transform"]["items_path"],
                previous_transform.get("id_path") != config["transform"]["id_path"],
            ))
            source["experience_source"] = config
            source["experience_items"] = items
            source["experience_last_sync_at"] = _now_iso()
            source["experience_last_error"] = ""
            if previous_config.get("url") and identity_changed:
                source["experience_mappings"] = {}
            self._activate(source)
            self._save_state()
            return self.status()

    async def set_experience_mapping(self, source_id, mappings):
        if not isinstance(mappings, dict):
            raise PoolSyncError("外部数据分组映射必须是对象")
        normalized = {
            str(local_id): str(external_id)
            for local_id, external_id in mappings.items()
            if local_id not in (None, "") and external_id not in (None, "")
        }
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            local_ids = {
                str(item.get("group_id")) for item in source.get("entries") or []
                if item.get("group_id") not in (None, "")
            }
            external_ids = {
                str(item.get("id")) for item in source.get("experience_items") or []
                if isinstance(item, dict) and item.get("id") not in (None, "")
            }
            unknown_local = set(normalized) - local_ids
            unknown_external = set(normalized.values()) - external_ids
            if unknown_local:
                raise PoolSyncError(f"本地分组不存在: {sorted(unknown_local)[0]}")
            if unknown_external:
                raise PoolSyncError(f"外部分组不存在: {sorted(unknown_external)[0]}")
            source["experience_mappings"] = normalized
            self._activate(source)
            self._save_state()
            return self.status()

    async def mark_model_unsupported(self, upstream, group_id, model):
        upstream = (upstream or "").rstrip("/")
        group_id = str(group_id or "")
        model = str(model or "").strip().lower()
        if not upstream or not group_id or not model:
            return False
        async with self._lock:
            source = next((item for item in self.sources.values()
                           if self._pool_url(item) == upstream), None)
            if source is None:
                return False
            rejections = source.get("group_model_rejections")
            if not isinstance(rejections, dict):
                rejections = source["group_model_rejections"] = {}
            models = rejections.setdefault(group_id, [])
            if model in models:
                return False
            models.append(model)
            models.sort()
            self._activate(source)
            self._save_state_throttled()
            logger.warning(
                f"上游模型能力已由真实请求修正: upstream={source['base_url']} "
                f"group={group_id} model={model}"
            )
            return True

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

    async def set_source_settings(self, source_id, strategy, target_ttft_s=5.0,
                                  check_model="", session_affinity=None,
                                  external_retest_weight=None,
                                  external_ttft_prior_strength=None):
        if strategy not in KEY_POOL_STRATEGIES:
            raise PoolSyncError("号池策略必须是 cost、ttft 或 balanced")
        try:
            target = float(target_ttft_s)
        except (TypeError, ValueError) as exc:
            raise PoolSyncError("可接受首 Token 上限必须是数字") from exc
        if not math.isfinite(target) or target < 0.1 or target > 300:
            raise PoolSyncError("可接受首 Token 上限必须在 0.1 到 300 秒之间")
        if external_retest_weight is not None:
            try:
                external_weight = float(external_retest_weight)
            except (TypeError, ValueError) as exc:
                raise PoolSyncError("外部复测权重必须是数字") from exc
            if not math.isfinite(external_weight) or external_weight < 0 or external_weight > 1:
                raise PoolSyncError("外部复测权重必须在 0 到 1 之间")
        if external_ttft_prior_strength is not None:
            try:
                prior_strength = float(external_ttft_prior_strength)
            except (TypeError, ValueError) as exc:
                raise PoolSyncError("外部参考强度必须是数字") from exc
            if not math.isfinite(prior_strength) or prior_strength < 0 or prior_strength > 100:
                raise PoolSyncError("外部参考强度必须在 0 到 100 之间")
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            if external_retest_weight is None:
                external_weight = float(source.get("external_retest_weight", 0.5))
            if external_ttft_prior_strength is None:
                prior_strength = float(source.get("external_ttft_prior_strength", 2.0))
            source["strategy"] = strategy
            source["target_ttft_s"] = target
            source["external_retest_weight"] = external_weight
            source["external_ttft_prior_strength"] = prior_strength
            source["check_model"] = str(check_model or "").strip()
            if session_affinity is not None:
                source["session_affinity"] = bool(session_affinity)
            pool = self.pools.get(self._pool_url(source))
            if pool is not None:
                affinity = bool(source.get("session_affinity", False))
                pool.apply_settings(
                    strategy, target, external_weight, prior_strength, affinity,
                )
                for view in pool.views():
                    view.apply_settings(
                        strategy, target, external_weight, prior_strength, affinity,
                    )
            self._save_state()
            return self.status()

    @staticmethod
    def _probe_status(status):
        return 200 <= status < 400

    @staticmethod
    def _probe_failure(status):
        if status == 0:
            return "transport_error", True
        if status in (401, 403):
            return "auth_error", True
        if status == 429:
            return "rate_limited", True
        if status >= 500:
            return "upstream_error", True
        return "request_rejected", False

    async def check_availability(self, source_id, model=None):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            pool = self.pools.get(self._pool_url(source))
            if pool is None or not pool.entries:
                raise PoolSyncError("号池没有可检测的 Key")
            check_model = str(model or source.get("check_model") or "").strip()
            if not check_model:
                raise PoolSyncError("请先填写检测模型")
            source["check_model"] = check_model
            self._save_state()
            adapter = self._adapter(source["adapter"])
            request_spec = adapter.availability_request(source, check_model)
            if not isinstance(request_spec, dict) or not request_spec.get("url"):
                raise PoolSyncError("同步适配器未提供有效的可用性检测请求")
            probe_url = request_spec["url"]
            probe_payload = request_spec.get("json") or {}
            probe_headers = request_spec.get("headers") or {}
            # Snapshot entries so probes don't race with replace_key_pool swaps.
            groups = {}
            for entry in list(pool.entries):
                groups.setdefault(entry.group_id or entry.key, []).append(entry)
            pool_signature = tuple(
                (id(entry), entry.group_id, entry.group_name,
                 entry.auth_header, entry.auth_scheme)
                for entry in pool.entries
            )

        # Network probes deliberately run outside the manager lock. Runtime
        # entries are stable snapshots; hot-replaced entries are marked retired.
        semaphore = asyncio.Semaphore(2)

        async def probe(entry):
            headers = dict(probe_headers)
            headers[entry.auth_header] = (
                f"{entry.auth_scheme} {entry.key}"
                if entry.auth_scheme else entry.key
            )
            try:
                async with semaphore:
                    started = time.monotonic()
                    response = await self.client.post(
                        probe_url, json=probe_payload, headers=headers, timeout=30,
                    )
                    elapsed = time.monotonic() - started
                available = self._probe_status(response.status_code)
                reason, circuit_failure = (
                    ("available", False) if available
                    else self._probe_failure(response.status_code)
                )
                return entry, response.status_code, available, elapsed, reason, circuit_failure
            except httpx.RequestError:
                return entry, 0, False, None, "transport_error", True

        async def probe_group(entries):
            attempts = []
            for entry in entries:
                result = await probe(entry)
                attempts.append(result)
                if result[2] or not result[5]:
                    break
            return attempts

        group_results = await asyncio.gather(
            *(probe_group(entries) for entries in groups.values())
        )
        by_group = dict(zip(groups, group_results))

        async with self._lock:
            current_source = self.sources.get(source_id)
            if current_source is not source:
                raise PoolSyncError("号池同步连接已在检测期间变更")
            current_pool = self.pools.get(self._pool_url(source))
            if current_pool is not pool:
                raise PoolSyncError("号池已在检测期间被替换")
            current_signature = tuple(
                (id(entry), entry.group_id, entry.group_name,
                 entry.auth_header, entry.auth_scheme)
                for entry in pool.entries
            )
            if current_signature != pool_signature:
                raise PoolSyncError("号池已在检测期间更新，请重试")
            summary = []
            for group_id, attempts in by_group.items():
                available = any(item[2] is True for item in attempts)
                explicitly_failed = bool(attempts) and all(item[2] is False for item in attempts)
                circuit_opened = (
                    explicitly_failed and all(item[5] is True for item in attempts)
                )
                for entry, _, item_available, elapsed, _, _ in attempts:
                    if item_available:
                        pool.record_probe(entry, elapsed)
                if circuit_opened:
                    status = next(item[1] for item in attempts if item[2] is False)
                    for entry in groups[group_id]:
                        pool.mark_cooldown(
                            entry, self.config.key_cooldown_5xx,
                            failure_kind="probe", status=status,
                        )
                summary.append({
                    "group_id": group_id,
                    "group_name": groups[group_id][0].group_name or groups[group_id][0].label,
                    "available": True if available else False if explicitly_failed else None,
                    "circuit_opened": circuit_opened,
                    "reason": "available" if available else attempts[-1][4],
                    "statuses": [item[1] for item in attempts],
                    "response_s": min((item[3] for item in attempts if item[2] and item[3] is not None),
                                      default=None),
                })
            unavailable = sum(item["available"] is False for item in summary)
            rejected = sum(
                item["available"] is False and not item["circuit_opened"] for item in summary
            )
            circuit_opened = sum(item["circuit_opened"] for item in summary)
            status_counts = {}
            for item in summary:
                for status in item["statuses"]:
                    status_counts[status] = status_counts.get(status, 0) + 1
            status_summary = ",".join(
                f"{status}:{count}" for status, count in sorted(status_counts.items())
            )
            logger.info(
                f"号池可用性检测完成: upstream={source['base_url']} model={check_model} "
                f"groups={len(summary)} statuses={status_summary or '-'} "
                f"unavailable={unavailable} request_rejected={rejected} "
                f"circuit_opened={circuit_opened}"
            )
            return {"model": check_model, "checks": summary, "state": self.status()}

    async def reset_group(self, source_id, group_id):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            pool = self.pools.get(self._pool_url(source))
            group_key = str(group_id)
            entries = [entry for entry in pool.entries
                       if (entry.group_id or entry.key) == group_key] if pool else []
            if not entries:
                # 未填分组的手动 Key 前端回退传 source_key_id，需据其解析真实 group_key。
                item = next((entry for entry in source.get("entries") or []
                             if str(entry.get("source_key_id")) == group_key), None)
                if item is not None and pool is not None:
                    group_key = str(item.get("group_id") or item.get("key") or "")
                    entries = [entry for entry in pool.entries
                               if (entry.group_id or entry.key) == group_key]
            if not entries:
                raise PoolSyncError("分组不存在或尚未加载")
            pool.reset_circuit(group_id=group_key)
            logger.info(
                f"号池分组已手动解除熔断: upstream={source['base_url']} "
                f"group={_mask_key(group_key)}"
            )
            return self.status()

    async def reset_groups(self, source_id):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            pool = self.pools.get(self._pool_url(source))
            if pool is None:
                raise PoolSyncError("号池尚未加载")
            pool.reset_circuit()
            logger.info(f"号池全部分组已手动解除熔断: upstream={source['base_url']}")
            return self.status()

    async def reset_key(self, source_id, source_key_id):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            item = next((entry for entry in source.get("entries") or []
                         if str(entry.get("source_key_id")) == str(source_key_id)), None)
            if item is None:
                raise PoolSyncError("Key 不存在或已被上游删除")
            pool = self.pools.get(self._pool_url(source))
            runtime = next((entry for entry in pool.entries if entry.key == item.get("key")), None) if pool else None
            if runtime is None:
                raise PoolSyncError("Key 尚未加载到运行时号池")
            pool.reset_circuit(entry_key=runtime.key)
            logger.info(
                f"号池 Key 已手动解除熔断: upstream={source['base_url']} "
                f"key={runtime.key_id}"
            )
            return self.status()

    async def set_key_enabled(self, source_id, source_key_id, enabled):
        if not isinstance(enabled, bool):
            raise PoolSyncError("enabled 必须是布尔值")
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            item = next((entry for entry in source.get("entries") or []
                         if str(entry.get("source_key_id")) == str(source_key_id)), None)
            if item is None:
                raise PoolSyncError("Key 不存在或已被上游删除")
            key_id = str(source_key_id)
            disabled = {
                str(value) for value in source.get("disabled_key_ids", [])
                if value not in (None, "")
            }
            if enabled:
                disabled.discard(key_id)
            else:
                disabled.add(key_id)
            source["disabled_key_ids"] = sorted(disabled)
            self._activate(source)
            self._save_state()
            logger.info(
                f"号池 Key 已手动{'启用' if enabled else '停用'}: "
                f"upstream={source['base_url']} source_key_id={key_id}"
            )
            return self.status()

    async def delete(self, source_id):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            adapter = self._adapter(source["adapter"])
            try:
                await adapter.disconnect(self.client, source, source.get("session") or {})
            except Exception as exc:
                logger.warning(f"上游会话撤销失败，继续删除本地号池: {exc}")
            self.sources.pop(source_id, None)
            self.operations.pop(source_id, None)
            self._restore_static(self._pool_url(source))
            if self.route_registry is not None:
                self.route_registry.unregister(source_id)
            self._save_state()
            result = self.status()
            logger.info(
                f"号池已删除: adapter={source['adapter']} upstream={source['base_url']}"
            )
        if not self._has_connected_sources():
            await self.stop()
        return result

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

    def _visible_entry(self, source, item, pool, runtime, disabled_key_ids, now):
        raw_key = item.get("key", "")
        entry = runtime.get(raw_key)
        prior = pool.prior_metrics.get(str(item.get("group_id") or "")) if pool else None
        ttft_stale_after = getattr(self.config, "key_ttft_stale_after", 300)
        return {
            "source_key_id": item.get("source_key_id"),
            "enabled": str(item.get("source_key_id")) not in disabled_key_ids,
            "key_masked": _mask_key(raw_key),
            "label": item.get("label", ""), "sort": item.get("sort", ""),
            "group_id": str(item.get("group_id") or ""),
            "key_name": item.get("key_name", ""), "group_name": item.get("group_name", ""),
            "platform": item.get("platform", ""),
            "allow_image_generation": bool(item.get("allow_image_generation")),
            "routing_capabilities": self._routing_capabilities(source, item),
            "models": item.get("models", []),
            "paths": item.get("paths", []),
            "cooled": bool(entry and entry.cooldown_until > now),
            "cooldown_remaining": round(max(entry.cooldown_until - now, 0), 1) if entry else 0,
            "last_failure_status": entry.last_failure_status if entry else None,
            "last_failure_kind": entry.last_failure_kind if entry else "",
            "ttft_ewma": round(entry.ttft_ewma, 3) if entry and entry.ttft_ewma is not None else None,
            "ttft_samples": entry.ttft_samples if entry else 0,
            "ttft_last_ts": entry.ttft_last_ts if entry else 0,
            "ttft_stale": bool(entry and entry.ttft_last_ts and
                               now - entry.ttft_last_ts >= ttft_stale_after),
            "experience_ttft_s": (round(prior["ttft"], 3)
                                   if prior and prior.get("ttft") is not None else None),
            "experience_samples": prior.get("samples", 0) if prior else 0,
            "experience_last_ts": prior.get("observed_ts", 0) if prior else 0,
            "probe_latency_s": (round(entry.probe_latency_s, 3)
                                if entry and entry.probe_latency_s is not None else None),
            "probe_last_ts": entry.probe_last_ts if entry else 0,
        }

    def _source_status(self, source, now):
        adapter = self._adapter(source["adapter"])
        pool = self.pools.get(self._pool_url(source))
        route_prefix = source.get("route_prefix", "")
        if not route_prefix and self.route_registry is not None:
            route_prefix = self.route_registry.environment_prefix_for_url(source["base_url"])
        runtime = {entry.key: entry for entry in pool.entries} if pool else {}
        disabled_key_ids = {
            str(value) for value in source.get("disabled_key_ids", [])
            if value not in (None, "")
        }
        visible_entries = [
            self._visible_entry(source, item, pool, runtime, disabled_key_ids, now)
            for item in (source.get("entries") or [])
        ]
        return {
            "id": source["id"], "adapter": source["adapter"], "adapter_label": adapter.label,
            "base_url": source["base_url"], "provider": source.get("provider", ""),
            "route_prefix": route_prefix,
            "connected": adapter.connected(source.get("session") or {}),
            "account": adapter.public_session(source.get("session") or {}),
            "last_sync_at": source.get("last_sync_at", ""),
            "last_attempt_at": source.get("last_attempt_at", ""),
            "last_error": source.get("last_error", ""),
            "strategy": source.get("strategy", "cost"),
            "target_ttft_s": source.get("target_ttft_s", 5.0),
            "external_retest_weight": source.get("external_retest_weight", 0.5),
            "external_ttft_prior_strength": source.get(
                "external_ttft_prior_strength", 2.0,
            ),
            "session_affinity": bool(source.get("session_affinity", False)),
            "ttft_policy": {
                "stale_after": getattr(self.config, "key_ttft_stale_after", 300),
                "retest_interval": getattr(self.config, "key_ttft_retest_interval", 60),
                "confirmations": getattr(self.config, "key_ttft_confirmations", 2),
                "hysteresis": getattr(self.config, "key_ttft_hysteresis", 0.1),
            },
            "scheduler_views": pool.scheduler_status(now) if pool else [],
            "experience": {
                **(source.get("experience_source") or {}),
                "items": [dict(item) for item in source.get("experience_items") or []],
                "mappings": dict(source.get("experience_mappings") or {}),
                "last_sync_at": source.get("experience_last_sync_at", ""),
                "last_error": source.get("experience_last_error", ""),
            },
            "check_model": source.get("check_model", ""),
            "key_count": len(visible_entries), "keys": visible_entries,
            "operation": dict(self.operations.get(source["id"]) or {}),
        }

    def status(self, source_id=None):
        selected = [self.sources[source_id]] if source_id in self.sources else list(self.sources.values())
        now = datetime.now().timestamp()
        public_sources = [self._source_status(source, now) for source in selected]
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
        return any(source.get("adapter") != "manual"
                   and self._adapter(source["adapter"]).connected(source.get("session") or {})
                   for source in self.sources.values())

    async def add_manual_keys(self, base_url, keys, provider="", route_prefix=""):
        base_url = _manual_text(base_url, "base_url") or self.default_url
        base_url = base_url.rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise PoolSyncError("上游地址必须以 http:// 或 https:// 开头")
        if not getattr(self.config, "key_pool_allow_private_base_url", False):
            await _validate_base_url_destination(base_url)
        if not isinstance(keys, list) or not keys:
            raise PoolSyncError("Key 列表必须是非空数组")
        requested_provider = _manual_text(provider, "provider")
        source_id = _source_id("manual", base_url)
        normalized_route_prefix = None
        if route_prefix is not None:
            try:
                normalized_route_prefix = normalize_route_prefix(
                    _manual_text(route_prefix, "route_prefix")
                )
            except ValueError as exc:
                raise PoolSyncError(str(exc)) from exc
        async with self._lock:
            source = self.sources.get(source_id)
            if source is not None and source.get("adapter") != "manual":
                raise PoolSyncError("该上游地址已由在线同步连接接管，请先删除或使用其他地址")
            conflict = next((s for s in self.sources.values()
                             if s.get("base_url") == base_url and s.get("adapter") != "manual"), None)
            if conflict is not None:
                raise PoolSyncError("该上游地址已由在线同步连接接管，请先删除或使用其他地址")

            if source is not None:
                current_provider = source.get("provider") or self.config.provider
                current_prefix = source.get("route_prefix", "")
                if requested_provider and requested_provider != current_provider:
                    raise PoolSyncError("该手动号池已使用其他 provider；请删除后重新创建")
                if (normalized_route_prefix is not None
                        and normalized_route_prefix != current_prefix):
                    raise PoolSyncError("该手动号池已使用其他代理前缀；请删除后重新创建")
                effective_provider = current_provider
                effective_prefix = current_prefix
            else:
                effective_provider = requested_provider or self.config.provider
                effective_prefix = normalized_route_prefix or ""
                if (self.route_registry is not None and not effective_prefix
                        and not self.route_registry.has_route_for_url(base_url)):
                    raise PoolSyncError(
                        "该上游地址尚无可用代理路由，请填写代理前缀"
                    )
                if self.route_registry is not None and effective_prefix:
                    try:
                        self.route_registry.validate(
                            source_id, effective_prefix, base_url, effective_provider,
                        )
                    except ValueError as exc:
                        raise PoolSyncError(str(exc)) from exc

            existing_keys = {
                item.get("key") for item in (source.get("entries") or [])
                if isinstance(item, dict)
            } if source else set()
            entries = list(source.get("entries") or []) if source else []
            added = 0
            skipped = 0
            for index, item in enumerate(keys):
                if not isinstance(item, dict):
                    raise PoolSyncError(f"keys[{index}] 必须是对象")
                key = _manual_text(item.get("key"), f"keys[{index}].key")
                if not key:
                    continue
                if key in existing_keys:
                    skipped += 1
                    continue
                label = _manual_text(item.get("label"), f"keys[{index}].label")
                sort = _manual_text(item.get("sort"), f"keys[{index}].sort")
                group_name = _manual_text(
                    item.get("group_name"), f"keys[{index}].group_name",
                )
                group_id = _manual_text(
                    item.get("group_id"), f"keys[{index}].group_id",
                ) or group_name
                entries.append({
                    "source_key_id": _manual_source_key_id(key),
                    "key": key,
                    "label": label,
                    "sort": sort,
                    "group_id": group_id,
                    "group_name": group_name,
                    "key_name": "",
                    "platform": "",
                    "allow_image_generation": False,
                    "routing_capabilities": {},
                    "models": _manual_patterns(
                        item.get("models"), f"keys[{index}].models",
                    ),
                    "paths": _manual_patterns(
                        item.get("paths"), f"keys[{index}].paths",
                    ),
                    "auth": _manual_auth(item.get("auth")),
                })
                existing_keys.add(key)
                added += 1
            if added == 0:
                raise PoolSyncError("所有 Key 均已存在或为空，未添加新 Key")
            if source is None:
                source = {
                    "id": source_id,
                    "adapter": "manual",
                    "base_url": base_url,
                    "provider": effective_provider,
                    "session": {},
                    "entries": entries,
                    "route_prefix": effective_prefix,
                    "strategy": "cost",
                    "target_ttft_s": 5.0,
                    "external_retest_weight": 0.5,
                    "external_ttft_prior_strength": 2.0,
                    "session_affinity": False,
                    "check_model": "",
                    "disabled_key_ids": [],
                    "group_model_cache": {},
                    "group_model_rejections": {},
                    "experience_source": {},
                    "experience_items": [],
                    "experience_mappings": {},
                    "experience_last_sync_at": "",
                    "experience_last_error": "",
                    "last_sync_at": _now_iso(),
                    "last_attempt_at": _now_iso(),
                    "last_error": "",
                }
                try:
                    source["pool_url"] = self._resolve_pool_url(source)
                except ValueError as exc:
                    raise PoolSyncError(str(exc)) from exc
                self.sources[source_id] = source
                if self.route_registry is not None and effective_prefix:
                    try:
                        self.route_registry.register(
                            source_id, effective_prefix, base_url, effective_provider,
                        )
                    except ValueError as exc:
                        self.sources.pop(source_id, None)
                        raise PoolSyncError(str(exc)) from exc
            else:
                source["entries"] = entries
                source["last_sync_at"] = _now_iso()
            self._activate(source)
            self._save_state()
            logger.info(
                f"手动号池已更新: upstream={base_url} 新增={added} 跳过={skipped} "
                f"总计={len(entries)}"
            )
        return self.status()

    async def remove_manual_keys(self, source_id, source_key_ids):
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            if source.get("adapter") != "manual":
                raise PoolSyncError("只能从手动管理的号池中删除 Key")
            if not source_key_ids:
                raise PoolSyncError("请指定要删除的 Key")
            remove_set = {str(kid) for kid in source_key_ids if kid}
            original_count = len(source.get("entries") or [])
            source["entries"] = [
                item for item in source.get("entries") or []
                if str(item.get("source_key_id", "")) not in remove_set
            ]
            removed = original_count - len(source["entries"])
            if removed == 0:
                raise PoolSyncError("未找到指定的 Key")
            disabled_key_ids = {
                str(value) for value in source.get("disabled_key_ids", [])
                if value not in (None, "")
            }
            source["disabled_key_ids"] = sorted(disabled_key_ids - remove_set)
            if not source["entries"]:
                pool_url = self._pool_url(source)
                self.sources.pop(source_id, None)
                self._restore_static(pool_url)
                if self.route_registry is not None:
                    self.route_registry.unregister(source_id)
                logger.info(
                    f"手动号池已清空并移除: upstream={source['base_url']}"
                )
            else:
                self._activate(source)
            self._save_state()
            logger.info(
                f"手动号池 Key 已删除: upstream={source['base_url']} "
                f"删除={removed} 剩余={len(source.get('entries', []))}"
            )
        return self.status()

    async def update_manual_key(self, source_id, source_key_id, updates):
        if not source_key_id:
            raise PoolSyncError("source_key_id 不能为空")
        if not isinstance(updates, dict):
            raise PoolSyncError("更新内容必须是对象")
        allowed_fields = {"label", "sort", "group_id", "group_name", "models", "paths", "auth"}
        filtered = {key: value for key, value in updates.items() if key in allowed_fields}
        if not filtered:
            raise PoolSyncError("没有可更新的字段")
        normalized = {}
        for field, value in filtered.items():
            if field in ("models", "paths"):
                normalized[field] = _manual_patterns(value, field)
            elif field == "auth":
                normalized[field] = _manual_auth(value)
            else:
                normalized[field] = _manual_text(value, field)
        async with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise PoolSyncError("号池同步连接不存在")
            if source.get("adapter") != "manual":
                raise PoolSyncError("只能编辑手动管理的号池 Key")
            entry = next(
                (item for item in source.get("entries") or []
                 if str(item.get("source_key_id")) == str(source_key_id)),
                None,
            )
            if entry is None:
                raise PoolSyncError("指定的 Key 不存在")
            old_group_name = entry.get("group_name", "")
            for field, value in normalized.items():
                entry[field] = value
            if ("group_name" in normalized and "group_id" not in normalized
                    and entry.get("group_id", "") == old_group_name):
                entry["group_id"] = normalized["group_name"]
            self._activate(source)
            self._save_state()
        return self.status()

    async def start(self):
        async with self._lock:
            if (self._task is None and self._has_connected_sources()
                    and self.config.key_pool_sync_interval > 0):
                self._task = asyncio.create_task(self._run(), name="key-pool-sync")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._flush_state()

    async def _run(self):
        while True:
            await asyncio.sleep(self.config.key_pool_sync_interval)
            try:
                await self.sync_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"号池自动同步失败，继续使用上次配置: {exc}")
