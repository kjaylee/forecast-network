"""Ordinary evidence cannot silently award receipts submitted after publication."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from forecast_application.errors import AppError
from forecast_application.resolution_timing import ResolutionTiming
from forecast_application.sources import Artifact
from forecast_domain.models import Outcome

from tests import model_fixtures as fixtures
from tests import test_markets as market_tests
from tests import test_receipt_eligibility as eligibility_tests
from tests import test_web_application as application_tests


def article(at: int | None, *, date_only: bool = False) -> str:
    if at is None:
        return "<article>The official announcement identifies Product X.</article>"
    value = datetime.fromtimestamp(at/1000, timezone.utc).strftime("%Y-%m-%d" if date_only else "%Y-%m-%dT%H:%M:%SZ")
    return '<meta property="article:published_time" content="'+value+'"><article>Product X announced.</article>'


def proposal(forecast, now, bodies):
    evidence = tuple(fixtures.evidence(forecast.specification, evidence_id="timing-"+str(index),
        content=body, collected_at_ms=now-10) for index, body in enumerate(bodies))
    resolution = fixtures.resolution(forecast.specification, forecast_id=forecast.forecast_id,
                                    proposed_at_ms=now, evidence=evidence)
    artifacts = tuple(Artifact(hashlib.sha256(body.encode()).hexdigest(), "source", body, "text/html") for body in bodies)
    return resolution, artifacts


class ResolutionTimingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = application_tests.ApplicationTests.asyncSetUp
    asyncTearDown = application_tests.ApplicationTests.asyncTearDown
    random_token = application_tests.ApplicationTests.random_token
    token_hash = staticmethod(application_tests.ApplicationTests.token_hash)
    publish = application_tests.ApplicationTests.publish
    prepare = eligibility_tests.ReceiptEligibilityTests.prepare

    def service(self):
        from forecast_application.eligibility import ForecastEligibility
        return ForecastEligibility(self.db, lambda: self.now, self.random_token)

    async def setup_case(self, vote=True):
        f = await self.publish()
        at = self.now
        if vote:
            await self.app.submit_forecast(self.other, f["id"], "YES", 80, f["revision"], "timing-vote", 300)
        self.now = f["closeAt"]+1000
        return f, await self.app._forecast(f["id"]), at, ResolutionTiming(self.db, lambda: self.now)

    async def expect_review(self, gate, forecast, resolution, artifacts=()):
        with self.assertRaises(AppError) as error:
            await gate.check(forecast, resolution, artifacts)
        self.assertEqual(error.exception.code, "resolution_timing_review")
        self.assertEqual((await gate.status(forecast.forecast_id))["status"], "review")

    async def test_no_participants_do_not_need_publication_metadata(self):
        _, forecast, _, gate = await self.setup_case(vote=False)
        resolution, artifacts = proposal(forecast, self.now, [article(None)])
        await gate.check(forecast, resolution, artifacts)
        self.assertEqual((await gate.status(forecast.forecast_id))["status"], "none")

    async def test_precise_publication_after_all_receipts_passes_from_retained_database(self):
        _, forecast, at, gate = await self.setup_case()
        resolution, artifacts = proposal(forecast, self.now, [article(at+1000)])
        for artifact in artifacts:
            await self.db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)", (artifact.content_hash, artifact.kind,
                artifact.body, artifact.media_type, self.now))
        await gate.check(forecast, resolution)
        self.assertEqual(await self.db.all("SELECT * FROM resolution_timing_reviews"), [])

    async def test_equal_and_earlier_publication_are_reviewed_without_refunding_or_awarding(self):
        for offset in (0, -1000):
            with self.subTest(offset=offset):
                _, forecast, at, gate = await self.setup_case()
                baseline = {table: await self.db.all("SELECT * FROM "+table)
                    for table in ("point_accounts", "point_positions", "point_ledger", "user_forecasts", "events", "command_receipts")}
                resolution, artifacts = proposal(forecast, self.now, [article(at+offset)])
                await self.expect_review(gate, forecast, resolution, artifacts)
                self.assertEqual((await gate.status(forecast.forecast_id))["candidateCutoffAt"], at+offset)
                for table, original in baseline.items():
                    self.assertEqual(await self.db.all("SELECT * FROM "+table), original)
                self.assertIsNotNone(await self.db.first("SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?", (forecast.forecast_id,)))
                with self.assertRaises(sqlite3.IntegrityError):
                    await self.db.execute("UPDATE forecasts SET state='FINALIZED' WHERE id=?", (forecast.forecast_id,))
                with self.assertRaises(sqlite3.IntegrityError):
                    await self.db.execute("INSERT INTO reputation_scores VALUES(?,?,'TECHNOLOGY','YES',80,1,0.04,?)",
                                          (forecast.forecast_id, self.other, self.now))
                await self.asyncTearDown()
                await self.asyncSetUp()

    async def test_date_only_or_missing_metadata_never_invents_midnight(self):
        for body in (article(None), article(1_800_000_000_000, date_only=True)):
            with self.subTest(body=body):
                _, forecast, _, gate = await self.setup_case()
                resolution, artifacts = proposal(forecast, self.now, [body])
                await self.expect_review(gate, forecast, resolution, artifacts)
                self.assertIsNone((await gate.status(forecast.forecast_id))["candidateCutoffAt"])
                await self.asyncTearDown()
                await self.asyncSetUp()

    async def test_missing_and_corrupt_retained_bytes_fail_closed(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                _, forecast, at, gate = await self.setup_case()
                resolution, artifacts = proposal(forecast, self.now, [article(at+1000)])
                supplied = (Artifact(artifacts[0].content_hash, "source", "forged", "text/html"),) if corrupt else ()
                await self.expect_review(gate, forecast, resolution, supplied)
                self.assertEqual((await gate.status(forecast.forecast_id))["reason"], "evidence_integrity" if corrupt else "evidence_unavailable")
                await self.asyncTearDown()
                await self.asyncSetUp()

    async def test_one_older_context_source_holds_entire_resolution(self):
        _, forecast, at, gate = await self.setup_case()
        resolution, artifacts = proposal(forecast, self.now, [article(at+1000), article(at-1000)])
        await self.expect_review(gate, forecast, resolution, artifacts)
        self.assertEqual((await gate.status(forecast.forecast_id))["reason"], "evidence_may_predate_participation")

    async def test_latest_accepted_revision_controls_cutoff_even_if_raw_projection_is_missing(self):
        f = await self.publish()
        first_at = self.now
        await self.app.submit_forecast(self.other, f["id"], "NO", 80, f["revision"], "first-timing-choice", 300)
        self.now += 2000
        record = await self.app._forecast(f["id"])
        await self.app.submit_forecast(self.other, f["id"], "YES", 80, record.revision, "last-timing-choice", 300)
        await self.db.execute("DELETE FROM user_forecasts WHERE forecast_id=?", (f["id"],))
        self.now = f["closeAt"]+1000
        record = await self.app._forecast(f["id"])
        resolution, artifacts = proposal(record, self.now, [article(first_at+1000)])
        gate = ResolutionTiming(self.db, lambda: self.now)
        await self.expect_review(gate, record, resolution, artifacts)
        row = await self.db.first("SELECT last_receipt_at FROM resolution_timing_reviews")
        self.assertEqual(row["last_receipt_at"], first_at+2000)

    async def test_newer_source_cannot_erase_existing_review_or_original_evidence(self):
        _, forecast, at, gate = await self.setup_case()
        resolution, artifacts = proposal(forecast, self.now, [article(at)])
        await self.expect_review(gate, forecast, resolution, artifacts)
        original = await self.db.all("SELECT * FROM resolution_timing_reviews")
        for sql in ("DELETE FROM resolution_timing_reviews", "UPDATE resolution_timing_reviews SET reason='passed'"):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute(sql)
        later, later_artifacts = proposal(forecast, self.now, [article(at+1000)])
        await self.expect_review(gate, forecast, later, later_artifacts)
        self.assertEqual(await self.db.all("SELECT * FROM resolution_timing_reviews"), original)
        retained = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (artifacts[0].content_hash,))
        self.assertEqual(retained["body"], artifacts[0].body)
        row = original[0]
        self.assertEqual(hashlib.sha256(row["body"].encode()).hexdigest(), row["proof_hash"])
        self.assertTrue(json.loads(row["body"])["evidence"][0]["hashVerified"])

    async def test_future_publication_is_not_valid_timing_evidence(self):
        _, forecast, _, gate = await self.setup_case()
        resolution, artifacts = proposal(forecast, self.now, [article(self.now+1000)])
        await self.expect_review(gate, forecast, resolution, artifacts)
        self.assertEqual((await gate.status(forecast.forecast_id))["reason"], "publication_time_inconsistent")

    async def test_completed_eligibility_correction_releases_retained_review(self):
        f, forecast, at, gate = await self.setup_case()
        resolution, artifacts = proposal(forecast, self.now, [article(at)])
        await self.expect_review(gate, forecast, resolution, artifacts)
        trigger = await self.prepare(f, at)
        eligibility = self.service()
        await eligibility.apply(trigger)
        self.assertEqual((await eligibility.finish(trigger))["status"], "complete")
        await gate.check(forecast, resolution, artifacts)
        self.assertEqual((await gate.status(forecast.forecast_id))["status"], "complete")
        self.assertIsNotNone(await self.db.first("SELECT * FROM resolution_timing_reviews"))


class ResolutionTimingClosureTests(unittest.IsolatedAsyncioTestCase):
    """A review whose own reason settles the question stops being a blocker.

    The blocker is a boolean that five separate consumers read -- the mutation gate, the
    state trigger, the scheduler entry, the outbox and the registry -- so an exception
    carved into one of them is four chances to leak a reward. Closing the review turns the
    boolean false everywhere at once, and the closure admits only its own determination.
    """
    asyncSetUp = application_tests.ApplicationTests.asyncSetUp
    asyncTearDown = application_tests.ApplicationTests.asyncTearDown
    random_token = application_tests.ApplicationTests.random_token
    token_hash = staticmethod(application_tests.ApplicationTests.token_hash)
    publish = application_tests.ApplicationTests.publish

    def resolved(self, forecast, bodies, outcome=Outcome.YES):
        evidence = tuple(fixtures.evidence(forecast.specification, evidence_id="closure-"+str(index),
            content=body, collected_at_ms=self.now-10) for index, body in enumerate(bodies))
        return fixtures.resolution(forecast.specification, forecast_id=forecast.forecast_id,
                                   proposed_at_ms=self.now, outcome=outcome, evidence=evidence)

    async def retained(self, body):
        """Publish a forecast, retain a review against it, and hand back the record.

        `body` may be a callable, which receives the moment the last receipt was accepted
        so a test can place publication relative to it.
        """
        forecast = await self.publish()
        submitted_at = self.now
        if callable(body):
            body = body(submitted_at)
        await self.app.submit_forecast(self.other, forecast["id"], "YES", 80, forecast["revision"], "closure-vote", 300)
        self.now = forecast["closeAt"]+1000
        record = await self.app._forecast(forecast["id"])
        gate = ResolutionTiming(self.db, lambda: self.now)
        artifacts = (Artifact(hashlib.sha256(body.encode()).hexdigest(), "source", body, "text/html"),)
        with self.assertRaises(AppError) as error:
            await gate.check(record, self.resolved(record, [body]), artifacts)
        self.assertEqual(error.exception.code, "resolution_timing_review")
        return record, gate, artifacts, submitted_at

    async def test_an_unknown_publication_time_closes_and_releases_every_consumer(self):
        record, gate, _, _ = await self.retained(article(None))
        self.assertEqual((await gate.status(record.forecast_id))["reason"], "publication_time_unknown")
        self.assertIsNotNone(await self.db.first("SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?", (record.forecast_id,)))

        self.assertTrue(await gate.close_indeterminate(record))

        self.assertIsNone(await self.db.first("SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?", (record.forecast_id,)),
                          "the outbox and registry read this view, so it has to empty for them too")
        self.assertIsNone(await self.db.first("SELECT 1 FROM unresolved_resolution_timing_reviews WHERE forecast_id=?", (record.forecast_id,)))
        self.assertEqual((await gate.status(record.forecast_id))["determination"], "INVALID")
        # The review itself is untouched: the closure is a conclusion drawn from it, not an
        # edit of it, and the original proof stays exactly as retained.
        row = await self.db.first("SELECT body, proof_hash FROM resolution_timing_reviews WHERE forecast_id=?", (record.forecast_id,))
        self.assertEqual(hashlib.sha256(row["body"].encode()).hexdigest(), row["proof_hash"])
        closure = await self.db.first("SELECT * FROM resolution_timing_closures WHERE forecast_id=?", (record.forecast_id,))
        self.assertEqual(closure["review_proof_hash"], row["proof_hash"])

    async def test_a_closed_review_admits_its_determination_and_nothing_else(self):
        record, gate, artifacts, _ = await self.retained(article(None))
        await gate.close_indeterminate(record)

        # The evidence still cannot place itself, so a rewarded outcome stays refused. The
        # closure released the blocker; it did not make YES supportable.
        with self.assertRaises(AppError) as error:
            await gate.check(record, self.resolved(record, [article(None)]), artifacts)
        self.assertEqual(error.exception.code, "resolution_timing_determined")
        with self.assertRaises(AppError) as error:
            await gate.check(record, self.resolved(record, [article(None)], outcome=Outcome.NO), artifacts)
        self.assertEqual(error.exception.code, "resolution_timing_determined")

        await gate.check(record, self.resolved(record, [article(None)], outcome=Outcome.INVALID), artifacts)

    async def test_only_an_indeterminable_reason_may_be_closed(self):
        # Every other review reason is a determinate failure with its own handling: evidence
        # that may predate participation is a real timing judgement for the eligibility path,
        # and evidence that is missing or unverified may yet be supplied.
        record, gate, _, submitted_at = await self.retained(lambda at: article(at))
        self.assertEqual((await gate.status(record.forecast_id))["reason"], "evidence_may_predate_participation")
        self.assertFalse(await gate.close_indeterminate(record))
        self.assertIsNone(await self.db.first("SELECT 1 FROM resolution_timing_closures WHERE forecast_id=?", (record.forecast_id,)))
        self.assertIsNotNone(await self.db.first("SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?", (record.forecast_id,)))

    async def test_a_closure_is_not_a_free_pass_on_a_forecast_with_no_review(self):
        forecast = await self.publish()
        self.now = forecast["closeAt"]+1000
        record = await self.app._forecast(forecast["id"])
        gate = ResolutionTiming(self.db, lambda: self.now)
        self.assertFalse(await gate.close_indeterminate(record))
        self.assertIsNone(await self.db.first("SELECT 1 FROM resolution_timing_closures WHERE forecast_id=?", (record.forecast_id,)))

    async def test_a_closure_cannot_be_edited_or_manufactured(self):
        record, gate, _, _ = await self.retained(article(None))
        await gate.close_indeterminate(record)
        original = await self.db.all("SELECT * FROM resolution_timing_closures")
        for sql in ("DELETE FROM resolution_timing_closures", "UPDATE resolution_timing_closures SET determination='YES'"):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute(sql)
        self.assertEqual(await self.db.all("SELECT * FROM resolution_timing_closures"), original)

        # A closure has to be the conclusion of the review it names: a different review's
        # proof, or an evidence item that review did not mark unplaceable, is refused.
        row = original[0]
        body = json.loads(row["body"])
        for column, value in (("review_proof_hash", "0"*64), ("evidence_hash", "1"*64)):
            with self.subTest(column=column):
                forged = json.dumps({**body, "reviewProofHash": value if column == "review_proof_hash" else body["reviewProofHash"],
                                     "evidenceHash": value if column == "evidence_hash" else body["evidenceHash"]},
                                    sort_keys=True, separators=(",", ":"))
                with self.assertRaises(sqlite3.IntegrityError):
                    await self.db.execute("INSERT INTO resolution_timing_closures(forecast_id,specification_hash,"
                        "review_proof_hash,determination,reason,evidence_hash,proof_hash,body,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (row["forecast_id"]+"-other", row["specification_hash"], value if column == "review_proof_hash" else row["review_proof_hash"],
                         "INVALID", "publication_time_unknown", value if column == "evidence_hash" else row["evidence_hash"],
                         hashlib.sha256(forged.encode()).hexdigest(), forged, self.now))


class MarketResolutionTimingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = market_tests.MarketTests.asyncSetUp
    asyncTearDown = market_tests.MarketTests.asyncTearDown
    token = market_tests.MarketTests.token
    opened = market_tests.MarketTests.opened
    user = market_tests.MarketTests.user
    event_sql = staticmethod(market_tests.MarketTests.event_sql)
    market = market_tests.MarketTests.market
    source = market_tests.MarketTests.source
    buy = market_tests.MarketTests.buy

    async def test_active_fills_need_timing_review_without_legacy_votes(self):
        await self.market(mode="active")
        await self.buy()
        at = self.now
        self.now = self.forecast.specification.close_at_ms+1000
        resolution, artifacts = proposal(self.forecast, self.now, [article(at)])
        gate = ResolutionTiming(self.db, lambda: self.now)
        with self.assertRaises(AppError) as error:
            await gate.check(self.forecast, resolution, artifacts)
        self.assertEqual(error.exception.code, "resolution_timing_review")
        self.assertEqual((await gate.status(self.forecast.forecast_id))["status"], "review")

    async def test_an_outcome_cannot_be_chosen_for_a_resolution_by_hand(self):
        # This is the constraint that shapes the fix for the two forecasts that cannot
        # resolve. An INVALID outcome would release them — the review exists to stop a
        # forecast being rewarded on evidence whose publication time is unknown, and INVALID
        # rewards nothing — but the outcome cannot be attached to a resolution by anyone
        # other than the judge that produced it. Editing one fails validation, so the gate
        # has to let the resolution path through rather than decide on its behalf.
        resolution, _ = proposal(self.forecast, self.now, [article(self.now)])
        self.assertEqual(resolution.proposed_outcome, Outcome.YES)
        with self.assertRaises(Exception) as error:
            replace(resolution, proposed_outcome=Outcome.INVALID)
        self.assertIn("commitment", str(error.exception).lower())

        # A resolution the fixture builds for INVALID from the start is a different record,
        # with a matching judge commitment, and validates.
        bodies = [article(self.now)]
        evidence = tuple(fixtures.evidence(self.forecast.specification, evidence_id="timing-" + str(index),
                                           content=body, collected_at_ms=self.now - 10)
                         for index, body in enumerate(bodies))
        fabricated = fixtures.resolution(self.forecast.specification, forecast_id=self.forecast.forecast_id,
                                         proposed_at_ms=self.now, outcome=Outcome.INVALID, evidence=evidence)
        self.assertEqual(fabricated.proposed_outcome, Outcome.INVALID)
        self.assertNotEqual(fabricated.judge.output_hash, resolution.judge.output_hash,
                            "the judge commitment is bound to the decision, not to the question")

    async def test_a_yes_outcome_is_still_held_when_the_publication_time_is_unknown(self):
        await self.market(mode="active")
        await self.buy()
        at = self.now
        self.now = self.forecast.specification.close_at_ms + 1000
        resolution, artifacts = proposal(self.forecast, self.now, [article(at)])
        gate = ResolutionTiming(self.db, lambda: self.now)
        with self.assertRaises(AppError) as error:
            await gate.check(self.forecast, resolution, artifacts)
        self.assertEqual(error.exception.code, "resolution_timing_review",
                         "the review must keep holding outcomes that would reward someone")

    async def test_shadow_fills_alone_do_not_freeze_real_resolution(self):
        await self.market()
        await self.buy()
        self.now = self.forecast.specification.close_at_ms+1000
        resolution, artifacts = proposal(self.forecast, self.now, [article(None)])
        gate = ResolutionTiming(self.db, lambda: self.now)
        await gate.check(self.forecast, resolution, artifacts)
        self.assertEqual((await gate.status(self.forecast.forecast_id))["status"], "none")
