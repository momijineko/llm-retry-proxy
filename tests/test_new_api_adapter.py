import json
import unittest

import httpx

from retry_proxy.sync_adapters import ADAPTERS, PoolSyncError
from retry_proxy.sync_adapters.new_api import NewAPIAdapter, _unwrap


def api_response(data=None, *, success=True, status=200, headers=None):
    return httpx.Response(
        status, json={"success": success, "message": "" if success else "failed", "data": data},
        headers=headers,
    )


class NewAPIAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_adapter_is_registered(self):
        self.assertIsInstance(ADAPTERS["newapi"], NewAPIAdapter)

    def test_non_json_cloudflare_response_does_not_expose_body(self):
        response = httpx.Response(
            403, text="<html>secret response body</html>",
            headers={"content-type": "text/html", "server": "cloudflare", "cf-ray": "ray"},
        )

        with self.assertRaises(PoolSyncError) as raised:
            _unwrap(response)

        self.assertIn("Cloudflare/CDN", str(raised.exception))
        self.assertNotIn("secret response body", str(raised.exception))

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
        self.assertEqual(entries[0]["label"], "coding-VIP")
        self.assertEqual(entries[0]["sort"], "0.25")
        self.assertEqual(entries[0]["group_id"], "vip")
        self.assertEqual(entries[0]["routing_capabilities"], {
            "model_patterns": ["gpt-5.4", "claude-sonnet-4-5"],
            "model_list_known": True,
        })
        self.assertEqual(len(calls), 4)

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
        self.assertEqual(created_bodies[0]["expired_time"], -1)
        self.assertTrue(created_bodies[0]["unlimited_quota"])
        self.assertEqual(deleted["requested"], 1)
        self.assertEqual(deleted_paths, ["/api/token/31"])


if __name__ == "__main__":
    unittest.main()
