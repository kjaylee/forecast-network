"""Actual SQLite migrations, eligibility reads and durable signed-publication CAS."""

from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError as error:  # pragma: no cover
    # The dependency-free CI job exercises the domain without installing anything.
    raise unittest.SkipTest(f"cryptography is required: {error}") from error
from forecast_application.risk_feed import (
    approve_binding,
    latest_feed,
    publish_feed,
    revoke_binding,
)
from forecast_domain.errors import ValidationError
from forecast_domain.models import Category
from forecast_domain.risk_feed import RiskFeedBinding, signing_bytes

from tests import test_web_application as fixtures
from tests.risk_feed_fixtures import GENESIS


class RiskFeedProducerTests(unittest.IsolatedAsyncioTestCase):
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown
    publish = fixtures.ApplicationTests.publish

    async def asyncSetUp(self):
        await fixtures.ApplicationTests.asyncSetUp(self)
        self.key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.public = (
            self.key.public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            .hex()
        )
        f = await self.publish()
        row = await self.db.first("SELECT * FROM forecasts WHERE id=?", (f["id"],))
        self.binding = RiskFeedBinding(
            binding_id="canonical-001",
            forecast_id=f["id"],
            specification_hash=row["specification_hash"],
            channel="depegRisk1d",
            horizon_hours=24,
            asset="USDC",
            category=Category(row["category"]),
            valid_from_ms=row["open_at"],
            valid_until_ms=row["close_at"],
        )
        await approve_binding(
            self.db,
            feed_id="risk",
            binding=self.binding,
            approved_by="authenticated-admin",
            now_ms=self.now,
        )
        await self.app.submit_forecast(
            self.other, f["id"], "YES", 80, f["revision"], "feed-vote-123"
        )

    async def sign(self, data):
        return self.key.sign(data)

    async def produce(self, signer=None):
        return await publish_feed(
            self.db,
            feed_id="risk",
            genesis_hash=GENESIS,
            key_id="test-key",
            public_key_hex=self.public,
            signer=signer or self.sign,
            now_ms=self.now,
            weight_set_hash="d" * 64,
            weight_set_version="source-calibration-v1",
        )

    async def test_real_signature_current_eligible_probability_and_persisted_sequence(self):
        first = await self.produce()
        self.key.public_key().verify(
            bytes.fromhex(first.signature_hex), signing_bytes(first.payload)
        )
        self.assertEqual(first.payload.signals[0].probability_bp, 8000)
        self.assertEqual(first.payload.signals[0].observed_at_ms, self.now)
        second = await self.produce()
        self.assertEqual(second.payload.sequence, 2)
        self.assertEqual(await latest_feed(self.db, feed_id="risk"), second)
        self.assertEqual((await self.db.first("SELECT COUNT(*) n FROM mutation_guards"))["n"], 0)

    async def test_concurrent_publish_and_source_mutation_roll_back_signed_candidate(self):
        async def race(data):
            await self.produce()
            return self.key.sign(data)

        with self.assertRaises(sqlite3.IntegrityError):
            await self.produce(race)
        self.assertEqual((await latest_feed(self.db, feed_id="risk")).payload.sequence, 1)

        async def change(data):
            await self.db.execute(
                "UPDATE forecasts SET state='PAUSED' WHERE id=?", (self.binding.forecast_id,)
            )
            return self.key.sign(data)

        with self.assertRaises(sqlite3.IntegrityError):
            await self.produce(change)
        self.assertEqual((await latest_feed(self.db, feed_id="risk")).payload.sequence, 1)

    async def test_binding_mismatch_and_revocation_fail_closed(self):
        with self.assertRaises(ValidationError):
            await approve_binding(
                self.db,
                feed_id="risk",
                binding=replace(self.binding, binding_id="bad", specification_hash="f" * 64),
                approved_by="admin",
                now_ms=self.now,
            )

        async def revoke(data):
            await revoke_binding(
                self.db,
                binding_id=self.binding.binding_id,
                revoked_by="admin",
                now_ms=self.now,
                reason="wrong mapping",
            )
            return self.key.sign(data)

        with self.assertRaises(sqlite3.IntegrityError):
            await self.produce(revoke)
        with self.assertRaises(ValidationError):
            await self.produce()
        self.assertIsNone(await latest_feed(self.db, feed_id="risk"))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("DELETE FROM risk_feed_bindings")

    async def test_old_inputs_are_not_retimestamped_and_future_rows_excluded(self):
        observed = self.now
        self.now += 1000
        feed = await self.produce()
        self.assertEqual(feed.payload.signals[0].observed_at_ms, observed)
        await self.db.execute("UPDATE user_forecasts SET submitted_at=?", (self.now + 1,))
        with self.assertRaises(ValidationError):
            await self.produce()

    async def test_qualified_top_is_emitted_and_history_changes_during_signing_fail_cas(self):
        import hashlib
        import json

        template = await self.db.first(
            "SELECT * FROM forecasts WHERE id=?", (self.binding.forecast_id,)
        )
        for i in range(10):
            fid = f"historical-{i}"
            timestamp = self.now - (i + 1) * 86400000
            row = dict(template)
            row.update(
                id=fid,
                draft_id=fid,
                normalized_question=fid,
                title=fid,
                question=fid,
                specification_hash=hashlib.sha256(fid.encode()).hexdigest(),
                state="FINALIZED",
                finalized_outcome="YES",
                open_at=timestamp - 2000,
                close_at=timestamp - 1000,
                created_at=timestamp - 2000,
                updated_at=timestamp,
                revision=1,
            )
            await self.db.execute(
                "INSERT INTO forecasts("
                + ",".join(row)
                + ") VALUES("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
            await self.db.execute(
                "INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)",
                (
                    fid,
                    1,
                    hashlib.sha256((fid + "event").encode()).hexdigest(),
                    json.dumps({"command_name": "finalize"}),
                    timestamp,
                ),
            )
            await self.db.execute(
                "INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,revision,body) VALUES(?,?,'YES',90,90,?,1,'{}')",
                (fid, self.other, timestamp - 1500),
            )
            await self.db.execute(
                "INSERT INTO reputation_scores(forecast_id,user_id,category,outcome,probability,correct,brier_score,created_at) VALUES(?,?,?,'YES',90,1,.01,?)",
                (fid, self.other, self.binding.category.value, timestamp),
            )
        result = await self.produce()
        signals = {s.source: s for s in result.payload.signals}
        self.assertEqual(signals["top"].probability_bp, 8000)
        self.assertEqual(signals["top"].sample_count, 1)
        self.assertEqual(signals["crowd"].dependence_group, signals["top"].dependence_group)

        async def change_history(data):
            await self.db.execute(
                "UPDATE reputation_scores SET created_at=? WHERE forecast_id='historical-0'",
                (self.now + 1,),
            )
            return self.key.sign(data)

        with self.assertRaises(sqlite3.IntegrityError):
            await self.produce(change_history)
        self.assertEqual((await latest_feed(self.db, feed_id="risk")).payload.sequence, 1)
        next_feed = await self.produce()
        self.assertNotIn("top", {s.source for s in next_feed.payload.signals})

    async def test_more_than_twelve_expired_approvals_do_not_block_current_feed(self):
        from forecast_application.risk_feed import active_bindings

        for index in range(13):
            historical = replace(
                self.binding,
                binding_id=f"historical-binding-{index:02d}",
                valid_until_ms=self.now + 1000,
            )
            await approve_binding(
                self.db, feed_id="risk", binding=historical, approved_by="admin", now_ms=self.now
            )
        self.now += 2000
        current = await active_bindings(self.db, feed_id="risk", now_ms=self.now)
        self.assertEqual(current, (self.binding,))
        result = await self.produce()
        self.assertEqual(result.payload.bindings, (self.binding,))
        self.assertEqual(
            (await self.db.first("SELECT COUNT(*) n FROM risk_feed_bindings"))["n"], 14
        )

    async def test_more_than_twelve_current_approvals_still_rejected(self):
        for index in range(12):
            current = replace(self.binding, binding_id=f"current-binding-{index:02d}")
            await approve_binding(
                self.db, feed_id="risk", binding=current, approved_by="admin", now_ms=self.now
            )
        with self.assertRaisesRegex(ValidationError, "capacity exceeded"):
            await self.produce()
        self.assertIsNone(await latest_feed(self.db, feed_id="risk"))
