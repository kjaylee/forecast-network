"""Safe unrelated-news release and legacy intake remain atomic around source polls."""
import hashlib
import json
import unittest

from forecast_application.errors import AppError

from tests import test_web_application as fixtures


class AutomationDismissalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ApplicationTests.asyncSetUp
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)
    publish = fixtures.ApplicationTests.publish

    async def prepare(self, *, suffix='a', forecast=None):
        f = forecast or await self.publish()
        await self.db.execute("INSERT OR IGNORE INTO official_watch_sources(id,url,kind,interval_ms,next_poll,checked_at) VALUES('source','https://www.apple.com/newsroom/','index',300000,?,?)", (self.now, self.now))
        await self.db.execute("INSERT OR IGNORE INTO official_watch_bindings VALUES(?,'source','[\"Product X\"]')", (f['id'],))
        raw = 'Official unrelated public article '+suffix
        artifact = hashlib.sha256(raw.encode()).hexdigest()
        await self.db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,'source',?,'text/plain',?)", (artifact, raw, self.now))
        content = hashlib.sha256(('content'+suffix).encode()).hexdigest()
        obs = {'id': 'observation-'+suffix, 'sourceId': 'source', 'url': 'https://www.apple.com/newsroom/2026/09/unrelated/',
               'contentHash': content, 'artifactHash': artifact, 'observedAt': self.now}
        await self.db.execute('INSERT INTO official_source_observations VALUES(?,?,?,?,?,?,?)',
                              (obs['id'], 'source', obs['url'], content, artifact, json.dumps(obs), self.now))
        key = hashlib.sha256((content+f['specificationHash']+'official-source-watch-v1').encode()).hexdigest()
        result = {'accepted': False, 'dismissible': True, 'observation': obs}
        await self.db.execute("INSERT INTO official_source_reviews(id,observation_id,forecast_id,specification_hash,content_hash,policy,state,next_attempt,result) VALUES(?,?,?,?,?,'official-source-watch-v1','reviewed',?,?)",
                              (key, obs['id'], f['id'], f['specificationHash'], content, self.now, json.dumps(result)))
        return f, key, result

    async def test_reviewed_unrelated_event_releases_only_automatic_hold(self):
        f, _, result = await self.prepare()
        await self.app.automation.hold(f['id'], result['observation'])
        before = await self.app._forecast(f['id'])
        await self.app.automation.dismiss(f['id'], result)
        self.assertIsNone(await self.app.participation_holds.active(f['id']))
        self.assertEqual(before, await self.app._forecast(f['id']))
        self.assertEqual((await self.app.participation_holds.status(f['id']))['revision'], 2)
        await self.app.automation.dismiss(f['id'], result)
        self.assertEqual((await self.app.participation_holds.status(f['id']))['revision'], 2)

    async def test_operator_hold_and_unqualified_result_never_auto_release(self):
        f, _, result = await self.prepare()
        await self.app.participation_holds.change(f['id'], {'action': 'hold', 'expectedRevision': 0, 'expectedHoldId': None,
            'specificationHash': f['specificationHash'], 'reason': 'known_outcome_review', 'evidenceUrl': result['observation']['url'], 'idempotencyKey': 'operator-hold-only'})
        for payload in (result, {**result, 'dismissible': False}):
            await self.app.automation.dismiss(f['id'], payload)
        self.assertIsNotNone(await self.app.participation_holds.active(f['id']))

    async def test_other_pending_review_keeps_hold_until_last_safe_dismissal(self):
        f, _, first = await self.prepare()
        _, second_key, second = await self.prepare(suffix='b', forecast=f)
        await self.db.execute("UPDATE official_source_reviews SET state='pending',result=NULL WHERE id=?", (second_key,))
        await self.app.automation.hold(f['id'], first['observation'])
        await self.app.automation.dismiss(f['id'], first)
        self.assertIsNotNone(await self.app.participation_holds.active(f['id']))
        await self.db.execute("UPDATE official_source_reviews SET state='reviewed',result=? WHERE id=?", (json.dumps(second), second_key))
        await self.app.automation.dismiss(f['id'], second)
        self.assertIsNone(await self.app.participation_holds.active(f['id']))

    async def test_polling_race_prevents_release_then_same_review_can_retry(self):
        f, _, result = await self.prepare()
        await self.app.automation.hold(f['id'], result['observation'])
        await self.db.execute('UPDATE official_watch_sources SET lease_until=?', (self.now+10000,))
        with self.assertRaises(AppError):
            await self.app.automation.dismiss(f['id'], result)
        self.assertIsNotNone(await self.app.participation_holds.active(f['id']))
        await self.db.execute('UPDATE official_watch_sources SET lease_until=0')
        await self.app.automation.dismiss(f['id'], result)
        self.assertIsNone(await self.app.participation_holds.active(f['id']))

    async def test_legacy_vote_during_polling_gap_is_accepted(self):
        f, review_id, _ = await self.prepare()
        await self.db.execute("UPDATE official_source_reviews SET state='complete' WHERE id=?", (review_id,))
        original = self.db.batch
        async def start_poll(statements):
            if any('INSERT INTO user_forecasts' in sql for sql, _ in statements):
                await self.db.execute('UPDATE official_watch_sources SET lease_until=?', (self.now+10000,))
            return await original(statements)
        self.db.batch = start_poll
        # A poll in progress is not evidence; the receipt-eligibility cutoff voids late
        # receipts retroactively if the poll turns up resolving news.
        accepted = await self.app.submit_forecast(self.other, f['id'], 'YES', 70, f['revision'], 'polling-gap-vote', 100)
        self.assertEqual(accepted['stake']['amount'], 100)
        self.assertEqual((await self.app.points.summary(self.other))['committed'], 100)
