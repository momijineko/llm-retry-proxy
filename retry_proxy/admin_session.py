"""管理端登录会话存储。

会话使用 ``secrets.token_urlsafe`` 生成的随机令牌，仅保存在进程内存中，
重启即失效；注销时服务端吊销令牌。取代旧版由 ADMIN_PASSWORD 派生的
确定性 Cookie（sha256 值），避免 Cookie 泄露后 30 天内无法单独吊销。
"""

import secrets
import threading
import time

SESSION_TTL_SECONDS = 30 * 86400
_MAX_SESSIONS = 10000

_sessions: dict[str, float] = {}  # token -> 过期时间戳（time.time）
_lock = threading.Lock()


def create_session(ttl=SESSION_TTL_SECONDS):
    """创建会话，返回随机会话令牌。"""
    token = secrets.token_urlsafe(32)
    ttl = max(float(ttl or 0), 0.0)
    with _lock:
        _prune_expired_locked(time.time())
        _sessions[token] = time.time() + ttl
    return token


def is_valid(token):
    """校验会话令牌是否存在且未过期；过期令牌会被顺手清理。"""
    if not token:
        return False
    now = time.time()
    with _lock:
        expiry = _sessions.get(token)
        if expiry is None:
            return False
        if expiry <= now:
            _sessions.pop(token, None)
            return False
        return True


def revoke(token):
    """吊销会话令牌（登出时调用）。"""
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def _prune_expired_locked(now):
    """容量兜底：清理过期会话；仍超限时淘汰最早过期的会话。"""
    if len(_sessions) <= _MAX_SESSIONS:
        return
    for token in [t for t, expiry in _sessions.items() if expiry <= now]:
        _sessions.pop(token, None)
    while len(_sessions) > _MAX_SESSIONS:
        oldest = min(_sessions, key=_sessions.get)
        _sessions.pop(oldest, None)
