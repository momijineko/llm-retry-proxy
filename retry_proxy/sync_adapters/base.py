import asyncio
from abc import ABC, abstractmethod
from urllib.parse import quote

import httpx

# 瞬时传输错误重试参数：最多 2 次额外尝试，间隔递增。
_TRANSPORT_RETRIES = 2
_TRANSPORT_BACKOFFS = (0.5, 1.5)


async def _do_request(client, method, url, **kwargs):
    """Dispatch via client.request() (httpx); fall back to per-method handlers
    for test doubles that only implement .get/.post/.delete."""
    if hasattr(client, "request"):
        return await client.request(method, url, **kwargs)
    method_lower = method.lower()
    handler = getattr(client, method_lower, None)
    if handler is not None:
        return await handler(url, **kwargs)
    raise AttributeError(f"client has no .request() or .{method_lower}() method")


async def request_with_retry(client, method, url, **kwargs):
    """发起 HTTP 请求，遇到瞬时传输错误（连接重置/超时/DNS）时有界重试。

    仅对幂等 GET 与写操作的重试保持保守：写操作（POST/DELETE）默认不重试，
    避免重复提交；调用方可通过 ``retry_writes=True`` 显式启用。
    """
    import asyncio
    retry_writes = kwargs.pop("retry_writes", False)
    is_idempotent = method.upper() in ("GET", "HEAD") or retry_writes
    attempts = _TRANSPORT_RETRIES + 1 if is_idempotent else 1
    last_exc = None
    for attempt in range(attempts):
        try:
            return await _do_request(client, method, url, **kwargs)
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            backoff = _TRANSPORT_BACKOFFS[min(attempt, len(_TRANSPORT_BACKOFFS) - 1)]
            await asyncio.sleep(backoff)
    raise last_exc


class PoolSyncError(RuntimeError):
    pass


# 建/删 Key 的默认限流参数。中转站对写接口通常有较严的频率限制，
# 批量操作必须留出间隔，否则会被上游直接阻塞或熔断。
DEFAULT_WRITE_DELAY = 1.5
MAX_WRITE_DELAY = 60.0


def is_rate_limited(exc):
    """写接口错误是否由上游限流引起。

    Sub2API、AIHub 与 New API 的错误文本各不相同（HTTP 429、``rate limit``、
    ``too many requests``、中文“限流”），统一在这里判定，避免各适配器各写一份。
    """
    text = str(exc).lower()
    return ("429" in text or "rate limit" in text or "too many" in text
            or "too frequent" in text or "frequency" in text or "限流" in text)


def write_delay(options=None, default=None):
    """从 options 读取写操作间隔，并夹在安全区间内。"""
    raw = (options or {}).get("delay_seconds")
    if raw is None:
        raw = DEFAULT_WRITE_DELAY if default is None else default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_WRITE_DELAY if default is None else float(default)
    return max(0.0, min(value, MAX_WRITE_DELAY))


class WriteThrottle:
    """跨建/删共享的写接口限流器。

    除了固定间隔，还维护失败退避：一旦命中 429/限流，就按指数把后续间隔
    抬高并继续本轮，而不是“成功了就立刻接着打”。这样即使上游只在第 N 次
    请求才限流，也不会把剩下的请求打成一波。
    """

    def __init__(self, delay_seconds=DEFAULT_WRITE_DELAY, backoff_base=None,
                 max_delay=MAX_WRITE_DELAY):
        self.base_delay = max(0.0, min(float(delay_seconds), MAX_WRITE_DELAY))
        if backoff_base is None:
            # 间隔设为 0 时仍要能退避，否则命中限流后仍会持续狂发
            backoff_base = self.base_delay or DEFAULT_WRITE_DELAY
        self.backoff_base = max(0.0, min(float(backoff_base), MAX_WRITE_DELAY))
        self.max_delay = max(0.0, min(float(max_delay), MAX_WRITE_DELAY))
        self._current_delay = self.base_delay
        self._sent = 0
        self.rate_limited = 0

    @property
    def delay(self):
        return self._current_delay

    async def before(self):
        """在每次写请求前等待。第一个请求不等待。"""
        if self._sent and self._current_delay > 0:
            await asyncio.sleep(self._current_delay)
        self._sent += 1

    def on_rate_limited(self):
        """命中限流后抬高后续间隔（指数退避，带上限）。"""
        self.rate_limited += 1
        grown = max(self._current_delay, self.backoff_base) * 2
        self._current_delay = min(grown, self.max_delay)

    def on_success(self):
        """成功后缓慢回落，避免一个短暂限流把整轮都拖慢。"""
        if self._current_delay > self.base_delay:
            self._current_delay = max(self._current_delay / 2, self.base_delay)


class PoolSyncAdapter(ABC):
    """Provider-specific authentication and key normalization contract.

    Normalized entries may include ``routing_capabilities`` with ``platform``,
    ``endpoint_families``, ``model_patterns``, ``model_scopes``,
    ``model_list_known``, ``rejected_model_routes`` and ``image_generation``.
    They may also include ``auth`` with ``header`` and ``scheme`` for per-entry
    upstream authentication. Adapters should omit either object when the
    upstream does not expose reliable metadata so legacy selection/configuration
    is retained.
    """

    name = ""
    label = ""
    credential_fields = []
    capabilities = []

    @abstractmethod
    async def connect(self, client, source, credentials):
        """Authenticate and return a serializable session dictionary."""

    @abstractmethod
    async def fetch(self, client, source, session):
        """Return (updated_session, normalized_entries)."""

    async def disconnect(self, client, source, session):
        """Optionally revoke the remote session before local credentials are cleared."""

    async def catalog(self, client, source, session):
        raise PoolSyncError(f"{self.label or self.name} 不支持分组目录")

    async def create_keys(self, client, source, session, group_ids, only_missing=False, options=None):
        raise PoolSyncError(f"{self.label or self.name} 不支持创建 Key")

    async def delete_keys(self, client, source, session, group_ids, options=None):
        raise PoolSyncError(f"{self.label or self.name} 不支持清空分组 Key")

    def routing_capabilities(self, group):
        """Return reliable normalized routing metadata, or an empty dict."""
        return {}

    def availability_request(self, source, model, endpoint_family="chat"):
        """Build a minimal generation request for one model protocol."""
        base_url = source["base_url"].rstrip("/")
        if endpoint_family == "responses":
            return {
                "url": base_url + "/v1/responses",
                "json": {
                    "model": model, "input": "Hi", "max_output_tokens": 1,
                    "stream": False,
                },
                "headers": {},
            }
        if endpoint_family == "messages":
            return {
                "url": base_url + "/v1/messages",
                "json": {
                    "model": model,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 1,
                    "stream": False,
                },
                "headers": {"anthropic-version": "2023-06-01"},
            }
        if endpoint_family in ("gemini", "gemini_image"):
            generation_config = {"maxOutputTokens": 1}
            if endpoint_family == "gemini_image":
                generation_config["responseModalities"] = ["IMAGE"]
            return {
                "url": (
                    base_url + "/v1beta/models/" + quote(model, safe="")
                    + ":generateContent"
                ),
                "json": {
                    "contents": [{"parts": [{"text": "Hi"}]}],
                    "generationConfig": generation_config,
                },
                "headers": {},
            }
        if endpoint_family == "images":
            return {
                "url": base_url + "/v1/images/generations",
                "json": {"model": model, "prompt": "Hi", "n": 1},
                "headers": {},
            }
        if endpoint_family == "embeddings":
            return {
                "url": base_url + "/v1/embeddings",
                "json": {"model": model, "input": "Hi"},
                "headers": {},
            }
        if endpoint_family == "audio_speech":
            return {
                "url": base_url + "/v1/audio/speech",
                "json": {
                    "model": model, "input": "Hi", "voice": "alloy",
                    "response_format": "mp3",
                },
                "headers": {},
            }
        if endpoint_family == "audio_transcription":
            # 44-byte PCM header plus one silent 16-bit sample.
            silent_wav = (
                b"RIFF\x26\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
                b"\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00"
                b"\x02\x00\x10\x00data\x02\x00\x00\x00\x00\x00"
            )
            return {
                "url": base_url + "/v1/audio/transcriptions",
                "data": {"model": model},
                "files": {"file": ("probe.wav", silent_wav, "audio/wav")},
                "headers": {},
            }
        if endpoint_family == "realtime":
            return {
                "url": base_url + "/v1/realtime/sessions",
                "json": {"model": model},
                "headers": {"OpenAI-Beta": "realtime=v1"},
            }
        return {
            "url": base_url + "/v1/chat/completions",
            "json": {
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 1,
                "stream": False,
            },
            "headers": {},
        }

    def connected(self, session):
        return bool(session)

    def public_session(self, session):
        return {}

    def persistent_session(self, session):
        """Return the session fields that may be written to the state file."""
        persisted = dict(session or {})
        persisted.pop("access_token", None)
        return persisted
