import asyncio
import uuid

from ..config import logger
from .base import PoolSyncAdapter, PoolSyncError


def _response_error_message(response):
    status = response.status_code
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    server = response.headers.get("server", "").lower()
    behind_cloudflare = bool(response.headers.get("cf-ray")) or "cloudflare" in server

    if status == 403:
        reason = (
            "请求被 Cloudflare/CDN 拦截"
            if behind_cloudflare else "上游拒绝了号池同步请求"
        )
        if content_type == "text/html" and not behind_cloudflare:
            reason = "上游返回了 HTML 拒绝页"
        return (
            f"{reason} (HTTP 403)；请确认填写的是站点根地址，并检查来源 IP、"
            "CDN/WAF 规则及人机验证设置"
        )
    if content_type == "text/html":
        return (
            f"上游返回了 HTML 而不是 API JSON (HTTP {status})；"
            "请确认填写的是站点根地址且 Sub2API 接口可访问"
        )
    return f"上游返回了非 JSON 响应 (HTTP {status})"


def _unwrap(response):
    if response.status_code == 204:
        return {}
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise PoolSyncError(_response_error_message(response)) from exc
    if response.status_code >= 400:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise PoolSyncError(message or f"上游请求失败 (HTTP {response.status_code})")
    if isinstance(payload, dict) and "code" in payload:
        if payload.get("code") not in (0, "0"):
            raise PoolSyncError(payload.get("message") or f"上游返回错误 code={payload.get('code')}")
        return payload.get("data")
    return payload


def _number_text(value):
    try:
        return format(float(value), ".12g")
    except (TypeError, ValueError):
        return ""


def _model_ids(payload):
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None:
            items = payload.get("models")
    elif isinstance(payload, list):
        items = payload
    else:
        items = None
    if not isinstance(items, list):
        raise PoolSyncError("模型列表响应格式无法识别")
    models = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("id") or item.get("name")
        else:
            continue
        value = str(value or "").strip()
        if value.startswith("models/"):
            value = value[7:]
        normalized = value.lower()
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        models.append(value)
    return models


class Sub2APIAdapter(PoolSyncAdapter):
    name = "sub2api"
    label = "Sub2API"
    credential_fields = [
        {"name": "email", "label": "登录邮箱", "type": "email"},
        {"name": "password", "label": "登录密码", "type": "password"},
    ]
    capabilities = ["group_catalog", "create_keys", "delete_keys"]

    @staticmethod
    def routing_capabilities(group):
        platform = str(group.get("platform") or "").strip().lower()
        endpoints = {
            "openai": {"chat", "responses", "embeddings", "audio"},
            "anthropic": {"messages"},
            "gemini": {"gemini"},
            "antigravity": {"chat", "messages", "gemini"},
            "grok": {"chat", "responses"},
        }.get(platform, set())
        if platform == "openai" and group.get("allow_messages_dispatch"):
            endpoints.add("messages")
        image_generation = bool(group.get("allow_image_generation"))
        if image_generation:
            endpoints.add("images")
        if not endpoints:
            return {}
        models_config = group.get("models_list_config") or {}
        model_patterns = (
            models_config.get("models") or []
            if isinstance(models_config, dict) and models_config.get("enabled") else []
        )
        return {
            "platform": platform,
            "endpoint_families": sorted(endpoints),
            "model_patterns": [str(value) for value in model_patterns if str(value).strip()],
            "model_scopes": [
                str(value) for value in (group.get("supported_model_scopes") or [])
                if str(value).strip()
            ],
            "image_generation": image_generation,
        }

    @staticmethod
    def _headers(access_token="", extra=None):
        headers = {"Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        headers.update(extra or {})
        return headers

    async def _post(self, client, source, path, body):
        response = await client.post(
            source["base_url"] + path, json=body, headers=self._headers(), timeout=20,
        )
        return _unwrap(response)

    async def connect(self, client, source, credentials):
        email = str(credentials.get("email") or "").strip()
        password = credentials.get("password") or ""
        if not email or not password:
            raise PoolSyncError("邮箱和密码不能为空")
        data = await self._post(client, source, "/api/v1/auth/login", {
            "email": email, "password": password, "turnstile_token": "",
        })
        if (data or {}).get("requires_2fa"):
            raise PoolSyncError("该账号启用了两步验证，当前适配器暂不支持")
        session = {
            "email": email,
            "access_token": (data or {}).get("access_token", ""),
            "refresh_token": (data or {}).get("refresh_token", ""),
        }
        if not session["access_token"] or not session["refresh_token"]:
            raise PoolSyncError("登录响应缺少可续期令牌")
        return session

    async def _refresh(self, client, source, session):
        refresh_token = session.get("refresh_token", "")
        if not refresh_token:
            raise PoolSyncError("该连接需要重新登录")
        data = await self._post(client, source, "/api/v1/auth/refresh", {
            "refresh_token": refresh_token,
        })
        session["access_token"] = (data or {}).get("access_token", "")
        session["refresh_token"] = (data or {}).get("refresh_token", "") or refresh_token
        if not session["access_token"]:
            raise PoolSyncError("刷新令牌响应缺少 access_token")
        return session

    async def _get(self, client, source, session, path, params=None, retry=True):
        if not session.get("access_token"):
            session = await self._refresh(client, source, session)
        response = await client.get(
            source["base_url"] + path, params=params,
            headers=self._headers(session["access_token"]), timeout=20,
        )
        if response.status_code == 401 and retry:
            session["access_token"] = ""
            session = await self._refresh(client, source, session)
            return await self._get(client, source, session, path, params, retry=False)
        return session, _unwrap(response)

    async def _get_group_models(self, client, source, api_key):
        response = await client.get(
            source["base_url"] + "/v1/models",
            headers=self._headers(api_key), timeout=20,
        )
        return _model_ids(_unwrap(response))

    async def _apply_group_model_cache(self, client, source, entries):
        raw_cache = source.get("group_model_cache")
        cache = raw_cache if isinstance(raw_cache, dict) else {}
        cache = {
            str(group_id): list(models)
            for group_id, models in cache.items()
            if isinstance(models, list)
        }
        source["group_model_cache"] = cache
        representatives = {}
        for item in entries:
            group_id = str(item.get("group_id") or "")
            if group_id and item.get("routing_capabilities"):
                representatives.setdefault(group_id, item)
        for group_id, item in representatives.items():
            if group_id in cache:
                continue
            try:
                cache[group_id] = await self._get_group_models(
                    client, source, item["key"],
                )
            except Exception as exc:
                logger.warning(
                    "分组模型列表读取失败: upstream=%s group=%s error=%s",
                    source["base_url"], item.get("group_name") or group_id, exc,
                )
        for item in entries:
            group_id = str(item.get("group_id") or "")
            capabilities = item.get("routing_capabilities")
            if group_id not in cache or not capabilities:
                continue
            capabilities["model_patterns"] = list(cache[group_id])
            capabilities["model_list_known"] = True
        return entries

    async def _authorized_post(self, client, source, session, path, body, headers=None, retry=True):
        if not session.get("access_token"):
            session = await self._refresh(client, source, session)
        request_headers = self._headers(session["access_token"], headers)
        response = await client.post(
            source["base_url"] + path, json=body, headers=request_headers, timeout=20,
        )
        if response.status_code == 401 and retry:
            session["access_token"] = ""
            session = await self._refresh(client, source, session)
            return await self._authorized_post(
                client, source, session, path, body, headers, retry=False,
            )
        return session, _unwrap(response)

    async def _authorized_delete(self, client, source, session, path, retry=True):
        if not session.get("access_token"):
            session = await self._refresh(client, source, session)
        response = await client.delete(
            source["base_url"] + path,
            headers=self._headers(session["access_token"]), timeout=20,
        )
        if response.status_code == 401 and retry:
            session["access_token"] = ""
            session = await self._refresh(client, source, session)
            return await self._authorized_delete(client, source, session, path, retry=False)
        return session, _unwrap(response)

    async def _fetch_all_keys(self, client, source, session):
        items = []
        page = 1
        while True:
            session, data = await self._get(
                client, source, session, "/api/v1/keys", {"page": page, "page_size": 100}
            )
            if isinstance(data, list):
                batch, total = data, len(data)
            elif isinstance(data, dict):
                batch = data.get("items") or data.get("data") or data.get("list") or []
                total = int(data.get("total", len(batch)))
            else:
                raise PoolSyncError("Key 列表响应格式无法识别")
            items.extend(batch)
            if not batch or len(items) >= total or len(batch) < 100:
                return session, items
            page += 1

    async def fetch(self, client, source, session):
        session, keys = await self._fetch_all_keys(client, source, session)
        session, groups = await self._get(client, source, session, "/api/v1/groups/available")
        session, rates = await self._get(client, source, session, "/api/v1/groups/rates")
        groups_by_id = {
            str(group.get("id")): group for group in (groups or []) if isinstance(group, dict)
        }
        rates = {str(key): value for key, value in (rates or {}).items()}
        entries = []
        for item in keys:
            if not isinstance(item, dict) or item.get("status") != "active" or not item.get("key"):
                continue
            group_id = item.get("group_id")
            group = dict(groups_by_id.get(str(group_id)) or {})
            group.update(item.get("group") or {})
            if group_id is None or not group or group.get("status") not in (None, "", "active"):
                continue
            key_name = str(item.get("name") or "").strip()
            group_name = str(group.get("name") or "").strip()
            if key_name and group_name and group_name.lower() not in key_name.lower():
                label = f"{key_name}-{group_name}"
            else:
                label = key_name or group_name
            rate = rates.get(str(group_id), group.get("rate_multiplier"))
            entries.append({
                "source_key_id": item.get("id"), "key": item["key"], "label": label,
                "sort": _number_text(rate), "key_name": key_name, "group_id": group_id,
                "group_name": group_name, "platform": group.get("platform", ""),
                "allow_image_generation": bool(group.get("allow_image_generation")),
                "routing_capabilities": self.routing_capabilities(group),
            })
        await self._apply_group_model_cache(client, source, entries)
        return session, entries

    async def catalog(self, client, source, session):
        session, keys = await self._fetch_all_keys(client, source, session)
        session, groups = await self._get(client, source, session, "/api/v1/groups/available")
        session, rates = await self._get(client, source, session, "/api/v1/groups/rates")
        rates = {str(key): value for key, value in (rates or {}).items()}
        counts = {}
        active_counts = {}
        for item in keys:
            group_id = item.get("group_id") if isinstance(item, dict) else None
            if group_id is None:
                continue
            key = str(group_id)
            counts[key] = counts.get(key, 0) + 1
            if item.get("status") == "active":
                active_counts[key] = active_counts.get(key, 0) + 1
        catalog = []
        model_cache = source.get("group_model_cache")
        model_cache = model_cache if isinstance(model_cache, dict) else {}
        for group in groups or []:
            if not isinstance(group, dict) or group.get("status") not in (None, "", "active"):
                continue
            group_id = group.get("id")
            key = str(group_id)
            capabilities = self.routing_capabilities(group)
            if key in model_cache and capabilities and isinstance(model_cache[key], list):
                capabilities["model_patterns"] = list(model_cache[key])
                capabilities["model_list_known"] = True
            catalog.append({
                "id": group_id, "name": group.get("name", ""),
                "platform": group.get("platform", ""),
                "allow_image_generation": bool(group.get("allow_image_generation")),
                "routing_capabilities": capabilities,
                "rate_multiplier": _number_text(rates.get(key, group.get("rate_multiplier"))),
                "key_count": counts.get(key, 0), "active_key_count": active_counts.get(key, 0),
            })
        return session, catalog

    async def create_keys(self, client, source, session, group_ids, only_missing=False, options=None):
        session, groups = await self.catalog(client, source, session)
        groups_by_id = {str(group["id"]): group for group in groups}
        selected = {str(group_id) for group_id in (group_ids or [])}
        if only_missing:
            targets = [group for group in groups if group["key_count"] == 0
                       and (not selected or str(group["id"]) in selected)]
        else:
            targets = [groups_by_id[group_id] for group_id in selected if group_id in groups_by_id]
        if not targets:
            raise PoolSyncError("没有需要创建 Key 的分组")
        # Names default to the upstream group name. A prefix remains an
        # optional adapter option for callers that explicitly need one.
        prefix = str((options or {}).get("name_prefix") or "").strip()[:40]
        delay_seconds = max(0.0, min(float((options or {}).get("delay_seconds", 1.5)), 60.0))
        progress = (options or {}).get("_progress")
        if progress:
            await progress(0, len(targets), 0, 0)
        created = []
        errors = []
        for index, group in enumerate(targets):
            if index:
                await asyncio.sleep(delay_seconds)
            name = f"{prefix}-{group['name']}"[:100] if prefix else str(group["name"])[:100]
            idempotency_key = f"pool-sync-key-{source['id']}-{group['id']}-{uuid.uuid4()}"
            try:
                session, item = await self._authorized_post(
                    client, source, session, "/api/v1/keys",
                    {"name": name, "group_id": group["id"]},
                    {"Idempotency-Key": idempotency_key},
                )
                created.append({
                    "group_id": group["id"], "group_name": group["name"],
                    "key_id": (item or {}).get("id"), "name": name,
                })
            except Exception as exc:
                errors.append({
                    "group_id": group["id"], "group_name": group["name"], "error": str(exc),
                })
            if progress:
                await progress(index + 1, len(targets), len(created), len(errors))
        return session, {"created": created, "errors": errors, "requested": len(targets)}

    async def delete_keys(self, client, source, session, group_ids, options=None):
        selected = {str(group_id) for group_id in (group_ids or [])}
        if not selected:
            raise PoolSyncError("没有选择要清空的分组")
        session, keys = await self._fetch_all_keys(client, source, session)
        targets = [item for item in keys if isinstance(item, dict)
                   and str(item.get("group_id")) in selected and item.get("id") is not None]
        deleted = []
        errors = []
        for item in targets:
            key_id = item["id"]
            try:
                session, _ = await self._authorized_delete(
                    client, source, session, f"/api/v1/keys/{key_id}"
                )
                deleted.append({"key_id": key_id, "group_id": item.get("group_id"),
                                "name": item.get("name", "")})
            except Exception as exc:
                errors.append({"key_id": key_id, "group_id": item.get("group_id"),
                               "name": item.get("name", ""), "error": str(exc)})
        return session, {"deleted": deleted, "errors": errors, "requested": len(targets)}

    async def disconnect(self, client, source, session):
        refresh_token = session.get("refresh_token", "")
        if refresh_token:
            await self._post(client, source, "/api/v1/auth/logout", {
                "refresh_token": refresh_token,
            })

    def connected(self, session):
        return bool(session.get("refresh_token"))

    def public_session(self, session):
        return {"email": session.get("email", "")}
