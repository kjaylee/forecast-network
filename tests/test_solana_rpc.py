"""Fail-closed RPC contract tests; real cryptographic/SBF integration is separate."""

from __future__ import annotations

import base64
import struct
import unittest
from typing import Any

from forecast_application.solana_rpc import SolanaRpcError, SolanaRpcTransport
from forecast_application.solana_wire import (
    base58_encode,
    config_address,
    encode_advance,
    encode_register,
    forecast_address,
)

PROGRAM, RELAYER, ADMIN = bytes([10]) * 32, bytes([20]) * 32, bytes([30]) * 32
IDENTITY, CREATOR, SPEC = bytes([1]) * 32, bytes([2]) * 32, bytes([3]) * 32
EVENT, SNAPSHOT = bytes([4]) * 32, bytes([5]) * 32
GENESIS = base58_encode(bytes([9]) * 32)
SIGNATURE = bytes([6]) * 64
CONFIG = config_address(PROGRAM)[0]
TARGET = forecast_address(PROGRAM, IDENTITY)[0]
CONFIG_DATA = b"FNCONF01" + ADMIN + RELAYER + bytes(32)
CLOCK_ADDRESS = "SysvarC1ock11111111111111111111111111111111"
CLOCK_OWNER = "Sysvar1111111111111111111111111111111111111"
CLOCK_DATA = struct.pack("<QqQQq", 123, 1_799_000_000, 4, 5, 1_800_000_000)
PUBLICATION = encode_register(
    forecast_id_hash=IDENTITY, creator_hash=CREATOR, specification_hash=SPEC,
    open_at_ms=1000, close_at_ms=5000, revision=3, occurred_at_ms=1000,
    event_hash=EVENT, snapshot_hash=SNAPSHOT)
FORECAST_DATA = (
    b"FNFORE01" + IDENTITY + CREATOR + SPEC + struct.pack("<qqQq", 1000, 5000, 3, 1000)
    + bytes([2, 0, 0, 0]) + EVENT + SNAPSHOT + bytes(128) + struct.pack("<qqqHH", 0, 0, 0, 0, 0))


def context(value: Any) -> dict[str, Any]:
    return {"context": {"slot": 123}, "value": value}


def account(data: bytes, owner: bytes = PROGRAM) -> dict[str, Any]:
    return {"owner": base58_encode(owner), "lamports": 4_000_000, "executable": False,
            "data": [base64.b64encode(data).decode(), "base64"]}


class RpcFixture:
    def __init__(self):
        self.calls = []
        self.signed = []
        self.reservations = []
        self.overrides = {}
        self.account_overrides = {}
        self.signature = SIGNATURE
        self.reject_spend = False

    async def rpc(self, method, params):
        self.calls.append((method, params))
        if method in self.overrides:
            value = self.overrides[method]
            if isinstance(value, Exception):
                raise value
            return value
        if method == "getAccountInfo":
            if params[0] in self.account_overrides:
                return context(self.account_overrides[params[0]])
            values = {
                base58_encode(PROGRAM): {"executable": True,
                    "owner": "BPFLoaderUpgradeab1e11111111111111111111111"},
                base58_encode(CONFIG): account(CONFIG_DATA), base58_encode(TARGET): None,
                CLOCK_ADDRESS: {**account(CLOCK_DATA), "owner": CLOCK_OWNER},
            }
            return context(values.get(params[0]))
        return {
            "getGenesisHash": GENESIS,
            "getLatestBlockhash": context({"blockhash": base58_encode(bytes([8]) * 32),
                                           "lastValidBlockHeight": 900}),
            "getFeeForMessage": context(5000),
            "getMinimumBalanceForRentExemption": 3_396_480,
            "getBalance": context(50_000_000),
            "simulateTransaction": context({"err": None, "logs": []}),
            "isBlockhashValid": context(True),
            "getBlockHeight": 850,
            "sendTransaction": base58_encode(SIGNATURE),
            "getSignatureStatuses": context([None]),
            "getSlot": 123,
            "getBlockTime": 1_800_000_000,
        }[method]

    async def sign(self, message):
        self.signed.append(message)
        return self.signature

    async def reserve(self, amount):
        self.reservations.append(amount)
        if self.reject_spend:
            raise ValueError("daily spend limit")

    def transport(self):
        return SolanaRpcTransport(self.rpc, self.sign, program_id=PROGRAM, relayer=RELAYER,
                                  expected_genesis_hash=GENESIS, authorize_spend=self.reserve)

    def methods(self):
        return [method for method, _ in self.calls]


class SolanaRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_publication_reserves_exact_cost_and_simulates_the_sent_bytes(self):
        rpc = RpcFixture()
        signature = await rpc.transport().send(PUBLICATION, TARGET, register=True)
        self.assertEqual(signature, base58_encode(SIGNATURE))
        self.assertEqual(rpc.reservations, [3_401_480])
        self.assertEqual(len(rpc.signed), 1)
        calls = dict(rpc.calls)
        simulated, options = calls["simulateTransaction"]
        self.assertTrue(options["sigVerify"])
        self.assertFalse(options["replaceRecentBlockhash"])
        self.assertEqual(calls["sendTransaction"][0], simulated)
        self.assertFalse(calls["sendTransaction"][1]["skipPreflight"])
        self.assertEqual(options["commitment"], "confirmed")
        self.assertEqual(calls["sendTransaction"][1]["preflightCommitment"],
                         options["commitment"])
        self.assertEqual(base64.b64decode(simulated), b"\1" + SIGNATURE + rpc.signed[0])
        self.assertNotIn("getSignatureStatuses", rpc.methods())

    async def test_wrong_cluster_and_rpc_outage_never_sign(self):
        for failure in (base58_encode(bytes([7]) * 32), {"result": GENESIS},
                        RuntimeError("private provider token must not escape")):
            with self.subTest(failure=type(failure).__name__):
                rpc = RpcFixture()
                rpc.overrides["getGenesisHash"] = failure
                with self.assertRaises(ValueError) as caught:
                    await rpc.transport().send(PUBLICATION, TARGET, register=True)
                self.assertNotIn("private provider token", str(caught.exception))
                self.assertEqual(rpc.signed, [])
                self.assertEqual(rpc.reservations, [])

    async def test_instruction_tampering_and_administrative_operations_never_sign(self):
        for instruction, target, register in (
            (PUBLICATION + b"\0", TARGET, True), (PUBLICATION[:-1], TARGET, True),
            (b"\3" + RELAYER, TARGET, False), (b"\0" + RELAYER, TARGET, True),
            (PUBLICATION, forecast_address(PROGRAM, SPEC)[0], True),
            (PUBLICATION, TARGET, False), (PUBLICATION, TARGET, 1),
        ):
            with self.subTest(size=len(instruction), register=register):
                rpc = RpcFixture()
                with self.assertRaises(ValueError):
                    await rpc.transport().send(instruction, target, register=register)
                self.assertEqual(rpc.calls, [])
                self.assertEqual(rpc.signed, [])

    async def test_program_config_and_pda_ownership_fail_closed(self):
        cases = (
            (PROGRAM, {"executable": False, "owner": base58_encode(PROGRAM)}),
            (PROGRAM, {"executable": True, "owner": base58_encode(PROGRAM)}),
            (CONFIG, None), (CONFIG, account(CONFIG_DATA, ADMIN)),
            (CONFIG, account(b"FNCONF01" + ADMIN + SPEC + bytes(32))),
            (CONFIG, account(CONFIG_DATA + bytes(1))),
            (TARGET, account(FORECAST_DATA)),
        )
        for address, value in cases:
            with self.subTest(address=address[:1], value=type(value).__name__):
                rpc = RpcFixture()
                rpc.account_overrides[base58_encode(address)] = value
                with self.assertRaises(ValueError):
                    await rpc.transport().send(PUBLICATION, TARGET, register=True)
                self.assertEqual(rpc.signed, [])

    async def test_fee_rent_balance_and_daily_limits_prevent_signing(self):
        for method, value in (
            ("getFeeForMessage", context(20_001)), ("getFeeForMessage", context(None)),
            ("getFeeForMessage", context(True)), ("getFeeForMessage", context(0)),
            ("getMinimumBalanceForRentExemption", 10_000_001),
            ("getMinimumBalanceForRentExemption", 0),
            ("getBalance", context(13_401_479)), ("getBalance", context("50000000")),
        ):
            with self.subTest(method=method, value=value):
                rpc = RpcFixture()
                rpc.overrides[method] = value
                with self.assertRaises(ValueError):
                    await rpc.transport().send(PUBLICATION, TARGET, register=True)
                self.assertEqual(rpc.signed, [])
                self.assertEqual(rpc.reservations, [])
        rpc = RpcFixture()
        rpc.reject_spend = True
        with self.assertRaisesRegex(ValueError, "daily spend"):
            await rpc.transport().send(PUBLICATION, TARGET, register=True)
        self.assertEqual(rpc.signed, [])

    async def test_bad_signature_bytes_never_reach_rpc_submission(self):
        for signature in (b"bad", bytes(64), bytearray(SIGNATURE), SIGNATURE + b"\0"):
            rpc = RpcFixture()
            rpc.signature = signature
            with self.assertRaises(ValueError):
                await rpc.transport().send(PUBLICATION, TARGET, register=True)
            self.assertNotIn("simulateTransaction", rpc.methods())
            self.assertNotIn("sendTransaction", rpc.methods())

    async def test_signature_verification_failure_and_simulation_outage_never_send(self):
        for value in (context({"err": "SignatureFailure"}), context({"logs": []}),
                      context({"err": {"InstructionError": [0, "Custom"]}}),
                      RuntimeError("RPC timeout")):
            rpc = RpcFixture()
            rpc.overrides["simulateTransaction"] = value
            with self.assertRaises(ValueError):
                await rpc.transport().send(PUBLICATION, TARGET, register=True)
            self.assertNotIn("sendTransaction", rpc.methods())
            self.assertEqual(len(rpc.reservations), 1)

    async def test_stale_contexts_cannot_authorize_signing_or_submission(self):
        for method, value in (("getLatestBlockhash", {
            "blockhash": base58_encode(bytes([8]) * 32), "lastValidBlockHeight": 900}),
            ("getFeeForMessage", 5000), ("getBalance", 50_000_000),
            ("isBlockhashValid", True), ("simulateTransaction", {"err": None})):
            rpc = RpcFixture()
            rpc.overrides[method] = {"context": {"slot": 122}, "value": value}
            with self.assertRaises(ValueError):
                await rpc.transport().send(PUBLICATION, TARGET, register=True)
            self.assertNotIn("sendTransaction", rpc.methods())
            if method != "simulateTransaction":
                self.assertEqual(rpc.signed, [])

    async def test_expired_or_invalid_blockhash_prevents_submission(self):
        for method, value in (("isBlockhashValid", context(False)),
                              ("isBlockhashValid", context(1)),
                              ("getBlockHeight", 901), ("getBlockHeight", -1)):
            rpc = RpcFixture()
            rpc.overrides[method] = value
            with self.assertRaises(ValueError):
                await rpc.transport().send(PUBLICATION, TARGET, register=True)
            self.assertNotIn("sendTransaction", rpc.methods())
            self.assertEqual(rpc.signed, [])

    async def test_submission_wrong_signature_or_lost_ack_is_never_success(self):
        for value in (base58_encode(bytes([11]) * 64), None, RuntimeError("lost ack")):
            rpc = RpcFixture()
            rpc.overrides["sendTransaction"] = value
            with self.assertRaises(ValueError):
                await rpc.transport().send(PUBLICATION, TARGET, register=True)
            self.assertEqual(rpc.methods().count("sendTransaction"), 1)
            self.assertEqual(len(rpc.reservations), 1)

    async def test_advance_requires_finalized_predecessor_and_needs_no_rent(self):
        data = encode_advance(revision=4, occurred_at_ms=5000, previous_event_hash=EVENT,
                              event_hash=SPEC, snapshot_hash=CREATOR, state=3, outcome=0)
        rpc = RpcFixture()
        rpc.account_overrides[base58_encode(TARGET)] = account(FORECAST_DATA)
        await rpc.transport().send(data, TARGET, register=False)
        self.assertEqual(rpc.reservations, [5000])
        self.assertNotIn("getMinimumBalanceForRentExemption", rpc.methods())
        for revision, predecessor in ((3, EVENT), (5, EVENT), (4, SNAPSHOT)):
            rpc = RpcFixture()
            rpc.account_overrides[base58_encode(TARGET)] = account(FORECAST_DATA)
            bad = encode_advance(revision=revision, occurred_at_ms=5000,
                                 previous_event_hash=predecessor, event_hash=SPEC,
                                 snapshot_hash=CREATOR, state=3, outcome=0)
            with self.assertRaises(ValueError):
                await rpc.transport().send(bad, TARGET, register=False)
            self.assertEqual(rpc.signed, [])

    async def test_account_data_is_finalized_bounded_and_bound_to_its_pda(self):
        rpc = RpcFixture()
        rpc.account_overrides[base58_encode(TARGET)] = account(FORECAST_DATA)
        result = await rpc.transport().account(TARGET)
        self.assertEqual((result.address, result.owner, result.data, result.slot, result.finalized),
                         (TARGET, PROGRAM, FORECAST_DATA, 123, True))
        self.assertEqual(rpc.calls[-1][1][1]["commitment"], "finalized")
        for data in (["!", "base64"], ["A" * 481, "base64"],
                     [base64.b64encode(FORECAST_DATA).decode(), "base64+zstd"],
                     [base64.b64encode(FORECAST_DATA.replace(IDENTITY, SPEC, 1)).decode(), "base64"]):
            rpc = RpcFixture()
            value = account(FORECAST_DATA)
            value["data"] = data
            rpc.account_overrides[base58_encode(TARGET)] = value
            with self.assertRaises(ValueError):
                await rpc.transport().account(TARGET)

    async def test_only_successful_finalized_status_acknowledges_a_transaction(self):
        for status, expected in (
            (None, False),
            ({"err": None, "slot": 10, "confirmationStatus": "processed"}, False),
            ({"err": None, "slot": 10, "confirmationStatus": "confirmed"}, False),
            ({"err": None, "slot": 10, "confirmationStatus": "finalized",
              "confirmations": None}, True),
        ):
            rpc = RpcFixture()
            rpc.overrides["getSignatureStatuses"] = context([status])
            self.assertIs(await rpc.transport().signature_finalized(base58_encode(SIGNATURE)),
                          expected)
        for status in ({"err": "failure", "confirmationStatus": "finalized", "slot": 10},
                       {"err": None, "confirmationStatus": "finalized", "slot": 10},
                       {"err": None, "confirmationStatus": "finalized", "slot": 10,
                        "confirmations": 1},
                       {"err": None, "confirmationStatus": "bogus", "slot": 10}):
            rpc = RpcFixture()
            rpc.overrides["getSignatureStatuses"] = context([status])
            with self.assertRaises(ValueError):
                await rpc.transport().signature_finalized(base58_encode(SIGNATURE))

    async def test_prefunded_empty_system_pda_remains_uninitialized(self):
        for lamports in (0, 1, 4_000_000, 50_000_000):
            rpc = RpcFixture()
            rpc.account_overrides[base58_encode(TARGET)] = {
                **account(b"", bytes(32)), "lamports": lamports}
            self.assertIsNone(await rpc.transport().account(TARGET))
            self.assertEqual(rpc.signed, [])

    async def test_prefunded_publication_is_signed_with_full_conservative_rent_reserve(self):
        for lamports in (1, 4_000_000, 50_000_000):
            rpc = RpcFixture()
            rpc.account_overrides[base58_encode(TARGET)] = {
                **account(b"", bytes(32)), "lamports": lamports}
            self.assertEqual(await rpc.transport().send(PUBLICATION, TARGET, register=True),
                             base58_encode(SIGNATURE))
            # Funding is unsolicited, never refunded or trusted to reduce the
            # worst-case cost reservation before signing.
            self.assertEqual(rpc.reservations, [3_401_480])
            self.assertEqual(len(rpc.signed), 1)

    async def test_prefunding_exception_rejects_nonempty_foreign_and_executable_accounts(self):
        for value in (account(b"\0", bytes(32)), account(FORECAST_DATA, bytes(32)),
                      account(b"", ADMIN), account(b"", PROGRAM),
                      {**account(b"", bytes(32)), "executable": True},
                      {**account(b"", bytes(32)), "lamports": True},
                      {**account(b"", bytes(32)), "data": ["", "base64+zstd"]},
                      {**account(b"", bytes(32)), "data": ["\n", "base64"]}):
            rpc = RpcFixture()
            rpc.account_overrides[base58_encode(TARGET)] = value
            with self.assertRaises(ValueError):
                await rpc.transport().account(TARGET)
            with self.assertRaises(ValueError):
                await rpc.transport().send(PUBLICATION, TARGET, register=True)
            self.assertEqual(rpc.signed, [])

    async def test_prefunding_does_not_allow_advancing_an_uninitialized_pda(self):
        rpc = RpcFixture()
        rpc.account_overrides[base58_encode(TARGET)] = account(b"", bytes(32))
        instruction = encode_advance(revision=4, occurred_at_ms=5000,
            previous_event_hash=EVENT, event_hash=SPEC, snapshot_hash=CREATOR, state=3, outcome=0)
        with self.assertRaises(ValueError):
            await rpc.transport().send(instruction, TARGET, register=False)
        self.assertEqual(rpc.signed, [])
        self.assertEqual(rpc.reservations, [])

    async def test_finalized_time_reads_exact_finalized_clock_sysvar(self):
        rpc = RpcFixture()
        self.assertEqual(await rpc.transport().finalized_time_ms(), 1_800_000_000_000)
        self.assertEqual(rpc.calls[-1], ("getAccountInfo", [CLOCK_ADDRESS, {
            "encoding": "base64", "commitment": "finalized"}]))
        self.assertNotIn("getBlockTime", rpc.methods())
        self.assertNotIn("getSlot", rpc.methods())

    async def test_block_time_past_deadline_cannot_override_clock_before_deadline(self):
        rpc = RpcFixture()
        deadline_ms = 1_800_000_000_000
        rpc.overrides["getBlockTime"] = 1_800_000_001
        data = struct.pack("<QqQQq", 123, 1_799_000_000, 4, 5, 1_799_999_999)
        rpc.account_overrides[CLOCK_ADDRESS] = {**account(data), "owner": CLOCK_OWNER}
        observed = await rpc.transport().finalized_time_ms()
        self.assertEqual(observed, deadline_ms - 1000)
        self.assertLess(observed, deadline_ms)
        self.assertNotIn("getBlockTime", rpc.methods())

    async def test_malformed_or_substituted_clock_sysvars_fail_closed(self):
        valid = {**account(CLOCK_DATA), "owner": CLOCK_OWNER}
        cases = [None, {**valid, "owner": base58_encode(PROGRAM)},
                 {**valid, "executable": True}, {**valid, "lamports": False},
                 {**valid, "lamports": 0},
                 {**valid, "data": ["!" * 56, "base64"]},
                 {**valid, "data": [base64.b64encode(CLOCK_DATA).decode(), "base64+zstd"]}]
        for data in (CLOCK_DATA[:-1], CLOCK_DATA + b"\0",
                     struct.pack("<QqQQq", 122, 1_799_000_000, 4, 5, 1_800_000_000),
                     struct.pack("<QqQQq", 124, 1_799_000_000, 4, 5, 1_800_000_000),
                     struct.pack("<QqQQq", 123, 0, 0, 0, -1),
                     struct.pack("<QqQQq", 123, 0, 0, 0, 9_007_199_254_740_991)):
            cases.append({**account(data), "owner": CLOCK_OWNER})
        for invalid in cases:
            rpc = RpcFixture()
            rpc.account_overrides[CLOCK_ADDRESS] = invalid
            with self.assertRaises(ValueError):
                await rpc.transport().finalized_time_ms()

    async def test_malformed_contexts_do_not_become_authoritative_observations(self):
        for response in ({"value": None}, {"context": {"slot": True}, "value": None},
                         {"context": {"slot": -1}, "value": None}):
            rpc = RpcFixture()
            rpc.overrides["getAccountInfo"] = response
            with self.assertRaises(SolanaRpcError):
                await rpc.transport().account(TARGET)
