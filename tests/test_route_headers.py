import unittest
from unittest.mock import patch

from retry_proxy.routes import parse_route_headers, route_headers_for


class RouteHeaderTests(unittest.TestCase):
    def test_multiple_routes_and_headers(self):
        result = parse_route_headers('/one:X-Session=sample,Originator=client;*:X-Tier=internal')
        self.assertEqual(result, {'/one': {'x-session': 'sample', 'originator': 'client'},
                                  '*': {'x-tier': 'internal'}})

    def test_invalid_configuration_does_not_log_secrets(self):
        secret = 'SENSITIVE_TEST_VALUE'
        raw = ';'.join(('bad-' + secret, '/bad path:authorization=' + secret,
                        '/one:invalid name=' + secret,
                        '/one:x-session=' + secret + '\r\nInjected: true'))
        with patch('retry_proxy.routes.logger.warning') as warning:
            self.assertEqual(parse_route_headers(raw), {})
        self.assertEqual(warning.call_count, 4)
        self.assertNotIn(secret, str(warning.call_args_list))

    def test_default_headers_do_not_leak_into_named_upstream(self):
        configured = {'*': {'authorization': 'test-default'}, '/one': {'x-session': 'test-one'}}
        for prefix, expected in (('', configured['*']), ('/one', configured['/one']), ('/other', {})):
            with self.subTest(prefix=prefix), patch('retry_proxy.routes.route_prefix_for', return_value=prefix):
                self.assertEqual(route_headers_for('path', headers=configured), expected)
