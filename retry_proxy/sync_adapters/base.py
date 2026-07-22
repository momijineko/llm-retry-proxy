from abc import ABC, abstractmethod


class PoolSyncError(RuntimeError):
    pass


class PoolSyncAdapter(ABC):
    """Provider-specific authentication and key normalization contract.

    Normalized entries may include ``routing_capabilities`` with ``platform``,
    ``endpoint_families``, ``model_patterns``, ``model_scopes``, ``model_list_known`` and
    ``image_generation``. They may also include ``auth`` with ``header`` and
    ``scheme`` for per-entry upstream authentication. Adapters should omit
    either object when the upstream does not expose reliable metadata so
    legacy selection/configuration is retained.
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

    def availability_request(self, source, model):
        """Build the request used by the manual availability check."""
        return {
            "url": source["base_url"].rstrip("/") + "/v1/chat/completions",
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
