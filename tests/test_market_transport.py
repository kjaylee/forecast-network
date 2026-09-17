"""Worker routing keeps preview, authenticated fills and operations separate."""
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


class MarketTransportTests(unittest.TestCase):
    def route(self, path, method='GET', body=None, user='user-a', admin=False):
        self.calls = []
        async def record(name, *args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {'operation': name, 'billable': False}
        async def authenticate(_, context=None):
            return {'id': user} if user else None
        async def rate_limit(*_):
            return None
        async def bounded(*_):
            return json.dumps(body or {}).encode()
        def operation(name):
            async def call(*args, **kwargs):
                return await record(name, *args, **kwargs)
            return call
        def expected_owner(data, owner):
            if not isinstance(data.get('expectedUserId'), str):
                raise AppError(400, 'account_precondition_required', 'Account required')
            if data['expectedUserId'] != owner:
                raise AppError(409, 'account_changed', 'Account changed')
        markets = SimpleNamespace(**{name: operation(name) for name in ('get', 'preview', 'quote', 'accept', 'budget', 'fund_treasury', 'create', 'positions', 'receipt_status')})
        app = SimpleNamespace(markets=markets, authenticate=authenticate, rate_limit=rate_limit, registry=None,
                              run_automation=operation('automation'), billing=SimpleNamespace(estimate=lambda: {'billable': False}))
        token = 'test-only-operator-'+'x'*40
        entry = SimpleNamespace(application=lambda: app, env=SimpleNamespace(SESSION_SECRET='test-secret', ADMIN_TOKEN=token))
        headers = {'origin': 'https://forecast.example', 'X-Forecast-Client': 'web', 'content-type': 'application/json'}
        if admin:
            headers['authorization'] = 'Bearer '+token
        request = SimpleNamespace(method=method, headers=headers)
        parsed = urlsplit('https://forecast.example'+path)
        route = scheduled_method('route_api', Response=Any, AppError=AppError, re=re, hmac=hmac, hashlib=hashlib,
            parse_qs=parse_qs, AUTH_CONTEXT_COOKIE="__Host-forecast_auth", cookie_token=lambda _, name=None: None, bounded_bytes=bounded,
            require_expected_user=expected_owner, MAX_BODY_BYTES=16384, MAX_PROVIDER_BYTES=524288,
            api_response=lambda data, **kwargs: {'data': data})
        return asyncio.run(route(entry, request, parsed, parsed.path))

    def test_anonymous_quote_is_only_nonbinding_preview(self):
        result = self.route('/api/forecasts/f_test/market/quote', 'POST', {'side': 'YES', 'spendPoints': 100, 'mode': 'active'}, user=None)
        self.assertEqual(result['data']['operation'], 'preview')
        self.assertEqual(self.calls, [('preview', ('f_test', 'YES', 100), {})])

    def test_account_change_blocks_quote_and_fill_before_adapter(self):
        for action in ('quote', 'fill'):
            with self.subTest(action=action), self.assertRaises(AppError) as raised:
                self.route('/api/forecasts/f_test/market/'+action, 'POST', {'expectedUserId': 'user-b'})
            self.assertEqual(raised.exception.code, 'account_changed')
            self.assertEqual(self.calls, [])

    def test_fill_requires_account_and_exact_atomic_integer_string(self):
        for value in (1000000, '1e6', '-1', '1.0', '99999999999999999'):
            with self.subTest(value=value), self.assertRaises(AppError):
                self.route('/api/forecasts/f_test/market/fill', 'POST', {'expectedUserId': 'user-a', 'minClaimsAtomic': value})
            self.assertEqual(self.calls, [])
        result = self.route('/api/forecasts/f_test/market/fill', 'POST', {'expectedUserId': 'user-a', 'minClaimsAtomic': '190902828', 'quoteId': 'quote-a', 'idempotencyKey': 'fill-key-1'})
        self.assertEqual(result['data']['operation'], 'accept')
        self.assertEqual(self.calls[0][1], ('user-a', 'f_test', 'quote-a', 190902828, 'fill-key-1'))
        with self.assertRaises(AppError) as raised:
            self.route('/api/forecasts/f_test/market/fill', 'POST', {}, user=None)
        self.assertEqual(raised.exception.status, 401)

    def test_treasury_and_automation_require_operator_bearer(self):
        for path in ('/api/admin/markets/treasury', '/api/admin/sweep'):
            with self.subTest(path=path), self.assertRaises(AppError) as raised:
                self.route(path, 'POST', {})
            self.assertEqual(raised.exception.status, 403)
            self.assertEqual(self.calls, [])
        self.route('/api/admin/sweep', 'POST', {}, admin=True)
        self.assertEqual(self.calls, [('automation', (), {'limit': 4})])

    def test_billing_estimate_is_explicitly_not_a_charge(self):
        self.assertEqual(self.route('/api/billing/estimate', user=None)['data'], {'billable': False})
        self.assertEqual(self.calls, [])

    def test_operator_treasury_read_selects_actual_ledger_mode(self):
        self.route('/api/admin/markets/treasury?mode=active', admin=True)
        self.assertEqual(self.calls, [('budget', ('active',), {})])
        self.route('/api/admin/markets/treasury', admin=True)
        self.assertEqual(self.calls, [('budget', ('shadow',), {})])
        with self.assertRaises(AppError):
            self.route('/api/admin/markets/treasury?mode=active', admin=False)
        self.assertEqual(self.calls, [])

    def test_receipt_lookup_is_read_only_and_bound_to_authenticated_owner(self):
        path = '/api/forecasts/f_test/market/receipt?quoteId=quote-a&idempotencyKey=key-a&userId=user-b'
        with self.assertRaises(AppError) as raised:
            self.route(path, user=None)
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(self.calls, [])
        self.route(path, user='user-a')
        self.assertEqual(self.calls, [('receipt_status', ('user-a', 'f_test', 'quote-a', 'key-a'), {})])


class WalletConstructionCompatibilityTests(unittest.TestCase):
    def test_worker_wallet_constructor_uses_only_supported_service_keywords(self):
        import ast
        import inspect
        from pathlib import Path

        from forecast_application.wallets import WalletService
        tree = ast.parse((Path(__file__).resolve().parents[1]/'apps/web/src/entry.py').read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'WalletService']
        self.assertEqual(len(calls), 1)
        call = calls[0]
        # Bind the actual adapter constructor signature, catching unrelated feature
        # flags even when a permissive transport mock would absorb **kwargs.
        inspect.signature(WalletService).bind(*([None]*len(call.args)), **{key.arg: None for key in call.keywords})
