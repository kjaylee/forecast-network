"""Real immutable receipts, D1 constraints, and adversarial evidence-cutoff tests."""
import asyncio
import hashlib
import sqlite3
import unittest

from forecast_application.eligibility import ForecastEligibility, receipt_status
from forecast_application.errors import AppError
from forecast_domain.early_resolution import (
    EarlyResolutionTrigger,
    early_qualification_output_hash,
    early_trigger_input_hash,
)
from forecast_domain.lifecycle import CommandReceipt, SubmitForecast
from forecast_domain.models import AITask, counter_judge_input_hash, counter_judge_output_hash
from forecast_domain.serialization import content_hash, dumps, loads

from tests import model_fixtures as fixtures
from tests import test_web_application as application_tests


def make_trigger(forecast, event_at, observed_at=None, basis="published_instant"):
    observed_at = observed_at if observed_at is not None else event_at+20
    spec = forecast.specification
    evidence = (fixtures.evidence(spec, collected_at_ms=observed_at),)
    verifications = tuple(fixtures.source_verification(item) for item in evidence)
    qualification = "The official announcement satisfies the precise irreversible YES condition."
    input_hash = early_trigger_input_hash(forecast.forecast_id, spec.specification_hash, "yes-rule", evidence,
                                          verifications, event_at, observed_at, basis)
    qualifier = fixtures.provenance(AITask.AMBIGUITY_JUDGE, input_hash=input_hash,
        output_hash=early_qualification_output_hash(input_hash, qualification), created_at_ms=observed_at+10)
    counter = fixtures.provenance(AITask.COUNTER_JUDGE, provider="independent-provider", created_at_ms=observed_at+20,
        input_hash=counter_judge_input_hash(input_hash, qualifier), output_hash=counter_judge_output_hash(qualifier.output_hash, True))
    return EarlyResolutionTrigger(forecast_id=forecast.forecast_id, specification_hash=spec.specification_hash,
        clause_id="yes-rule", evidence=evidence, source_verifications=verifications, event_at_ms=event_at,
        observed_at_ms=observed_at, event_time_basis=basis, qualification=qualification,
        qualifier=qualifier, counter_qualifier=counter)


class ReceiptEligibilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = application_tests.ApplicationTests.asyncSetUp
    asyncTearDown = application_tests.ApplicationTests.asyncTearDown
    random_token = application_tests.ApplicationTests.random_token
    token_hash = staticmethod(application_tests.ApplicationTests.token_hash)
    publish = application_tests.ApplicationTests.publish

    def service(self):
        return ForecastEligibility(self.db, lambda: self.now, self.random_token)

    async def prepare(self, forecast, cutoff, basis="published_instant"):
        record = await self.app._forecast(forecast["id"])
        trigger = make_trigger(record, cutoff, cutoff if basis == "observed_upper_bound" else cutoff+20, basis)
        self.now = max(self.now, trigger.qualified_at_ms+1)
        body = "Immutable official announcement."
        await self.db.execute("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,'evidence',?,'text/plain',?)",
                              (hashlib.sha256(body.encode()).hexdigest(), body, self.now))
        return trigger

    async def vote(self, f, side, points, key, user=None):
        record = await self.app._forecast(f["id"])
        return await self.app.submit_forecast(user or self.other, f["id"], side, 80, record.revision, key, points)

    async def test_boundary_and_observation_are_not_interchangeable(self):
        for at, expected in [(9, "eligible"), (10, "void"), (11, "void")]:
            self.assertEqual(receipt_status(at, 10, "published_instant"), expected)
        self.assertEqual(receipt_status(9, 10, "observed_upper_bound"), "review")
        self.assertEqual(receipt_status(10, 10, "observed_upper_bound"), "void")
        for bad in [True, -1, 2.5]:
            with self.assertRaises(ValueError):
                receipt_status(bad, 10, "published_instant")

    async def test_late_switch_restores_losing_side_and_original_stake_without_rewriting_receipts(self):
        f = await self.publish()
        first = await self.vote(f, "NO", 300, "before-evidence")
        cutoff = self.now+10
        self.now = cutoff
        await self.vote(f, "YES", 500, "after-evidence")
        before = await self.db.all("SELECT * FROM user_forecasts")
        ledgers = await self.db.all("SELECT * FROM point_ledger")
        receipts = await self.db.all("SELECT * FROM command_receipts")
        trigger = await self.prepare(f, cutoff)
        svc = self.service()
        await svc.apply(trigger)
        self.assertEqual((await svc.finish(trigger))["status"], "complete")
        effective = await self.db.first("SELECT * FROM eligible_user_forecasts WHERE forecast_id=?", (f["id"],))
        self.assertEqual((effective["outcome"], effective["revision"]), ("NO", first["myForecast"]["revision"]))
        position = await self.app.points.position(self.other, f["id"])
        self.assertEqual((position["amount"], position["outcome"]), (300, "NO"))
        self.assertEqual(await self.db.all("SELECT * FROM user_forecasts"), before)
        self.assertEqual(await self.db.all("SELECT * FROM point_ledger"), ledgers)
        self.assertEqual(await self.db.all("SELECT * FROM command_receipts"), receipts)
        personal = (await svc.status(f["id"], self.other))["personal"]
        self.assertEqual((personal["status"], personal["refundedPoints"]), ("restored", 200))
        await asyncio.gather(svc.apply(trigger), svc.apply(trigger))
        self.assertEqual((await self.db.first("SELECT COUNT(*) n FROM point_eligibility_adjustments"))["n"], 1)
        self.assertEqual((await self.db.first("SELECT COUNT(*) n FROM activity WHERE kind='forecast_eligibility'"))["n"], 1)

    async def test_post_only_entry_refunded_and_excluded_from_reputation(self):
        f = await self.publish()
        cutoff = self.now
        await self.vote(f, "YES", 600, "post-only-entry")
        trigger = await self.prepare(f, cutoff)
        svc = self.service()
        await svc.apply(trigger)
        await svc.finish(trigger)
        self.assertIsNone(await self.db.first("SELECT * FROM eligible_user_forecasts WHERE forecast_id=?", (f["id"],)))
        self.assertEqual((await self.app.points.summary(self.other))["available"], 1000)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("INSERT INTO reputation_scores VALUES(?,?,'TECHNOLOGY','YES',80,1,0.04,?)", (f["id"], self.other, self.now))
        self.assertEqual((await svc.status(f["id"], self.other))["personal"]["status"], "void")
        with self.assertRaises(AppError):
            await self.vote(f, "NO", 0, "after-cutoff-installed")

    async def test_observation_keeps_ambiguous_stake_and_refunds_only_late_increment(self):
        f = await self.publish()
        await self.vote(f, "NO", 300, "ambiguous-before")
        cutoff = self.now+10
        self.now = cutoff
        await self.vote(f, "YES", 500, "definite-after")
        trigger = await self.prepare(f, cutoff, "observed_upper_bound")
        svc = self.service()
        await svc.apply(trigger)
        self.assertEqual((await svc.finish(trigger))["status"], "review")
        self.assertIsNone(await self.db.first("SELECT * FROM eligible_user_forecasts WHERE forecast_id=?", (f["id"],)))
        position = await self.app.points.position(self.other, f["id"])
        self.assertEqual((position["amount"], position["outcome"]), (300, "NO"))
        self.assertIsNone(await self.db.first("SELECT * FROM point_eligibility_account_holds WHERE user_id=?", (self.other,)))
        self.assertIsNotNone(await self.db.first("SELECT * FROM forecast_resolution_blockers WHERE forecast_id=?", (f["id"],)))

    async def test_withdrawal_spent_elsewhere_cannot_be_forgiven_and_retries_after_credit(self):
        f = await self.publish()
        await self.vote(f, "NO", 900, "preserved-loser")
        cutoff = self.now+10
        self.now = cutoff
        await self.vote(f, "YES", 0, "late-withdrawal")
        other = await application_tests.ApplicationTests._points_forecast(self, "other")
        await self.vote(other, "NO", 1000, "spend-late-withdrawal")
        trigger = await self.prepare(f, cutoff)
        svc = self.service()
        await svc.apply(trigger)
        self.assertEqual((await svc.finish(trigger))["status"], "pending")
        self.assertIsNone(await self.db.first("SELECT * FROM point_eligibility_adjustments"))
        with self.assertRaises(AppError):
            await self.vote(other, "YES", 0, "cannot-manipulate-stake")
        # Simulate externally completed legitimate credit; adapter test checks
        # correction retries, while ordinary settlement credit has separate tests.
        await self.db.execute("UPDATE point_accounts SET available=900 WHERE user_id=?", (self.other,))
        await svc.apply(trigger)
        self.assertEqual((await svc.finish(trigger))["status"], "complete")
        self.assertEqual((await self.app.points.position(self.other, f["id"]))["amount"], 900)
        self.assertEqual((await self.app.points.summary(self.other))["available"], 0)

    async def test_missing_receipt_fails_closed_and_rejects_completion(self):
        f = await self.publish()
        await self.vote(f, "YES", 100, "missing-receipt")
        trigger = await self.prepare(f, self.now)
        await self.db.execute("DELETE FROM command_receipts WHERE forecast_id=? AND json_extract(receipt,'$.accepted_user_forecast') IS NOT NULL", (f["id"],))
        svc = self.service()
        with self.assertRaises(AppError) as error:
            await svc.apply(trigger)
        self.assertEqual(error.exception.code, "eligibility_history_review")
        self.assertEqual((await svc.finish(trigger))["status"], "pending")
        self.assertEqual((await self.app.points.position(self.other, f["id"]))["amount"], 100)

    async def test_different_trigger_and_missing_retained_bytes_never_reclassify(self):
        f = await self.publish()
        await self.vote(f, "YES", 100, "one-cutoff-vote")
        trigger = await self.prepare(f, self.now)
        svc = self.service()
        await svc.apply(trigger)
        other = await self.prepare(f, trigger.event_at_ms+1)
        with self.assertRaises(AppError) as error:
            await svc.apply(other)
        self.assertEqual(error.exception.code, "early_trigger_changed")
        self.assertEqual((await self.db.first("SELECT body FROM forecast_eligibility_decisions"))["body"], dumps(trigger))
        await self.db.execute("DELETE FROM artifacts WHERE hash=?", (trigger.evidence[0].content_sha256,))
        with self.assertRaises(AppError) as error:
            await svc.apply(trigger)
        self.assertEqual(error.exception.code, "early_evidence_unavailable")

    async def test_cutoff_projection_rows_and_adjustments_are_immutable(self):
        f = await self.publish()
        await self.vote(f, "YES", 100, "immutable-cutoff")
        trigger = await self.prepare(f, self.now)
        svc = self.service()
        await svc.apply(trigger)
        await svc.finish(trigger)
        for table in ("forecast_eligibility_decisions", "forecast_receipt_eligibility", "forecast_eligibility_completions", "point_eligibility_adjustments"):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute("DELETE FROM " + table)

    async def test_no_cutoff_retains_original_projection_contract(self):
        f = await self.publish()
        await self.vote(f, "YES", 100, "unchanged-no-cutoff")
        self.assertEqual(await self.db.all("SELECT * FROM user_forecasts"), await self.db.all("SELECT * FROM eligible_user_forecasts"))
        self.assertEqual((await self.service().status(f["id"]))["status"], "none")

    async def test_restored_loser_settles_original_loss_and_scores_original_probability(self):
        f = await self.publish()
        await self.vote(f, "NO", 300, "original-no-position")
        cutoff = self.now+10
        self.now = cutoff
        await self.vote(f, "YES", 500, "late-yes-position")
        trigger = await self.prepare(f, cutoff)
        svc = self.service()
        await svc.apply(trigger)
        await svc.finish(trigger)
        self.now = f["closeAt"]+1000
        self.assertEqual((await self.app.run_due_jobs())["failed"], 0)
        challenge = await self.app._forecast(f["id"])
        self.now = challenge.challenge_until_ms
        self.assertEqual((await self.app.run_due_jobs())["failed"], 0)
        self.assertEqual((await self.app.points.position(self.other, f["id"]))["returned"], 0)
        score = await self.db.first("SELECT * FROM eligible_reputation_scores WHERE forecast_id=? AND user_id=?", (f["id"], self.other))
        self.assertEqual((score["outcome"], score["probability"], score["correct"]), ("YES", 20, 0))
        self.assertAlmostEqual(score["brier_score"], 0.64)

    async def test_legacy_practice_before_points_rollout_can_be_restored(self):
        f = await self.publish()
        original = await self.app._forecast(f["id"])
        choice = fixtures.user_forecast(original.specification, forecast_id=f["id"], forecaster_id=self.other,
                                        submitted_at_ms=self.now)
        await self.app._mutate(original, SubmitForecast(user_forecast=choice), key="old-practice-command", extra=[
            self.app._record_artifact(choice, "user_forecast"),
            ("INSERT INTO user_forecasts VALUES(?,?,'YES',70,70,?,?,?)", (f["id"], self.other, self.now, original.revision+1, dumps(choice))),
            ("INSERT INTO forecast_history VALUES(?,?,?,?,70,1,?)", (f["id"], original.revision+1, self.other, dumps(choice), self.now)),
        ])
        cutoff = self.now+10
        self.now = cutoff
        await self.vote(f, "NO", 300, "new-points-command")
        trigger = await self.prepare(f, cutoff)
        svc = self.service()
        await svc.apply(trigger)
        self.assertEqual((await svc.finish(trigger))["status"], "complete")
        effective = await self.db.first("SELECT * FROM eligible_user_forecasts WHERE forecast_id=?", (f["id"],))
        self.assertEqual((effective["outcome"], effective["confidence"]), ("YES", 70))
        self.assertEqual((await self.app.points.position(self.other, f["id"]))["amount"], 0)
        self.assertEqual((await self.app.points.summary(self.other))["available"], 1000)

    async def partial_history(self):
        f = await self.publish()
        first = await self.vote(f, "NO", 100, "first-preserved-position")
        self.now += 1
        middle = await self.vote(f, "NO", 900, "last-preserved-position")
        cutoff = self.now+10
        self.now = cutoff
        late = await self.vote(f, "YES", 900, "late-side-switch")
        trigger = await self.prepare(f, cutoff)
        svc = self.service()
        await svc._decide(trigger)
        revisions = [entry["myForecast"]["revision"] for entry in (first, middle, late)]
        for revision in (revisions[0], revisions[2]):
            saved = await self.db.first("SELECT receipt FROM command_receipts WHERE forecast_id=? AND json_extract(receipt,'$.revision')=?",
                                         (f["id"], revision))
            receipt = loads(CommandReceipt, saved["receipt"])
            choice = receipt.accepted_user_forecast
            await self.db.execute("INSERT INTO forecast_receipt_eligibility VALUES(?,?,?,?,?,?,?,?)",
                (trigger.trigger_hash, f["id"], self.other, revision, content_hash(receipt),
                 receipt_status(choice.submitted_at_ms, cutoff, "published_instant"), dumps(choice), choice.submitted_at_ms))
        account = await self.db.first("SELECT available,committed FROM point_accounts WHERE user_id=?", (self.other,))
        invalid_adjustment = (
            "INSERT INTO point_eligibility_adjustments(id,decision_id,forecast_id,user_id,old_amount,new_amount,old_revision,new_revision,"
            "outcome,available_delta,committed_delta,available_after,committed_after,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("incomplete-history-correction", trigger.trigger_hash, f["id"], self.other, 900, 100, revisions[2], revisions[0], "NO",
             800, -800, account["available"]+800, account["committed"]-800, self.now))
        return f, trigger, svc, revisions, invalid_adjustment

    async def test_partial_classification_cannot_refund_a_superseded_smaller_stake(self):
        f, trigger, svc, revisions, adjustment = await self.partial_history()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "eligibility_receipts_incomplete"):
            await self.db.execute(*adjustment)
        self.assertEqual((await self.app.points.position(self.other, f["id"]))["amount"], 900)
        self.assertEqual((await self.app.points.summary(self.other))["available"], 100)
        self.assertIsNone(await self.db.first("SELECT id FROM point_eligibility_adjustments"))
        await svc.apply(trigger)
        self.assertEqual((await svc.finish(trigger))["status"], "complete")
        position = await self.db.first("SELECT * FROM point_positions WHERE forecast_id=? AND user_id=?", (f["id"], self.other))
        self.assertEqual((position["forecast_revision"], position["amount"], position["outcome"]), (revisions[1], 900, "NO"))
        self.assertEqual((await self.app.points.summary(self.other))["available"], 100)

    async def test_completion_independently_rejects_preexisting_wrong_correction_target(self):
        f, trigger, svc, revisions, adjustment = await self.partial_history()
        # Fault-inject the exact historical buggy adapter effect, then reinstall
        # the real guard. Completion must detect the error independently.
        guard = await self.db.first("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='point_eligibility_adjustment_validate'")
        self.connection.execute("DROP TRIGGER point_eligibility_adjustment_validate")
        await self.db.execute(*adjustment)
        self.connection.execute(guard["sql"])
        await svc.apply(trigger)
        effective = await self.db.first("SELECT revision FROM eligible_user_forecasts WHERE forecast_id=?", (f["id"],))
        self.assertEqual(effective["revision"], revisions[1])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "eligibility_incomplete"):
            await self.db.execute(*svc.completion_sql(trigger))
        self.assertEqual((await svc.finish(trigger))["status"], "pending")
        self.assertIsNotNone(await self.db.first("SELECT * FROM forecast_resolution_blockers WHERE forecast_id=?", (f["id"],)))

    async def test_completion_requires_actual_position_and_separate_retry_metadata(self):
        f = await self.publish()
        await self.vote(f, "NO", 300, "correct-target-position")
        trigger = await self.prepare(f, self.now+10)
        svc = self.service()
        await svc.apply(trigger)
        decision = await self.db.first("SELECT * FROM forecast_eligibility_decisions")
        retry = await self.db.first("SELECT * FROM forecast_eligibility_retry")
        self.assertEqual(retry, {"decision_id": trigger.trigger_hash, "attempts": 0, "next_attempt": 0})
        await self.db.execute("UPDATE forecast_eligibility_retry SET attempts=attempts+1,next_attempt=? WHERE decision_id=?", (self.now+60000, trigger.trigger_hash))
        self.assertEqual(await self.db.first("SELECT * FROM forecast_eligibility_decisions"), decision)
        await self.db.execute("UPDATE point_positions SET amount=100 WHERE forecast_id=? AND user_id=?", (f["id"], self.other))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "eligibility_incomplete"):
            await self.db.execute(*svc.completion_sql(trigger))
        self.assertEqual((await svc.finish(trigger))["status"], "pending")
