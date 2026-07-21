import asyncio
import uuid

from .base import PoolSyncAdapter, PoolSyncError


def _unwrap(response):
    if response.status_code == 204:
        return {}
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise PoolSyncError(f"上游返回了非 JSON 响应 (HTTP {response.status_code})") from exc
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

    async def _post(self, client, source, path, body):
        response = await client.post(source["base_url"] + path, json=body, timeout=20)
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
            headers={"Authorization": f"Bearer {session['access_token']}"}, timeout=20,
        )
        if response.status_code == 401 and retry:
            session["access_token"] = ""
            session = await self._refresh(client, source, session)
            return await self._get(client, source, session, path, params, retry=False)
        return session, _unwrap(response)

    async def _authorized_post(self, client, source, session, path, body, headers=None, retry=True):
        if not session.get("access_token"):
            session = await self._refresh(client, source, session)
        request_headers = {"Authorization": f"Bearer {session['access_token']}"}
        request_headers.update(headers or {})
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
            headers={"Authorization": f"Bearer {session['access_token']}"}, timeout=20,
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
        for group in groups or []:
            if not isinstance(group, dict) or group.get("status") not in (None, "", "active"):
                continue
            group_id = group.get("id")
            key = str(group_id)
            catalog.append({
                "id": group_id, "name": group.get("name", ""),
                "platform": group.get("platform", ""),
                "allow_image_generation": bool(group.get("allow_image_generation")),
                "routing_capabilities": self.routing_capabilities(group),
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
