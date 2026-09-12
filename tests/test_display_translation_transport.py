"""Public translation transport must preserve same-origin write boundaries."""
import asyncio
import hashlib
import hmac
import json
import re
import unittest
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from forecast_application.errors import AppError

from tests.test_web_transport import scheduled_method


class TranslationTransportTests(unittest.TestCase):
    def route(self, method, *, origin='https://forecast.example', client='web', language='ko'):
        self.calls = []
        async def noop(*args):
            return None
        async def get(*args):
            self.calls.append(('get', args))
            return {'status': 'missing'}
        async def generate(*args):
            self.calls.append(('generate', args))
            return {'status': 'ready'}
        async def bounded(*args):
            return json.dumps({'language': language, 'sourceHash': 'a'*64, 'specificationHash': 'b'*64}).encode()
        app = SimpleNamespace(authenticate=noop, rate_limit=noop,
                              display_translations=SimpleNamespace(get=get, generate=generate))
        entry = SimpleNamespace(application=lambda: app, env=SimpleNamespace(SESSION_SECRET='test-only-session'))
        headers = {'content-type': 'application/json', 'origin': origin, 'X-Forecast-Client': client,
                   'CF-Connecting-IP': '192.0.2.1'}
        request = SimpleNamespace(method=method, headers=headers)
        path = '/api/forecasts/f-1/translation'
        route = scheduled_method('route_api', Response=Any, AppError=AppError, re=re, hmac=hmac,
            hashlib=hashlib, parse_qs=parse_qs, AUTH_CONTEXT_COOKIE="__Host-forecast_auth", cookie_token=lambda _, name=None: None,
            bounded_bytes=bounded, MAX_BODY_BYTES=16384, MAX_PROVIDER_BYTES=524288,
            api_response=lambda data, **kwargs: {'data': data})
        return asyncio.run(route(entry, request, urlsplit('https://forecast.example'+path+'?language='+language), path))

    def test_cache_read_is_public_and_never_invokes_generation(self):
        self.assertEqual(self.route('GET')['data']['status'], 'missing')
        self.assertEqual(self.calls, [('get', ('f-1', 'ko'))])

    def test_public_generation_uses_server_fingerprint_not_raw_ip(self):
        self.assertEqual(self.route('POST')['data']['status'], 'ready')
        name, args = self.calls[0]
        self.assertEqual(name, 'generate')
        self.assertEqual(args[0], 'f-1')
        self.assertEqual(args[1]['language'], 'ko')
        self.assertRegex(args[2], r'^[0-9a-f]{64}$')
        self.assertNotIn('192.0.2.1', str(args))

    def test_cross_origin_and_missing_client_header_fail_before_generation(self):
        for options in ({'origin': 'https://foreign.example'}, {'client': None}):
            with self.subTest(options=options), self.assertRaises(AppError) as raised:
                self.route('POST', **options)
            self.assertEqual(raised.exception.code, 'origin_denied')
            self.assertEqual(self.calls, [])


class NumericTranslationGuardTests(unittest.TestCase):
    def test_full_width_url_delimiters_preserve_the_source_url(self):
        from forecast_application.ai import AIRejected
        from forecast_application.display_translations import validate_translation
        source = {'title': 'Official release?', 'question': 'Read https://example.org/ for the release.',
                  'rules': [], 'invalidationRules': [], 'aiRationale': None}
        output = {'title': '正式リリースですか？', 'question': 'リリースは（https://example.org/）を参照。',
                  'rules': [], 'invalidationRules': [], 'aiRationale': None}
        self.assertEqual(validate_translation(source, output, 'ja'), output)
        output['question'] = 'リリースは（https://other.example.org/）を参照。'
        with self.assertRaises(AIRejected):
            validate_translation(source, output, 'ja')

    def test_decimals_and_negative_values_are_not_equivalent(self):
        from forecast_application.display_translations import _numbers
        self.assertNotEqual(_numbers('Threshold -1.50'), _numbers('Threshold 1.50'))
        self.assertNotEqual(_numbers('Threshold 1.05'), _numbers('Threshold 1.50'))
        self.assertEqual(_numbers('2026-09-10'), _numbers('2026년 9월 10일'))
        self.assertEqual(_numbers('1,000.50'), _numbers('1000.5'))
