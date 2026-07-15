from .config import logger, settings

EXCLUDE_PATHS = {"favicon.ico", "robots.txt", "sitemap.xml", "manifest.json", "site.webmanifest", "browserconfig.xml"}


def is_excluded_path(path: str) -> bool:
    return path.lstrip("/").lower() in EXCLUDE_PATHS


def build_routes():
    routes = []
    for entry in settings.extra_upstreams.strip().split(",") if settings.extra_upstreams.strip() else []:
        parts = entry.strip().split("|")
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            logger.warning(f"EXTRA_UPSTREAMS 条目格式错误，跳过: {entry!r}（应为 prefix|url|provider）")
            continue
        prefix, url = parts[0].strip(), parts[1].strip().rstrip("/")
        provider = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else prefix.lstrip("/")
        routes.append((prefix, url, provider, True))
    routes.sort(key=lambda r: len(r[0]), reverse=True)
    routes.append(("", settings.upstream_url, settings.provider, False))
    return routes


ROUTES = build_routes()


def match_route(path: str):
    for prefix, upstream_url, provider, strip in ROUTES:
        if not prefix:
            return upstream_url, provider, path
        pfx = prefix.lstrip("/")
        if pfx and (path == pfx or path.startswith(pfx + "/")):
            return upstream_url, provider, path[len(pfx):].lstrip("/") if strip else path
    return settings.upstream_url, settings.provider, path
