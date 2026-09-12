"""Known-result receipts cannot escape containment through ordinary expiry."""
import sqlite3
import unittest

from forecast_application.errors import AppError
from forecast_application.points import settlement_sql
from forecast_domain.lifecycle import Finalize, LifecycleState, Lock

from tests import test_automation_dismissal as dismissal
from tests import test_automation_integration as integration


class EligibilityContainmentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = integration.AutomationIntegrationTests.asyncSetUp
    asyncTearDown = integration.AutomationIntegrationTests.asyncTearDown
    random_token = integration.AutomationIntegrationTests.random_token
    token_hash = staticmethod(integration.AutomationIntegrationTests.token_hash)
    publish = integration.AutomationIntegrationTests.publish
    reviewed_trigger = integration.AutomationIntegrationTests.reviewed_trigger
    hold = integration.AutomationIntegrationTests.hold
    accept = integration.AutomationIntegrationTests.accept

    async def late_receipt(self):
        self.now = self.base + 90
        await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
                                      self.card['revision'], 'known-result-receipt', 100)
        trigger = await self.reviewed_trigger()
        await self.hold(trigger)
        original = await self.app._forecast(self.fid)
        await self.accept(trigger)
        self.assertEqual((await self.app._forecast(self.fid)).upgrade_source, original)
        self.assertEqual((await self.app._forecast(self.fid)).state, LifecycleState.LOCKED)
        points = await self.app.points.summary(self.other)
        self.assertEqual((points['available'], points['committed']), (1000, 0))
        self.assertEqual((await self.app.eligibility.status(self.fid, self.other))['personal']['status'], 'void')
        return trigger

    async def test_known_result_receipt_cannot_win_at_original_expiry(self):
        await self.late_receipt()
        before = await self.app.points.summary(self.other)
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        self.now = self.card['closeAt'] + 1000
        result = await self.app.run_due_jobs()
        self.assertEqual(result['failed'], 0)
        self.assertEqual((await self.app._forecast(self.fid)).state, LifecycleState.FINALIZED)
        self.now += 3 * 86400000
        await self.app.run_due_jobs()
        self.assertEqual(await self.app.points.summary(self.other), before)
        self.assertEqual(await self.db.all('SELECT * FROM reputation_scores'), [])
        self.assertEqual(await self.db.all("SELECT * FROM point_ledger WHERE kind='settlement'"), [])

    async def test_intake_release_cannot_restore_a_voided_receipt_or_allow_new_intake(self):
        trigger = await self.late_receipt()
        status = await self.app.participation_holds.status(self.fid)
        await self.app.participation_holds.change(self.fid, {
            'action': 'release', 'expectedRevision': status['revision'],
            'expectedHoldId': status['hold']['holdId'], 'specificationHash': self.card['specificationHash'],
            'reason': 'known_outcome_review', 'evidenceUrl': trigger.evidence[0].url,
            'idempotencyKey': 'release-intake-only',
        })
        self.assertIsNone(await self.app.participation_holds.active(self.fid))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute('DELETE FROM forecast_eligibility_decisions')
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute('UPDATE forecast_eligibility_decisions SET cutoff_at=0')
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE user_forecasts SET outcome='NO' WHERE forecast_id=?", (self.fid,))
        self.assertEqual((await self.app.points.summary(self.other))['available'], 1000)
        self.assertEqual(await self.db.all('SELECT * FROM eligible_user_forecasts'), [])
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        self.now = (await self.app._forecast(self.fid)).challenge_until_ms
        self.assertEqual((await self.app.run_due_jobs())['failed'], 0)
        self.assertEqual(await self.db.all('SELECT * FROM reputation_scores'), [])
        self.assertEqual((await self.app.points.summary(self.other))['available'], 1000)

    async def test_hold_racing_ordinary_lock_rolls_back_snapshot_events_and_receipts(self):
        record = await self.app._forecast(self.fid)
        trigger = await self.reviewed_trigger()
        self.now = self.card['closeAt'] + 1000
        original = self.db.batch
        fired = False
        async def intercept(statements):
            nonlocal fired
            if not fired and any('UPDATE forecasts SET snapshot=' in sql for sql, _ in statements):
                fired = True
                await self.hold(trigger)
            return await original(statements)
        before_events = await self.db.all('SELECT * FROM events')
        self.db.batch = intercept
        with self.assertRaises(AppError) as raised:
            await self.app._mutate(record, Lock(), key='racing-ordinary-lock')
        self.assertEqual(raised.exception.code, 'early_eligibility_review')
        self.assertEqual(await self.app._forecast(self.fid), record)
        self.assertEqual(await self.db.all('SELECT * FROM events'), before_events)
        self.assertEqual(await self.db.all('SELECT * FROM mutation_guards'), [])

    async def test_pending_review_racing_finalization_rolls_back(self):
        self.now = self.card['closeAt'] + 1000
        await self.app.run_due_jobs()
        record = await self.app._forecast(self.fid)
        self.now = record.challenge_until_ms
        original = self.db.batch
        fired = False
        async def intercept(statements):
            nonlocal fired
            if not fired and any('UPDATE forecasts SET snapshot=' in sql for sql, _ in statements):
                fired = True
                await dismissal.AutomationDismissalTests.prepare(self, forecast=self.card)
            return await original(statements)
        self.db.batch = intercept
        with self.assertRaises(AppError) as raised:
            await self.app._mutate(record, Finalize(), key='racing-finalization')
        self.assertEqual(raised.exception.code, 'early_eligibility_review')
        self.assertEqual(await self.app._forecast(self.fid), record)
        self.assertEqual(await self.db.all('SELECT * FROM reputation_scores'), [])

    async def test_retained_review_blocks_outbox_and_direct_settlement_after_finalization(self):
        await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
                                      self.card['revision'], 'ordinary-before-news', 100)
        self.now = self.card['closeAt'] + 1000
        await self.app.run_due_jobs()
        record = await self.app._forecast(self.fid)
        self.now = record.challenge_until_ms
        await self.app._mutate(record, Finalize(), key='final-before-pending-observation')
        await dismissal.AutomationDismissalTests.prepare(self, forecast=self.card)
        self.assertEqual(await self.app._process_outbox(50), 0)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.batch(settlement_sql(self.fid, self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute('INSERT INTO reputation_scores VALUES(?,?,?,?,?,?,?,?)',
                (self.fid, self.other, 'technology', 'YES', 100, 1, 0.0, self.now))
        self.assertEqual((await self.app.points.summary(self.other))['available'], 900)
        self.assertEqual((await self.app.points.summary(self.other))['committed'], 100)

    async def test_legacy_intake_rejects_unhealthy_watch_and_undismissed_reviews(self):
        _, review_id, _ = await dismissal.AutomationDismissalTests.prepare(self, forecast=self.card)
        for condition in ('pending', 'reviewed', 'exhausted', 'accepted_complete', 'failed',
                          'unchecked', 'stale', 'disabled', 'polling'):
            with self.subTest(condition=condition):
                await self.db.execute("UPDATE official_source_reviews SET state='complete',result='{\"accepted\":false,\"dismissible\":true}'")
                await self.db.execute('UPDATE official_watch_sources SET enabled=1,failure_count=0,checked_at=?,lease_until=0', (self.now,))
                if condition in ('pending', 'reviewed', 'exhausted'):
                    await self.db.execute('UPDATE official_source_reviews SET state=?,result=NULL WHERE id=?', (condition, review_id))
                elif condition == 'accepted_complete':
                    await self.db.execute("UPDATE official_source_reviews SET result='{\"accepted\":true}'")
                elif condition == 'failed':
                    await self.db.execute('UPDATE official_watch_sources SET failure_count=1')
                elif condition == 'unchecked':
                    await self.db.execute('UPDATE official_watch_sources SET checked_at=NULL')
                elif condition == 'stale':
                    await self.db.execute('UPDATE official_watch_sources SET checked_at=?', (self.now-360001,))
                elif condition == 'disabled':
                    await self.db.execute('UPDATE official_watch_sources SET enabled=0')
                else:
                    await self.db.execute('UPDATE official_watch_sources SET lease_until=?', (self.now+1,))
                for amount in (None, 0, 100):
                    with self.subTest(amount=amount), self.assertRaises(AppError) as raised:
                        await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
                            self.card['revision'], 'blocked-'+condition, amount)
                    self.assertEqual(raised.exception.code, 'participation_on_hold')
                self.assertEqual(await self.db.all('SELECT * FROM user_forecasts'), [])
                self.assertEqual((await self.app.points.summary(self.other))['available'], 1000)
        await self.db.execute("UPDATE official_source_reviews SET state='complete',result='{\"accepted\":false,\"dismissible\":true}'")
        await self.db.execute('UPDATE official_watch_sources SET enabled=1,failure_count=0,checked_at=?,lease_until=0', (self.now-360000,))
        accepted = await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
            self.card['revision'], 'watch-recovers-at-inclusive-boundary', 100)
        self.assertEqual(accepted['stake']['amount'], 100)

    async def test_watch_failure_between_read_and_vote_update_rolls_back_existing_position(self):
        _, review_id, _ = await dismissal.AutomationDismissalTests.prepare(self, forecast=self.card)
        await self.db.execute("UPDATE official_source_reviews SET state='complete' WHERE id=?", (review_id,))
        accepted = await self.app.submit_forecast(self.other, self.fid, 'YES', 70,
            self.card['revision'], 'before-source-outage', 100)
        before = await self.app._forecast(self.fid)
        points = await self.app.points.summary(self.other)
        original = self.db.batch

        async def failure_at_commit(statements):
            if any('INSERT INTO user_forecasts' in sql for sql, _ in statements):
                await self.db.execute('UPDATE official_watch_sources SET failure_count=1')
            return await original(statements)

        self.db.batch = failure_at_commit
        with self.assertRaises(AppError) as raised:
            await self.app.submit_forecast(self.other, self.fid, 'NO', 100,
                accepted['forecast']['revision'], 'after-source-outage', 1000)
        self.assertEqual(raised.exception.code, 'participation_on_hold')
        self.assertEqual(await self.app._forecast(self.fid), before)
        self.assertEqual(await self.app.points.summary(self.other), points)
        self.assertEqual((await self.db.first('SELECT outcome FROM user_forecasts'))['outcome'], 'YES')
