"""Actual Worker route authenticates KPI reads and constrains the reported window."""

import hashlib
import hmac
import re
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

from forecast_application.errors import AppError

from tests.test_web_transport import scheduled_method


class ProductAnalyticsHttpTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, query="", *, admin=True):
        self.aggregate = AsyncMock(return_value={"formulaVersion": "product-analytics-v1"})
        self.app = SimpleNamespace(db=object(), now_ms=lambda: 1800000000000)
        worker = SimpleNamespace(env=SimpleNamespace(ADMIN_TOKEN="a"*64), application=lambda: self.app)
        parsed = urlsplit("https://forecast.example/api/admin/analytics"+query)
        request = SimpleNamespace(method="GET", headers={"authorization": "Bearer "+"a"*64} if admin else {})
        route = scheduled_method("route_api", Response=Any, AppError=AppError, re=re, hmac=hmac,
            hashlib=hashlib, parse_qs=parse_qs, product_analytics=self.aggregate,
            api_response=lambda data, **kwargs: {"data": data})
        return await route(worker, request, parsed, parsed.path)

    async def test_private_endpoint_rejects_before_aggregation(self):
        with self.assertRaises(AppError) as error:
            await self.call(admin=False)
        self.assertEqual(error.exception.status, 403)
        self.aggregate.assert_not_called()

    async def test_default_window_is_last_thirty_complete_utc_days(self):
        await self.call()
        args = self.aggregate.call_args.kwargs
        self.assertEqual(args["window_end_ms"] % 86400000, 0)
        self.assertEqual(args["window_end_ms"]-args["window_start_ms"], 30*86400000)
        self.assertLessEqual(args["window_end_ms"], args["as_of_ms"])

    async def test_operator_cannot_change_population_or_duplicate_window_values(self):
        for query in ("?population_kind=fixture", "?start=0&start=1", "?start=-1", "?start=1e3", "?end="):
            with self.subTest(query=query), self.assertRaises(ValueError):
                await self.call(query)
            self.aggregate.assert_not_called()
