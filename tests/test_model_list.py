import gzip
import unittest
from types import SimpleNamespace

import httpx

from retry_proxy.key_pool import KeyEntry, KeyPool
from retry_proxy.retry import RetryProxy


class ModelListTests(unittest.IsolatedAsyncioTestCase):
    async def test_compressed_catalog_is_merged_without_decoding_twice(self):
        pool = KeyPool([])
        pool.entries = [KeyEntry('test-key', 'test', group_id='group')]
        pool.finalize_entries()

        def upstream(request):
            return httpx.Response(
                200, content=gzip.compress(b'{"data":[{"id":"test-model"}]}'),
                headers={'content-encoding': 'gzip',
                         'content-type': 'application/json'},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            service = RetryProxy(SimpleNamespace(), client=client)
            result = await service.request_model_list(
                'GET', 'https://upstream.test/v1/models', {},
                'v1/models', 'test', pool,
            )
        self.assertEqual(result.response.json()['data'], [{'id': 'test-model'}])
        self.assertNotIn('content-encoding', result.response.headers)
        self.assertEqual(int(result.response.headers['content-length']),
                         len(result.response.content))

    async def test_catalogs_merge_deduplicate_and_fill_cooling_group_from_cache(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry('test-a', 'a', group_id='a'),
            KeyEntry('test-b', 'b', group_id='b'),
            KeyEntry('test-c', 'c', group_id='c'),
        ]
        pool.entries[0].routing_capabilities = {
            'model_list_known': True, 'model_patterns': ('stale-model',),
        }
        pool.entries[2].routing_capabilities = {
            'model_list_known': True,
            'model_patterns': ('cached-model', 'wildcard-*'),
        }
        pool.entries[2].cooldown_until = float('inf')
        pool.finalize_entries()
        requested = []

        def upstream(request):
            auth = request.headers['authorization']
            requested.append(auth)
            models = ([{'id': 'shared', 'owned_by': 'a'}, {'id': 'model-a'}]
                      if auth == 'Bearer test-a'
                      else [{'id': 'shared'}, {'id': 'model-b'}])
            return httpx.Response(200, json={'object': 'list', 'data': models})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await RetryProxy(SimpleNamespace(), client=client).request_model_list(
                'GET', 'https://upstream.test/v1/models', {},
                'v1/models', 'test', pool,
            )
        self.assertCountEqual(requested, ['Bearer test-a', 'Bearer test-b'])
        payload = result.response.json()
        self.assertEqual(payload['object'], 'list')
        self.assertEqual([item['id'] for item in payload['data']],
                         ['shared', 'model-a', 'model-b', 'cached-model'])
        self.assertEqual(payload['data'][0]['owned_by'], 'a')
        self.assertEqual(result.total_sent, 2)

    async def test_network_failure_uses_next_real_upstream_error(self):
        pool = KeyPool([])
        pool.entries = [KeyEntry('a', 'a', group_id='a'), KeyEntry('b', 'b', group_id='b')]
        pool.finalize_entries()
        def upstream(request):
            if request.headers['authorization'] == 'Bearer a':
                raise httpx.ConnectError('unreachable', request=request)
            return httpx.Response(503, json={'error': 'upstream unavailable'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await RetryProxy(SimpleNamespace(), client=client).request_model_list(
                'GET', 'https://upstream.test/v1/models', {}, 'v1/models', 'test', pool)
        self.assertEqual(result.response.status_code, 503)
        self.assertEqual(result.total_sent, 2)

    async def test_all_network_failures_return_no_response(self):
        pool = KeyPool([])
        pool.entries = [KeyEntry('a', 'a', group_id='a')]
        pool.finalize_entries()
        def upstream(request):
            raise httpx.ConnectError('unreachable', request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await RetryProxy(SimpleNamespace(), client=client).request_model_list(
                'GET', 'https://upstream.test/v1/models', {}, 'v1/models', 'test', pool)
        self.assertIsNone(result.response)
        self.assertEqual(result.total_sent, 1)
        self.assertEqual(len(result.key_attempts), 1)
