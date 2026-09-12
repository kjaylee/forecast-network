"""Evidence-time compensation preserves early fills, balances and audit history."""
from __future__ import annotations

import sqlite3
import unittest

from forecast_application.errors import AppError
from forecast_application.markets import SCALE
from forecast_domain import dumps
from forecast_domain.early_resolution import (
    EarlyResolutionTrigger,
    early_qualification_output_hash,
    early_trigger_input_hash,
)
from forecast_domain.models import (
    AITask,
    Outcome,
    counter_judge_input_hash,
    counter_judge_output_hash,
)

from tests import model_fixtures as model
from tests import test_markets as market_tests


class MarketEligibilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = market_tests.MarketTests.asyncSetUp
    asyncTearDown = market_tests.MarketTests.asyncTearDown
    token = market_tests.MarketTests.token
    opened = market_tests.MarketTests.opened
    user = market_tests.MarketTests.user
    event_sql = staticmethod(market_tests.MarketTests.event_sql)
    market = market_tests.MarketTests.market
    source = market_tests.MarketTests.source
    buy = market_tests.MarketTests.buy
    finalize = market_tests.MarketTests.finalize

    async def decision(self, cutoff, basis='published_instant'):
        evidence = model.evidence(self.forecast.specification, collected_at_ms=cutoff)
        verification = model.source_verification(evidence)
        observed = cutoff if basis == 'observed_upper_bound' else cutoff+10
        fid, spec_hash = self.forecast.forecast_id, self.forecast.specification_hash
        digest = early_trigger_input_hash(fid, spec_hash, 'yes-rule', (evidence,), (verification,), cutoff, observed, basis)
        explanation = 'The retained official announcement meets the exact YES clause.'
        qualifier = model.provenance(AITask.AMBIGUITY_JUDGE, input_hash=digest,
            output_hash=early_qualification_output_hash(digest, explanation), created_at_ms=cutoff+20)
        counter = model.provenance(AITask.COUNTER_JUDGE, provider='independent-provider',
            input_hash=counter_judge_input_hash(digest, qualifier),
            output_hash=counter_judge_output_hash(qualifier.output_hash, True), created_at_ms=cutoff+30)
        trigger = EarlyResolutionTrigger(forecast_id=fid, specification_hash=spec_hash, clause_id='yes-rule',
            evidence=(evidence,), source_verifications=(verification,), event_at_ms=cutoff, observed_at_ms=observed,
            event_time_basis=basis, qualification=explanation, qualifier=qualifier, counter_qualifier=counter)
        self.now = max(self.now, cutoff+40)
        body = dumps(trigger)
        await self.db.batch([
            ('INSERT INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)',
             (trigger.trigger_hash, 'early_resolution_trigger', body, 'application/json', self.now)),
            ('INSERT INTO forecast_eligibility_decisions(id,forecast_id,specification_hash,cutoff_at,event_time_basis,created_at,body) VALUES(?,?,?,?,?,?,?)',
             (trigger.trigger_hash, fid, spec_hash, cutoff, basis, self.now, body)),
        ])
        return trigger.trigger_hash

    async def complete(self, decision):
        await self.db.execute('INSERT INTO forecast_eligibility_completions(decision_id,created_at) VALUES(?,?)', (decision, self.now))

    async def raw_void(self, fill_id, decision, **changes):
        fill = await self.db.first('SELECT * FROM market_fills WHERE id=?', (fill_id,))
        market = await self.db.first('SELECT * FROM point_markets WHERE forecast_id=?', (fill['forecast_id'],))
        account = await self.markets._account(fill['user_id'], market['mode'])
        values = dict(fill_id=fill_id, decision_id=decision, user_id=fill['user_id'], forecast_id=fill['forecast_id'],
            mode=market['mode'], spend=fill['spend'], claims_atomic=fill['claims_atomic'], side=fill['side'],
            available_before=account['available'], committed_before=account['committed'],
            reserve_before_atomic=market['reserve_atomic'], created_at=self.now)
        values.update(changes)
        await self.db.execute('INSERT INTO market_fill_voids('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')', tuple(values.values()))

    async def test_database_rejects_forged_refund_and_non_suffix_order(self):
        await self.market(mode='active')
        early = await self.buy()
        self.now += 1
        cutoff = self.now
        first = await self.buy(uid='user-b')
        last = await self.buy(uid='user-c', side='NO')
        decision = await self.decision(cutoff)
        baseline = await self.db.all('SELECT * FROM point_accounts ORDER BY user_id')
        for fill, changes in (
            (early, {}), (first, {}), (last, {'spend': 99}), (last, {'claims_atomic': 1}),
            (last, {'side': 'YES'}), (last, {'user_id': 'user-b'}),
            (last, {'mode': 'shadow'}), (last, {'reserve_before_atomic': 1}),
            (last, {'decision_id': '0'*64}), (last, {'created_at': cutoff-1}),
        ):
            with self.subTest(fill=fill['id'], changes=changes):
                with self.assertRaises(sqlite3.IntegrityError):
                    await self.raw_void(fill['id'], decision, **changes)
                self.assertEqual(await self.db.all('SELECT * FROM point_accounts ORDER BY user_id'), baseline)
                self.assertEqual(await self.db.all('SELECT * FROM market_fill_voids'), [])
        self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['status'], 'completed')

    async def test_earlier_losing_claim_remains_a_loss_and_void_gets_no_win(self):
        await self.market(mode='active')
        await self.buy(side='NO')
        cutoff = self.now+1
        self.now = cutoff
        await self.buy(uid='user-b', side='YES')
        decision = await self.decision(cutoff)
        await self.markets.void_after_evidence('market-one', decision)
        await self.complete(decision)
        await self.finalize('market-one', Outcome.YES)
        await self.markets.settle('market-one')
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['availablePoints'], 900)
        self.assertEqual((await self.markets.positions('user-b', 'market-one'))['availablePoints'], 1000)
        self.assertEqual(await self.db.all('SELECT user_id,payout_atomic FROM market_settlements'), [{'user_id': 'user-a', 'payout_atomic': 0}])
        self.assertEqual((await self.markets.budget('active'))['availableAtomic'], str(800*SCALE))

    async def test_exact_cutoff_voids_both_sides_preserves_early_claims_and_prices(self):
        await self.market(mode='active')
        early = await self.buy(spend=75)
        cutoff = self.now+50
        self.now = cutoff
        late_yes = await self.buy(uid='user-b', spend=80)
        self.now += 1
        late_no = await self.buy(side='NO', spend=60)
        original = await self.db.all('SELECT * FROM market_fills ORDER BY revision')
        history = (await self.db.first('SELECT state,state_hash,revision FROM point_markets'))
        decision = await self.decision(cutoff)
        before = await self.markets.get('market-one')
        self.assertEqual(before['yesProbabilityBps'], early['priceAfterBps'])
        self.assertEqual(before['probabilityStatus'], 'frozen_before_evidence')
        with self.assertRaises(sqlite3.IntegrityError):
            await self.complete(decision)
        first = await self.markets.void_after_evidence('market-one', decision, limit=1)
        self.assertEqual(first['status'], 'pending')
        self.assertIsNotNone(await self.db.first('SELECT 1 FROM market_fill_voids WHERE fill_id=?', (late_no['id'],)))
        result = await self.markets.void_after_evidence('market-one', decision)
        self.assertEqual(result['status'], 'completed')
        await self.complete(decision)
        position = await self.markets.positions('user-a', 'market-one')
        self.assertEqual((position['grossPoints'], position['yesClaimsAtomic'], position['noClaimsAtomic']), (75, early['claimsAtomic'], '0'))
        self.assertEqual((position['availablePoints'], position['committedPoints']), (925, 75))
        loser = await self.markets.positions('user-b', 'market-one')
        self.assertEqual((loser['availablePoints'], loser['committedPoints'], loser['settled']), (1000, 0, True))
        self.assertEqual(await self.db.all('SELECT * FROM market_fills ORDER BY revision'), original)
        self.assertEqual(await self.db.first('SELECT state,state_hash,revision FROM point_markets'), history)
        replay = await self.markets.accept('user-b', 'market-one', late_yes['quoteId'], int(late_yes['claimsAtomic']), original[1]['idempotency_key'])
        self.assertEqual((replay['status'], replay['refundedPoints']), ('void', 80))
        self.assertEqual((await self.markets.get('market-one'))['refundedPoints'], 140)
        for operation in ('UPDATE market_fill_voids SET spend=1', 'DELETE FROM market_fill_voids'):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute(operation)
        with self.assertRaises(AppError):
            await self.markets.quote('user-c', 'market-one', 'YES', 1)
        await self.finalize('market-one')
        await self.markets.settle('market-one')
        paid = await self.markets.positions('user-a', 'market-one')
        self.assertEqual((paid['availablePoints']-925)*SCALE+int(paid['fractionAtomic']), int(early['claimsAtomic']))
        self.assertEqual(int((await self.markets.budget('active'))['availableAtomic'])+int(early['claimsAtomic']), 775*SCALE)

    async def test_all_late_refunds_are_exact_once_and_subsidy_returns_at_closure(self):
        await self.market(mode='active')
        cutoff = self.now
        await self.buy()
        await self.buy(uid='user-b', side='NO', spend=99)
        decision = await self.decision(cutoff)
        self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['processed'], 2)
        accounts = await self.db.all('SELECT * FROM point_accounts ORDER BY user_id')
        reserve = await self.db.first('SELECT reserve_atomic FROM point_markets')
        self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['processed'], 0)
        self.assertEqual(await self.db.all('SELECT * FROM point_accounts ORDER BY user_id'), accounts)
        self.assertEqual(await self.db.first('SELECT reserve_atomic FROM point_markets'), reserve)
        self.assertEqual((await self.markets.get('market-one'))['yesProbabilityBps'], 5000)
        await self.complete(decision)
        await self.finalize('market-one')
        self.assertEqual((await self.markets.settle('market-one'))['status'], 'settled')
        self.assertEqual((await self.markets.budget('active'))['availableAtomic'], str(700*SCALE))
        self.assertEqual(await self.db.all('SELECT * FROM market_settlements'), [])

    async def test_observation_bound_refunds_proven_late_but_holds_ambiguous_early(self):
        await self.market(mode='active')
        early = await self.buy()
        self.now += 20
        cutoff = self.now
        await self.buy(uid='user-b')
        decision = await self.decision(cutoff, 'observed_upper_bound')
        result = await self.markets.void_after_evidence('market-one', decision)
        self.assertEqual((result['status'], result['processed']), ('review', 1))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.complete(decision)
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['yesClaimsAtomic'], early['claimsAtomic'])
        self.assertIsNone((await self.markets.get('market-one'))['yesProbabilityBps'])

    async def test_clock_inversion_is_held_without_repricing_or_issuing_refunds(self):
        await self.market(mode='active')
        cutoff = self.now+50
        self.now = cutoff
        await self.buy()
        self.now = cutoff-1
        await self.buy(uid='user-b')
        decision = await self.decision(cutoff)
        self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['status'], 'review')
        self.assertEqual(await self.db.all('SELECT * FROM market_fill_voids'), [])
        self.assertIsNone((await self.markets.get('market-one'))['yesProbabilityBps'])
        with self.assertRaises(sqlite3.IntegrityError):
            await self.complete(decision)

    async def test_corrupt_reserve_and_position_keep_completion_closed(self):
        for corruption in ('UPDATE point_markets SET reserve_atomic=1', 'UPDATE market_positions SET gross=gross+1'):
            with self.subTest(corruption=corruption):
                await self.market(mode='active')
                cutoff = self.now
                await self.buy()
                decision = await self.decision(cutoff)
                await self.db.execute(corruption)
                self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['status'], 'review')
                self.assertEqual(await self.db.all('SELECT * FROM market_fill_voids'), [])
                with self.assertRaises(sqlite3.IntegrityError):
                    await self.complete(decision)
                await self.asyncTearDown()
                await self.asyncSetUp()

    async def test_any_existing_settlement_requires_review(self):
        await self.market(mode='active')
        cutoff = self.now
        await self.buy()
        await self.buy(uid='user-b')
        await self.finalize('market-one')
        await self.markets.settle('market-one', limit=1)
        decision = await self.decision(cutoff)
        self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['status'], 'review')
        self.assertEqual(await self.db.all('SELECT * FROM market_fill_voids'), [])

    async def test_lost_acknowledgement_and_account_race_never_double_refund(self):
        await self.market(mode='active')
        cutoff = self.now
        await self.buy()
        decision = await self.decision(cutoff)
        original = self.db.execute
        injected = False
        async def lose_ack(sql, params=()):
            nonlocal injected
            result = await original(sql, params)
            if sql.startswith('INSERT INTO market_fill_voids') and not injected:
                injected = True
                raise RuntimeError('lost transport acknowledgement')
            return result
        self.db.execute = lose_ack
        self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['status'], 'completed')
        self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['processed'], 0)
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['availablePoints'], 1000)

    async def test_account_cas_conflict_rolls_back_refund_then_retries(self):
        await self.market(mode='active')
        cutoff = self.now
        await self.buy()
        decision = await self.decision(cutoff)
        original = self.db.execute
        injected = False
        async def race(sql, params=()):
            nonlocal injected
            if sql.startswith('INSERT INTO market_fill_voids') and not injected:
                injected = True
                await original("UPDATE point_accounts SET available=available-1 WHERE user_id='user-a'")
            return await original(sql, params)
        self.db.execute = race
        with self.assertRaises(AppError):
            await self.markets.void_after_evidence('market-one', decision)
        self.assertEqual(await self.db.all('SELECT * FROM market_fill_voids'), [])
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['committedPoints'], 100)
        self.assertEqual((await self.markets.void_after_evidence('market-one', decision))['status'], 'completed')
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['availablePoints'], 999)

    async def test_shadow_refunds_never_touch_real_accounts(self):
        await self.market()
        real = await self.db.all('SELECT * FROM point_accounts ORDER BY user_id')
        cutoff = self.now
        await self.buy()
        decision = await self.decision(cutoff)
        await self.markets.void_after_evidence('market-one', decision)
        self.assertEqual(await self.db.all('SELECT * FROM point_accounts ORDER BY user_id'), real)
        await self.complete(decision)
        await self.finalize('market-one', Outcome.INVALID)
        await self.markets.settle('market-one')
        self.assertEqual((await self.markets.budget())['availableAtomic'], str(700*SCALE))

    async def test_receipt_status_is_read_only_and_absence_requires_persisted_cutoff(self):
        await self.market(mode='active')
        quote = await self.markets.quote('user-a', 'market-one', 'YES', 100)
        cutoff = self.now
        changes = self.connection.total_changes
        status = await self.markets.receipt_status('user-a', 'market-one', quote['quoteId'], 'uncertain-operation')
        self.assertEqual(status, {'forecastId': 'market-one', 'userId': 'user-a', 'quoteId': quote['quoteId'], 'status': 'pending', 'receipt': None})
        self.assertEqual(self.connection.total_changes, changes)
        # Expiration alone does not prove a previously dispatched request failed.
        self.now = quote['expiresAt']+1
        self.assertEqual((await self.markets.receipt_status('user-a', 'market-one', quote['quoteId'], 'uncertain-operation'))['status'], 'pending')
        await self.decision(cutoff)
        changes = self.connection.total_changes
        queries = []
        original = self.db.first
        async def observe(sql, params=()):
            queries.append(sql)
            return await original(sql, params)
        self.db.first = observe
        try:
            status = await self.markets.receipt_status('user-a', 'market-one', quote['quoteId'], 'uncertain-operation')
        finally:
            self.db.first = original
        self.assertEqual((status['status'], status['receipt']), ('not_accepted', None))
        self.assertEqual(len(queries), 1)
        self.assertIn('forecast_eligibility_decisions', queries[0])
        self.assertIn('market_fills', queries[0])
        self.assertEqual(self.connection.total_changes, changes)

    async def test_receipt_status_never_exposes_other_users_or_conflicting_operations(self):
        await self.market(mode='active')
        quote = await self.markets.quote('user-a', 'market-one', 'YES', 100)
        await self.markets.accept('user-a', 'market-one', quote['quoteId'], int(quote['claimsAtomic']), 'accepted-operation')
        another_quote = await self.markets.quote('user-a', 'market-one', 'NO', 1)
        for user, fid, qid, key, code in (
            ('user-b', 'market-one', quote['quoteId'], 'accepted-operation', 404),
            ('user-a', 'other-market', quote['quoteId'], 'accepted-operation', 404),
            ('user-a', 'market-one', 'missing-quote', 'accepted-operation', 404),
            ('user-a', 'market-one', quote['quoteId'], 'different-operation', 409),
            ('user-a', 'market-one', another_quote['quoteId'], 'accepted-operation', 409),
        ):
            with self.subTest(user=user, fid=fid, quote=qid, key=key), self.assertRaises(AppError) as caught:
                await self.markets.receipt_status(user, fid, qid, key)
            self.assertEqual(caught.exception.status, code)

    async def test_receipt_status_preserves_accepted_and_void_receipts_after_close(self):
        await self.market(mode='active')
        early = await self.buy()
        cutoff = self.now+1
        self.now = cutoff
        late = await self.buy(uid='user-b')
        fills = await self.db.all('SELECT * FROM market_fills ORDER BY revision')
        before = await self.markets.receipt_status('user-a', 'market-one', early['quoteId'], fills[0]['idempotency_key'])
        self.assertEqual((before['status'], before['receipt']['id']), ('accepted', early['id']))
        decision = await self.decision(cutoff)
        await self.markets.void_after_evidence('market-one', decision)
        await self.complete(decision)
        await self.finalize('market-one')
        await self.markets.settle('market-one')
        changes = self.connection.total_changes
        accepted = await self.markets.receipt_status('user-a', 'market-one', early['quoteId'], fills[0]['idempotency_key'])
        voided = await self.markets.receipt_status('user-b', 'market-one', late['quoteId'], fills[1]['idempotency_key'])
        self.assertEqual(accepted, before)
        self.assertEqual((voided['status'], voided['receipt']['id'], voided['receipt']['refundedPoints']), ('void', late['id'], 100))
        self.assertEqual(self.connection.total_changes, changes)
