import unittest

import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from retry_proxy.application import _api_docs_options, _register_disabled_api_docs


class ApiDocsConfigurationTests(unittest.TestCase):
    def test_api_docs_are_disabled_by_default(self):
        options = _api_docs_options(False)

        self.assertIsNone(options["docs_url"])
        self.assertIsNone(options["redoc_url"])
        self.assertIsNone(options["openapi_url"])
        self.assertIsNone(options["swagger_ui_oauth2_redirect_url"])

    def test_api_docs_can_be_explicitly_enabled(self):
        options = _api_docs_options(True)

        self.assertEqual(options["docs_url"], "/docs")
        self.assertEqual(options["redoc_url"], "/redoc")
        self.assertEqual(options["openapi_url"], "/openapi.json")
        self.assertEqual(options["swagger_ui_oauth2_redirect_url"], "/docs/oauth2-redirect")


class DisabledApiDocsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_paths_return_404_without_reaching_proxy(self):
        app = FastAPI(**_api_docs_options(False))
        _register_disabled_api_docs(app)

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def proxy_catchall(path):
            return PlainTextResponse(f"proxied: {path}")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in (
                "/docs", "/docs/", "/docs/oauth2-redirect",
                "/redoc", "/redoc/", "/openapi.json", "/openapi.json/",
            ):
                response = await client.get(path)
                self.assertEqual(response.status_code, 404, path)
                self.assertEqual(response.text, "api docs disabled", path)


if __name__ == "__main__":
    unittest.main()
