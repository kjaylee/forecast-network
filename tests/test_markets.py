"""Transactional market, reserve, quote provenance and legacy compatibility checks."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path

from forecast_application.database import SQLiteDatabase
from forecast_application.errors import AppError
from forecast_application.markets import SCALE, PointMarkets
from forecast_application.points import PointsService
from forecast_domain.models import Outcome
from forecast_domain.pricing import PricingPolicy

from tests import test_points

ROOT = Path(__file__).resolve().parents[1]


class MarketTests(unittest.IsolatedAsyncioTestCase):
    opened = test_points.PointsTests.opened
    user = test_points.PointsTests.user
    event_sql = staticmethod(test_points.PointsTests.event_sql)
    finalize = test_points.PointsTests.finalize
    reserve = test_points.PointsTests.reserve

    async def asyncSetUp(self):
        self.connection = sqlite3.connect(':memory:')
        for migration in sorted((ROOT/'apps/web/migrations').glob('*.sql')):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now, self.counter = 1_000_000, 0
        self.markets = PointMarkets(self.db, lambda: self.now, self.token, live_enabled=True)
        for uid in ('user-a', 'user-b', 'user-c'):
            await self.user(uid)
        self.forecast = await self.opened('market-one')

    def token(self):
        self.counter += 1
        return 'test-market-token-'+str(self.counter)

    async def asyncTearDown(self):
        self.connection.close()

    async def market(self, fid='market-one', mode='shadow', amount=700, policy=None):
        await self.markets.fund_treasury(amount, self.token(), mode)
        row = await self.db.first('SELECT specification_hash FROM forecasts WHERE id=?', (fid,))
        await self.markets.create(fid, policy, mode, row['specification_hash'])
        if mode == 'active':
            await self.source(fid)

    async def source(self, fid):
        await self.db.execute("INSERT OR IGNORE INTO official_watch_sources(id,url,kind,interval_ms,next_poll,checked_at) VALUES(?,?,'index',60000,?,?)",
                              ('source:'+fid, 'https://example.com/'+fid, self.now+60000, self.now))
        await self.db.execute('INSERT OR IGNORE INTO official_watch_bindings(forecast_id,source_id,families) VALUES(?,?,?)',
                              (fid, 'source:'+fid, '[]'))

    async def buy(self, uid='user-a', fid='market-one', side='YES', spend=100):
        quote = await self.markets.quote(uid, fid, side, spend)
        return await self.markets.accept(uid, fid, quote['quoteId'], int(quote['claimsAtomic']), self.token())

    async def hold(self, fid='market-one'):
        row = await self.db.first('SELECT specification_hash FROM forecasts WHERE id=?', (fid,))
        await self.db.execute("INSERT INTO participation_hold_events(id,forecast_id,revision,action,hold_id,specification_hash,reason,evidence_url,actor,request_key,request_hash,body,created_at) "
                              "VALUES(?,?,1,'hold','hold',?,'known_outcome_review','https://example.com/news','authenticated_admin',?,?,'{}',?)",
                              (self.token(), fid, row['specification_hash'], self.token(), 'a'*64, self.now))

    async def test_preview_neither_funds_nor_opens_accounts(self):
        before = self.connection.total_changes
        preview = await self.markets.preview('market-one', 'YES', 100)
        self.assertTrue(preview['nonbinding'])
        self.assertEqual(preview['mode'], 'preview')
        self.assertEqual(preview['priceBeforeBps'], 5000)
        self.assertGreater(preview['priceAfterBps'], 5000)
        self.assertGreater(int(preview['claimsAtomic']), 190*SCALE)
        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual((await self.markets.budget())['issuedAtomic'], '0')

    async def test_default_active_disabled_and_no_automatic_funding(self):
        service = PointMarkets(self.db, lambda: self.now, self.token)
        with self.assertRaises(AppError):
            await service.fund_treasury(700, 'fund', 'active')
        with self.assertRaises(AppError):
            await self.markets.create('market-one', expected_specification_hash=self.forecast.specification_hash)
        self.assertIsNone(await self.markets.get('market-one'))
        self.assertEqual((await self.markets.budget())['availableAtomic'], '0')

    async def test_treasury_cap_and_idempotency(self):
        await self.markets.fund_treasury(20000, 'fund')
        await self.markets.fund_treasury(20000, 'fund')
        with self.assertRaises(AppError):
            await self.markets.fund_treasury(1, 'extra')
        with self.assertRaises(AppError):
            await self.markets.fund_treasury(1, 'fund')
        self.assertEqual((await self.markets.budget())['availableAtomic'], str(20000*SCALE))

    async def test_shadow_buy_isolated_price_moves_and_quote_stales(self):
        await self.market()
        real = await PointsService(self.db).summary('user-a')
        first = await self.markets.quote('user-a', 'market-one', 'YES', 100)
        other = await self.markets.quote('user-b', 'market-one', 'YES', 100)
        receipt = await self.markets.accept('user-a', 'market-one', first['quoteId'], int(first['claimsAtomic']), 'one')
        self.assertEqual(receipt['claimsAtomic'], first['claimsAtomic'])
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['availablePoints'], 900)
        self.assertEqual(await PointsService(self.db).summary('user-a'), real)
        with self.assertRaises(AppError):
            await self.markets.accept('user-b', 'market-one', other['quoteId'], 0, 'stale')
        fresh = await self.markets.quote('user-b', 'market-one', 'YES', 100)
        self.assertLess(int(fresh['claimsAtomic']), int(first['claimsAtomic']))

    async def test_owner_expiry_minimum_and_duplicate_receipt_after_close(self):
        await self.market()
        quote = await self.markets.quote('user-a', 'market-one', 'YES', 1)
        with self.assertRaises(AppError):
            await self.markets.accept('user-b', 'market-one', quote['quoteId'], 0, 'stolen')
        with self.assertRaises(AppError):
            await self.markets.accept('user-a', 'market-one', quote['quoteId'], int(quote['claimsAtomic'])+1, 'slippage')
        accepted = await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'accepted')
        await self.finalize('market-one')
        replay = await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'accepted')
        self.assertEqual(replay, accepted)
        with self.assertRaises(AppError):
            await self.markets.accept('user-a', 'market-one', quote['quoteId'], 1, 'accepted')

    async def test_quote_expiration_and_immutability(self):
        await self.market()
        quote = await self.markets.quote('user-a', 'market-one', 'YES', 1)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE market_quotes SET body='{}' WHERE id=?", (quote['quoteId'],))
        self.now = quote['expiresAt']
        with self.assertRaises(AppError):
            await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'late')
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['committedPoints'], 0)

    async def test_hold_between_read_and_batch_blocks_without_debit(self):
        await self.market()
        quote = await self.markets.quote('user-a', 'market-one', 'YES', 100)
        original = self.db.batch
        injected = False

        async def interleaved(statements):
            nonlocal injected
            if not injected and any('INSERT INTO market_fills' in sql for sql, _ in statements):
                injected = True
                await self.hold()
            return await original(statements)

        self.db.batch = interleaved
        with self.assertRaises(AppError):
            await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'held')
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['availablePoints'], 1000)
        self.assertEqual((await self.db.first('SELECT COUNT(*) n FROM market_fills'))['n'], 0)

    async def test_concurrent_accepts_same_revision_only_one_commits(self):
        await self.market()
        qa = await self.markets.quote('user-a', 'market-one', 'YES', 100)
        qb = await self.markets.quote('user-b', 'market-one', 'NO', 100)
        result = await asyncio.gather(self.markets.accept('user-a', 'market-one', qa['quoteId'], 0, 'a'),
                                      self.markets.accept('user-b', 'market-one', qb['quoteId'], 0, 'b'), return_exceptions=True)
        self.assertEqual(sum(isinstance(item, dict) for item in result), 1)
        self.assertEqual((await self.db.first('SELECT COUNT(*) n FROM market_fills'))['n'], 1)
        self.assertEqual((await self.markets.get('market-one'))['revision'], 1)

    async def test_lost_ack_does_not_double_fill_or_fund(self):
        await self.market()
        quote = await self.markets.quote('user-a', 'market-one', 'YES', 100)
        original = self.db.batch

        async def lost(statements):
            result = await original(statements)
            if any('INSERT INTO market_fills' in sql for sql, _ in statements):
                raise OSError('lost acknowledgement')
            return result

        self.db.batch = lost
        first = await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'lost')
        self.assertEqual(await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'lost'), first)
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['availablePoints'], 900)

    async def test_owner_gross_limit_counts_both_sides(self):
        await self.market()
        await self.buy(side='YES')
        await self.buy(side='NO')
        await self.buy(side='YES')
        with self.assertRaises(AppError):
            await self.buy(side='NO', spend=1)
        self.assertEqual((await self.markets.positions('user-a', 'market-one'))['grossPoints'], 300)

    async def test_active_requires_fresh_source_and_rechecks_child(self):
        await self.markets.fund_treasury(700, 'fund', 'active')
        await self.markets.create('market-one', mode='active', expected_specification_hash=self.forecast.specification_hash)
        with self.assertRaises(AppError):
            await self.markets.quote('user-a', 'market-one', 'YES', 100)
        await self.source('market-one')
        quote = await self.markets.quote('user-a', 'market-one', 'YES', 100)
        await self.db.execute("INSERT INTO official_watch_sources(id,url,kind,parent_id,interval_ms,next_poll) VALUES('new-news','https://example.com/new','article','source:market-one',60000,?)", (self.now,))
        with self.assertRaises(AppError):
            await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'unsafe')
        await self.db.execute('UPDATE official_watch_sources SET checked_at=? WHERE id=?', (self.now, 'new-news'))
        await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'safe')
        self.assertEqual((await PointsService(self.db).summary('user-a'))['available'], 900)

    async def test_active_rejects_existing_legacy_stake_and_preserves_contract(self):
        await self.reserve('user-a', 'market-one', 100)
        await self.markets.fund_treasury(700, 'fund', 'active')
        with self.assertRaises(AppError):
            await self.markets.create('market-one', mode='active', expected_specification_hash=self.forecast.specification_hash)
        self.assertIsNone(await self.markets.get('market-one'))
        self.assertEqual((await PointsService(self.db).position('user-a', 'market-one'))['amount'], 100)

    async def test_active_blocks_future_legacy_stakes_but_practice_allowed(self):
        await self.market(mode='active')
        with self.assertRaises(sqlite3.IntegrityError):
            await self.reserve('user-a', 'market-one', 100)
        await self.reserve('user-a', 'market-one', 0)
        self.assertEqual((await PointsService(self.db).position('user-a', 'market-one'))['status'], 'practice')
        await self.buy()
        self.assertEqual((await PointsService(self.db).summary('user-a'))['available'], 900)

    async def test_legacy_other_market_and_live_account_snapshot_race(self):
        await self.market(mode='active')
        await self.opened('legacy-other')
        quote = await self.markets.quote('user-a', 'market-one', 'YES', 100)
        original = self.db.batch
        injected = False

        async def raced(statements):
            nonlocal injected
            if not injected and any('INSERT INTO market_fills' in sql for sql, _ in statements):
                injected = True
                await self.reserve('user-a', 'legacy-other', 950)
            return await original(statements)

        self.db.batch = raced
        with self.assertRaises(AppError):
            await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'race')
        self.assertEqual((await PointsService(self.db).summary('user-a'))['available'], 50)
        self.assertEqual((await self.db.first('SELECT COUNT(*) n FROM market_fills'))['n'], 0)

    async def test_partial_settlement_dust_identity_replay_and_surplus_once(self):
        await self.market()
        yes = await self.buy()
        await self.buy(uid='user-b', side='NO')
        await self.finalize('market-one')
        with self.assertRaises(AppError):
            await self.markets.quote('user-c', 'market-one', 'YES', 1)
        first = await self.markets.settle('market-one', limit=1)
        self.assertEqual(first['remaining'], 1)
        self.assertEqual(first['status'], 'settling')
        self.assertEqual(int(first['reserveAtomic']), 900*SCALE-int(yes['claimsAtomic']))
        balance = await self.markets.positions('user-a', 'market-one')
        self.assertEqual((balance['availablePoints']-900)*SCALE+int(balance['fractionAtomic']), int(yes['claimsAtomic']))
        second = await self.markets.settle('market-one', limit=1)
        self.assertEqual(second['status'], 'settled')
        budget = await self.markets.budget()
        self.assertEqual(int(budget['availableAtomic']), 900*SCALE-int(yes['claimsAtomic']))
        await self.markets.settle('market-one')
        self.assertEqual(await self.markets.budget(), budget)
        self.assertEqual((await self.markets.positions('user-b', 'market-one'))['availablePoints'], 900)
        self.assertEqual((await PointsService(self.db).summary('user-a'))['available'], 1000)

    async def test_invalid_returns_principal_and_entire_subsidy(self):
        await self.market(mode='active')
        await self.buy()
        await self.buy(uid='user-b', side='NO', spend=99)
        await self.finalize('market-one', Outcome.INVALID)
        await self.markets.settle('market-one')
        for uid in ('user-a', 'user-b'):
            self.assertEqual((await PointsService(self.db).summary(uid))['available'], 1000)
            self.assertEqual((await PointsService(self.db).summary(uid))['committed'], 0)
        self.assertEqual((await self.markets.budget('active'))['availableAtomic'], str(700*SCALE))

    async def test_dust_carries_between_two_market_settlements(self):
        await self.market(mode='active')
        second = await self.opened('market-two')
        await self.market('market-two', mode='active')
        one = await self.buy(spend=1)
        two = await self.buy(fid=second.forecast_id, spend=1)
        await self.finalize('market-one')
        await self.finalize('market-two')
        await self.markets.settle('market-one')
        await self.markets.settle('market-two')
        account = await self.markets.positions('user-a', 'market-two')
        total = int(one['claimsAtomic'])+int(two['claimsAtomic'])
        self.assertEqual(account['availablePoints'], 998+total//SCALE)
        self.assertEqual(int(account['fractionAtomic']), total % SCALE)
        row = await self.db.first('SELECT SUM(available_delta) a,SUM(committed_delta) c FROM market_account_ledger WHERE user_id=?', ('user-a',))
        self.assertEqual(account['availablePoints'], 1000+row['a'])
        self.assertEqual(row['c'], 0)

    async def test_settlement_failure_rolls_back_and_lost_ack_replays(self):
        await self.market()
        await self.buy()
        await self.finalize('market-one')
        await self.db.execute("CREATE TRIGGER reject_credit BEFORE INSERT ON market_account_ledger WHEN NEW.kind='market_settlement' BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(AppError):
            await self.markets.settle('market-one')
        self.assertEqual((await self.markets.get('market-one'))['status'], 'open')
        self.assertEqual((await self.db.first('SELECT COUNT(*) n FROM market_settlements'))['n'], 0)
        await self.db.execute('DROP TRIGGER reject_credit')
        original = self.db.batch

        async def lost(statements):
            await original(statements)
            raise OSError('lost acknowledgement')

        self.db.batch = lost
        self.assertEqual((await self.markets.settle('market-one'))['status'], 'settled')
        self.assertEqual((await self.db.first('SELECT COUNT(*) n FROM market_settlements'))['n'], 1)

    async def test_timing_finding_racing_settlement_preserves_reserve_balance_and_receipts(self):
        await self.market(mode='active')
        await self.buy()
        await self.finalize('market-one')
        before_account = await self.markets.positions('user-a', 'market-one')
        before_budget = await self.markets.budget('active')
        original = self.db.batch
        fired = False

        async def pending_timing_review(statements):
            nonlocal fired
            if not fired and any('INSERT INTO market_settlements' in sql for sql, _ in statements):
                fired = True
                await self.db.execute("INSERT INTO artifacts VALUES(?,'early-resolution-trigger','{}','application/json',?)",
                                      ('a'*64, self.now))
                await self.db.execute('INSERT INTO forecast_timing_reviews VALUES(?,?,?,?,?,?)',
                    ('market-one', self.forecast.specification_hash, 'a'*64, self.now, 'published_instant', self.now))
            return await original(statements)

        self.db.batch = pending_timing_review
        with self.assertRaises(AppError):
            await self.markets.settle('market-one')
        self.assertEqual(await self.markets.positions('user-a', 'market-one'), before_account)
        self.assertEqual(await self.markets.budget('active'), before_budget)
        self.assertEqual((await self.markets.get('market-one'))['status'], 'open')
        self.assertEqual(await self.db.all('SELECT * FROM market_settlements'), [])
        self.assertEqual(await self.db.all('SELECT * FROM market_closures'), [])

    async def test_policy_pilot_bounds_and_invalid_inputs(self):
        await self.markets.fund_treasury(700, 'fund')
        policy = replace(PricingPolicy(), maximum_fill_atomic=101*SCALE)
        with self.assertRaises(AppError):
            await self.markets.create('market-one', policy, expected_specification_hash=self.forecast.specification_hash)
        for spend in (True, 0, 101, 1.5, '1'):
            with self.assertRaises(AppError):
                await self.markets.preview('market-one', 'YES', spend)
        with self.assertRaises(AppError):
            await self.markets.preview('market-one', 'INVALID', 1)

    async def test_receipts_policies_and_settlements_are_immutable(self):
        await self.market()
        await self.buy()
        for table in ('market_quotes', 'market_fills', 'market_account_ledger', 'market_funding'):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute('DELETE FROM '+table)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE point_markets SET mode='active'")
        row = await self.db.first('SELECT body FROM market_fills')
        self.assertIn('state', json.loads(row['body']))

    async def test_terminal_empty_market_returns_reserve_without_claims(self):
        await self.market()
        await self.finalize('market-one', Outcome.NO)
        result = await self.markets.settle('market-one')
        self.assertEqual(result['status'], 'settled')
        self.assertEqual(result['processed'], 0)
        self.assertEqual((await self.markets.budget())['availableAtomic'], str(700*SCALE))

    async def test_treasury_admission_is_atomic_and_daily_limited(self):
        await self.markets.fund_treasury(3500, 'five-markets')
        for index in range(5):
            fid = 'market-'+str(index)
            forecast = await self.opened(fid)
            await self.markets.create(fid, expected_specification_hash=forecast.specification_hash)
        self.assertEqual((await self.markets.budget())['availableAtomic'], '0')
        await self.markets.fund_treasury(700, 'sixth-reserve')
        with self.assertRaises(AppError):
            await self.markets.create('market-one', expected_specification_hash=self.forecast.specification_hash)
        self.assertEqual((await self.markets.budget())['availableAtomic'], str(700*SCALE))

    async def test_owner_unsettled_cap_across_markets(self):
        # Verified legacy reward permits a 1,500-point balance; it does not prove
        # a unique human and does not change the independent 1,000-point cap.
        await test_points.PointsTests.wallet(self, 'user-a', 'verified-test-address')
        for index in range(4):
            fid = 'portfolio-'+str(index)
            await self.opened(fid)
            await self.market(fid, mode='active')
            for _ in range(3 if index < 3 else 1):
                await self.buy(fid=fid)
        self.assertEqual((await PointsService(self.db).summary('user-a'))['available'], 500)
        with self.assertRaises(AppError):
            await self.buy(fid='portfolio-3', spend=1)

    async def test_settlement_does_not_require_live_buy_switch(self):
        await self.market(mode='active')
        await self.buy()
        await self.finalize('market-one', Outcome.INVALID)
        disabled = PointMarkets(self.db, lambda: self.now, self.token)
        self.assertEqual((await disabled.settle('market-one'))['status'], 'settled')
        self.assertEqual((await PointsService(self.db).summary('user-a'))['available'], 1000)

    async def test_account_summary_merges_market_audit_and_preserves_dust(self):
        await self.market(mode='active')
        accepted = await self.buy(spend=1)
        before = await PointsService(self.db).summary('user-a')
        self.assertIn('market_buy', [e['kind'] for e in before['entries']])
        await self.finalize('market-one')
        await self.markets.settle('market-one')
        after = await PointsService(self.db).summary('user-a')
        self.assertIn('market_settlement', [e['kind'] for e in after['entries']])
        self.assertEqual(int(after['fractionAtomic']), int(accepted['claimsAtomic']) % SCALE)
        self.assertEqual(after['atomicScale'], SCALE)

    async def test_expired_or_disabled_bound_sources_block_real_quotes(self):
        await self.market(mode='active')
        for statement in ('UPDATE official_watch_sources SET checked_at=1',
                          'UPDATE official_watch_sources SET enabled=0',
                          'UPDATE official_watch_sources SET failure_count=1'):
            await self.db.execute(statement)
            with self.assertRaises(AppError):
                await self.markets.quote('user-a', 'market-one', 'YES', 1)
            await self.db.execute('UPDATE official_watch_sources SET enabled=1,failure_count=0,checked_at=?', (self.now,))
        # Retired, unpinned old children are not perpetual freshness blockers.
        await self.db.execute("INSERT INTO official_watch_sources(id,url,kind,parent_id,interval_ms,next_poll,enabled) VALUES('old-news','https://example.com/old','article','source:market-one',60000,0,0)")
        self.assertEqual((await self.markets.quote('user-a', 'market-one', 'YES', 1))['spendPoints'], 1)

    async def test_active_poll_lease_between_quote_and_commit_blocks_fill(self):
        await self.market(mode='active')
        await self.db.execute("INSERT INTO official_watch_sources(id,url,kind,parent_id,interval_ms,next_poll,checked_at) "
                              "VALUES('leased-child','https://example.com/leased','article','source:market-one',60000,?,?)",
                              (self.now+60000, self.now))
        for source in ('source:market-one', 'leased-child'):
            with self.subTest(source=source):
                quote = await self.markets.quote('user-a', 'market-one', 'YES', 1)
                market_before = await self.markets.get('market-one')
                account_before = await PointsService(self.db).summary('user-a')
                original = self.db.batch
                injected = False

                async def during_poll(statements):
                    nonlocal injected
                    if not injected and any('INSERT INTO market_fills' in sql for sql, _ in statements):
                        injected = True
                        # Polling has claimed the source, but has not yet written
                        # its pending review/hold for newly discovered evidence.
                        await self.db.execute('UPDATE official_watch_sources SET lease_until=?,lease_token=? WHERE id=?',
                                              (self.now+60000, 'poll-in-progress', source))
                    return await original(statements)

                self.db.batch = during_poll
                try:
                    with self.assertRaises(AppError):
                        await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'poll-race:'+source)
                finally:
                    self.db.batch = original
                self.assertEqual(await self.markets.get('market-one'), market_before)
                self.assertEqual(await PointsService(self.db).summary('user-a'), account_before)
                self.assertIsNone(await self.db.first('SELECT id FROM market_fills WHERE quote_id=?', (quote['quoteId'],)))
                self.assertEqual((await self.db.first('SELECT COUNT(*) n FROM market_write_guards'))['n'], 0)
                await self.db.execute('UPDATE official_watch_sources SET lease_until=0,lease_token=NULL WHERE id=?', (source,))
                # A completed, unchanged poll can resume participation without
                # changing the user's still-valid confirmed quote.
                accepted = await self.markets.accept('user-a', 'market-one', quote['quoteId'], 0, 'poll-race:'+source)
                self.assertEqual(accepted['status'], 'accepted')
