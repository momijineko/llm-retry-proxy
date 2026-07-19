from .config import logger, settings

EXCLUDE_PATHS = {"favicon.ico", "robots.txt", "sitemap.xml", "manifest.json", "site.webmanifest", "browserconfig.xml"}


def is_excluded_path(path: str) -> bool:
    return path.lstrip("/").lower() in EXCLUDE_PATHS


def normalize_route_prefix(prefix: str) -> str:
    value = (prefix or "").strip()
    if not value:
        return ""
    if "://" in value or any(char.isspace() or char in "?#|," for char in value):
        raise ValueError("代理前缀只能是路径，例如 /aihub")
    value = "/" + value.strip("/")
    if value == "/":
        return ""
    return value


class RouteRegistry:
    """Combines immutable environment routes with persisted runtime routes."""

    def __init__(self, config=settings):
        self.config = config
        self._environment = self._build_environment_routes()
        self._managed = {}
        self.routes = []
        self._refresh()

    def _build_environment_routes(self):
        routes = []
        raw_entries = self.config.extra_upstreams.strip()
        for entry in raw_entries.split(",") if raw_entries else []:
            parts = entry.strip().split("|")
            if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
                logger.warning(f"EXTRA_UPSTREAMS 条目格式错误，跳过: {entry!r}（应为 prefix|url|provider）")
                continue
            try:
                prefix = normalize_route_prefix(parts[0])
            except ValueError as exc:
                logger.warning(f"EXTRA_UPSTREAMS 条目格式错误，跳过: {entry!r}（{exc}）")
                continue
            if not prefix:
                logger.warning(f"EXTRA_UPSTREAMS 不支持默认路由，跳过: {entry!r}")
                continue
            url = parts[1].strip().rstrip("/")
            provider = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else prefix.lstrip("/")
            routes.append((prefix, url, provider, True))
        return routes

    def _refresh(self):
        environment = {route[0]: route for route in self._environment}
        managed = [
            route for route in self._managed.values()
            if route[0] not in environment or route[2] == environment[route[0]][2]
        ]
        overridden = {route[0] for route in managed}
        extras = managed + [route for route in self._environment if route[0] not in overridden]
        extras.sort(key=lambda route: len(route[0]), reverse=True)
        self.routes[:] = extras + [("", self.config.upstream_url, self.config.provider, False)]

    def environment_upstream(self, prefix: str, upstream_url: str, provider: str = "") -> str:
        """Resolve the runtime upstream when a sync source reuses an env route."""
        prefix = normalize_route_prefix(prefix)
        upstream_url = (upstream_url or "").strip().rstrip("/")
        environment = next((route for route in self._environment if route[0] == prefix), None)
        if environment is None or environment[1] == upstream_url:
            return upstream_url
        if (provider or "").strip() == environment[2]:
            return upstream_url
        raise ValueError(f"代理前缀 {prefix} 已由 EXTRA_UPSTREAMS 配置为 {environment[1]}")

    def validate(self, source_id: str, prefix: str, upstream_url: str, provider: str = ""):
        prefix = normalize_route_prefix(prefix)
        if not prefix:
            return prefix
        self.environment_upstream(prefix, upstream_url, provider)
        conflict = next((route for sid, route in self._managed.items()
                         if sid != source_id and route[0] == prefix), None)
        if conflict is not None:
            raise ValueError(f"代理前缀 {prefix} 已被其他号池连接使用")
        return prefix

    def register(self, source_id: str, prefix: str, upstream_url: str, provider: str):
        prefix = self.validate(source_id, prefix, upstream_url, provider)
        if not prefix:
            self.unregister(source_id)
            return ""
        upstream_url = (upstream_url or "").strip().rstrip("/")
        self._managed[source_id] = (prefix, upstream_url, (provider or prefix.lstrip("/")).strip(), True)
        self._refresh()
        return prefix

    def unregister(self, source_id: str):
        if self._managed.pop(source_id, None) is not None:
            self._refresh()

    def clear_managed(self):
        if self._managed:
            self._managed.clear()
            self._refresh()

    def environment_prefix_for_url(self, upstream_url: str) -> str:
        upstream_url = (upstream_url or "").strip().rstrip("/")
        route = next((route for route in self._environment if route[1] == upstream_url), None)
        return route[0] if route else ""

    def match(self, path: str):
        for prefix, upstream_url, provider, strip in self.routes:
            if not prefix:
                return upstream_url, provider, path
            path_prefix = prefix.lstrip("/")
            if path == path_prefix or path.startswith(path_prefix + "/"):
                remaining = path[len(path_prefix):].lstrip("/") if strip else path
                return upstream_url, provider, remaining
        return self.config.upstream_url, self.config.provider, path


route_registry = RouteRegistry()
ROUTES = route_registry.routes


def build_routes():
    return list(RouteRegistry(settings).routes)


def match_route(path: str):
    return route_registry.match(path)
