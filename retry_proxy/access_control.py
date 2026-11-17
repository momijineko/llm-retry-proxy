import ipaddress
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict

from starlette.responses import Response


logger = logging.getLogger("forward")


def parse_ip_networks(raw, setting_name):
    """Parse comma/space separated IP addresses and CIDR ranges."""
    networks = []
    for value in re.split(r"[\s,;]+", (raw or "").strip()):
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ValueError(f"{setting_name} 包含无效 IP 或 CIDR: {value}") from exc
    return tuple(networks)


def _parse_ip(value):
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def ip_in_networks(address, networks):
    if address is None:
        return False
    return any(address.version == network.version and address in network
               for network in networks)


def resolve_client_ip(scope, trusted_proxies=()):
    """Resolve the client IP without trusting headers from direct clients."""
    client = scope.get("client")
    direct_value = client[0] if client else ""
    direct = _parse_ip(direct_value)
    if direct is None:
        return direct_value
    if not ip_in_networks(direct, trusted_proxies):
        return str(direct)

    headers = {}
    for name, value in scope.get("headers", []):
        headers[name.decode("latin-1").lower()] = value.decode("latin-1")

    cf_ip = _parse_ip(headers.get("cf-connecting-ip", ""))
    if cf_ip is not None:
        return str(cf_ip)

    forwarded = []
    for value in headers.get("x-forwarded-for", "").split(","):
        address = _parse_ip(value)
        if address is not None:
            forwarded.append(address)
    for address in reversed(forwarded):
        if not ip_in_networks(address, trusted_proxies):
            return str(address)
    if forwarded:
        return str(forwarded[0])

    real_ip = _parse_ip(headers.get("x-real-ip", ""))
    return str(real_ip) if real_ip is not None else str(direct)


class IPBlocklistMiddleware:
    """静态黑名单 + 基于“不同路径数”的动态封禁中间件。

    动态封禁原本把任何不同路径都计入扫描计数，而正常的号池探测会把
    ``/<前缀>/v1/models``、``/<前缀>/v1/sub2api/billing`` 等一批路径在
    几秒内打完，于是被误判为 URL 扫描并永久封禁。因此这里改用
    :meth:`_counted_path` 归一化：代理 API 路径与已知页面共用一个计数键，
    真正像扫描的路径（``/.env``、``/wp-admin`` 等）才逐个累计。

    号池客户端还会对每个上游前缀各探测一遍监控端点（``/agent/api/status``、
    ``/aihub/api/status`` …），这类路径按版本段/``api`` 段归一，避免前缀数
    放大计数。
    """
    _MAX_TRACKED_CLIENTS = 10000
    _MAX_PERSISTED_BANS = 100000

    def __init__(self, app, blacklist=(), trusted_proxies=(),
                 auto_ban_threshold=0, auto_ban_window=10,
                 auto_ban_duration=0, auto_ban_exempt=(),
                 state_file="", clock=None):
        self.app = app
        self.blacklist = tuple(blacklist)
        self.trusted_proxies = tuple(trusted_proxies)
        self.auto_ban_threshold = int(auto_ban_threshold)
        self.auto_ban_window = float(auto_ban_window)
        self.auto_ban_duration = float(auto_ban_duration)
        self.auto_ban_exempt = tuple(auto_ban_exempt)
        self.state_file = state_file
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._activity = OrderedDict()
        self._bans = {}
        self._persistence_warning_logged = False
        if self.auto_ban_threshold < 0:
            raise ValueError("IP_AUTO_BAN_THRESHOLD 不能小于 0")
        if self.auto_ban_threshold and self.auto_ban_window <= 0:
            raise ValueError("IP_AUTO_BAN_WINDOW 必须大于 0")
        if self.auto_ban_duration < 0:
            raise ValueError("IP_AUTO_BAN_DURATION 不能小于 0")
        self._load_bans()

    # 代理 API 端点尾段：不同上游前缀共用同一个计数键。号池探测会在一两秒内
    # 依次访问多个前缀下的 /v1/models、/v1/sub2api/billing，逐个路径计数会把
    # 合法探测误判为 URL 扫描（曾导致永久封禁正常客户端）。
    # 按长度降序匹配，否则 ``chat/completions`` 会先命中 ``completions``，
    # 集合迭代顺序不稳定时同一路径会得到不同计数键。
    _SHARED_ENDPOINTS = (
        "audio/transcriptions", "audio/translations", "images/generations",
        "images/variations", "chat/completions", "images/edits",
        "sub2api/billing", "audio/speech", "moderations", "embeddings",
        "completions", "responses", "messages", "realtime", "uploads",
        "batches", "models", "files",
    )
    # 正常页面/资源不计入扫描路径数。
    _IGNORED_PATHS = frozenset({
        "", "health", "stats", "stats/api", "logs", "logs/api",
        "logs/stream", "key-pools", "settings", "requests", "requests/api",
        "admin/login", "admin/logout", "admin/settings", "favicon.ico",
    })
    _VERSION_PREFIX = re.compile(r"^v\d+(?:beta\d*|alpha\d*)?/")
    # 路径中标记“逻辑 API 起点”的段：其前的段都是路由前缀。
    _API_SEGMENT = re.compile(r"^(?:v\d+(?:beta\d*|alpha\d*)?|api)$")

    @classmethod
    def _counted_path(cls, path):
        """把一次访问归一化成扫描计数的键；``None`` 表示不计入。

        只看路径尾段的 API 端点名，因此 ``/aihub/v1/models`` 与
        ``/ocg/v1/models`` 归一为同一个键；未知路径（如 ``/.env``、
        ``/wp-admin/x.php``）保持完整路径，仍然逐个累计。
        """
        normalized = (path or "").strip("/").lower()
        if normalized in cls._IGNORED_PATHS:
            return None
        if normalized.startswith("admin/assets/"):
            return None
        stripped = cls._VERSION_PREFIX.sub("", normalized)
        segments = [segment for segment in stripped.split("/") if segment]
        # 无路由前缀的 ``/v1/usage``、``/api/status`` 也要归一，否则与
        # ``/<前缀>/v1/usage`` 落到不同计数键。
        head_index = 0
        if len(segments) > 1 and cls._API_SEGMENT.match(segments[0]):
            head_index = 1
        stripped = "/".join(segments[head_index:])
        for endpoint in cls._SHARED_ENDPOINTS:
            if stripped == endpoint or stripped.endswith("/" + endpoint):
                return endpoint
        head = stripped.split("/", 1)[0]
        if head in cls._SHARED_ENDPOINTS:
            return head
        # 监控类端点（``/v1/usage``、``/api/status``、``/api/log/token``）在路由
        # 前缀后带版本段或 ``api`` 段；保留前缀会按前缀数量放大计数。
        for index, segment in enumerate(segments[head_index + 1:], head_index + 1):
            if cls._API_SEGMENT.match(segment):
                return "/".join(segments[index + 1:]) or segment
        return stripped

    def _load_bans(self):
        if not self.state_file:
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            logger.warning("动态封禁状态读取失败，本次启动使用空状态")
            return

        raw_bans = payload.get("bans", {}) if isinstance(payload, dict) else {}
        now = self._clock()
        for raw_ip, raw_expiry in list(raw_bans.items())[:self._MAX_PERSISTED_BANS]:
            address = _parse_ip(raw_ip)
            try:
                expiry = float(raw_expiry)
            except (TypeError, ValueError):
                continue
            if address is not None and (expiry == 0 or expiry > now):
                self._bans[str(address)] = expiry

    def _save_bans_locked(self):
        if not self.state_file:
            return
        directory = os.path.dirname(os.path.abspath(self.state_file))
        temp_path = ""
        try:
            os.makedirs(directory, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=".ip_bans.", dir=directory)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump({"version": 1, "bans": self._bans}, file,
                              ensure_ascii=True, separators=(",", ":"))
                    file.flush()
                    os.fsync(file.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(temp_path, self.state_file)
            self._persistence_warning_logged = False
        except OSError:
            if not self._persistence_warning_logged:
                logger.warning("动态封禁状态保存失败，封禁仅在当前进程内有效")
                self._persistence_warning_logged = True
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _is_dynamically_banned_locked(self, client_ip, now):
        expiry = self._bans.get(client_ip)
        if expiry is None:
            return False
        if expiry == 0 or expiry > now:
            return True
        del self._bans[client_ip]
        self._save_bans_locked()
        return False

    def _observe_path_locked(self, client_ip, path, now):
        counted = self._counted_path(path)
        if counted is None:
            # 正常页面/静态资源不计入扫描计数；API 路径已归一为同一计数键。
            return False
        paths = self._activity.get(client_ip)
        if paths is None:
            paths = {}
            self._activity[client_ip] = paths
        else:
            self._activity.move_to_end(client_ip)

        cutoff = now - self.auto_ban_window
        for old_path, last_seen in list(paths.items()):
            if last_seen <= cutoff:
                del paths[old_path]
        paths[counted] = now

        while len(self._activity) > self._MAX_TRACKED_CLIENTS:
            self._activity.popitem(last=False)
        if len(paths) < self.auto_ban_threshold:
            return False

        self._activity.pop(client_ip, None)
        self._bans[client_ip] = (
            0 if self.auto_ban_duration == 0 else now + self.auto_ban_duration
        )
        if len(self._bans) > self._MAX_PERSISTED_BANS:
            oldest = min(
                self._bans,
                key=lambda ip: self._bans[ip] if self._bans[ip] > 0 else float("inf"),
            )
            del self._bans[oldest]
        self._save_bans_locked()
        return True

    def active_bans(self, now=None):
        """Return current dynamic bans as ``{ip: expires_at}`` (0 = permanent)."""
        now = self._clock() if now is None else now
        with self._lock:
            return {
                ip: expiry for ip, expiry in sorted(self._bans.items())
                if expiry == 0 or expiry > now
            }

    def unban(self, addresses=()):
        """Remove dynamic bans; empty input clears every ban.

        Returns the IPs that were actually removed so callers can report them.
        """
        targets = {str(value).strip() for value in addresses if str(value).strip()}
        removed = []
        with self._lock:
            for ip in list(self._bans):
                if targets and ip not in targets:
                    continue
                del self._bans[ip]
                removed.append(ip)
            for ip in list(self._activity):
                if not targets or ip in targets:
                    del self._activity[ip]
            if removed:
                self._save_bans_locked()
        return sorted(removed)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        client_ip = resolve_client_ip(scope, self.trusted_proxies)
        address = _parse_ip(client_ip)
        blocked = ip_in_networks(address, self.blacklist)
        newly_banned = False
        if (not blocked and address is not None and self.auto_ban_threshold
                and not ip_in_networks(address, self.auto_ban_exempt)):
            now = self._clock()
            with self._lock:
                blocked = self._is_dynamically_banned_locked(client_ip, now)
                if not blocked:
                    path = (scope.get("path") or "/")[:4096]
                    newly_banned = self._observe_path_locked(client_ip, path, now)
                    blocked = newly_banned

        if not blocked:
            await self.app(scope, receive, send)
            return

        if newly_banned:
            duration = (
                "永久封禁" if self.auto_ban_duration == 0 else
                f"封禁{self.auto_ban_duration:g}s"
            )
            logger.warning(
                f"[{client_ip}] 动态封禁: {self.auto_ban_window:g}s内访问"
                f"{self.auto_ban_threshold}个不同路径, "
                f"{duration}"
            )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "IP blocked"})
            return
        response = Response(status_code=403)
        await response(scope, receive, send)
