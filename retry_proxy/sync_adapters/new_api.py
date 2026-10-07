import asyncio
import fnmatch

from ..config import logger
from .base import PoolSyncAdapter, PoolSyncError, request_with_retry


def _response_error_message(response):
    status = response.status_code
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    server = response.headers.get("server", "").lower()
    behind_cloudflare = bool(response.headers.get("cf-ray")) or "cloudflare" in server
    if status == 403 and behind_cloudflare:
        return (
            "请求被 Cloudflare/CDN 拦截 (HTTP 403)；请确认填写的是站点根地址，"
            "并检查来源 IP、CDN/WAF 规则及人机验证设置"
        )
    if content_type == "text/html":
        return (
            f"上游返回了 HTML 而不是 API JSON (HTTP {status})；"
            "请确认填写的是 New API 站点根地址"
        )
    return f"上游返回了非 JSON 响应 (HTTP {status})"


def _unwrap(response):
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise PoolSyncError(_response_error_message(response)) from exc
    if response.status_code >= 400:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise PoolSyncError(message or f"上游请求失败 (HTTP {response.status_code})")
    if isinstance(payload, dict) and payload.get("success") is False:
        raise PoolSyncError(payload.get("message") or "New API 返回操作失败")
    if isinstance(payload, dict) and "success" in payload:
        return payload.get("data")
    return payload


def _is_auth_failure(response):
    if response.status_code == 401:
        return True
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or payload.get("success") is not False:
        return False
    message = str(payload.get("message") or "").lower()
    return any(marker in message for marker in (
        "登录状态", "重新登录", "login expired", "login required", "unauthorized",
    ))


def _number_text(value):
    try:
        return format(float(value), ".12g")
    except (TypeError, ValueError):
        return ""


def _cookie_header(cookies):
    return "; ".join(
        f"{name}={value}" for name, value in (cookies or {}).items()
        if name and value
    )


def _copy_session(session):
    copied = dict(session or {})
    copied["cookies"] = dict(copied.get("cookies") or {})
    return copied


def _token_group(item, default_group="default"):
    return str(item.get("group") or default_group or "default").strip() or "default"


def _model_limits(item):
    if not item.get("model_limits_enabled"):
        return []
    raw = item.get("model_limits") or ""
    values = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


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


def _models_allowed_by_limits(models, limits):
    patterns = [str(value).strip().lower() for value in limits if str(value).strip()]
    return [
        model for model in models
        if any(fnmatch.fnmatchcase(model.lower(), pattern) for pattern in patterns)
    ]


class NewAPIAdapter(PoolSyncAdapter):
    name = "newapi"
    label = "New API"
    credential_fields = [
        {"name": "username", "label": "用户名或邮箱", "type": "text"},
        {"name": "password", "label": "登录密码", "type": "password"},
    ]
    capabilities = ["group_catalog", "create_keys", "delete_keys"]

    @staticmethod
    def _headers(source, session, extra=None, include_access=True):
        base_url = source["base_url"].rstrip("/")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": base_url,
            "Referer": base_url + "/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        }
        if include_access and session.get("access_token"):
            headers["Authorization"] = f"Bearer {session['access_token']}"
        if session.get("user_id") not in (None, ""):
            # Older New API/One API releases require this header with the
            # dashboard session cookie. Current releases safely ignore it.
            headers["New-Api-User"] = str(session["user_id"])
        cookie = _cookie_header(session.get("cookies"))
        if cookie:
            headers["Cookie"] = cookie
        if session.get("session_id"):
            headers["X-Auth-Session"] = str(session["session_id"])
        headers.update(extra or {})
        return headers

    @staticmethod
    def _merge_response_cookies(session, response):
        cookies = dict(session.get("cookies") or {})
        for name, value in response.cookies.items():
            if value:
                cookies[name] = value
            else:
                cookies.pop(name, None)
        session["cookies"] = cookies
        return session

    async def connect(self, client, source, credentials):
        username = str(credentials.get("username") or "").strip()
        password = credentials.get("password") or ""
        if not username or not password:
            raise PoolSyncError("用户名和密码不能为空")
        response = await client.post(
            source["base_url"] + "/api/user/login",
            json={"username": username, "password": password},
            headers=self._headers(source, {}), timeout=20,
        )
        data = _unwrap(response)
        if not isinstance(data, dict):
            raise PoolSyncError("登录响应格式无法识别")
        if data.get("require_2fa") or data.get("requires_2fa"):
            raise PoolSyncError("该账号启用了两步验证，当前适配器暂不支持")
        user = data.get("user") if isinstance(data.get("user"), dict) else data
        login_session = data.get("session") if isinstance(data.get("session"), dict) else {}
        session = {
            "username": str(user.get("username") or username),
            "email": str(user.get("email") or ""),
            "user_id": user.get("id"),
            "user_group": str(user.get("group") or "default"),
            "session_id": login_session.get("sid", ""),
            "access_token": str(data.get("access_token") or ""),
            "cookies": {},
        }
        self._merge_response_cookies(session, response)
        if not session["access_token"] and not session["cookies"]:
            raise PoolSyncError("登录响应缺少可用的访问令牌或会话 Cookie")
        if not session["cookies"].get("new_api_refresh"):
            session["password"] = password
        return session

    async def _refresh(self, client, source, session):
        cookies = session.get("cookies") or {}
        if not cookies.get("new_api_refresh"):
            raise PoolSyncError("该连接需要重新登录")
        response = await client.post(
            source["base_url"] + "/api/user/auth/refresh", json={},
            headers=self._headers(source, session, include_access=False), timeout=20,
        )
        data = _unwrap(response)
        if not isinstance(data, dict) or not data.get("access_token"):
            raise PoolSyncError("刷新会话响应缺少 access_token")
        session["access_token"] = str(data["access_token"])
        login_session = data.get("session")
        if isinstance(login_session, dict) and login_session.get("sid"):
            session["session_id"] = str(login_session["sid"])
        user = data.get("user")
        if isinstance(user, dict):
            session["username"] = str(user.get("username") or session.get("username") or "")
            session["email"] = str(user.get("email") or session.get("email") or "")
            session["user_id"] = user.get("id", session.get("user_id"))
            session["user_group"] = str(
                user.get("group") or session.get("user_group") or "default"
            )
        self._merge_response_cookies(session, response)
        return session

    async def _request(self, client, source, session, method, path, *, params=None,
                       body=None, retry=True, sender=None, preserve_envelope=False):
        session = _copy_session(session)
        if not session.get("access_token") and (session.get("cookies") or {}).get(
                "new_api_refresh"):
            session = await self._refresh(client, source, session)
        send = sender or request_with_retry
        response = await send(
            client, method, source["base_url"] + path, params=params, json=body,
            headers=self._headers(source, session), timeout=20,
        )
        self._merge_response_cookies(session, response)
        if retry and _is_auth_failure(response):
            if (session.get("cookies") or {}).get("new_api_refresh"):
                refreshed = _copy_session(session)
                refreshed["access_token"] = ""
                refreshed = await self._refresh(client, source, refreshed)
                return await self._request(
                    client, source, refreshed, method, path, params=params, body=body,
                    retry=False, sender=sender, preserve_envelope=preserve_envelope,
                )
            if session.get("access_token") and session.get("cookies"):
                # Some New API forks send a short-lived bearer together with a
                # longer-lived legacy session cookie. Retry without the expired
                # bearer so the server can authenticate the cookie instead.
                cookie_session = _copy_session(session)
                cookie_session["access_token"] = ""
                cookie_response = await send(
                    client, method, source["base_url"] + path, params=params, json=body,
                    headers=self._headers(source, cookie_session), timeout=20,
                )
                self._merge_response_cookies(cookie_session, cookie_response)
                if not _is_auth_failure(cookie_response):
                    data = _unwrap(cookie_response)
                    return cookie_session, cookie_response.json() if preserve_envelope else data
            username = str(session.get("username") or "").strip()
            password = session.get("password") or ""
            if username and password:
                relogged = await self.connect(
                    client, source, {"username": username, "password": password},
                )
                return await self._request(
                    client, source, relogged, method, path, params=params, body=body,
                    retry=False, sender=sender, preserve_envelope=preserve_envelope,
                )
        data = _unwrap(response)
        return session, response.json() if preserve_envelope else data

    async def _fetch_all_tokens(self, client, source, session):
        items = []
        page = 1
        page_size = 100
        previous_page_ids = None
        while True:
            session, data = await self._request(
                client, source, session, "GET", "/api/token/",
                params={"p": page, "size": page_size, "page_size": page_size},
            )
            if isinstance(data, list):
                return session, data
            elif isinstance(data, dict):
                batch = data.get("items") or data.get("data") or data.get("list") or []
                try:
                    total = int(data["total"]) if data.get("total") is not None else None
                except (TypeError, ValueError):
                    total = None
            else:
                raise PoolSyncError("Token 列表响应格式无法识别")
            if not isinstance(batch, list):
                raise PoolSyncError("Token 列表响应格式无法识别")
            page_ids = tuple(
                str(item.get("id")) if isinstance(item, dict) and item.get("id") is not None
                else repr(item)
                for item in batch
            )
            if batch and page_ids == previous_page_ids:
                raise PoolSyncError("Token 列表分页重复，上游可能不支持分页参数")
            previous_page_ids = page_ids
            items.extend(batch)
            if (not batch or (total is not None and len(items) >= total)
                    or (total is None and len(batch) < page_size)):
                return session, items
            if page >= 1000:
                raise PoolSyncError("Token 列表分页超过安全上限")
            page += 1

    async def _fill_full_keys(self, client, source, session, tokens):
        missing = [
            item for item in tokens
            if isinstance(item, dict) and item.get("id") is not None
            and (not item.get("key") or "*" in str(item.get("key")))
        ]
        for offset in range(0, len(missing), 100):
            batch = missing[offset:offset + 100]
            ids = [item["id"] for item in batch]
            session, data = await self._request(
                client, source, session, "POST", "/api/token/batch/keys",
                body={"ids": ids},
            )
            keys = data.get("keys") if isinstance(data, dict) else None
            if not isinstance(keys, dict):
                raise PoolSyncError("完整 Token Key 响应格式无法识别")
            for item in batch:
                key = keys.get(str(item["id"]), keys.get(item["id"]))
                if key:
                    item["key"] = str(key)
        unresolved = [item.get("id") for item in missing if "*" in str(item.get("key") or "")]
        unresolved.extend(item.get("id") for item in missing if not item.get("key"))
        if unresolved:
            raise PoolSyncError("完整 Token Key 读取结果不完整，请重新登录后重试")
        return session, tokens

    async def _fetch_groups(self, client, source, session):
        try:
            return await self._request(
                client, source, session, "GET", "/api/user/self/groups",
            )
        except PoolSyncError:
            return await self._request(
                client, source, session, "GET", "/api/user/groups",
            )

    async def _get_group_models(self, client, source, api_key):
        response = await request_with_retry(
            client, "GET", source["base_url"] + "/v1/models",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=20,
        )
        return _model_ids(_unwrap(response))

    async def _apply_group_model_cache(self, client, source, entries):
        raw_cache = source.get("group_model_cache")
        cache = raw_cache if isinstance(raw_cache, dict) else {}
        cache = {
            str(group_id): [str(model) for model in models]
            for group_id, models in cache.items()
            if isinstance(models, list)
        }
        source["group_model_cache"] = cache

        # Restricted Tokens may receive an already filtered /v1/models result,
        # so only an unrestricted Token can represent the whole group.
        representatives = {}
        for item in entries:
            group_id = str(item.get("group_id") or "")
            capabilities = item.get("routing_capabilities") or {}
            if group_id and not capabilities.get("model_list_known"):
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
                    "New API 分组模型列表读取失败: upstream=%s group=%s error=%s",
                    source["base_url"], item.get("group_name") or group_id, exc,
                )

        for item in entries:
            group_id = str(item.get("group_id") or "")
            if group_id not in cache:
                continue
            capabilities = dict(item.get("routing_capabilities") or {})
            models = list(cache[group_id])
            if capabilities.get("model_list_known"):
                models = _models_allowed_by_limits(
                    models, capabilities.get("model_patterns") or [],
                )
            capabilities["model_patterns"] = models
            capabilities["model_list_known"] = True
            item["routing_capabilities"] = capabilities
        return entries

    @staticmethod
    def _group_catalog(groups, tokens, default_group="default"):
        counts = {}
        active_counts = {}
        for item in tokens:
            if not isinstance(item, dict):
                continue
            group_id = _token_group(item, default_group)
            counts[group_id] = counts.get(group_id, 0) + 1
            if item.get("status") in (None, 1, "1", "enabled", "active"):
                active_counts[group_id] = active_counts.get(group_id, 0) + 1
        catalog = []
        for group_id, metadata in (groups or {}).items():
            metadata = metadata if isinstance(metadata, dict) else {}
            catalog.append({
                "id": str(group_id), "name": str(group_id),
                "platform": "", "allow_image_generation": None,
                "routing_capabilities": {},
                "rate_multiplier": _number_text(metadata.get("ratio")),
                "key_count": counts.get(str(group_id), 0),
                "active_key_count": active_counts.get(str(group_id), 0),
            })
        return catalog

    async def fetch(self, client, source, session):
        session, tokens = await self._fetch_all_tokens(client, source, session)
        session, tokens = await self._fill_full_keys(client, source, session, tokens)
        session, groups = await self._fetch_groups(client, source, session)
        groups = groups if isinstance(groups, dict) else {}
        invalid_tokens = [
            item for item in tokens
            if isinstance(item, dict) and item.get("id") is not None
            and _token_group(item, session.get("user_group")) not in groups
        ]
        deleted_invalid = 0
        for item in invalid_tokens:
            try:
                session, _ = await self._request(
                    client, source, session, "DELETE", f"/api/token/{item['id']}",
                )
                deleted_invalid += 1
            except Exception as exc:
                logger.warning(
                    f"失效分组 Key 自动删除失败: upstream={source['base_url']} "
                    f"source_key_id={item['id']} error={exc}"
                )
        if deleted_invalid:
            logger.info(
                f"失效分组 Key 已自动删除: upstream={source['base_url']} "
                f"count={deleted_invalid}"
            )
        invalid_ids = {str(item["id"]) for item in invalid_tokens}
        tokens = [item for item in tokens if str(item.get("id")) not in invalid_ids]
        entries = []
        for item in tokens:
            if not isinstance(item, dict) or item.get("status") not in (
                    None, 1, "1", "enabled", "active") or not item.get("key"):
                continue
            group_id = _token_group(item, session.get("user_group"))
            metadata = groups.get(group_id)
            metadata = metadata if isinstance(metadata, dict) else {}
            group_name = str(group_id)
            key_name = str(item.get("name") or "").strip()
            label = key_name or group_name
            if key_name and group_name.lower() not in key_name.lower():
                label = f"{key_name}-{group_name}"
            model_limits = _model_limits(item)
            capabilities = {}
            if model_limits:
                capabilities = {
                    "model_patterns": model_limits,
                    "model_list_known": True,
                }
            entries.append({
                "source_key_id": item.get("id"), "key": str(item["key"]),
                "label": label, "sort": _number_text(metadata.get("ratio")),
                "key_name": key_name, "group_id": group_id,
                "group_name": group_name, "platform": "",
                "allow_image_generation": None,
                "routing_capabilities": capabilities,
            })
        await self._apply_group_model_cache(client, source, entries)
        return session, entries

    async def catalog(self, client, source, session):
        session, tokens = await self._fetch_all_tokens(client, source, session)
        session, groups = await self._fetch_groups(client, source, session)
        groups = groups if isinstance(groups, dict) else {}
        catalog = self._group_catalog(groups, tokens, session.get("user_group"))
        model_cache = source.get("group_model_cache")
        model_cache = model_cache if isinstance(model_cache, dict) else {}
        for group in catalog:
            models = model_cache.get(str(group.get("id")))
            if isinstance(models, list):
                group["routing_capabilities"] = {
                    "model_patterns": list(models),
                    "model_list_known": True,
                }
        return session, catalog

    async def create_keys(self, client, source, session, group_ids, only_missing=False,
                          options=None):
        session, groups = await self.catalog(client, source, session)
        selected = {str(group_id) for group_id in (group_ids or [])}
        if only_missing:
            targets = [group for group in groups if group["key_count"] == 0
                       and (not selected or str(group["id"]) in selected)]
        else:
            targets = [group for group in groups if str(group["id"]) in selected]
        if not targets:
            raise PoolSyncError("没有需要创建 Token 的分组")
        prefix = str((options or {}).get("name_prefix") or "").strip()[:30]
        delay_seconds = max(0.0, min(float((options or {}).get("delay_seconds", 1.5)), 60.0))
        progress = (options or {}).get("_progress")
        if progress:
            await progress(0, len(targets), 0, 0)
        created = []
        errors = []
        for index, group in enumerate(targets):
            if index:
                await asyncio.sleep(delay_seconds)
            # Catalog names may be long descriptions. New API's Go len check
            # limits token names to 50 UTF-8 bytes, not Python characters.
            name = f"{prefix}-{group['id']}" if prefix else str(group["id"])
            name = name.encode("utf-8")[:50].decode("utf-8", errors="ignore")
            try:
                session, _ = await self._request(
                    client, source, session, "POST", "/api/token/", body={
                        "name": name, "group": group["id"], "expired_time": -1,
                        "unlimited_quota": True, "remain_quota": 0,
                        "model_limits_enabled": False, "model_limits": "",
                        "allow_ips": "", "cross_group_retry": False,
                    },
                )
                created.append({
                    "group_id": group["id"], "group_name": group["name"],
                    "key_id": None, "name": name,
                })
            except Exception as exc:
                errors.append({
                    "group_id": group["id"], "group_name": group["name"],
                    "error": str(exc),
                })
            if progress:
                await progress(index + 1, len(targets), len(created), len(errors))
        return session, {"created": created, "errors": errors, "requested": len(targets)}

    async def delete_keys(self, client, source, session, group_ids, options=None):
        selected = {str(group_id) for group_id in (group_ids or [])}
        if not selected:
            raise PoolSyncError("没有选择要清空的分组")
        session, tokens = await self._fetch_all_tokens(client, source, session)
        default_group = session.get("user_group")
        targets = [item for item in tokens if isinstance(item, dict)
                   and _token_group(item, default_group) in selected
                   and item.get("id") is not None]
        deleted = []
        errors = []
        for item in targets:
            try:
                session, _ = await self._request(
                    client, source, session, "DELETE", f"/api/token/{item['id']}",
                )
                deleted.append({
                    "key_id": item["id"], "group_id": _token_group(item, default_group),
                    "name": item.get("name", ""),
                })
            except Exception as exc:
                errors.append({
                    "key_id": item["id"], "group_id": _token_group(item, default_group),
                    "name": item.get("name", ""), "error": str(exc),
                })
        return session, {"deleted": deleted, "errors": errors, "requested": len(targets)}

    async def disconnect(self, client, source, session):
        if not session:
            return
        response = await client.post(
            source["base_url"] + "/api/user/auth/logout", json={},
            headers=self._headers(source, session), timeout=20,
        )
        _unwrap(response)

    def connected(self, session):
        return bool(session.get("access_token") or session.get("cookies"))

    def public_session(self, session):
        return {
            "email": session.get("email") or session.get("username", ""),
            "username": session.get("username", ""),
        }

    def persistent_session(self, session):
        persisted = _copy_session(session)
        if persisted["cookies"].get("new_api_refresh"):
            persisted.pop("access_token", None)
            persisted.pop("password", None)
        return persisted
