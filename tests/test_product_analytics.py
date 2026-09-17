"""Actual SQLite migrations and retained-event projections; all populations are fixtures."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

from forecast_application.analytics import DAY_MS, _ratio, product_analytics
from forecast_application.database import SQLiteDatabase
from forecast_domain.records import MAX_SAFE_INTEGER

ROOT = Path(__file__).resolve().parents[1]
BASE = 1005 * DAY_MS  # Monday, explicitly UTC.
HOUR = 3_600_000


class ReadOnlyDatabase(SQLiteDatabase):
    def __init__(self, connection):
        super().__init__(connection)
        self.batches = 0

    async def batch(self, statements):
        self.batches += 1
        if any(not sql.lstrip().startswith(("SELECT", "WITH")) for sql, _ in statements):
            raise AssertionError("analytics attempted a write")
        return await super().batch(statements)


class ProductAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    def test_scaled_ratio_safe_boundary_and_out_of_range_preserve_original_counts(self):
        boundary = _ratio(MAX_SAFE_INTEGER, 10000, unit='USD_MICRO_per_predictor')
        self.assertEqual(boundary['valueScaled'], MAX_SAFE_INTEGER)
        self.assertEqual(boundary['status'], 'available')
        just_above = _ratio((MAX_SAFE_INTEGER + 1) // 8, 1250, unit='USD_MICRO_per_predictor')
        self.assertEqual(just_above['numerator'] * 10000 // just_above['denominator'], MAX_SAFE_INTEGER + 1)
        self.assertIsNone(just_above['valueScaled'])
        self.assertEqual(just_above['status'], 'out_of_range')
        # Both inputs remain safe, while the rounded scaled result crosses the boundary.
        above = _ratio(MAX_SAFE_INTEGER, 9999, unit='USD_MICRO_per_predictor')
        self.assertEqual((above['numerator'], above['denominator']), (MAX_SAFE_INTEGER, 9999))
        self.assertIsNone(above['valueScaled'])
        self.assertIsNone(above['valueBp'])
        self.assertEqual(above['status'], 'out_of_range')
        enormous = _ratio(MAX_SAFE_INTEGER, 1, unit='USD_MICRO_per_predictor')
        self.assertEqual(enormous['numerator'], MAX_SAFE_INTEGER)
        self.assertIsNone(enormous['valueScaled'])
        self.assertEqual(enormous['status'], 'out_of_range')
        share = _ratio(MAX_SAFE_INTEGER, 9999)
        self.assertIsNone(share['valueBp'])
        self.assertEqual(share['status'], 'out_of_range')
        ordinary = _ratio(1, 3)
        self.assertEqual((ordinary['valueBp'], ordinary['valueScaled'], ordinary['status']),
                         (3333, 3333, 'available'))
        empty = _ratio(0, 0)
        self.assertIsNone(empty['valueScaled'])
        self.assertEqual(empty['status'], 'no_denominator')

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = ReadOnlyDatabase(self.connection)
        self.revisions = {}
        self.user("owner", BASE - DAY_MS)
        self.user("alice", BASE)
        self.user("bob", BASE)
        self.forecast("question", "owner", BASE)

    def tearDown(self):
        self.connection.close()

    def user(self, user_id, at):
        self.connection.execute("INSERT INTO users VALUES(?,?,?,?,?)", (user_id, user_id, user_id, user_id, at))

    def forecast(self, forecast_id, creator, at):
        spec = hashlib.sha256(forecast_id.encode()).hexdigest()
        self.connection.execute("INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
            "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) "
            "VALUES(?,?,?,'{}',0,'OPEN','OTHER',?,?,?,?,?,?,?,?,?)", (forecast_id, creator, 'draft:'+forecast_id,
            forecast_id, forecast_id, forecast_id, spec, at, at+100*DAY_MS, at, at, forecast_id))
        self.event(forecast_id, "publish", at, None)

    def event(self, forecast_id, command, at, artifact):
        revision = self.revisions.get(forecast_id, 0) + 1
        self.revisions[forecast_id] = revision
        artifact_hash = None
        if artifact is not None:
            body = json.dumps(artifact, sort_keys=True)
            artifact_hash = hashlib.sha256(body.encode()).hexdigest()
            self.connection.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?)",
                                    (artifact_hash, command, body, 'application/json', at))
        event = json.dumps({'command_name': command, 'artifact_hash': artifact_hash,
                            'new_state': 'FINALIZED' if command == 'finalize' else 'OPEN'})
        digest = hashlib.sha256(f'{forecast_id}:{revision}:{event}'.encode()).hexdigest()
        self.connection.execute("INSERT INTO events VALUES(?,?,?,?,?)", (forecast_id, revision, digest, event, at))
        return revision, artifact_hash

    def predict(self, user, at, forecast="question"):
        spec = hashlib.sha256(forecast.encode()).hexdigest()
        return self.event(forecast, "submit_forecast", at, {'forecaster_id': user, 'forecast_id': forecast,
            'specification_hash': spec, 'submitted_at_ms': at, 'outcome': 'YES', 'confidence': 70})

    def exclusion(self, subject_id, kind="user", reason="test"):
        self.connection.execute("INSERT INTO product_analytics_exclusions VALUES(?,?,?,?,?)",
                                (kind, subject_id, reason, BASE+80*DAY_MS, 'a'*64))

    async def report(self, **changes):
        kwargs = dict(as_of_ms=BASE+40*DAY_MS, window_start_ms=BASE, window_end_ms=BASE+7*DAY_MS,
                      cohort_start_ms=BASE, cohort_end_ms=BASE+DAY_MS, population_kind="fixture")
        kwargs.update(changes)
        return await product_analytics(self.db, **kwargs)

    async def test_real_sqlite_read_batch_and_empty_denominators(self):
        result = await self.report()
        self.assertEqual(self.db.batches, 1)
        self.assertEqual(result['activity']['activePredictors'], 0)
        self.assertIsNone(result['retention']['D30']['valueBp'])
        self.assertEqual(result['population']['evidenceClass'], 'fixture_or_load_only')
        self.assertIsNone(result['costs']['provider']['completeActualTotalAtomic'])

    async def test_utc_days_funnels_weekly_and_retention(self):
        for day in (0, 1, 7, 30):
            self.predict("alice", BASE+day*DAY_MS+HOUR)
        # A legitimate same-day revision does not inflate active-day metrics.
        self.predict("alice", BASE+DAY_MS+2*HOUR)
        result = await self.report()
        self.assertEqual(result['activity']['activePredictors'], 1)
        self.assertEqual(result['activity']['activeQuestions'], 1)
        self.assertEqual(result['activity']['distinctPredictorQuestionDays'], 2)
        self.assertEqual(result['activationWithin7Days']['numerator'], 1)
        self.assertEqual(result['activationWithin7Days']['denominator'], 2)
        self.assertEqual(result['creationToExternalParticipationWithin7Days']['valueBp'], 10000)
        self.assertEqual(result['weeklyActiveDaysPerActiveUserWeek']['valueScaled'], 20000)
        for name in ('D1', 'D7', 'D30'):
            self.assertEqual(result['retention'][name]['valueBp'], 10000)

    async def test_day30_requires_entire_target_day(self):
        self.predict("alice", BASE+HOUR)
        self.predict("alice", BASE+30*DAY_MS+HOUR)
        immature = await self.report(as_of_ms=BASE+31*DAY_MS-1)
        mature = await self.report(as_of_ms=BASE+31*DAY_MS)
        self.assertEqual(immature['retention']['D30']['status'], 'immature')
        self.assertIsNone(immature['cohorts'][0]['retention']['D30']['numerator'])
        self.assertEqual(mature['retention']['D30']['numerator'], 1)

    async def test_late_event_replay_and_idempotency(self):
        self.predict("alice", BASE+HOUR)
        before = await self.report()
        revision, _ = self.predict("alice", BASE+DAY_MS+HOUR)
        after = await self.report()
        self.assertEqual(before['retention']['D1']['numerator'], 0)
        self.assertEqual(after['retention']['D1']['numerator'], 1)
        self.assertNotEqual(before['inputHash'], after['inputHash'])
        row = self.connection.execute("SELECT * FROM events WHERE forecast_id='question' AND revision=?", (revision,)).fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("INSERT INTO events VALUES(?,?,?,?,?)", tuple(row))
        self.assertEqual((await self.report())['inputHash'], after['inputHash'])

    async def test_aliases_deduplicate_and_deletion_applies_to_all_aliases(self):
        self.user('alias', BASE+HOUR)
        self.connection.execute("INSERT INTO product_analytics_identity_links VALUES(?,?,?,?)", ('alias','alice',BASE,'b'*64))
        self.predict('alice', BASE+HOUR)
        self.predict('alias', BASE+2*HOUR)
        self.assertEqual((await self.report())['activity']['distinctPredictorQuestionDays'], 1)
        self.exclusion('alias', reason='deleted')
        result = await self.report()
        self.assertEqual(result['activity']['activePredictors'], 0)
        self.assertEqual(result['retention']['D1']['denominator'], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM product_analytics_exclusions")

    async def test_identity_cycle_rejected(self):
        self.connection.executemany("INSERT INTO product_analytics_identity_links VALUES(?,?,?,?)",
            [('alice','bob',BASE,'a'*64),('bob','alice',BASE,'b'*64)])
        with self.assertRaisesRegex(ValueError,'cyclic'):
            await self.report()

    async def test_staff_test_load_and_deleted_question_do_not_anchor_cohorts(self):
        self.user('test:robot',BASE)
        self.user('employee',BASE)
        self.exclusion('employee',reason='staff')
        self.forecast('load:question','owner',BASE)
        self.predict('alice',BASE+HOUR,'load:question')
        self.predict('test:robot',BASE+HOUR)
        self.predict('employee',BASE+HOUR)
        self.predict('alice',BASE+DAY_MS+HOUR)
        result=await self.report(cohort_end_ms=BASE+2*DAY_MS)
        self.assertEqual(result['cohorts'][0]['activatedUsers'],0)
        self.assertEqual(result['cohorts'][1]['activatedUsers'],1)
        self.exclusion('question',kind='forecast',reason='deleted')
        self.assertEqual((await self.report())['activity']['activeQuestions'],0)

    async def test_creator_alias_cannot_fake_external_participation(self):
        self.user('owner-alias',BASE)
        self.connection.execute("INSERT INTO product_analytics_identity_links VALUES(?,?,?,?)",
                                ('owner-alias','owner',BASE,'b'*64))
        self.predict('owner-alias',BASE+HOUR)
        self.assertEqual((await self.report())['creationToExternalParticipationWithin7Days']['numerator'],0)

    async def test_editorial_actor_is_excluded_but_its_real_question_and_participation_remain(self):
        self.user('system_editorial',BASE)
        self.forecast('editorial-real','system_editorial',BASE)
        self.predict('alice',BASE+HOUR,'editorial-real')
        self.predict('system_editorial',BASE+2*HOUR,'editorial-real')
        result=await self.report()
        self.assertEqual(result['activationWithin7Days']['newAccounts'],2)
        self.assertEqual(result['activationWithin7Days']['denominator'],2)
        self.assertEqual(result['activity']['activePredictors'],1)
        self.assertEqual(result['activity']['activeQuestions'],1)
        self.assertEqual(result['activity']['openQuestionsAtAsOf'],2)
        self.assertEqual(result['creationToExternalParticipationWithin7Days']['publishedQuestions'],2)
        self.assertEqual(result['creationToExternalParticipationWithin7Days']['numerator'],1)

    async def test_editorial_question_quality_remains_but_creator_quality_excludes_operator(self):
        self.user('system_editorial',BASE)
        self.forecast('editorial-real','system_editorial',BASE)
        self.event('editorial-real','finalize',BASE+3*DAY_MS,None)
        self.connection.execute("UPDATE forecasts SET finalized_outcome='YES' WHERE id='editorial-real'")
        result=await self.report()
        self.assertEqual(result['quality']['finalizedQuestions'],1)
        self.assertEqual(result['quality']['questionValidity']['valueBp'],10000)
        self.assertEqual(result['quality']['creatorsWithFinalizedResults'],0)

    async def test_staff_and_deleted_creators_do_not_remove_other_users_activity(self):
        self.user('employee',BASE)
        self.forecast('staff-real','employee',BASE)
        self.predict('alice',BASE+HOUR,'staff-real')
        self.predict('alice',BASE+DAY_MS+HOUR,'staff-real')
        self.exclusion('employee',reason='staff')
        staff=await self.report()
        self.assertEqual(staff['activity']['activePredictors'],1)
        self.assertEqual(staff['retention']['D1']['valueBp'],10000)
        self.exclusion('employee',reason='deleted')
        deleted=await self.report()
        self.assertEqual(deleted['activity'],staff['activity'])
        self.assertEqual(deleted['creationToExternalParticipationWithin7Days']['numerator'],1)

    async def test_actor_classification_does_not_guess_question_population(self):
        self.user('load:actor',BASE)
        self.forecast('public-real','load:actor',BASE)
        self.forecast('load:synthetic','owner',BASE)
        self.predict('alice',BASE+HOUR,'public-real')
        self.predict('bob',BASE+HOUR,'load:synthetic')
        result=await self.report()
        self.assertEqual(result['activity']['activePredictors'],1)
        self.assertEqual(result['activity']['activeQuestions'],1)
        self.exclusion('public-real',kind='forecast',reason='test')
        self.assertEqual((await self.report())['activity']['activeQuestions'],0)

    async def test_operating_cost_survives_staff_exclusion_and_actor_deletion(self):
        self.user('system_editorial',BASE)
        self.predict('alice',BASE+HOUR)
        self.cost(operation='editorial-operation',user='system_editorial',amount=45678)
        before=await self.report()
        self.assertEqual(before['costs']['provider']['knownRecordedSubtotalAtomic'],45678)
        self.exclusion('system_editorial',reason='deleted')
        after=await self.report()
        self.assertEqual(after['costs']['provider']['knownRecordedSubtotalAtomic'],45678)
        self.cost(operation='wrong-population',user='system_editorial',amount=99999,population='isolated-load')
        self.assertEqual((await self.report())['costs']['provider']['knownRecordedSubtotalAtomic'],45678)

    async def test_invalidity_and_dispute_reviews_use_event_window_and_distinct_artifacts(self):
        self.forecast('invalid','owner',BASE)
        self.event('question','finalize',BASE+3*DAY_MS,None)
        self.event('invalid','finalize',BASE+3*DAY_MS,None)
        self.connection.execute("UPDATE forecasts SET finalized_outcome=CASE WHEN id='invalid' THEN 'INVALID' ELSE 'YES' END")
        _, dispute = self.event('question','submit_dispute',BASE+DAY_MS,{'disputant_id':'alice'})
        review={'dispute_hash':dispute,'material_conflict':True}
        self.event('question','review_dispute',BASE+2*DAY_MS,review)
        self.event('question','review_dispute',BASE+2*DAY_MS+1,review)
        result=await self.report()
        self.assertEqual(result['quality']['invalidity']['valueBp'],5000)
        self.assertEqual(result['disputes']['reviewed'],1)
        self.assertEqual(result['disputes']['materialConflictAmongReviewed']['valueBp'],10000)

    def cost(self, operation='provider-op', source='provider', revision=1, status='known', amount=12345,
             recorded=None, user='alice', population='fixture'):
        self.connection.execute("INSERT INTO product_cost_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (source,operation,revision,population,'question',user,BASE+HOUR,recorded or BASE+2*HOUR,status,
             amount,'USD_MICRO' if source=='provider' else 'DEVNET_LAMPORT','c'*64 if status=='known' else None))

    async def test_known_unknown_estimated_costs_do_not_mix_and_revisions_do_not_double_count(self):
        self.predict('alice',BASE+HOUR)
        self.cost(status='unknown',amount=None)
        self.cost(revision=2,amount=30000,recorded=BASE+3*HOUR)
        self.cost(operation='estimate',status='estimated',amount=90000)
        self.cost(operation='unresolved',status='unknown',amount=None)
        self.cost(operation='chain-op',source='chain',amount=5000)
        self.cost(operation='production-cost',population='application',amount=1000000)
        self.connection.execute("INSERT INTO registry_spend VALUES(?,?)",(BASE//DAY_MS,700000))
        result=await self.report()
        provider=result['costs']['provider']
        self.assertEqual(provider['knownRecordedSubtotalAtomic'],30000)
        self.assertEqual(provider['knownOperations'],1)
        self.assertEqual(provider['unknownRecordedOperations'],1)
        self.assertEqual(provider['estimatedRecordedSubtotalAtomic'],90000)
        self.assertIsNone(provider['completeActualTotalAtomic'])
        self.assertEqual(provider['knownSubtotalPerActivePredictor']['denominator'],1)
        self.assertEqual(result['costs']['chain']['knownRecordedSubtotalAtomic'],5000)
        self.assertEqual(result['costs']['chain']['reservedLamportsAllPopulations'],700000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.cost(revision=3,amount=0,user='bob')

    async def test_later_receipt_not_visible_before_admission(self):
        self.cost(status='unknown',amount=None)
        self.cost(revision=2,amount=30000,recorded=BASE+50*DAY_MS)
        self.assertEqual((await self.report())['costs']['provider']['knownRecordedSubtotalAtomic'],0)
        self.assertEqual((await self.report(as_of_ms=BASE+51*DAY_MS))['costs']['provider']['knownRecordedSubtotalAtomic'],30000)

    async def test_registry_signature_needs_actual_fee_and_is_counted_once(self):
        for offset in (0,1):
            revision,_=self.predict('alice',BASE+HOUR+offset)
            digest=self.connection.execute("SELECT hash FROM events WHERE forecast_id='question' AND revision=?",(revision,)).fetchone()[0]
            self.connection.execute("INSERT INTO registry_intents VALUES(?,?,?,?,?)",('question',revision,digest,'{}',BASE+HOUR))
        self.connection.execute("UPDATE registry_delivery SET status='confirmed',signature='real-signature',submitted_at=?",(BASE+2*HOUR,))
        unknown=await self.report()
        self.assertEqual(unknown['costs']['chain']['observedDeliverySignatures'],1)
        self.assertEqual(unknown['costs']['chain']['deliverySignaturesWithoutActualFee'],1)
        self.assertEqual(unknown['costs']['chain']['knownRecordedSubtotalAtomic'],0)
        self.cost(operation='real-signature',source='chain',amount=5000)
        known=await self.report()
        self.assertEqual(known['costs']['chain']['deliverySignaturesWithoutActualFee'],0)
        self.assertEqual(known['costs']['chain']['knownRecordedSubtotalAtomic'],5000)

    async def test_current_latest_choice_is_not_required_for_history(self):
        self.predict('alice',BASE+HOUR)
        self.predict('alice',BASE+DAY_MS+HOUR)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM user_forecasts').fetchone()[0],0)
        self.assertEqual((await self.report())['retention']['D1']['valueBp'],10000)

    async def test_eligibility_overlay_requires_completion_and_excludes_void_and_review(self):
        first, _ = self.predict('alice',BASE+HOUR)
        second, _ = self.predict('alice',BASE+DAY_MS+HOUR)
        bodies={}
        for revision in (first,second):
            row=self.connection.execute("SELECT e.hash,a.body FROM events e JOIN artifacts a "
                "ON a.hash=json_extract(e.event,'$.artifact_hash') WHERE e.forecast_id='question' AND e.revision=?",(revision,)).fetchone()
            bodies[revision]=row['body']
            self.connection.execute("INSERT INTO command_receipts VALUES(?,?,?)",('question','cmd'+str(revision),
                json.dumps({'revision':revision,'event_hash':row['hash'],'accepted_user_forecast':json.loads(row['body'])})))
        self.connection.execute("INSERT INTO user_forecasts VALUES(?,?,?,?,?,?,?,?)",
            ('question','alice','YES',70,70,BASE+DAY_MS+HOUR,second,bodies[second]))
        balance=self.connection.execute("SELECT available,committed FROM point_accounts WHERE user_id='alice'").fetchone()
        decision='d'*64
        decision_body=json.dumps({'forecast_id':'question','specification_hash':hashlib.sha256(b'question').hexdigest(),
                                  'event_at_ms':BASE+DAY_MS,'event_time_basis':'published_instant'})
        self.connection.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)",(decision,'eligibility',decision_body,'application/json',BASE+2*DAY_MS))
        self.connection.execute("INSERT INTO forecast_eligibility_decisions VALUES(?,?,?,?,?,?,?)",
            (decision,'question',hashlib.sha256(b'question').hexdigest(),BASE+DAY_MS,'published_instant',BASE+2*DAY_MS,decision_body))
        self.connection.executemany("INSERT INTO forecast_receipt_eligibility VALUES(?,?,?,?,?,?,?,?)",[
            (decision,'question','alice',first,'a'*64,'eligible',bodies[first],BASE+HOUR),
            (decision,'question','alice',second,'b'*64,'void',bodies[second],BASE+DAY_MS+HOUR)])
        pending=await self.report()
        self.assertEqual(pending['activity']['activePredictors'],0)
        self.connection.execute("INSERT INTO point_eligibility_adjustments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('adjustment',decision,'question','alice',0,0,0,first,'YES',0,0,balance['available'],balance['committed'],BASE+3*DAY_MS))
        self.connection.execute("INSERT INTO forecast_eligibility_completions VALUES(?,?)",(decision,BASE+3*DAY_MS))
        complete=await self.report()
        self.assertEqual(complete['activity']['distinctPredictorQuestionDays'],1)
        self.assertEqual(complete['retention']['D1']['numerator'],0)

    async def test_hold_and_release_restate_activity_and_open_inventory(self):
        self.predict('alice',BASE+HOUR)
        spec=hashlib.sha256(b'question').hexdigest()
        self.connection.execute("INSERT INTO participation_hold_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('hold','question',1,'hold','hold',spec,'known_outcome_review','https://example.org/evidence',
             'authenticated_admin','hold-request','f'*64,'{}',BASE+2*DAY_MS))
        held=await self.report()
        self.assertEqual(held['activity']['activePredictors'],0)
        self.assertEqual(held['activity']['openQuestionsAtAsOf'],0)
        self.connection.execute("INSERT INTO participation_hold_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('release','question',2,'release','hold',spec,'known_outcome_review','https://example.org/evidence',
             'authenticated_admin','release-request','e'*64,'{}',BASE+3*DAY_MS))
        self.assertEqual((await self.report())['activity']['activePredictors'],1)

    async def test_unknown_finalized_outcome_and_missing_clarity_stay_unavailable(self):
        self.event('question','finalize',BASE+3*DAY_MS,None)
        result=await self.report()
        self.assertEqual(result['quality']['unavailableFinalizedOutcomes'],1)
        self.assertIsNone(result['quality']['questionValidity']['valueBp'])
        self.assertIsNone(result['quality']['publishedQuestionClarity']['valueBp'])
        self.connection.execute("UPDATE forecasts SET snapshot=?",(json.dumps({'specification':{'ambiguity_score_bp':700}}),))
        self.assertEqual((await self.report())['quality']['publishedQuestionClarity']['valueBp'],9300)

    async def test_future_admitted_identity_tombstone_applies_to_past_windows(self):
        self.user('alias',BASE)
        self.predict('alice',BASE+HOUR)
        self.connection.execute("INSERT INTO product_analytics_identity_links VALUES(?,?,?,?)",
                                ('alias','alice',BASE+80*DAY_MS,'b'*64))
        self.exclusion('alias',reason='deleted')
        self.assertEqual((await self.report())['activity']['activePredictors'],0)

    async def test_invalid_windows_fail_before_query(self):
        for change in ({'window_start_ms':BASE+1},{'window_end_ms':BASE},{'as_of_ms':True},
                       {'window_start_ms':BASE-367*DAY_MS},{'population_kind':'unknown'}):
            with self.assertRaises(ValueError):
                await self.report(**change)
        self.assertEqual(self.db.batches,0)


if __name__ == '__main__':
    unittest.main()
