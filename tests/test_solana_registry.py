"""Exact historical replay, transactional budgets and chain acknowledgement gates."""
from __future__ import annotations

import asyncio
import sqlite3
import struct
import unittest
from dataclasses import replace

from forecast_application import solana_wire as wire
from forecast_application.solana_registry import (
    DEVNET_GENESIS,
    ChainDeadlineNotReached,
    RegistryAccount,
    RegistryError,
    SolanaRegistry,
    identity_hash,
    registry_intent_sql,
    reserve_daily_spend,
)
from forecast_domain import dumps
from forecast_domain.early_resolution import loads_forecast
from forecast_domain.lifecycle import Finalize

from tests import test_automation_integration as automation_tests
from tests import test_web_application as application_tests

PROGRAM = bytes([11])*32
RELAYER = bytes([12])*32
ADMIN = bytes([13])*32


class Transport:
    def __init__(self):
        self.genesis = DEVNET_GENESIS
        self.accounts = {}
        address, _ = wire.config_address(PROGRAM)
        self.accounts[address] = RegistryAccount(address, PROGRAM,
            b"FNCONF01"+ADMIN+RELAYER+bytes(32), 100, True)
        self.sent = []
        self.finalized = False
        self.chain_time = 0

    async def genesis_hash(self):
        return self.genesis

    async def account(self, address):
        return self.accounts.get(address)

    async def send(self, instruction, forecast_address, *, register):
        self.sent.append((instruction, forecast_address, register))
        return wire.base58_encode(bytes([14])*64)

    async def signature_finalized(self, signature):
        return self.finalized

    async def finalized_time_ms(self):
        return self.chain_time


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = application_tests.ApplicationTests.asyncSetUp
    asyncTearDown = application_tests.ApplicationTests.asyncTearDown
    random_token = application_tests.ApplicationTests.random_token
    token_hash = staticmethod(application_tests.ApplicationTests.token_hash)
    publish = application_tests.ApplicationTests.publish
    challenge = application_tests.ApplicationTests.challenge

    def registry(self):
        transport = Transport()
        return SolanaRegistry(self.db, transport, program_id=PROGRAM, relayer=RELAYER,
                              now_ms=lambda: self.now, random_token=self.random_token), transport

    async def chain_account(self, registry, transport, forecast, *, deadline=0):
        material = await registry._material(forecast)
        raw = bytearray(360)
        raw[:8] = b"FNFORE01"
        raw[8:40] = identity_hash("forecast", forecast.forecast_id)
        raw[40:72] = identity_hash("creator", forecast.creator_id)
        raw[72:104] = bytes.fromhex(forecast.specification_hash)
        struct.pack_into("<qqQq", raw, 104, forecast.specification.open_at_ms,
                         forecast.specification.close_at_ms, forecast.revision, forecast.updated_at_ms)
        raw[136:139] = bytes((material["state"], material["outcome"], 0))
        for offset, name in ((140, "event_hash"), (172, "snapshot_hash"), (204, "resolution_hash"),
                             (236, "dispute_hash"), (268, "reputation_hash"), (300, "trigger_hash")):
            raw[offset:offset+32] = material[name]
        struct.pack_into("<qqqHH", raw, 332, material["challenge_until_ms"], deadline, 0,
                         material["pending_disputes"], material["material_disputes"])
        address, _ = wire.forecast_address(PROGRAM, bytes(raw[8:40]))
        account = RegistryAccount(address, PROGRAM, bytes(raw), 103, True)
        transport.accounts[address] = account
        return account

    async def test_reconstructs_exact_published_and_challenge_history(self):
        forecast = await self.challenge()
        registry, _ = self.registry()
        await registry.backfill(forecast.forecast_id)
        rows = await self.db.all("SELECT * FROM registry_intents ORDER BY revision")
        self.assertEqual([row["revision"] for row in rows], list(range(2, forecast.revision+1)))
        self.assertEqual(rows[-1]["snapshot"], dumps(forecast))
        self.assertEqual(await registry.backfill(forecast.forecast_id), 0)

    async def test_history_missing_receipt_fails_without_partial_backfill(self):
        forecast = await self.challenge()
        registry, _ = self.registry()
        before = await self.db.all("SELECT * FROM registry_intents")
        await self.db.execute("DELETE FROM command_receipts WHERE forecast_id=? AND command_id='publish'",
                              (forecast.forecast_id,))
        with self.assertRaisesRegex(RegistryError, "history_receipt_missing"):
            await registry.backfill(forecast.forecast_id)
        self.assertEqual(before, await self.db.all("SELECT * FROM registry_intents"))

    async def test_intents_immutable_and_event_bound(self):
        value = await self.publish()
        registry, _ = self.registry()
        await registry.enable(value["id"])
        for sql in ("UPDATE registry_intents SET event_hash='bad'", "DELETE FROM registry_intents"):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute(sql)
        snapshot = await self.app._forecast(value["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.batch(registry_intent_sql(snapshot))

    async def test_rpc_acceptance_is_not_confirmation_and_lost_ack_reconciles(self):
        value = await self.publish()
        registry, transport = self.registry()
        await registry.enable(value["id"])
        self.assertEqual((await registry.sync())["confirmed"], 0)
        self.assertEqual((await registry.status(value["id"]))["status"], "pending")
        self.now += 6000
        forecast = await self.app._forecast(value["id"])
        await self.chain_account(registry, transport, forecast)
        self.assertEqual((await registry.sync())["confirmed"], 1)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual((await registry.status(value["id"]))["status"], "confirmed")

    async def test_wrong_cluster_or_relayer_prevents_any_send(self):
        value = await self.publish()
        registry, transport = self.registry()
        await registry.enable(value["id"])
        transport.genesis = "mainnet"
        with self.assertRaisesRegex(RegistryError, "wrong_cluster"):
            await registry.sync()
        transport.genesis = DEVNET_GENESIS
        address, _ = wire.config_address(PROGRAM)
        original = transport.accounts[address]
        transport.accounts[address] = replace(original, data=b"FNCONF01"+ADMIN+bytes([99])*32+bytes(32))
        with self.assertRaisesRegex(RegistryError, "relayer_mismatch"):
            await registry.sync()
        self.assertEqual(transport.sent, [])

    async def test_foreign_owner_and_same_revision_fork_block_acknowledgement(self):
        value = await self.publish()
        registry, transport = self.registry()
        await registry.enable(value["id"])
        forecast = await self.app._forecast(value["id"])
        account = await self.chain_account(registry, transport, forecast)
        transport.accounts[account.address] = replace(account, owner=bytes([90])*32)
        await registry.sync()
        self.assertEqual((await registry.status(value["id"]))["pendingReason"], "chain_account_unverified")
        await self.db.execute("UPDATE registry_delivery SET status='pending'")
        corrupted = bytearray(account.data)
        corrupted[140:172] = bytes([89])*32
        transport.accounts[account.address] = replace(account, data=bytes(corrupted))
        await registry.sync()
        self.assertEqual((await registry.status(value["id"]))["pendingReason"], "chain_commitment_mismatch")
        self.assertEqual(transport.sent, [])

    async def test_finalization_requires_fresh_matching_chain_time_attestation(self):
        forecast = await self.challenge(vote=False)
        registry, transport = self.registry()
        await registry.enable(forecast.forecast_id)
        self.now = forecast.challenge_until_ms+1000
        chain_deadline = self.now+86_400_000
        await self.chain_account(registry, transport, forecast, deadline=chain_deadline)
        transport.chain_time = self.now
        # The chain agrees about the record and refuses only on its own clock, which is a
        # schedule rather than a fault. Saying so precisely is what lets the scheduler wait
        # for the deadline instead of recording an error against a wall time alone moves.
        with self.assertRaises(ChainDeadlineNotReached) as deferred:
            await registry.prepare_finalization(forecast.forecast_id)
        self.assertEqual(deferred.exception.not_before_ms, chain_deadline)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "registry_finalization_pending"):
            await self.app._mutate(forecast, Finalize(), key="registry-finalize-test")
        self.assertEqual((await self.app._forecast(forecast.forecast_id)).revision, forecast.revision)
        self.now = chain_deadline+1000
        transport.chain_time = self.now
        self.assertTrue(await registry.prepare_finalization(forecast.forecast_id))
        self.now += 60_001
        with self.assertRaisesRegex(sqlite3.IntegrityError, "registry_finalization_pending"):
            await self.app._mutate(forecast, Finalize(), key="registry-finalize-test")
        transport.chain_time = self.now
        self.assertTrue(await registry.prepare_finalization(forecast.forecast_id))
        final = await self.app._mutate(forecast, Finalize(), key="registry-finalize-test")
        self.assertEqual(final.state.value, "FINALIZED")
        self.assertNotEqual(await registry._reputation_hash(final), bytes(32))

    async def test_application_finalization_refreshes_time_after_awaited_chain_attestation(self):
        forecast = await self.challenge(vote=False)
        registry, transport = self.registry()
        await registry.enable(forecast.forecast_id)
        self.app.registry = registry
        original_deadline = forecast.challenge_until_ms
        self.now = original_deadline+1000
        scheduler_time = self.now
        await self.chain_account(registry, transport, forecast, deadline=original_deadline)

        async def delayed_chain_clock():
            self.now += 750
            return self.now

        transport.finalized_time_ms = delayed_chain_clock
        final = await self.app._mutate(forecast, Finalize(), key="registry-delayed-finalize", now=scheduler_time)
        self.assertEqual(final.state.value, "FINALIZED")
        self.assertEqual(final.updated_at_ms, scheduler_time+750)
        self.assertEqual(final.challenge_until_ms, original_deadline)
        attestation = await self.db.first("SELECT observed_at FROM registry_forecasts WHERE forecast_id=?",
                                          (forecast.forecast_id,))
        self.assertEqual(final.updated_at_ms, attestation["observed_at"])
        persisted = await self.app._forecast(forecast.forecast_id)
        self.assertEqual(persisted, final)

    async def test_budget_reservations_are_atomic_and_conservative(self):
        results = await asyncio.gather(*(reserve_daily_spend(self.db, 60, self.now, 100)
                                         for _ in range(3)), return_exceptions=True)
        self.assertEqual(sum(value is None for value in results), 1)
        row = await self.db.first("SELECT reserved_lamports FROM registry_spend")
        self.assertEqual(row["reserved_lamports"], 60)
        with self.assertRaisesRegex(RegistryError, "daily_budget_exhausted"):
            await reserve_daily_spend(self.db, 101, self.now, 100)
        await reserve_daily_spend(self.db, 100, self.now+86_400_000, 100)

    async def test_expired_leases_are_recoverable_and_live_leases_are_not_stolen(self):
        value = await self.publish()
        registry, transport = self.registry()
        await registry.enable(value["id"])
        await self.db.execute("UPDATE registry_delivery SET lease_token='other',lease_until=?", (self.now+100,))
        self.assertEqual((await registry.sync())["considered"], 0)
        self.now += 101
        await registry.sync()
        self.assertEqual(len(transport.sent), 1)

    async def test_disabled_forecast_preserves_intents_without_sending(self):
        value = await self.publish()
        registry, transport = self.registry()
        await registry.backfill(value["id"])
        self.assertEqual((await registry.sync())["considered"], 0)
        self.assertEqual((await registry.status(value["id"]))["status"], "disabled")
        self.assertEqual(transport.sent, [])

    async def test_transport_failure_backs_off_without_confirming_or_exposing_response(self):
        value = await self.publish()
        registry, transport = self.registry()
        await registry.enable(value["id"])
        async def fail(*args, **kwargs):
            raise RuntimeError("private upstream credentials must never escape")
        transport.send = fail
        await registry.sync()
        row = await self.db.first("SELECT * FROM registry_delivery")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error_code"], "transport_unavailable")
        self.assertGreater(row["retry_at"], self.now)
        self.assertIsNone(row["confirmed_slot"])

    async def test_program_change_cannot_reuse_prior_deployment_receipts(self):
        value = await self.publish()
        registry, transport = self.registry()
        await registry.enable(value["id"])
        await registry.sync()
        other_program = bytes([45])*32
        other_address, _ = wire.config_address(other_program)
        transport.accounts[other_address] = RegistryAccount(other_address, other_program,
            b"FNCONF01"+ADMIN+RELAYER+bytes(32), 100, True)
        different = SolanaRegistry(self.db, transport, program_id=other_program, relayer=RELAYER,
                                  now_ms=lambda: self.now, random_token=self.random_token)
        with self.assertRaisesRegex(RegistryError, "registry_deployment_mismatch"):
            await different.sync()


    async def test_stale_finalized_rpc_context_retries_without_permanent_block(self):
        forecast = await self.challenge(vote=False)
        registry, transport = self.registry()
        await registry.enable(forecast.forecast_id)
        rows = await self.db.all("SELECT snapshot,revision FROM registry_intents ORDER BY revision")
        await self.chain_account(registry, transport, loads_forecast(rows[0]["snapshot"]))
        await self.db.execute("UPDATE registry_delivery SET status='confirmed',confirmed_slot=105 WHERE revision<=?",
                              (rows[1]["revision"],))
        await registry.sync()
        pending = await self.db.first("SELECT * FROM registry_delivery WHERE revision=?", (rows[2]["revision"],))
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["error_code"], "chain_observation_stale")
        self.assertEqual(transport.sent, [])
        account = await self.chain_account(registry, transport, loads_forecast(rows[1]["snapshot"]))
        transport.accounts[account.address] = replace(account, slot=106)
        self.now += 30_001
        await registry.sync()
        self.assertEqual(len(transport.sent), 1)

    async def test_missing_submission_receipt_blocks_reputation_commitment(self):
        forecast = await self.challenge(vote=True)
        registry, _ = self.registry()
        self.now = forecast.challenge_until_ms+1
        final = await self.app._mutate(forecast, Finalize(), key="reputation-missing-receipt")
        self.assertNotEqual(await registry._reputation_hash(final), bytes(32))
        await self.db.execute("DELETE FROM command_receipts WHERE json_extract(receipt,'$.accepted_user_forecast') IS NOT NULL")
        with self.assertRaisesRegex(RegistryError, "reputation_receipt_missing"):
            await registry._reputation_hash(final)

    async def test_application_hook_enables_publication_and_preserves_each_revision(self):
        registry, _ = self.registry()
        self.app.registry = registry
        value = await self.publish()
        state = await self.db.first("SELECT enabled FROM registry_forecasts WHERE forecast_id=?", (value["id"],))
        self.assertEqual(state["enabled"], 1)
        await self.app.submit_forecast(self.other, value["id"], "YES", 80,
                                       value["revision"], "registry-hook-vote")
        rows = await self.db.all("SELECT revision FROM registry_intents WHERE forecast_id=? ORDER BY revision", (value["id"],))
        self.assertEqual([row["revision"] for row in rows], [2, 3])



class RegistryEarlyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = automation_tests.AutomationIntegrationTests.asyncSetUp
    asyncTearDown = automation_tests.AutomationIntegrationTests.asyncTearDown
    random_token = automation_tests.AutomationIntegrationTests.random_token
    token_hash = staticmethod(automation_tests.AutomationIntegrationTests.token_hash)
    publish = automation_tests.AutomationIntegrationTests.publish
    reviewed_trigger = automation_tests.AutomationIntegrationTests.reviewed_trigger
    hold = automation_tests.AutomationIntegrationTests.hold
    accept = automation_tests.AutomationIntegrationTests.accept
    registry = RegistryTests.registry

    async def test_reconstructs_exact_v1_to_v2_early_history(self):
        trigger = await self.reviewed_trigger()
        await self.hold(trigger)
        await self.accept(trigger)
        await self.app.run_due_jobs()
        current = await self.app._forecast(self.fid)
        self.assertEqual(current.state.value, "CHALLENGE")
        registry, _ = self.registry()
        await registry.backfill(self.fid)
        rows = await self.db.all("SELECT snapshot FROM registry_intents WHERE forecast_id=? ORDER BY revision", (self.fid,))
        self.assertEqual(rows[-1]["snapshot"], dumps(current))
        self.assertIn('"schema_version":1', rows[0]["snapshot"])
        self.assertEqual(len(rows), current.revision-1)
