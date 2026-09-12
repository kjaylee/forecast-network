"""Participation containment races preserve accepted votes, points and domain proofs."""
import hashlib
import hmac
import re
import unittest
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from forecast_application.errors import AppError
from forecast_domain.lifecycle import Archive, LifecycleState

from tests import test_web_application as application_tests
from tests.test_web_transport import scheduled_method


class ParticipationHoldTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = application_tests.ApplicationTests.asyncSetUp
    asyncTearDown = application_tests.ApplicationTests.asyncTearDown
    random_token = application_tests.ApplicationTests.random_token
    token_hash = staticmethod(application_tests.ApplicationTests.token_hash)
    publish = application_tests.ApplicationTests.publish

    def request(self, forecast, **changes):
        return {"action": "hold", "expectedRevision": 0, "expectedHoldId": None,
                "specificationHash": forecast["specificationHash"], "reason": "known_outcome_review",
                "evidenceUrl": "https://example.org/official-announcement", "idempotencyKey": "hold-key-123", **changes}

    async def test_hold_preserves_proofs_positions_and_excludes_recommendations(self):
        f = await self.publish()
        accepted = await self.app.submit_forecast(self.other, f['id'], 'YES', 70, f['revision'], 'vote-key-123', 100)
        before = await self.app._forecast(f['id'])
        points = await self.app.points.summary(self.other)
        hold = await self.app.participation_holds.change(f['id'], self.request(f))
        self.assertEqual(await self.app._forecast(f['id']), before)
        self.assertEqual(await self.app.points.summary(self.other), points)
        detail = await self.app.forecast_detail(f['id'], self.other)
        self.assertEqual(detail['forecast']['participationHold'], hold)
        feed = await self.app.list_forecasts()
        self.assertEqual(feed['counts']['active'], 0)
        self.assertEqual(feed['dailyIds'], [])
        retry = await self.app.submit_forecast(self.other, f['id'], 'YES', 70, f['revision'], 'vote-key-123', 100)
        self.assertEqual(retry['myForecast'], accepted['myForecast'])
        self.assertEqual(retry['stake'], accepted['stake'])
        for stake in (None, 0, 50, 100, 200):
            with self.subTest(stake=stake), self.assertRaises(AppError) as raised:
                await self.app.submit_forecast(self.other, f['id'], 'NO', 80, before.revision, 'new-vote-key', stake)
            self.assertEqual(raised.exception.code, 'participation_on_hold')

    async def test_ordinary_resolution_requires_review_release_and_preserves_admin_audit(self):
        f = await self.publish()
        hold = await self.app.participation_holds.change(f['id'], self.request(f))
        self.assertEqual((await self.app._card(f['id']))['participationHold'], hold)
        self.now = f['closeAt'] + 1000
        self.assertEqual((await self.app.run_due_jobs())['failed'], 1)
        self.assertEqual((await self.app._forecast(f['id'])).state, LifecycleState.OPEN)
        await self.app.participation_holds.change(f['id'], self.request(f, action='release',
            expectedRevision=1, expectedHoldId=hold['holdId'], idempotencyKey='reviewed-release-before-resolution'))
        audit = await self.app.participation_holds.status(f['id'])
        self.now += 60001
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        challenge = await self.app._forecast(f['id'])
        self.assertEqual(challenge.state, LifecycleState.CHALLENGE)
        self.assertIsNone((await self.app._card(f['id']))['participationHold'])
        self.now = challenge.challenge_until_ms
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        for state in (LifecycleState.FINALIZED, LifecycleState.ARCHIVED):
            with self.subTest(state=state):
                record = await self.app._forecast(f['id'])
                if state == LifecycleState.ARCHIVED:
                    record = await self.app._mutate(record, Archive(), key='archive-held-forecast')
                self.assertEqual(record.state, state)
                self.assertEqual(record.specification_hash, f['specificationHash'])
                self.assertEqual(record.specification.close_at_ms, f['closeAt'])
                self.assertIsNone((await self.app._card(f['id']))['participationHold'])
                detail = await self.app.forecast_detail(f['id'])
                self.assertIsNone(detail['forecast']['participationHold'])
                self.assertEqual(detail['forecast']['finalizedOutcome'], 'YES')
                self.assertIsNone((await self.app.list_forecasts())['items'][0]['participationHold'])
                self.assertEqual(await self.app.participation_holds.status(f['id']), audit)
                self.assertIsNone(await self.app.participation_holds.active(f['id']))

    async def test_release_cas_prevents_stale_release_of_new_hold_and_retries_are_immutable(self):
        f = await self.publish()
        request = self.request(f)
        hold = await self.app.participation_holds.change(f['id'], request)
        self.assertEqual(await self.app.participation_holds.change(f['id'], request), hold)
        release_request = self.request(f, action='release', expectedRevision=1, expectedHoldId=hold['holdId'], idempotencyKey='release-key-1')
        released = await self.app.participation_holds.change(f['id'], release_request)
        second = await self.app.participation_holds.change(f['id'], self.request(f, expectedRevision=2, idempotencyKey='hold-key-222'))
        self.assertEqual(await self.app.participation_holds.change(f['id'], release_request), released)
        self.assertEqual((await self.app.participation_holds.active(f['id']))['holdId'], second['holdId'])
        with self.assertRaises(AppError) as raised:
            await self.app.participation_holds.change(f['id'], {**release_request, 'idempotencyKey': 'release-stale'})
        self.assertEqual(raised.exception.code, 'participation_hold_changed')
        self.assertEqual((await self.app.participation_holds.status(f['id']))['revision'], 3)
        with self.assertRaises(Exception):
            await self.db.execute('DELETE FROM participation_hold_events')
        with self.assertRaises(Exception):
            await self.db.execute("UPDATE participation_hold_events SET actor='authenticated_admin'")

    async def test_hold_between_vote_read_and_commit_rolls_back_every_effect(self):
        for amount in (0, 100):
            with self.subTest(amount=amount):
                await self.asyncTearDown()
                await self.asyncSetUp()
                f = await self.publish()
                before = await self.app._forecast(f['id'])
                points = await self.app.points.summary(self.other)
                batch = self.db.batch
                triggered = False
                async def intercept(statements):
                    nonlocal triggered
                    if not triggered and any('INSERT INTO user_forecasts' in sql for sql, _ in statements):
                        triggered = True
                        await self.app.participation_holds.change(f['id'], self.request(f))
                    return await batch(statements)
                self.db.batch = intercept
                with self.assertRaises(AppError) as raised:
                    await self.app.submit_forecast(self.other, f['id'], 'YES', 80, f['revision'], 'vote-race-key', amount)
                self.assertEqual(raised.exception.code, 'participation_on_hold')
                self.assertEqual(await self.app._forecast(f['id']), before)
                self.assertEqual(await self.app.points.summary(self.other), points)
                self.assertEqual(await self.db.all('SELECT * FROM user_forecasts'), [])
                self.assertEqual(await self.db.all('SELECT * FROM point_positions'), [])

    async def test_lost_ack_for_hold_and_vote_returns_receipt_even_if_hold_started(self):
        f = await self.publish()
        batch = self.db.batch
        async def lost_hold_ack(statements):
            result = await batch(statements)
            if any('INSERT INTO participation_hold_events' in sql for sql, _ in statements):
                raise OSError('acknowledgment lost')
            return result
        self.db.batch = lost_hold_ack
        hold = await self.app.participation_holds.change(f['id'], self.request(f))
        self.assertEqual(hold['revision'], 1)
        self.db.batch = batch
        await self.app.participation_holds.change(f['id'], self.request(f, action='release', expectedRevision=1, expectedHoldId=hold['holdId'], idempotencyKey='release-key-1'))
        async def lost_vote_ack(statements):
            result = await batch(statements)
            if any('INSERT INTO user_forecasts' in sql for sql, _ in statements):
                await self.app.participation_holds.change(f['id'], self.request(f, expectedRevision=2, idempotencyKey='hold-key-222'))
                raise OSError('acknowledgment lost')
            return result
        self.db.batch = lost_vote_ack
        accepted = await self.app.submit_forecast(self.other, f['id'], 'YES', 80, f['revision'], 'vote-ack-key', 100)
        self.assertEqual(accepted['stake']['amount'], 100)
        self.assertIsNotNone(accepted['forecast']['participationHold'])
        self.assertEqual(len(await self.db.all('SELECT * FROM point_ledger WHERE kind=\'reservation\'')), 1)

    async def test_changed_request_invalid_url_and_hash_do_not_create_audit(self):
        f = await self.publish()
        for changes in ({'evidenceUrl': 'javascript:alert(1)'}, {'expectedRevision': True},
                        {'expectedHoldId': 'stale'}, {'specificationHash': 'a'*64}, {'reason': 'declare_yes'}):
            with self.subTest(changes=changes), self.assertRaises(AppError):
                await self.app.participation_holds.change(f['id'], self.request(f, **changes))
        self.assertEqual(await self.db.all('SELECT * FROM participation_hold_events'), [])
        await self.app.participation_holds.change(f['id'], self.request(f))
        with self.assertRaises(AppError) as raised:
            await self.app.participation_holds.change(f['id'], self.request(f, evidenceUrl='https://example.org/different'))
        self.assertEqual(raised.exception.code, 'idempotency_conflict')


class ParticipationTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_admin_bearer_can_hold_or_read_operator_audit(self):
        calls = []
        async def change(*args):
            calls.append(args)
            return {'revision': 1}
        async def bounded(*args):
            return b'{}'
        entry = SimpleNamespace(env=SimpleNamespace(ADMIN_TOKEN='a'*40), application=lambda: SimpleNamespace(
            participation_holds=SimpleNamespace(change=change, status=change)))
        path = '/api/admin/forecasts/f-1/participation'
        route = scheduled_method('route_api', Response=Any, AppError=AppError, re=re, hmac=hmac,
            hashlib=hashlib, parse_qs=parse_qs, cookie_token=lambda _: None, bounded_bytes=bounded,
            MAX_BODY_BYTES=16384, MAX_PROVIDER_BYTES=524288, api_response=lambda data, **kwargs: data)
        for method in ('GET', 'POST'):
            for token in (None, 'Bearer wrong'):
                request = SimpleNamespace(method=method, headers={'origin': 'https://forecast.example',
                    'X-Forecast-Client': 'web', 'content-type': 'application/json', 'authorization': token})
                with self.assertRaises(AppError) as raised:
                    await route(entry, request, urlsplit('https://forecast.example'+path), path)
                self.assertEqual(raised.exception.status, 403)
        self.assertEqual(calls, [])
        request = SimpleNamespace(method='POST', headers={'content-type': 'application/json', 'authorization': 'Bearer '+'a'*40})
        self.assertEqual(await route(entry, request, urlsplit('https://forecast.example'+path), path), {'revision': 1})
        self.assertEqual(calls, [('f-1', {})])
