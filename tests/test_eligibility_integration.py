"""Public projections, response retries and reputation commitments share eligibility."""

import unittest

from forecast_application.solana_registry import SolanaRegistry
from forecast_domain import content_hash, dumps, loads
from forecast_domain.lifecycle import CommandReceipt

from tests import test_automation_integration as integration


class EligibilityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = integration.AutomationIntegrationTests.asyncSetUp
    asyncTearDown = integration.AutomationIntegrationTests.asyncTearDown
    random_token = integration.AutomationIntegrationTests.random_token
    token_hash = staticmethod(integration.AutomationIntegrationTests.token_hash)
    publish = integration.AutomationIntegrationTests.publish
    reviewed_trigger = integration.AutomationIntegrationTests.reviewed_trigger
    hold = integration.AutomationIntegrationTests.hold
    accept = integration.AutomationIntegrationTests.accept
    market_receipt = integration.AutomationIntegrationTests.market_receipt

    async def test_void_is_visible_but_not_crowd_or_replayed_as_an_eligible_forecast(self):
        self.now = self.base+90
        receipt = await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
            self.card['revision'], 'late-visible-receipt', 100)
        original = await self.db.first('SELECT body FROM user_forecasts WHERE forecast_id=?', (self.fid,))
        trigger = await self.reviewed_trigger()
        await self.hold(trigger)
        await self.accept(trigger)
        detail = await self.app.forecast_detail(self.fid, self.other)
        self.assertIsNone(detail['myForecast'])
        self.assertEqual(detail['forecast']['crowd'], {'probability': None, 'count': 0})
        self.assertEqual(detail['eligibility']['personal']['status'], 'void')
        self.assertEqual(detail['eligibility']['personal']['refundedPoints'], 100)
        replay = await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
            self.card['revision'], 'late-visible-receipt', 100)
        self.assertIsNone(replay['myForecast'])
        self.assertEqual(replay['originalReceipt'], receipt['myForecast'])
        self.assertEqual(replay['points']['available'], 1000)
        self.assertEqual((await self.db.first('SELECT body FROM user_forecasts WHERE forecast_id=?', (self.fid,))), original)
        profile = await self.app.me(self.other)
        self.assertEqual(profile['myForecasts'][0]['eligibility']['personal']['status'], 'void')
        self.assertEqual(profile['reputation']['totalForecasts'], 0)
        self.assertEqual(profile['points']['entries'][0]['kind'], 'evidence_refund')

    async def test_reputation_commitment_binds_restored_forecast_and_void_receipts(self):
        self.now = self.base+20
        before = await self.app.submit_forecast(self.other, self.fid, 'NO', 80,
            self.card['revision'], 'pre-evidence-no', 100)
        self.now = self.base+90
        await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
            before['forecast']['revision'], 'late-switch-yes', 900)
        trigger = await self.reviewed_trigger()
        await self.hold(trigger)
        await self.accept(trigger)
        await self.app.run_due_jobs()
        current = await self.app._forecast(self.fid)
        self.now = current.challenge_until_ms
        await self.app.run_due_jobs()
        final = await self.app._forecast(self.fid)
        registry = SolanaRegistry(self.db, None, program_id=bytes([11])*32, relayer=bytes([12])*32,
                                  now_ms=lambda: self.now, random_token=self.random_token)
        digest = await registry._reputation_hash(final)
        rows = await self.db.all('SELECT * FROM forecast_receipt_eligibility WHERE decision_id=? ORDER BY revision',
                                 (trigger.trigger_hash,))
        receipt_row = await self.db.first("SELECT receipt FROM command_receipts WHERE forecast_id=? "
            "AND json_extract(receipt,'$.revision')=?", (self.fid, before['myForecast']['revision']))
        prior = loads(CommandReceipt, receipt_row['receipt']).accepted_user_forecast
        expected = content_hash({'schema_version': 2, 'kind': 'eligible_forecast_reputation',
            'forecast_id': self.fid, 'specification_hash': final.specification_hash,
            'resolution_hash': final.finalized_resolution_hash, 'outcome': final.finalized_outcome,
            'eligibility': {'policy_version': 'evidence-cutoff-v1', 'trigger_hash': trigger.trigger_hash,
                'receipts': tuple({'revision': row['revision'], 'receipt_hash': row['receipt_hash'],
                                   'status': row['status']} for row in rows)},
            'submissions': (prior,)})
        self.assertEqual(digest.hex(), expected)
        self.assertEqual((await self.app.points.summary(self.other))['available'], 900)
        scores = await self.db.all('SELECT * FROM eligible_reputation_scores')
        self.assertEqual((scores[0]['correct'], scores[0]['probability']), (0, 20))
        self.assertEqual((await self.db.first('SELECT body FROM eligible_user_forecasts'))['body'], dumps(prior))

    async def test_legacy_and_market_refunds_share_one_account_without_creating_points(self):
        original_card, original_id = self.card, self.fid
        self.now = self.base+1
        draft = await self.app.compile_forecast(self.uid, 'Will another official product announcement meet its deadline?')
        alternate = (await self.app.publish_forecast(self.uid, draft['draftId'], 'publish-refund-second'))['forecast']
        self.now = self.base+90
        await self.app.submit_forecast(self.other, self.fid, 'YES', 100,
            self.card['revision'], 'late-legacy-plus-market', 100)
        self.card, self.fid = alternate, alternate['id']
        await self.market_receipt(at=91)
        market_trigger = await self.reviewed_trigger()
        await self.hold(market_trigger)
        self.card, self.fid = original_card, original_id
        legacy_trigger = await self.reviewed_trigger()
        await self.hold(legacy_trigger)
        await self.accept(legacy_trigger)
        self.card, self.fid = alternate, alternate['id']
        await self.accept(market_trigger)
        balance = await self.app.points.summary(self.other)
        self.assertEqual((balance['available'], balance['committed']), (1000, 0))
        self.assertEqual({row['kind'] for row in balance['entries'] if row['amount'] == 100},
                         {'evidence_refund', 'market_void_refund'})
        await self.accept(market_trigger)
        self.assertEqual(await self.app.points.summary(self.other), balance)
