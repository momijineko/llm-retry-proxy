import json
import unittest

import httpx

from retry_proxy.pool_sync import PoolSyncManager
from retry_proxy.sync_adapters import ADAPTERS, PoolSyncError
from retry_proxy.sync_adapters.base import request_with_retry
from retry_proxy.sync_adapters.new_api import NewAPIAdapter, _model_ids, _unwrap


def api_response(data=None, *, success=True, status=200, headers=None):
    return httpx.Response(
        status, json={"success": success, "message": "" if success else "failed", "data": data},
        headers=headers,
    )


class NewAPIAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_adapter_is_registered(self):
        self.assertIsInstance(ADAPTERS["newapi"], NewAPIAdapter)

    def test_persistent_session_keeps_bearer_only_without_refresh_cookie(self):
        adapter = NewAPIAdapter()
        legacy = adapter.persistent_session({
            "access_token": "legacy-access", "password": "legacy-password",
            "cookies": {"session": "legacy-session"},
        })
        current = adapter.persistent_session({
            "access_token": "short-access", "password": "unused-password",
            "cookies": {"new_api_refresh": "refresh-cookie"},
        })

        self.assertEqual(legacy["access_token"], "legacy-access")
        self.assertEqual(legacy["password"], "legacy-password")
        self.assertNotIn("access_token", current)
        self.assertNotIn("password", current)
        self.assertEqual(current["cookies"]["new_api_refresh"], "refresh-cookie")

    def test_pool_sync_state_keeps_legacy_new_api_bearer(self):
        adapter = NewAPIAdapter()
        manager = PoolSyncManager({}, config=None, adapters={"newapi": adapter})
        manager.sources = {"source": {
            "id": "source", "adapter": "newapi", "base_url": "https://legacy.test",
            "session": {
                "access_token": "persistent-access",
                "password": "persistent-password",
                "cookies": {"session": "persistent-session"},
            },
        }}

        persisted = manager._persistent_sources()[0]["session"]

        self.assertEqual(persisted["access_token"], "persistent-access")
        self.assertEqual(persisted["password"], "persistent-password")
        self.assertEqual(persisted["cookies"]["session"], "persistent-session")

    def test_non_json_cloudflare_response_does_not_expose_body(self):
        response = httpx.Response(
            403, text="<html>secret response body</html>",
            headers={"content-type": "text/html", "server": "cloudflare", "cf-ray": "ray"},
        )

        with self.assertRaises(PoolSyncError) as raised:
            _unwrap(response)

        self.assertIn("Cloudflare/CDN", str(raised.exception))
        self.assertNotIn("secret response body", str(raised.exception))

    def test_model_ids_parse_openai_and_gemini_shapes(self):
        self.assertEqual(_model_ids({"data": [
            {"id": "gpt-5.4"}, {"id": "gpt-5.4"},
        ]}), ["gpt-5.4"])
        self.assertEqual(_model_ids({"models": [
            {"name": "models/gemini-2.5-pro"},
        ]}), ["gemini-2.5-pro"])

    async def test_connect_and_fetch_secure_masked_tokens(self):
        calls = []

        async def handler(request):
            calls.append(request)
            if request.url.path == "/api/user/login":
                self.assertTrue(request.headers["user-agent"].startswith("Mozilla/5.0"))
                self.assertEqual(request.headers["origin"], "https://new-api.test")
                self.assertEqual(request.headers["referer"], "https://new-api.test/")
                self.assertEqual(json.loads(request.content), {
                    "username": "user@example.com", "password": "secret",
                })
                return api_response({
                    "access_token": "access-1",
                    "session": {"sid": "sid-1"},
                    "user": {
                        "id": 7, "username": "tester", "email": "user@example.com",
                        "group": "vip",
                    },
                }, headers={
                    "set-cookie": "new_api_refresh=refresh-1; Path=/api/user/auth; HttpOnly; Secure",
                })
            if request.url.path == "/api/token/" and request.method == "GET":
                self.assertEqual(request.headers["authorization"], "Bearer access-1")
                return api_response({
                    "page": 1, "page_size": 100, "total": 2,
                    "items": [
                        {
                            "id": 11, "key": "sk-a**********1234", "name": "coding",
                            "status": 1, "group": "", "model_limits_enabled": True,
                            "model_limits": "gpt-5.4, claude-sonnet-4-5",
                        },
                        {"id": 12, "key": "sk-b**********5678", "status": 2},
                    ],
                })
            if request.url.path == "/api/token/batch/keys":
                self.assertEqual(json.loads(request.content), {"ids": [11, 12]})
                return api_response({"keys": {
                    "11": "sk-full-one", "12": "sk-disabled",
                }})
            if request.url.path == "/api/user/self/groups":
                return api_response({
                    "default": {"ratio": 1, "desc": "默认分组"},
                    "vip": {"ratio": 0.25, "desc": "VIP"},
                })
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = NewAPIAdapter()
            source = {"base_url": "https://new-api.test"}
            session = await adapter.connect(client, source, {
                "username": "user@example.com", "password": "secret",
            })
            session, entries = await adapter.fetch(client, source, session)

        self.assertEqual(session["cookies"]["new_api_refresh"], "refresh-1")
        self.assertEqual(session["session_id"], "sid-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["key"], "sk-full-one")
        self.assertEqual(entries[0]["label"], "coding-vip")
        self.assertEqual(entries[0]["group_name"], "vip")
        self.assertEqual(entries[0]["sort"], "0.25")
        self.assertEqual(entries[0]["group_id"], "vip")
        self.assertEqual(entries[0]["routing_capabilities"], {
            "model_patterns": ["gpt-5.4", "claude-sonnet-4-5"],
            "model_list_known": True,
        })
        self.assertEqual(len(calls), 4)

    async def test_fetch_probes_and_caches_unrestricted_group_models(self):
        model_requests = []

        async def handler(request):
            if request.url.path == "/api/token/":
                return api_response({"items": [
                    {
                        "id": 1, "key": "sk-unrestricted", "name": "all",
                        "status": 1, "group": "vip",
                    },
                    {
                        "id": 2, "key": "sk-limited", "name": "mini",
                        "status": 1, "group": "vip", "model_limits_enabled": True,
                        "model_limits": "gpt-5.4-mini, missing-model",
                    },
                    {
                        "id": 3, "key": "sk-limited-only", "name": "claude",
                        "status": 1, "group": "limited", "model_limits_enabled": True,
                        "model_limits": "claude-*",
                    },
                ], "total": 3})
            if request.url.path == "/api/user/self/groups":
                return api_response({
                    "vip": {"ratio": 0.5, "desc": "VIP"},
                    "limited": {"ratio": 1, "desc": "受限"},
                })
            if request.url.path == "/v1/models":
                model_requests.append(request)
                self.assertEqual(
                    request.headers["authorization"], "Bearer sk-unrestricted",
                )
                return httpx.Response(200, json={"object": "list", "data": [
                    {"id": "gpt-5.4"}, {"id": "gpt-5.4-mini"},
                    {"id": "gpt-image-1"},
                ]})
            raise AssertionError(request.url)

        source = {"base_url": "https://new-api.test"}
        session = {"access_token": "access-1"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = NewAPIAdapter()
            _, first = await adapter.fetch(client, source, session)
            _, second = await adapter.fetch(client, source, session)
            _, catalog = await adapter.catalog(client, source, session)

        self.assertEqual(len(model_requests), 1)
        self.assertEqual(source["group_model_cache"]["vip"], [
            "gpt-5.4", "gpt-5.4-mini", "gpt-image-1",
        ])
        self.assertNotIn("limited", source["group_model_cache"])
        self.assertEqual(first[0]["routing_capabilities"]["model_patterns"], [
            "gpt-5.4", "gpt-5.4-mini", "gpt-image-1",
        ])
        self.assertEqual(first[1]["routing_capabilities"]["model_patterns"], [
            "gpt-5.4-mini",
        ])
        self.assertEqual(first[2]["routing_capabilities"]["model_patterns"], [
            "claude-*",
        ])
        self.assertEqual(
            first[0]["routing_capabilities"], second[0]["routing_capabilities"],
        )
        vip = next(group for group in catalog if group["id"] == "vip")
        self.assertIsNone(vip["allow_image_generation"])
        self.assertEqual(vip["routing_capabilities"]["model_patterns"], [
            "gpt-5.4", "gpt-5.4-mini", "gpt-image-1",
        ])

    async def test_restored_session_refreshes_before_fetch(self):
        calls = []

        async def handler(request):
            calls.append(request)
            if request.url.path == "/api/user/auth/refresh":
                self.assertIn("new_api_refresh=refresh-1", request.headers["cookie"])
                self.assertEqual(request.headers["x-auth-session"], "sid-1")
                return api_response({
                    "access_token": "access-2", "session": {"sid": "sid-1"},
                    "user": {"id": 7, "username": "tester"},
                }, headers={
                    "set-cookie": "new_api_refresh=refresh-2; Path=/api/user/auth; HttpOnly; Secure",
                })
            if request.url.path == "/api/token/":
                self.assertEqual(request.headers["authorization"], "Bearer access-2")
                return api_response({"items": [], "total": 0})
            if request.url.path == "/api/user/self/groups":
                return api_response({})
            raise AssertionError(request.url)

        session = {
            "username": "tester", "user_id": 7, "session_id": "sid-1",
            "cookies": {"new_api_refresh": "refresh-1"},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            session, entries = await NewAPIAdapter().fetch(
                client, {"base_url": "https://new-api.test"}, session,
            )

        self.assertEqual(entries, [])
        self.assertEqual(session["access_token"], "access-2")
        self.assertEqual(session["cookies"]["new_api_refresh"], "refresh-2")
        self.assertEqual([request.url.path for request in calls], [
            "/api/user/auth/refresh", "/api/token/", "/api/user/self/groups",
        ])

    async def test_expired_bearer_falls_back_to_legacy_cookie_session(self):
        token_requests = []

        async def handler(request):
            if request.url.path == "/api/token/":
                token_requests.append(request)
                if len(token_requests) == 1:
                    self.assertEqual(request.headers["authorization"], "Bearer expired")
                    return httpx.Response(200, json={
                        "success": False,
                        "message": "登录状态已失效，请重新登录",
                        "data": None,
                    })
                self.assertNotIn("authorization", request.headers)
                self.assertIn("session=legacy-cookie", request.headers["cookie"])
                self.assertEqual(request.headers["new-api-user"], "9")
                return api_response({"items": [], "total": 0})
            if request.url.path == "/api/user/self/groups":
                self.assertNotIn("authorization", request.headers)
                return api_response({})
            raise AssertionError(request.url)

        session = {
            "access_token": "expired", "user_id": 9,
            "cookies": {"session": "legacy-cookie"},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            session, entries = await NewAPIAdapter().fetch(
                client, {"base_url": "https://legacy.test"}, session,
            )

        self.assertEqual(entries, [])
        self.assertEqual(session["access_token"], "")
        self.assertEqual(len(token_requests), 2)

    async def test_failed_cookie_fallback_relogs_in_without_mutating_original_session(self):
        calls = []

        async def handler(request):
            calls.append(request)
            if request.url.path == "/api/user/login":
                return api_response({
                    "access_token": "fresh-access",
                    "user": {"id": 9, "username": "legacy"},
                }, headers={"set-cookie": "session=fresh-session; Path=/; HttpOnly"})
            if request.url.path == "/api/token/":
                authorization = request.headers.get("authorization")
                cookie = request.headers.get("cookie", "")
                if authorization == "Bearer fresh-access" and "session=fresh-session" in cookie:
                    return api_response({"items": [], "total": 0})
                return httpx.Response(200, json={
                    "success": False,
                    "message": "登录状态已失效，请重新登录",
                    "data": None,
                })
            if request.url.path == "/api/user/self/groups":
                return api_response({})
            raise AssertionError(request.url)

        original = {
            "username": "legacy", "password": "secret", "user_id": 9,
            "access_token": "expired", "cookies": {"session": "expired-session"},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            session, entries = await NewAPIAdapter().fetch(
                client, {"base_url": "https://legacy.test"}, original,
            )

        self.assertEqual(entries, [])
        self.assertEqual(original["access_token"], "expired")
        self.assertEqual(original["cookies"], {"session": "expired-session"})
        self.assertEqual(session["access_token"], "fresh-access")
        self.assertEqual(session["password"], "secret")
        self.assertEqual([request.url.path for request in calls[:4]], [
            "/api/token/", "/api/token/", "/api/user/login", "/api/token/",
        ])

    async def test_failed_cookie_fallback_does_not_mutate_session_without_password(self):
        async def handler(request):
            return httpx.Response(200, json={
                "success": False,
                "message": "登录状态已失效，请重新登录",
                "data": None,
            })

        original = {
            "username": "legacy", "user_id": 9, "access_token": "expired",
            "cookies": {"session": "expired-session"},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(PoolSyncError, "登录状态已失效"):
                await NewAPIAdapter().fetch(
                    client, {"base_url": "https://legacy.test"}, original,
                )

        self.assertEqual(original["access_token"], "expired")
        self.assertEqual(original["cookies"], {"session": "expired-session"})

    async def test_token_pagination_supports_size_and_short_server_pages(self):
        pages = []

        async def handler(request):
            self.assertEqual(request.url.path, "/api/token/")
            page = int(request.url.params["p"])
            pages.append(page)
            self.assertEqual(request.url.params["size"], "100")
            self.assertEqual(request.url.params["page_size"], "100")
            start = (page - 1) * 10
            count = 10 if page == 1 else 5
            return api_response({
                "items": [{"id": index, "key": f"sk-{index}"}
                          for index in range(start, start + count)],
                "total": 15,
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            _, tokens = await NewAPIAdapter()._fetch_all_tokens(
                client,
                {"base_url": "https://legacy.test"},
                {"cookies": {"session": "legacy-cookie"}},
            )

        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(tokens), 15)

    async def test_top_level_token_list_of_exact_page_size_is_not_paginated(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return api_response([{"id": index, "key": f"sk-{index}"}
                                 for index in range(100)])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            _, tokens = await NewAPIAdapter()._fetch_all_tokens(
                client, {"base_url": "https://legacy.test"},
                {"cookies": {"session": "legacy-cookie"}},
            )

        self.assertEqual(calls, 1)
        self.assertEqual(len(tokens), 100)

    async def test_repeated_token_page_is_rejected(self):
        async def handler(request):
            return api_response({
                "items": [{"id": index, "key": f"sk-{index}"}
                          for index in range(100)],
                "total": 200,
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(PoolSyncError, "分页重复"):
                await NewAPIAdapter()._fetch_all_tokens(
                    client, {"base_url": "https://legacy.test"},
                    {"cookies": {"session": "legacy-cookie"}},
                )

    async def test_legacy_cookie_session_and_full_token_list_are_supported(self):
        async def handler(request):
            if request.url.path == "/api/user/login":
                return api_response(
                    {"id": 9, "username": "legacy", "email": "legacy@example.com"},
                    headers={"set-cookie": "session=legacy-cookie; Path=/; HttpOnly"},
                )
            if request.url.path == "/api/token/":
                self.assertEqual(request.headers["new-api-user"], "9")
                self.assertIn("session=legacy-cookie", request.headers["cookie"])
                return api_response({"items": [{
                    "id": 21, "key": "sk-legacy-full", "name": "legacy-key",
                    "status": 1, "group": "default",
                }], "total": 1})
            if request.url.path == "/api/user/self/groups":
                return api_response(None, success=False, status=404)
            if request.url.path == "/api/user/groups":
                return api_response({"default": {"ratio": 1, "desc": "默认分组"}})
            if request.url.path == "/v1/models":
                self.assertEqual(
                    request.headers["authorization"], "Bearer sk-legacy-full",
                )
                return httpx.Response(200, json={
                    "object": "list", "data": [{"id": "gpt-4o-mini"}],
                })
            raise AssertionError(request.url)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = NewAPIAdapter()
            session = await adapter.connect(client, {"base_url": "https://legacy.test"}, {
                "username": "legacy", "password": "secret",
            })
            session, entries = await adapter.fetch(
                client, {"base_url": "https://legacy.test"}, session,
            )

        self.assertEqual(session["cookies"], {"session": "legacy-cookie"})
        self.assertEqual(entries[0]["key"], "sk-legacy-full")
        self.assertEqual(entries[0]["sort"], "1")
        self.assertEqual(entries[0]["routing_capabilities"], {
            "model_patterns": ["gpt-4o-mini"], "model_list_known": True,
        })

    async def test_catalog_create_and_delete_tokens_by_group(self):
        created_bodies = []
        deleted_paths = []

        async def handler(request):
            if request.url.path == "/api/token/" and request.method == "GET":
                return api_response({"items": [{
                    "id": 31, "key": "sk-p**********0001", "name": "paid-key",
                    "status": 1, "group": "paid",
                }], "total": 1})
            if request.url.path == "/api/user/self/groups":
                return api_response({
                    "default": {"ratio": 1, "desc": "默认分组"},
                    "paid": {"ratio": 2, "desc": "付费分组"},
                })
            if request.url.path == "/api/token/" and request.method == "POST":
                created_bodies.append(json.loads(request.content))
                return api_response(None)
            if request.url.path == "/api/token/31" and request.method == "DELETE":
                deleted_paths.append(request.url.path)
                return api_response(None)
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        session = {"access_token": "access", "cookies": {"new_api_refresh": "refresh"}}
        source = {"base_url": "https://new-api.test"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = NewAPIAdapter()
            session, created = await adapter.create_keys(
                client, source, session, ["default"], only_missing=True,
                options={"delay_seconds": 0},
            )
            session, deleted = await adapter.delete_keys(
                client, source, session, ["paid"],
            )

        self.assertEqual(created["requested"], 1)
        self.assertEqual(created["errors"], [])
        self.assertEqual(created_bodies[0]["group"], "default")
        self.assertEqual(created_bodies[0]["name"], "default")
        self.assertEqual(created_bodies[0]["expired_time"], -1)
        self.assertTrue(created_bodies[0]["unlimited_quota"])
        self.assertEqual(deleted["requested"], 1)
        self.assertEqual(deleted_paths, ["/api/token/31"])

    async def test_create_names_fit_utf8_byte_limit_and_preserve_group(self):
        groups = ["short-id", "a" * 60, "中文分组" * 20, "a" * 48 + "图", "😀" * 20]
        description = "很长的分组说明" * 30
        bodies = []

        async def handler(request):
            if request.url.path == "/api/user/self/groups":
                return api_response({group: {"desc": description} for group in groups})
            if request.url.path == "/api/token/" and request.method == "GET":
                return api_response({"items": [], "total": 0})
            if request.url.path == "/api/token/" and request.method == "POST":
                body = json.loads(request.content)
                if len(body["name"].encode("utf-8")) > 50:
                    return httpx.Response(200, json={"success": False, "message": "令牌名称过长"})
                bodies.append(body)
                return api_response(None)
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for prefix in ("", "前缀" * 20):
                with self.subTest(prefix=prefix):
                    bodies.clear()
                    _, result = await NewAPIAdapter().create_keys(
                        client, {"base_url": "https://new-api.test"},
                        {"access_token": "test-access"}, [], only_missing=True,
                        options={"delay_seconds": 0, "name_prefix": prefix},
                    )
                    self.assertEqual(result["errors"], [])
                    self.assertEqual(len(result["created"]), len(groups))
                    self.assertEqual([body["group"] for body in bodies], groups)
                    for body, created in zip(bodies, result["created"]):
                        self.assertTrue(body["name"])
                        self.assertLessEqual(len(body["name"].encode("utf-8")), 50)
                        self.assertNotIn("�", body["name"])
                        self.assertEqual(created["name"], body["name"])
                        self.assertEqual(created["group_name"], body["group"])
                    if not prefix:
                        self.assertEqual(bodies[0]["name"], "short-id")

    async def test_sync_deletes_removed_group_tokens_and_hides_group(self):
        deleted_paths = []

        async def handler(request):
            if request.url.path == "/api/token/" and request.method == "GET":
                return api_response({"items": [{
                    "id": 41, "key": "sk-orphaned-full", "name": "orphaned",
                    "status": 1, "group": "removed",
                }], "total": 1})
            if request.url.path == "/api/user/self/groups":
                return api_response({"default": {"ratio": 1, "desc": "默认分组"}})
            if request.url.path == "/api/token/41" and request.method == "DELETE":
                deleted_paths.append(request.url.path)
                return api_response(None)
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        session = {"access_token": "access", "cookies": {"new_api_refresh": "refresh"}}
        source = {"base_url": "https://new-api.test", "id": "source-1"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = NewAPIAdapter()
            _, entries = await adapter.fetch(client, source, session)
            _, catalog = await adapter.catalog(client, source, session)

        self.assertEqual(entries, [])
        self.assertNotIn("removed", {group["id"] for group in catalog})
        self.assertEqual(deleted_paths, ["/api/token/41"])


class TransportRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_retries_transient_request_error(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("transient", request=request)
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await request_with_retry(client, "GET", "https://up.test/x")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls["n"], 3)

    async def test_post_does_not_retry_by_default(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            raise httpx.ConnectError("transient", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.ConnectError):
                await request_with_retry(client, "POST", "https://up.test/x", json={})

        self.assertEqual(calls["n"], 1)

    async def test_post_retries_when_explicitly_enabled(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectError("transient", request=request)
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await request_with_retry(
                client, "POST", "https://up.test/x", json={}, retry_writes=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
