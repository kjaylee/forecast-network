"""Actual SQLite migrations, typed-target approval, clock provenance and durable v2 publication CAS."""

from __future__ import annotations

import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError as error:  # pragma: no cover
    # The dependency-free CI job exercises the domain without installing anything.
    raise unittest.SkipTest(f"cryptography is required: {error}") from error
from forecast_application.risk_feed_v2 import (
    admit_definition,
    admit_profile,
    approve_binding_v2,
    configure_operation,
    latest_feed_v2,
    operate_feeds_v2,
    operational_bindings_v2,
    operations_health,
    publish_feed_v2,
    revoke_binding_v2,
    stale_bindings_v2,
    training_export,
)
from forecast_application.risk_refresh import refresh_bound_prediction_v2
from forecast_domain.errors import ValidationError
from forecast_domain.models import Category
from forecast_domain.risk_feed import RiskFeedBindingV2, signing_bytes_v2
from forecast_domain.serialization import content_hash

from tests import test_web_application as fixtures
from tests.test_risk_feed_contract import GENESIS
from tests.test_risk_feed_v2_contract import golden_definition, golden_profile
from tests.test_web_ai import Transport, coordinator, specification

HOUR = 3_600_000


def spell(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RiskFeedV2ProducerTests(unittest.IsolatedAsyncioTestCase):
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.ApplicationTests.asyncSetUp(self)
        self.key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        self.start = (self.now // 1000 + 2 * 3600) * 1000   # opens now; operational two hours later
        self.end = self.start + 48 * HOUR
        question = (f"During [{spell(self.start)}, {spell(self.end)}), will USDC/USD close strictly below USD 0.9900 "
                    f"on both Kraken and Bitstamp in any same completed 5-minute candle? Submissions close exactly "
                    f"{spell(self.end)}.")
        original_spec = fixtures.fixtures.specification
        policy = specification().source_policy
        with patch.object(fixtures.fixtures, "specification", side_effect=lambda **kw: original_spec(
                **{**kw, "source_policy": policy, "close_at_ms": self.end, "category": Category.CRYPTO})):
            draft = await self.app.compile_forecast(self.uid, question)
            self.forecast = (await self.app.publish_forecast(self.uid, draft["draftId"], "publish-risk-v2"))["forecast"]
        self.row = await self.db.first("SELECT * FROM forecasts WHERE id=?", (self.forecast["id"],))
        self.definition = golden_definition()
        self.profile = golden_profile()
        await admit_definition(self.db, feed_id="risk-v2", definition=self.definition,
                               approved_by="authenticated-admin", now_ms=self.now)
        await admit_profile(self.db, feed_id="risk-v2", profile=self.profile,
                            approved_by="authenticated-admin", now_ms=self.now)
        self.binding = RiskFeedBindingV2(
            binding_id="usdc-depeg-1d-episode-1", forecast_id=self.forecast["id"],
            specification_hash=self.row["specification_hash"], channel="depegRisk1d", asset="USDC",
            category=Category.CRYPTO, series_id="usdc-depeg-1d-w48", episode_id=spell(self.start),
            target_start_ms=self.start, target_end_ms=self.end, policy_horizon_ms=24 * HOUR,
            definition_hash=content_hash(self.definition), mapping_profile_id=self.profile.profile_id,
            mapping_profile_version=self.profile.profile_version, mapping_profile_hash=content_hash(self.profile),
            mapping_kind="containing_upper_estimate",
            question_event_definition_hash=content_hash({
                "specification_hash": self.row["specification_hash"],
                "window": f"[{spell(self.start)}, {spell(self.end)})"}),
            approval_artifact_hash="1" * 64,
            authorization_valid_from_ms=self.row["open_at"], authorization_valid_until_ms=self.end,
            operational_valid_from_ms=self.start, operational_valid_until_ms=self.end - 24 * HOUR,
        )
        await approve_binding_v2(self.db, feed_id="risk-v2", binding=self.binding,
                                 approved_by="authenticated-admin", now_ms=self.now)
        self.transport = Transport([{"yesProbabilityBp": 180, "rationale": "Retained context; tail uncertainty."}])
        self.app.ai = coordinator(self.transport)

    async def sign(self, data):
        return self.key.sign(data)

    async def produce(self, signer=None):
        return await publish_feed_v2(
            self.db, feed_id="risk-v2", genesis_hash=GENESIS, key_id="test-key", public_key_hex=self.public,
            signer=signer or self.sign, now_ms=self.now, weight_set_hash="d" * 64,
            weight_set_version="source-calibration-v2", calibration_cohort_id="usdc-depeg-1d-w48-containing")

    async def test_typed_target_must_equal_published_interval_and_admitted_records(self):
        for changes in [
            dict(binding_id="b1", target_start_ms=self.start + 300_000),
            dict(binding_id="b2", target_end_ms=self.end - 300_000, operational_valid_until_ms=self.end - 24 * HOUR - 300_000),
            dict(binding_id="b3", definition_hash="0" * 64),
            dict(binding_id="b4", mapping_profile_hash="0" * 64),
            dict(binding_id="b5", question_event_definition_hash="0" * 64),
            dict(binding_id="b6", asset="USDT"),
        ]:
            with self.subTest(changes):
                with self.assertRaises(ValidationError):
                    await approve_binding_v2(self.db, feed_id="risk-v2", binding=replace(self.binding, **changes),
                                             approved_by="admin", now_ms=self.now)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("DELETE FROM risk_feed_bindings_v2")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE risk_feed_definitions_v2 SET channel='depegRisk7d'")

    async def test_compile_time_estimate_without_clock_is_never_published_as_fresh(self):
        # Before operational validity there is no episode; after it, the compile-time
        # estimate still lacks capture/completion clocks. Both publish an honest,
        # signed "unavailable" status instead of a manufactured probability.
        before = await self.produce()
        self.assertEqual(before.payload.bindings, ())
        status = {c.channel: (c.status, c.reason) for c in before.payload.channel_coverage}
        self.assertEqual(status["depegRisk1d"], ("unavailable", "no operational episode"))
        self.assertEqual(status["depegRisk7d"], ("unsupported", "no admitted definition"))
        self.now = self.start + 1000
        self.assertEqual(len(await operational_bindings_v2(self.db, feed_id="risk-v2", now_ms=self.now)), 1)
        after = await self.produce()
        self.assertEqual(after.payload.bindings, ())
        self.assertEqual(after.payload.sequence, 2)
        status = {c.channel: (c.status, c.reason) for c in after.payload.channel_coverage}
        self.assertEqual(status["depegRisk1d"], ("unavailable", "no fresh estimate with complete clock provenance"))
        self.assertEqual(await latest_feed_v2(self.db, feed_id="risk-v2"), after)

    async def test_refresh_retains_every_clock_role_and_publication_carries_them(self):
        self.now = self.start + 1000
        result = await refresh_bound_prediction_v2(self.app, self.binding.binding_id)
        estimate_hash = result["aiForecast"]["artifactHash"]
        clock_row = await self.db.first(
            "SELECT body FROM artifacts WHERE hash=(SELECT clock_artifact_hash FROM risk_prediction_clocks_v2 "
            "WHERE estimate_artifact_hash=?)", (estimate_hash,))
        clock = json.loads(clock_row["body"])
        self.assertEqual(clock["version"], "risk-prediction-clock-v1")
        self.assertEqual(clock["forecast_as_of_ms"], result["aiForecast"]["asOf"])
        self.assertLessEqual(clock["source_capture_started_at_ms"], clock["source_capture_completed_at_ms"])
        self.assertLessEqual(clock["forecast_as_of_ms"], clock["evaluation_completed_at_ms"])
        self.assertIsNone(clock["source_watermark_ms"])
        sources = await self.db.first("SELECT kind FROM artifacts WHERE hash=?", (clock["source_bundle_hash"],))
        self.assertEqual(sources["kind"], "risk-prediction-sources")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE risk_prediction_clocks_v2 SET recorded_at=0")

        self.now += 5000
        envelope = await self.produce()
        self.key.public_key().verify(bytes.fromhex(envelope.signature_hex), signing_bytes_v2(envelope.payload))
        payload = envelope.payload
        self.assertEqual(payload.purpose, "forecast-risk-feed-v2")
        self.assertEqual(payload.bindings, (self.binding,))
        signal = payload.signals[0]
        self.assertEqual((signal.source, signal.question_probability_bp, signal.value_kind),
                         ("ai", 180, "question_probability"))
        self.assertEqual(signal.forecast_as_of_ms, result["aiForecast"]["asOf"])
        self.assertEqual(signal.evaluation_completed_at_ms, clock["evaluation_completed_at_ms"])
        self.assertEqual(signal.estimate_hash, estimate_hash)
        self.assertEqual(signal.source_bundle_hash, clock["source_bundle_hash"])
        self.assertLessEqual(payload.expires_at_ms, signal.forecast_as_of_ms + self.profile.max_forecast_age_ms)
        self.assertLessEqual(payload.expires_at_ms, self.binding.operational_valid_until_ms)
        covered = [c for c in payload.channel_coverage if c.status == "covered"]
        self.assertEqual([(c.channel, c.binding_id) for c in covered], [("depegRisk1d", self.binding.binding_id)])
        self.assertEqual(len(payload.channel_coverage), 12)
        self.assertTrue(all(c.status == "unsupported" for c in payload.channel_coverage if c.status != "covered"))
        self.assertEqual(payload.profile_set_hash,
                         content_hash({"feed_id": "risk-v2", "profiles": [content_hash(self.profile)]}))
        self.assertEqual(await latest_feed_v2(self.db, feed_id="risk-v2"), envelope)
        self.assertEqual((await self.db.first("SELECT COUNT(*) n FROM mutation_guards"))["n"], 0)
        second = await self.produce()
        self.assertEqual(second.payload.sequence, 2)
        self.assertEqual(second.payload.signals[0].forecast_as_of_ms, signal.forecast_as_of_ms)  # never retimed

    async def test_crowd_pool_is_as_fresh_as_its_oldest_member(self):
        self.now = self.start + 1000
        await self.app.submit_forecast(self.other, self.forecast["id"], "YES", 80, self.forecast["revision"], "v2-vote-1")
        first_vote = self.now
        self.now += 60_000
        third = (await self.app.register("세 번째 사용자"))["user"]["id"]
        revision = (await self.app._forecast(self.forecast["id"])).revision
        await self.app.submit_forecast(third, self.forecast["id"], "NO", 60, revision, "v2-vote-2")
        self.now += 1000
        envelope = await self.produce()
        crowd = [s for s in envelope.payload.signals if s.source == "crowd"][0]
        self.assertEqual(crowd.sample_count, 2)
        self.assertEqual((crowd.oldest_member_as_of_ms, crowd.newest_member_as_of_ms), (first_vote, first_vote + 60_000))
        self.assertEqual(crowd.forecast_as_of_ms, first_vote + 60_000)
        self.assertLessEqual(envelope.payload.expires_at_ms, first_vote + self.profile.max_forecast_age_ms)
        self.assertEqual(crowd.constituent_dataset_hash, crowd.source_bundle_hash)
        self.assertFalse(any(s.source == "ai" for s in envelope.payload.signals))

    async def approve_second_episode(self, start_offset_ms, binding_id):
        """A second overlapping 48h episode on the same channel, published by another user."""
        start = self.start + start_offset_ms
        end = start + 48 * HOUR
        question = (f"During [{spell(start)}, {spell(end)}), will USDC/USD close strictly below USD 0.9900 on both "
                    f"Kraken and Bitstamp in any same completed 5-minute candle? Submissions close exactly {spell(end)}.")
        original_spec = fixtures.fixtures.specification
        policy = specification().source_policy
        real_ai, self.app.ai = self.app.ai, self.ai
        with patch.object(fixtures.fixtures, "specification", side_effect=lambda **kw: original_spec(
                **{**kw, "source_policy": policy, "close_at_ms": end, "category": Category.CRYPTO})):
            draft = await self.app.compile_forecast(self.other, question)
            forecast = (await self.app.publish_forecast(self.other, draft["draftId"], "publish-" + binding_id))["forecast"]
        self.app.ai = real_ai
        row = await self.db.first("SELECT * FROM forecasts WHERE id=?", (forecast["id"],))
        binding = replace(self.binding, binding_id=binding_id, forecast_id=forecast["id"],
                          specification_hash=row["specification_hash"], episode_id=spell(start),
                          target_start_ms=start, target_end_ms=end,
                          question_event_definition_hash=content_hash({
                              "specification_hash": row["specification_hash"],
                              "window": f"[{spell(start)}, {spell(end)})"}),
                          authorization_valid_from_ms=row["open_at"], authorization_valid_until_ms=end,
                          operational_valid_from_ms=start, operational_valid_until_ms=end - 24 * HOUR)
        await approve_binding_v2(self.db, feed_id="risk-v2", binding=binding, approved_by="a", now_ms=self.now)
        return forecast, binding

    async def test_one_episode_per_channel_is_selected_by_newest_target_start_with_fallback(self):
        later, later_binding = await self.approve_second_episode(12 * HOUR, "usdc-depeg-1d-episode-2")
        self.now = self.start + 12 * HOUR + 1000   # both episodes operational
        voter = (await self.app.register("투표자"))["user"]["id"]
        await self.app.submit_forecast(voter, self.forecast["id"], "YES", 80, self.forecast["revision"], "vote-episode-one-1")
        await self.app.submit_forecast(voter, later["id"], "YES", 70, later["revision"], "vote-episode-two-1")
        self.now += 1000
        # The newer episode's first grid candle is still open: consumers could not yet cover its
        # window from its start, so the older overlapping episode keeps the channel published.
        envelope = await self.produce()          # would fail on duplicate channel without selection
        self.assertEqual([b.binding_id for b in envelope.payload.bindings], [self.binding.binding_id])
        self.assertEqual([s.question_probability_bp for s in envelope.payload.signals], [8000])
        self.now = self.start + 12 * HOUR + self.definition.sampling_grid_ms
        envelope = await self.produce()
        self.assertEqual([b.binding_id for b in envelope.payload.bindings], [later_binding.binding_id])
        self.assertEqual([s.question_probability_bp for s in envelope.payload.signals], [7000])
        # Probability never drives selection: the newest episode wins even with the higher value.
        await revoke_binding_v2(self.db, binding_id=later_binding.binding_id, revoked_by="a", now_ms=self.now,
                                reason="fall back")
        envelope = await self.produce()
        self.assertEqual([b.binding_id for b in envelope.payload.bindings], [self.binding.binding_id])
        self.assertEqual([s.question_probability_bp for s in envelope.payload.signals], [8000])

    async def test_newer_episode_without_estimate_falls_back_to_older_episode(self):
        later, later_binding = await self.approve_second_episode(12 * HOUR, "usdc-depeg-1d-episode-2")
        self.now = self.start + 12 * HOUR + self.definition.sampling_grid_ms + 1000
        voter = (await self.app.register("투표자"))["user"]["id"]
        await self.app.submit_forecast(voter, self.forecast["id"], "YES", 80, self.forecast["revision"], "vote-episode-one-1")
        self.now += 1000
        envelope = await self.produce()
        self.assertEqual([b.binding_id for b in envelope.payload.bindings], [self.binding.binding_id])
        self.assertEqual([c.status for c in envelope.payload.channel_coverage if c.channel == "depegRisk1d"], ["covered"])

    async def test_concurrent_publish_revocation_and_source_mutation_roll_back(self):
        self.now = self.start + 1000
        await self.app.submit_forecast(self.other, self.forecast["id"], "YES", 80, self.forecast["revision"], "v2-vote-1")
        self.now += 1000

        async def race(data):
            await self.produce()
            return self.key.sign(data)

        with self.assertRaises(sqlite3.IntegrityError):
            await self.produce(race)
        self.assertEqual((await latest_feed_v2(self.db, feed_id="risk-v2")).payload.sequence, 1)

        async def change(data):
            await self.db.execute("UPDATE forecasts SET state='PAUSED' WHERE id=?", (self.binding.forecast_id,))
            return self.key.sign(data)

        with self.assertRaises(sqlite3.IntegrityError):
            await self.produce(change)
        await self.db.execute("UPDATE forecasts SET state='OPEN' WHERE id=?", (self.binding.forecast_id,))

        async def revoke(data):
            await revoke_binding_v2(self.db, binding_id=self.binding.binding_id, revoked_by="admin",
                                    now_ms=self.now, reason="wrong mapping")
            return self.key.sign(data)

        with self.assertRaises(sqlite3.IntegrityError):
            await self.produce(revoke)
        self.assertEqual((await latest_feed_v2(self.db, feed_id="risk-v2")).payload.sequence, 1)
        revoked = await self.produce()
        self.assertEqual((revoked.payload.sequence, revoked.payload.bindings), (2, ()))
        self.assertEqual([c.reason for c in revoked.payload.channel_coverage if c.channel == "depegRisk1d"],
                         ["no operational episode"])

    async def configure(self, enabled=True):
        document = {"version": "source-calibration-v2", "weights": {}}
        await self.db.execute("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
                              (content_hash(document), "risk-weight-set", json.dumps(document, sort_keys=True),
                               "application/json", self.now))
        await configure_operation(self.db, feed_id="risk-v2", weight_set_hash=content_hash(document),
                                  weight_set_version="source-calibration-v2",
                                  calibration_cohort_id="usdc-depeg-1d-w48-containing", enabled=enabled,
                                  configured_by="authenticated-admin", now_ms=self.now)

    async def tick(self):
        refreshed = []

        async def refresh(binding_id):
            refreshed.append(binding_id)
            return await refresh_bound_prediction_v2(self.app, binding_id)

        async def publish(feed_id, digest, version, cohort):
            return await publish_feed_v2(self.db, feed_id=feed_id, genesis_hash=GENESIS, key_id="test-key",
                                         public_key_hex=self.public, signer=self.sign, now_ms=self.now,
                                         weight_set_hash=digest, weight_set_version=version,
                                         calibration_cohort_id=cohort)

        return await operate_feeds_v2(self.db, now_ms=self.now, refresh=refresh, publish=publish), refreshed

    async def test_operation_requires_admitted_weights_and_only_enabled_feeds_tick(self):
        with self.assertRaises(ValidationError):
            await configure_operation(self.db, feed_id="risk-v2", weight_set_hash="0" * 64,
                                      weight_set_version="source-calibration-v2", calibration_cohort_id="c",
                                      enabled=True, configured_by="admin", now_ms=self.now)
        await self.configure(enabled=False)
        outcomes, refreshed = await self.tick()
        self.assertEqual((outcomes, refreshed), ([], []))
        self.assertIsNone(await latest_feed_v2(self.db, feed_id="risk-v2"))

    async def test_scheduled_tick_prewarms_refreshes_once_and_publishes_with_the_estimate(self):
        await self.configure()
        # Half a forecast age before operational start the binding is stale (no clock-backed estimate).
        self.now = self.start - self.profile.max_forecast_age_ms // 2 - 1000
        self.assertEqual(await stale_bindings_v2(self.db, feed_id="risk-v2", now_ms=self.now), [])
        self.now += 2000
        self.assertEqual(await stale_bindings_v2(self.db, feed_id="risk-v2", now_ms=self.now),
                         [self.binding.binding_id])
        outcomes, refreshed = await self.tick()
        self.assertEqual(refreshed, [self.binding.binding_id])
        self.assertEqual(outcomes[0]["published"], 1)
        self.assertEqual(outcomes[0]["covered"], [])          # not yet operational: honest empty publication
        refreshed_at = self.now
        # Inside operational validity with a fresh clock-backed estimate: no second refresh, AI published.
        self.now = self.start + 500
        outcomes, refreshed = await self.tick()
        self.assertEqual(refreshed, [])
        self.assertEqual((outcomes[0]["published"], outcomes[0]["covered"]), (2, ["depegRisk1d"]))
        latest = await latest_feed_v2(self.db, feed_id="risk-v2")
        self.assertEqual(latest.payload.signals[0].source, "ai")
        self.assertLessEqual(refreshed_at, latest.payload.signals[0].forecast_as_of_ms)
        # Once the estimate reaches half its permitted age the next tick refreshes again.
        self.now = refreshed_at + self.profile.max_forecast_age_ms // 2
        outcomes, refreshed = await self.tick()
        self.assertEqual(refreshed, [self.binding.binding_id])
        self.assertEqual(outcomes[0]["published"], 3)
        log = await self.db.all("SELECT outcome FROM risk_feed_operation_log_v2 ORDER BY tick_at")
        self.assertEqual([row["outcome"] for row in log], ["published", "published", "published"])

    async def test_every_tick_records_where_its_time_went(self):
        # A tick was measured taking 130 seconds with no refresh and no episode due, so the
        # time was not being spent where the work was. These phases say where it was.
        await self.configure()
        self.now = self.start + 1
        outcomes, _ = await self.tick()
        phase = outcomes[0]["phaseMs"]
        self.assertEqual(sorted(phase), ["episodes", "list", "publish", "refresh", "stale"])
        for name, milliseconds in phase.items():
            with self.subTest(phase=name):
                self.assertIs(type(milliseconds), int)
                self.assertGreaterEqual(milliseconds, 0)

    async def test_a_tick_with_nothing_due_still_records_the_phases_it_skipped(self):
        await self.configure()
        self.now = self.start + 500
        outcomes, _ = await self.tick()
        phase = outcomes[0]["phaseMs"]
        self.assertEqual(phase["episodes"], 0, "no episode was due, so that phase cost nothing")
        self.assertIn("publish", phase)

    async def test_operations_health_reports_feed_age_failures_and_source_staleness(self):
        await self.configure()
        self.now = self.start + 1000
        await self.tick()
        health = await operations_health(self.db, now_ms=self.now + 5000)
        feed = health["feeds"][0]
        self.assertEqual((feed["feedId"], feed["enabled"], feed["latestSequence"], feed["latestAgeMs"]),
                         ("risk-v2", True, 1, 5000))
        self.assertEqual((feed["ticksLast30m"], feed["failedTicksLast30m"]), (1, 0))
        self.assertEqual(feed["coveredChannels"], ["depegRisk1d"])   # the tick refreshed and published
        self.assertEqual(health["sourceWatch"], {"total": 0, "failing": 0, "stale": 0})
        self.assertEqual(health["series"], [])
        self.assertEqual(health["stuckForecasts"], 0)

    async def test_stuck_forecasts_are_counted_because_nothing_else_counts_them(self):
        # Three have been retrying for days in production. Nothing reported the count, so it
        # grew unremarked while the monitor called the pipeline healthy.
        await self.configure()
        self.now = self.start + 1000
        await self.tick()
        await self.db.execute("UPDATE forecasts SET job_error='held for review'")
        health = await operations_health(self.db, now_ms=self.now + 5000)
        self.assertEqual(health["stuckForecasts"], 1)

    async def test_training_export_uses_finalized_labels_and_first_retained_signals(self):
        await self.configure()
        self.now = self.start + 1000
        await self.tick()                                   # refresh + publish (ai signal, seq 1)
        self.now += 60_000
        await self.tick()                                   # seq 2 repeats the same estimate
        self.assertEqual((await training_export(self.db, feed_id="risk-v2", now_ms=self.now))["bindings"], [])
        # The label is the retained finalization event plus the finalized outcome; the
        # lifecycle that produces them is covered elsewhere, so record them directly here.
        self.now = self.end + 50 * HOUR
        await self.db.execute("UPDATE forecasts SET state='FINALIZED',finalized_outcome='NO' WHERE id=?", (self.forecast["id"],))
        await self.db.execute("INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)",
                              (self.forecast["id"], 99, "f" * 64, json.dumps({"command_name": "finalize"}), self.now))
        finalized = {"finalized_outcome": "NO"}
        export = await training_export(self.db, feed_id="risk-v2", now_ms=self.now)
        item = export["bindings"][0]
        self.assertEqual((item["forecastId"], item["outcome"]), (self.forecast["id"], finalized["finalized_outcome"]))
        self.assertLessEqual(item["finalizedAtMs"], item["eligibilityAtMs"])
        self.assertEqual([s["source"] for s in item["firstSignals"]], ["ai"])
        self.assertEqual(item["firstSignals"][0]["sequence"], 1)
        self.assertEqual(item["firstSignals"][0]["question_probability_bp"], 180)

    async def test_exact_dated_binding_is_not_refreshed_after_its_target_start(self):
        exact_profile = replace(self.profile, profile_id="exact-dated-target", profile_version="exact-profile-v1",
                                mapping_kind="exact_dated", predicate_class="exact_target", max_policy_lag_ms=HOUR)
        exact_definition = replace(self.definition, definition_version="exact", mapping_kind="exact_dated",
                                   mapping_profile_id=exact_profile.profile_id,
                                   mapping_profile_version=exact_profile.profile_version)
        await admit_definition(self.db, feed_id="risk-v2", definition=exact_definition, approved_by="a", now_ms=self.now)
        await admit_profile(self.db, feed_id="risk-v2", profile=exact_profile, approved_by="a", now_ms=self.now)
        await revoke_binding_v2(self.db, binding_id=self.binding.binding_id, revoked_by="a", now_ms=self.now,
                                reason="switch episode to exact dated for this test")
        original_spec = fixtures.fixtures.specification
        policy = specification().source_policy
        end = self.start + 24 * HOUR
        question = f"During [{spell(self.start)}, {spell(end)}), will USDC/USD close below USD 0.9900 on both venues?"
        real_ai, self.app.ai = self.app.ai, self.ai   # deterministic compile adapter for the second question
        with patch.object(fixtures.fixtures, "specification", side_effect=lambda **kw: original_spec(
                **{**kw, "source_policy": policy, "close_at_ms": end, "category": Category.CRYPTO})):
            draft = await self.app.compile_forecast(self.other, question)
            forecast = (await self.app.publish_forecast(self.other, draft["draftId"], "publish-exact"))["forecast"]
        self.app.ai = real_ai
        row = await self.db.first("SELECT * FROM forecasts WHERE id=?", (forecast["id"],))
        binding = replace(self.binding, binding_id="exact-episode", forecast_id=forecast["id"],
                          specification_hash=row["specification_hash"], target_end_ms=end,
                          definition_hash=content_hash(exact_definition), mapping_profile_id=exact_profile.profile_id,
                          mapping_profile_version=exact_profile.profile_version,
                          mapping_profile_hash=content_hash(exact_profile), mapping_kind="exact_dated",
                          question_event_definition_hash=content_hash({
                              "specification_hash": row["specification_hash"],
                              "window": f"[{spell(self.start)}, {spell(end)})"}),
                          authorization_valid_from_ms=row["open_at"], authorization_valid_until_ms=end,
                          operational_valid_until_ms=end)
        await approve_binding_v2(self.db, feed_id="risk-v2", binding=binding, approved_by="a", now_ms=self.now)
        self.now = self.start - 1000
        self.assertEqual(await stale_bindings_v2(self.db, feed_id="risk-v2", now_ms=self.now), ["exact-episode"])
        self.now = self.start + 1000
        self.assertEqual(await stale_bindings_v2(self.db, feed_id="risk-v2", now_ms=self.now), [])


if __name__ == "__main__":
    unittest.main()
