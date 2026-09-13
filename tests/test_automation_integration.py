"""Real migrated persistence checks for observed events and the ordinary lifecycle."""
from __future__ import annotations

import json
import sqlite3
import unittest
from dataclasses import fields

from forecast_application.ai import CompileResult, ResolutionResult
from forecast_application.errors import AppError
from forecast_application.markets import PointMarkets
from forecast_application.service import CHALLENGE_MS
from forecast_application.sources import Artifact
from forecast_domain import content_hash, dumps, to_dict
from forecast_domain.early_resolution import (
    EarlyResolution,
    EarlyResolutionTrigger,
    ForecastV2,
    early_qualification_output_hash,
    early_trigger_input_hash,
)
from forecast_domain.lifecycle import LifecycleState
from forecast_domain.models import (
    AITask,
    counter_judge_input_hash,
    counter_judge_output_hash,
    resolution_input_hash,
    resolution_output_hash,
)

from tests import model_fixtures as model
from tests import test_web_application as application_tests
from tests.test_display_translations import TranslationAI

SOURCE_BODY = "Retained official local-test announcement: Acme announced Product X."


class AutomationFixtureAI(TranslationAI):
    """Deterministic fixtures only; never used by production provider routing."""
    def __init__(self):
        super().__init__()
        self.early_calls = 0
        self.source_calls = 0

    async def compile_question(self, question, candidates, now_ms):
        result = await super().compile_question(question, candidates, now_ms)
        spec = model.specification(canonical_question=question, share_title=question,
                                   open_at_ms=now_ms, close_at_ms=now_ms + 7*86400000)
        return CompileResult(spec, model.validation(spec, validated_at_ms=now_ms), result.artifacts)

    async def propose_early_resolution(self, forecast, now_ms):
        self.early_calls += 1
        t = forecast.early_trigger
        source = model.resolution(forecast.specification, forecast_id=forecast.forecast_id,
                                   evidence=t.evidence, source_verifications=t.source_verifications,
                                   proposed_at_ms=now_ms)
        values = {f.name: getattr(source, f.name) for f in fields(source)}
        values.update(schema_version=2, trigger=t)
        digest = content_hash({"schema_version": 2, "kind": "early_resolution_input",
            "trigger_hash": t.trigger_hash, "resolution_input_hash": resolution_input_hash(
                forecast.forecast_id, forecast.specification_hash, t.evidence, t.source_verifications)})
        output = resolution_output_hash(digest, source.proposed_outcome, source.confidence_bp,
            source.rule_matches, source.rule_conflicts, source.reason_summary, source.conflict_status,
            source.conflict_explanation)
        values['judge'] = model.provenance(AITask.RESOLUTION_JUDGE, input_hash=digest,
                                           output_hash=output, created_at_ms=now_ms-1)
        values['counter_judge'] = model.provenance(AITask.COUNTER_JUDGE,
            input_hash=counter_judge_input_hash(digest, values['judge']),
            output_hash=counter_judge_output_hash(output, True), created_at_ms=now_ms)
        return ResolutionResult(EarlyResolution(**values), ())

    async def review_source_observation(self, *args):
        self.source_calls += 1
        raise AssertionError("Disabled source observer must not call the model")


class AutomationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = application_tests.ApplicationTests.asyncTearDown
    random_token = application_tests.ApplicationTests.random_token
    token_hash = staticmethod(application_tests.ApplicationTests.token_hash)
    publish = application_tests.ApplicationTests.publish

    async def asyncSetUp(self):
        await application_tests.ApplicationTests.asyncSetUp(self)
        self.ai = AutomationFixtureAI()
        self.app.ai = self.ai
        self.app.display_translations.ai = self.ai
        self.base = self.now
        self.card = await self.publish()
        self.fid = self.card['id']

    async def test_apple_bootstrap_uses_official_feed_without_rewriting_specification(self):
        from forecast_application.automation import ForecastAutomation
        from forecast_application.sources import SourceCollector
        async def no_network(*args):
            raise AssertionError("Bootstrap registration must not fetch or invoke a model")
        from dataclasses import replace
        original_compile = self.ai.compile_question
        async def apple_compile(question, candidates, now_ms):
            result = await original_compile(question, candidates, now_ms)
            policy = result.specification.source_policy
            source = replace(policy.primary_sources[0], url="https://www.apple.com/newsroom", is_official=True)
            spec = replace(result.specification, source_policy=replace(policy, primary_sources=(source,)))
            return CompileResult(spec, model.validation(spec, validated_at_ms=now_ms), result.artifacts)
        self.ai.compile_question = apple_compile
        draft = await self.app.compile_forecast(self.uid, "Will Apple officially announce a foldable phone before the deadline?")
        card = (await self.app.publish_forecast(self.uid, draft["draftId"], "publish-apple-feed-test"))["forecast"]
        original = await self.app._forecast(card['id'])
        self.app.ai.collector = SourceCollector(no_network)
        self.app.automation = ForecastAutomation(self.app, enabled=True)
        await self.app.automation.bootstrap()
        await self.app.automation.bootstrap()
        sources = await self.db.all('SELECT url FROM official_watch_sources')
        self.assertEqual(sources, [{'url': 'https://www.apple.com/newsroom/rss-feed.rss'}])
        self.assertEqual(await self.app._forecast(card['id']), original)

    async def reviewed_trigger(self, *, basis='published_instant', retain=True, url=None, content=SOURCE_BODY):
        record = await self.app._forecast(self.fid)
        evidence = model.evidence(record.specification, collected_at_ms=self.base+100, content=content,
                                  **({'url': url} if url else {}))
        verification = model.source_verification(evidence)
        observed = self.base + 110
        event = observed if basis == 'observed_upper_bound' else self.base + 90
        args = (self.fid, record.specification_hash, 'yes-rule', (evidence,), (verification,), event, observed, basis)
        digest = early_trigger_input_hash(*args)
        explanation = 'The official announcement irreversibly meets the exact YES clause; no identity or invalidation conflict exists.'
        qualifier = model.provenance(AITask.AMBIGUITY_JUDGE, input_hash=digest,
            output_hash=early_qualification_output_hash(digest, explanation), created_at_ms=self.base+120)
        counter = model.provenance(AITask.COUNTER_JUDGE, provider='independent-provider',
            input_hash=counter_judge_input_hash(digest, qualifier),
            output_hash=counter_judge_output_hash(qualifier.output_hash, True), created_at_ms=self.base+130)
        t = EarlyResolutionTrigger(forecast_id=self.fid, specification_hash=record.specification_hash,
            clause_id='yes-rule', evidence=(evidence,), source_verifications=(verification,),
            event_at_ms=event, observed_at_ms=observed, event_time_basis=basis,
            qualification=explanation, qualifier=qualifier, counter_qualifier=counter)
        if retain:
            await self.db.batch(self.app._artifact_sql((Artifact(evidence.content_sha256, 'source', content, 'text/plain'),)))
        self.now = self.base + 200
        return t

    async def hold(self, t):
        await self.app.automation.hold(self.fid, {'id': t.trigger_hash, 'url': t.evidence[0].url})

    async def accept(self, t):
        await self.app.automation.accept(self.fid, {'trigger': to_dict(t)})

    async def test_retained_review_hold_upgrade_and_scheduler_keep_public_proofs_and_translation(self):
        before = await self.app._forecast(self.fid)
        initial_translation = await self.app.display_translations.get(self.fid, 'ko')
        translation = await self.app.display_translations.generate(self.fid, {
            'language': 'ko', 'specificationHash': before.specification_hash,
            'sourceHash': initial_translation['sourceHash']}, 'local-test-client')
        before_integrity = await self.app.integrity(self.fid)
        t = await self.reviewed_trigger()
        await self.hold(t)
        await self.accept(t)
        locked = await self.app._forecast(self.fid)
        self.assertIs(type(locked), ForecastV2)
        self.assertEqual(locked.state, LifecycleState.LOCKED)
        self.assertEqual(locked.upgrade_source, before)
        self.assertEqual(locked.latest_event.previous_event_hash, before.audit_head_hash)
        self.assertEqual(await self.app.display_translations.get(self.fid, 'ko'), translation)
        result = await self.app.run_due_jobs()
        self.assertEqual(result['failed'], 0)
        self.assertEqual(self.ai.early_calls, 1)
        challenge = await self.app._forecast(self.fid)
        self.assertEqual(challenge.state, LifecycleState.CHALLENGE)
        self.assertLess(challenge.updated_at_ms, before.specification.close_at_ms)
        self.assertEqual(challenge.challenge_until_ms, self.now+CHALLENGE_MS)
        integrity = await self.app.integrity(self.fid)
        self.assertEqual(integrity['specification'], before_integrity['specification'])
        self.assertEqual(content_hash(json.loads(integrity['resolution']['canonicalJson'])), integrity['resolution']['resolutionHash'])
        self.assertEqual((await self.app.forecast_detail(self.fid))['forecast']['specificationHash'], before.specification_hash)
        await self.app.list_forecasts()
        self.assertEqual(await self.app.display_translations.get(self.fid, 'ko'), translation)
        events = await self.db.all('SELECT event FROM events WHERE forecast_id=? ORDER BY revision', (self.fid,))
        decoded = [json.loads(row['event']) for row in events]
        self.assertEqual([item['revision'] for item in decoded], list(range(1, challenge.revision+1)))
        for previous, current in zip(decoded, decoded[1:]):
            self.assertEqual(current['previous_event_hash'], content_hash(previous))

    async def test_precise_pre_event_receipt_settles_at_real_challenge_deadline_exactly_once(self):
        self.now = self.base + 50
        accepted = await self.app.submit_forecast(self.other, self.fid, 'YES', 80, self.card['revision'], 'pre-event-vote', 100)
        t = await self.reviewed_trigger()
        await self.hold(t)
        await self.accept(t)
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        challenge = await self.app._forecast(self.fid)
        self.now = challenge.challenge_until_ms - 1
        await self.app.run_due_jobs()
        self.assertEqual((await self.app._forecast(self.fid)).state, LifecycleState.CHALLENGE)
        self.assertEqual((await self.app.points.summary(self.other))['committed'], 100)
        self.now += 1
        self.assertLess(self.now, self.card['closeAt'])
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        finalized = await self.app._forecast(self.fid)
        self.assertEqual(finalized.state, LifecycleState.FINALIZED)
        balance = await self.app.points.summary(self.other)
        self.assertEqual((balance['available'], balance['committed']), (1100, 0))
        self.assertEqual((await self.app.reputation(self.other))['resolvedForecasts'], 1)
        await self.app.run_due_jobs()
        self.assertEqual(await self.app.points.summary(self.other), balance)
        retry = await self.app.submit_forecast(self.other, self.fid, 'YES', 80, self.card['revision'], 'pre-event-vote', 100)
        self.assertEqual(retry['myForecast'], accepted['myForecast'])

    async def test_idempotent_accept_preserves_revision_receipts_and_trigger_artifact(self):
        t = await self.reviewed_trigger()
        await self.hold(t)
        await self.accept(t)
        before = await self.app._forecast(self.fid)
        events = await self.db.all('SELECT * FROM events')
        receipts = await self.db.all('SELECT * FROM command_receipts')
        await self.accept(t)
        self.assertEqual(await self.app._forecast(self.fid), before)
        self.assertEqual(await self.db.all('SELECT * FROM events'), events)
        self.assertEqual(await self.db.all('SELECT * FROM command_receipts'), receipts)
        self.assertEqual(await self.app.read_artifact(t.trigger_hash), dumps(t))

    async def test_missing_or_corrupted_retained_bytes_prevent_upgrade(self):
        t = await self.reviewed_trigger(retain=False)
        await self.hold(t)
        original = await self.app._forecast(self.fid)
        for corrupted in (False, True):
            if corrupted:
                await self.db.execute('INSERT INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)',
                    (t.evidence[0].content_sha256, 'source', 'Corrupted fixture bytes', 'text/plain', self.now))
            with self.subTest(corrupted=corrupted), self.assertRaises(AppError) as caught:
                await self.accept(t)
            self.assertEqual(caught.exception.code, 'early_evidence_unavailable')
            self.assertEqual(await self.app._forecast(self.fid), original)

    async def test_observation_bound_requires_zero_receipts_even_if_all_precede_observation(self):
        self.now = self.base + 20
        await self.app.submit_forecast(self.other, self.fid, 'YES', 80, self.card['revision'], 'prior-unknown-instant', 0)
        t = await self.reviewed_trigger(basis='observed_upper_bound')
        await self.hold(t)
        original = await self.app._forecast(self.fid)
        with self.assertRaises(AppError) as caught:
            await self.accept(t)
        self.assertEqual(caught.exception.code, 'early_eligibility_review')
        self.assertEqual(await self.app._forecast(self.fid), original)
        self.assertEqual(len(await self.db.all('SELECT * FROM user_forecasts')), 1)

    async def test_precise_event_voids_and_refunds_receipt_at_event_inclusive(self):
        self.now = self.base + 90
        await self.app.submit_forecast(self.other, self.fid, 'YES', 80, self.card['revision'], 'known-event-vote', 100)
        t = await self.reviewed_trigger()
        await self.hold(t)
        original = await self.app._forecast(self.fid)
        receipts = await self.db.all('SELECT * FROM command_receipts')
        await self.accept(t)
        upgraded = await self.app._forecast(self.fid)
        self.assertEqual(upgraded.state, LifecycleState.LOCKED)
        self.assertEqual(upgraded.upgrade_source, original)
        points = await self.app.points.summary(self.other)
        self.assertEqual((points['available'], points['committed']), (1000, 0))
        self.assertEqual((await self.app.eligibility.status(self.fid, self.other))['personal']['status'], 'void')
        self.assertEqual(await self.db.all('SELECT * FROM eligible_user_forecasts'), [])
        for receipt in receipts:
            self.assertIn(receipt, await self.db.all('SELECT * FROM command_receipts'))
        self.assertEqual(await self.db.all('SELECT * FROM reputation_scores'), [])

    async def test_late_side_change_and_withdrawal_cannot_cancel_earlier_loss(self):
        self.now = self.base+50
        earlier = await self.app.submit_forecast(self.other, self.fid, 'NO', 80,
            self.card['revision'], 'before-news-losing-side', 100)
        self.now = self.base+90
        await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
            earlier['forecast']['revision'], 'after-news-withdraw-and-switch', 0)
        self.assertEqual((await self.app.points.summary(self.other))['available'], 1000)
        original = await self.app._forecast(self.fid)
        receipts = await self.db.all('SELECT * FROM command_receipts')
        t = await self.reviewed_trigger()
        await self.hold(t)
        await self.accept(t)
        self.assertEqual((await self.app._forecast(self.fid)).upgrade_source, original)
        for receipt in receipts:
            self.assertIn(receipt, await self.db.all('SELECT * FROM command_receipts'))
        position = await self.app.points.position(self.other, self.fid)
        self.assertEqual((position['amount'], position['outcome']), (100, 'NO'))
        self.assertEqual((await self.app.eligibility.status(self.fid, self.other))['personal']['status'], 'restored')
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        self.now = (await self.app._forecast(self.fid)).challenge_until_ms
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        points = await self.app.points.summary(self.other)
        self.assertEqual((points['available'], points['committed']), (900, 0))
        score = await self.db.first('SELECT outcome,correct,probability FROM reputation_scores WHERE forecast_id=? AND user_id=?', (self.fid, self.other))
        self.assertEqual(score, {'outcome': 'YES', 'correct': 0, 'probability': 20})

    async def test_atomic_eligibility_guard_rechecks_receipts_even_if_precheck_is_stale(self):
        self.now = self.base + 20
        await self.app.submit_forecast(self.other, self.fid, 'YES', 80, self.card['revision'], 'guard-existing-vote', 100)
        t = await self.reviewed_trigger(basis='observed_upper_bound')
        await self.hold(t)
        original = await self.app._forecast(self.fid)
        events = await self.db.all('SELECT * FROM events')
        points = await self.app.points.summary(self.other)
        first, execute = self.db.first, self.db.execute
        stale_read, guarded_write = False, False
        guard_errors = []
        async def stale_review(sql, params=()):
            nonlocal stale_read
            if sql.startswith('SELECT revision FROM forecast_receipt_eligibility') and "status='review'" in sql:
                stale_read = True
                return None
            return await first(sql, params)
        async def observe_completion(sql, params=()):
            nonlocal guarded_write
            if sql.startswith('INSERT OR IGNORE INTO forecast_eligibility_completions'):
                guarded_write = True
            try:
                return await execute(sql, params)
            except sqlite3.IntegrityError as error:
                guard_errors.append(str(error))
                raise
        self.db.first, self.db.execute = stale_review, observe_completion
        try:
            with self.assertRaises(AppError):
                await self.accept(t)
        finally:
            self.db.first, self.db.execute = first, execute
        self.assertTrue(stale_read)
        self.assertTrue(guarded_write)
        self.assertEqual(guard_errors, ['eligibility_incomplete'])
        self.assertEqual(await self.app._forecast(self.fid), original)
        self.assertEqual(await self.db.all('SELECT * FROM events'), events)
        actual = await self.app.points.summary(self.other)
        self.assertEqual((actual['available'], actual['committed']), (points['available'], points['committed']))
        self.assertEqual(await self.app.read_artifact(t.trigger_hash), dumps(t))
        self.assertEqual(await self.db.all('SELECT * FROM forecast_eligibility_completions'), [])
        self.assertEqual((await self.db.first('SELECT status FROM forecast_receipt_eligibility'))['status'], 'review')
        self.assertEqual(await self.db.all('SELECT * FROM mutation_guards'), [])

    async def test_hold_between_vote_read_and_commit_prevents_late_receipt_and_allows_empty_upgrade(self):
        t = await self.reviewed_trigger(basis='observed_upper_bound')
        batch = self.db.batch
        fired = False
        async def hold_before_vote(statements):
            nonlocal fired
            if not fired and any('INSERT INTO user_forecasts' in sql for sql, _ in statements):
                fired = True
                await self.hold(t)
            return await batch(statements)
        self.db.batch = hold_before_vote
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, self.fid, 'YES', 80, self.card['revision'], 'racing-event-vote', 100)
        self.assertEqual(caught.exception.code, 'participation_on_hold')
        self.db.batch = batch
        self.assertEqual(await self.db.all('SELECT * FROM user_forecasts'), [])
        self.assertEqual((await self.app.points.summary(self.other))['available'], 1000)
        await self.accept(t)
        self.assertIs(type(await self.app._forecast(self.fid)), ForecastV2)

    async def test_missing_hold_fails_and_default_disabled_observer_never_calls_model(self):
        t = await self.reviewed_trigger()
        with self.assertRaises(AppError) as caught:
            await self.accept(t)
        self.assertEqual(caught.exception.code, 'early_trigger_changed')
        self.assertEqual(await self.app.automation.run(), {'enabled': False, 'polled': 0, 'reviewed': 0, 'failed': 0})
        result = await self.app.run_automation()
        self.assertFalse(result['sources']['enabled'])
        self.assertEqual(self.ai.source_calls, 0)
        self.assertEqual(self.ai.early_calls, 0)
        self.assertEqual(await self.db.all('SELECT * FROM official_watch_sources'), [])
        # A configured collector must not silently enable default-off polling.
        self.ai.collector = object()
        default_app = type(self.app)(self.db, self.ai, now_ms=lambda: self.now,
                                    random_token=self.random_token, token_hash=self.token_hash)
        self.assertFalse(default_app.automation.enabled)
        self.assertIsNone(default_app.automation.watch)
        self.assertFalse((await default_app.automation.run())['enabled'])
        self.assertEqual(self.ai.source_calls, 0)

    async def market_receipt(self, *, mode="active", at=90):
        """Exercise the real market adapter with explicit test-only funding."""
        self.now = self.base + at
        markets = PointMarkets(self.db, lambda: self.now, self.random_token, live_enabled=True)
        await markets.fund_treasury(700, "automation-market-funding", mode)
        await markets.create(self.fid, mode=mode, expected_specification_hash=self.card['specificationHash'])
        if mode == "active":
            source = "integration-source-" + self.fid
            await self.db.execute(
                "INSERT INTO official_watch_sources(id,url,kind,interval_ms,next_poll,checked_at) VALUES(?,?,'index',60000,?,?)",
                (source, "https://acme.example/news", self.now+60000, self.now))
            await self.db.execute(
                'INSERT INTO official_watch_bindings(forecast_id,source_id,families) VALUES(?,?,?)',
                (self.fid, source, '[]'))
        quote = await markets.quote(self.other, self.fid, "YES", 100)
        return await markets.accept(self.other, self.fid, quote['quoteId'], int(quote['claimsAtomic']), "automation-market-fill")

    async def test_precise_late_active_market_fill_refunds_without_user_forecast_row(self):
        receipt = await self.market_receipt(at=90)
        self.assertEqual(await self.db.all('SELECT * FROM user_forecasts'), [])
        t = await self.reviewed_trigger()
        await self.hold(t)
        original = await self.app._forecast(self.fid)
        fills = await self.db.all('SELECT * FROM market_fills')
        await self.accept(t)
        upgraded = await self.app._forecast(self.fid)
        self.assertEqual(upgraded.state, LifecycleState.LOCKED)
        self.assertEqual(upgraded.upgrade_source, original)
        balances = await self.app.points.summary(self.other)
        self.assertEqual((balances['available'], balances['committed']), (1000, 0))
        self.assertEqual(await self.db.all('SELECT * FROM market_fills'), fills)
        self.assertIsNotNone(await self.db.first('SELECT fill_id FROM market_fill_voids WHERE fill_id=?', (receipt['id'],)))
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        challenge = await self.app._forecast(self.fid)
        self.now = challenge.challenge_until_ms
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        balances = await self.app.points.summary(self.other)
        self.assertEqual((balances['available'], balances['committed']), (1000, 0))
        self.assertEqual(await self.db.all('SELECT * FROM reputation_scores'), [])
        self.assertEqual(await self.db.all('SELECT * FROM market_settlements'), [])

    async def test_observation_bound_blocks_any_active_market_fill_even_before_bound(self):
        await self.market_receipt(at=20)
        self.assertEqual(await self.db.all('SELECT * FROM user_forecasts'), [])
        t = await self.reviewed_trigger(basis='observed_upper_bound')
        await self.hold(t)
        with self.assertRaises(AppError) as caught:
            await self.accept(t)
        self.assertEqual(caught.exception.code, 'early_eligibility_review')
        self.assertEqual((await self.app._forecast(self.fid)).state, LifecycleState.OPEN)
        self.assertEqual(len(await self.db.all('SELECT * FROM market_fills')), 1)

    async def test_shadow_market_fill_does_not_block_observation_bound_upgrade(self):
        await self.market_receipt(mode='shadow', at=90)
        self.assertEqual(await self.db.all('SELECT * FROM user_forecasts'), [])
        t = await self.reviewed_trigger(basis='observed_upper_bound')
        await self.hold(t)
        await self.accept(t)
        self.assertIs(type(await self.app._forecast(self.fid)), ForecastV2)
        self.assertEqual(len(await self.db.all('SELECT * FROM market_fills')), 1)

    async def test_atomic_market_receipt_guard_rejects_stale_empty_precheck(self):
        await self.market_receipt(at=20)
        t = await self.reviewed_trigger(basis='observed_upper_bound')
        await self.hold(t)
        original = await self.app._forecast(self.fid)
        balances = await self.app.points.summary(self.other)
        events = await self.db.all('SELECT * FROM events')
        first, execute = self.db.first, self.db.execute
        stale_read, guarded_write = False, False
        guard_errors = []
        async def stale_market_review(sql, params=()):
            nonlocal stale_read
            if sql.startswith('SELECT 1 FROM market_effective_fills') and 'created_at<?' in sql:
                stale_read = True
                return None
            return await first(sql, params)
        async def observe_completion(sql, params=()):
            nonlocal guarded_write
            if sql.startswith('INSERT OR IGNORE INTO forecast_eligibility_completions'):
                guarded_write = True
            try:
                return await execute(sql, params)
            except sqlite3.IntegrityError as error:
                guard_errors.append(str(error))
                raise
        self.db.first, self.db.execute = stale_market_review, observe_completion
        try:
            with self.assertRaises(AppError):
                await self.accept(t)
        finally:
            self.db.first, self.db.execute = first, execute
        self.assertTrue(stale_read)
        self.assertTrue(guarded_write)
        self.assertEqual(guard_errors, ['market_eligibility_pending'])
        self.assertEqual(await self.app._forecast(self.fid), original)
        self.assertEqual(await self.db.all('SELECT * FROM events'), events)
        self.assertEqual(await self.app.points.summary(self.other), balances)
        self.assertEqual(await self.app.read_artifact(t.trigger_hash), dumps(t))
        self.assertEqual(await self.db.all('SELECT * FROM forecast_eligibility_completions'), [])
        self.assertEqual(await self.db.all('SELECT * FROM market_fill_voids'), [])
        self.assertEqual(await self.db.all('SELECT * FROM mutation_guards'), [])

    async def test_live_market_feature_requires_enabled_source_watch_and_configured_collector(self):
        for collector in (None, object()):
            self.ai.collector = collector
            for watch in (False, True):
                for live in (False, True):
                    with self.subTest(collector=collector is not None, watch=watch, live=live):
                        app = type(self.app)(self.db, self.ai, now_ms=lambda: self.now,
                            random_token=self.random_token, token_hash=self.token_hash,
                            source_watch_enabled=watch, live_markets_enabled=live)
                        expected = live and watch and collector is not None
                        self.assertEqual(app.markets.live_enabled, expected)
                        if not expected:
                            with self.assertRaises(AppError) as caught:
                                await app.markets.fund_treasury(700, self.random_token(), 'active')
                            self.assertEqual(caught.exception.code, 'market_unavailable')
        self.assertEqual(await self.db.all('SELECT * FROM market_funding'), [])


class PublisherFeedMappingTests(unittest.TestCase):
    def test_roots_map_to_readable_feeds_and_everything_else_is_untouched(self) -> None:
        from forecast_application.automation import publisher_feed_url
        self.assertEqual(publisher_feed_url("https://www.apple.com/newsroom/"), "https://www.apple.com/newsroom/rss-feed.rss")
        self.assertEqual(publisher_feed_url("https://news.microsoft.com/"), "https://news.microsoft.com/source/feed/")
        self.assertEqual(publisher_feed_url("https://news.microsoft.com"), "https://news.microsoft.com/source/feed/")
        self.assertEqual(publisher_feed_url("https://news.microsoft.com/source/"), "https://news.microsoft.com/source/feed/")
        article = "https://news.microsoft.com/source/2026/09/09/example/"
        self.assertEqual(publisher_feed_url(article), article)
        self.assertEqual(publisher_feed_url("https://blogs.microsoft.com/"), "https://blogs.microsoft.com/")


class EvidenceReportTests(AutomationIntegrationTests):
    """A forecaster's report enters the watcher path, pauses entry, and earns the reward once accepted."""

    ARTICLE = "https://www.apple.com/newsroom/2026/09/product-x-announced/"
    ARTICLE_HTML = ('<html><head><title>Apple announces Product X</title>'
                    '<meta property="article:published_time" content="2027-01-15T14:00:00Z"></head>'
                    '<body><main><h1>Apple announces Product X</h1><p>Retained official local-test announcement: '
                    'Acme announced Product X, available to order today from apple.com.</p></main></body></html>')

    async def apple_forecast_with_watcher(self):
        from dataclasses import replace

        from forecast_application.automation import ForecastAutomation
        from forecast_application.sources import SourceCollector, TextResponse
        original_compile = self.ai.compile_question

        async def apple_compile(question, candidates, now_ms):
            result = await original_compile(question, candidates, now_ms)
            policy = result.specification.source_policy
            source = replace(policy.primary_sources[0], url="https://www.apple.com/newsroom", is_official=True)
            spec = replace(result.specification, source_policy=replace(policy, primary_sources=(source,)))
            return CompileResult(spec, model.validation(spec, validated_at_ms=now_ms), result.artifacts)
        self.ai.compile_question = apple_compile
        draft = await self.app.compile_forecast(self.uid, "Will Apple officially announce Product X before the deadline?")
        card = (await self.app.publish_forecast(self.uid, draft["draftId"], "publish-report-test"))["forecast"]
        self.fetched = []

        async def fetch(url, method, headers):
            self.fetched.append(url)
            if url == self.ARTICLE:
                return TextResponse(200, self.ARTICLE_HTML, {"content-type": "text/html"})
            return TextResponse(404, "", {"content-type": "text/plain"})
        self.app.ai.collector = SourceCollector(fetch)
        self.app.automation = ForecastAutomation(self.app, enabled=True)
        await self.app.automation.bootstrap()
        return card

    async def test_report_holds_entry_then_accepted_trigger_rewards_the_first_reporter(self):
        card = await self.apple_forecast_with_watcher()
        fid = card["id"]
        before = (await self.app.points.summary(self.other))["available"]
        # Not an official publisher, an official publisher the question does not cite, then the real source.
        with self.assertRaises(AppError) as raised:
            await self.app.report_evidence(self.other, fid, "https://example.com/news/product-x")
        self.assertEqual(raised.exception.code, "evidence_report_url")
        with self.assertRaises(AppError) as raised:
            await self.app.report_evidence(self.other, fid, "https://news.microsoft.com/source/2026/09/09/product-x/")
        self.assertEqual(raised.exception.code, "evidence_report_source")
        report = await self.app.report_evidence(self.other, fid, self.ARTICLE)
        self.assertEqual(report["status"], "held")
        self.assertEqual(self.fetched, [self.ARTICLE])
        self.assertIsNotNone(await self.app.participation_holds.active(fid))
        with self.assertRaises(AppError) as blocked:
            await self.app.submit_forecast(self.uid, fid, "YES", 70, card["revision"], "after-report", 0)
        self.assertEqual(blocked.exception.code, "participation_on_hold")
        self.assertEqual((await self.app.report_evidence(self.other, fid, self.ARTICLE))["duplicate"], True)
        detail = await self.app.forecast_detail(fid, self.other)
        self.assertEqual(detail["evidenceReports"], {"count": 1, "mine": "held", "reward": 100})
        # The review accepts the very evidence the report retained.
        self.fid = fid
        record = await self.app._forecast(fid)
        evidence = model.evidence(record.specification, collected_at_ms=self.base+100, content=self.ARTICLE_HTML, url=self.ARTICLE)
        stored = await self.db.first("SELECT artifact_hash FROM evidence_reports WHERE id=?", (report["reportId"],))
        self.assertEqual(stored["artifact_hash"], evidence.content_sha256)
        trigger = await self.reviewed_trigger(url=self.ARTICLE, content=self.ARTICLE_HTML)
        # The review job records its accepted verdict (as production does) before calling accept.
        await self.db.execute("UPDATE official_source_reviews SET state='reviewed',result=? WHERE forecast_id=?",
                              (json.dumps({"accepted": True, "trigger": to_dict(trigger)}, sort_keys=True, separators=(",", ":")), fid))
        await self.accept(trigger)
        self.assertEqual((await self.app.points.summary(self.other))["available"], before + 100)
        self.assertEqual((await self.db.first("SELECT status FROM evidence_reports WHERE id=?", (report["reportId"],)))["status"], "rewarded")
        entries = (await self.app.points.summary(self.other))["entries"]
        self.assertEqual(entries[0]["kind"], "evidence_reward")
        self.assertEqual(entries[0]["amount"], 100)
        # Accepting again (idempotent replay) cannot pay twice.
        await self.accept(trigger)
        self.assertEqual((await self.app.points.summary(self.other))["available"], before + 100)

    async def test_reports_close_with_the_question_and_are_rate_limited(self):
        card = await self.apple_forecast_with_watcher()
        for index in range(10):
            await self.app.report_evidence(self.other, card["id"], f"https://www.apple.com/newsroom/2026/09/other-{index}/")
        with self.assertRaises(AppError) as raised:
            await self.app.report_evidence(self.other, card["id"], "https://www.apple.com/newsroom/2026/09/eleventh/")
        self.assertEqual(raised.exception.code, "rate_limited")
