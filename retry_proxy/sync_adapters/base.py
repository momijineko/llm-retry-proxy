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
