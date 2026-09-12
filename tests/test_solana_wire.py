"""Registry ABI corruption checks and independent official SDK wire vectors."""

import struct
import unittest
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256

from forecast_application import solana_wire as w

A, B, C, D, E = (bytes([n]) * 32 for n in range(1, 6))
ZERO = bytes(32)


def forecast_bytes(**overrides):
    f = dict(forecast_id_hash=A, creator_hash=B, specification_hash=C,
             open_at_ms=1, close_at_ms=100, revision=3, occurred_at_ms=2,
             state=2, outcome=0, paused_from=0, reserved=0, event_hash=D,
             snapshot_hash=E, resolution_hash=ZERO, dispute_hash=ZERO,
             reputation_hash=ZERO, trigger_hash=ZERO, challenge_until_ms=0,
             chain_finalize_not_before_ms=0, paused_at_ms=0, pending_disputes=0,
             material_disputes=0)
    f.update(overrides)
    return (b"FNFORE01" + f["forecast_id_hash"] + f["creator_hash"] + f["specification_hash"]
            + struct.pack("<qqQqBBBB", f["open_at_ms"], f["close_at_ms"], f["revision"],
                          f["occurred_at_ms"], f["state"], f["outcome"], f["paused_from"],
                          f["reserved"])
            + b"".join(f[name] for name in ("event_hash", "snapshot_hash", "resolution_hash",
                                           "dispute_hash", "reputation_hash", "trigger_hash"))
            + struct.pack("<qqqHH", f["challenge_until_ms"], f["chain_finalize_not_before_ms"],
                          f["paused_at_ms"], f["pending_disputes"], f["material_disputes"]))


class Base58AndPdaTests(unittest.TestCase):
    def test_base58_preserves_leading_zeros_and_exact_length(self):
        for raw in (ZERO, A, bytes(range(32)), bytes(range(64)), b"\0" * 31 + b"\1"):
            self.assertEqual(w.base58_decode(w.base58_encode(raw), length=len(raw)), raw)
        self.assertEqual(w.base58_encode(ZERO), "1" * 32)
        for value in ("", "0" * 32, "1" * 31, "1" * 33, " " + "1" * 32,
                      "1" * 32 + "\n", b"1" * 32, "\u00e9" * 32, "z" * 1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                w.base58_decode(value, length=32)
        for raw in (bytearray(32), "text", 5):
            with self.assertRaises(ValueError):
                w.base58_encode(raw)

    def test_curve_decompression_includes_small_order_and_noncanonical_points(self):
        p = 2**255 - 19
        for y in (0, 1, p - 1, p, p + 1):
            for sign in (0, 1 << 255):
                self.assertTrue(w.is_edwards_point((y | sign).to_bytes(32, "little")))
        self.assertFalse(w.is_edwards_point((2).to_bytes(32, "little")))
        for invalid in (b"", bytes(31), bytes(33), bytearray(32)):
            with self.assertRaises(ValueError):
                w.is_edwards_point(invalid)

    def test_pda_vectors_from_official_solana_program_2_2_0(self):
        # Generated independently with Pubkey::find_program_address, including
        # retries through on-curve bumps 255 and 254; no SDK needed at runtime.
        vectors = (
            (0, False, 'bJV4vxUzyGExGeMnUdGrBCnBzqcQqBZbSq5hG2mH7Mt', 255),
            (0, True, 'AQcMsxAUH4DsYWKfWqgSHe12gZ6Hbkh5Qwtomz7GoDmQ', 254),
            (1, False, '13k8oBgVsC9Yyi9MhYeLuQW5LjAdmcXNutSRaVacpQMx', 254),
            (1, True, 'BzXx9v2x8Kvrz2eD6pt1A1zubrPVWrxcAUBsbShEaYuc', 254),
            (7, False, '3vdhRboaxszmoBCWSSq7ZBfEJAEpbYizuwtAJ4vBYz1L', 255),
            (7, True, 'HdWWvm7VubZJteL9WWtUeAhJhEhxemAMmvjXPVk21eJA', 255),
            (255, False, '7LNp4Dkuvz9jRq7bgA61ndLvUxifSqL4bCiMDspFYzwX', 253),
            (255, True, '4HMLSobWMRsm4Xi8qcADbHe18t1v5fvSqVtunvEwFZPH', 254),
        )
        for byte, forecast, address, bump in vectors:
            program = bytes([byte]) * 32
            actual, actual_bump = (w.forecast_address(program, bytes([9]) * 32)
                                   if forecast else w.config_address(program))
            self.assertEqual((w.base58_encode(actual), actual_bump), (address, bump))
            self.assertFalse(w.is_edwards_point(actual))

    def test_pda_seed_and_key_limits(self):
        for seeds in ([b"config"], (bytes(33),), (b"a",) * 16, (bytearray(1),)):
            with self.assertRaises(ValueError):
                w.find_program_address(A, seeds)
        for key in (bytes(31), bytes(33), bytearray(32)):
            with self.assertRaises(ValueError):
                w.config_address(key)
        with self.assertRaises(ValueError):
            w.forecast_address(A, ZERO)


class RegistryAbiTests(unittest.TestCase):
    def test_publication_instruction_exact_offsets(self):
        data = w.encode_register(forecast_id_hash=A, creator_hash=B, specification_hash=C,
                                 open_at_ms=10, close_at_ms=100, revision=3, occurred_at_ms=5,
                                 event_hash=D, snapshot_hash=E)
        self.assertEqual(len(data), 193)
        self.assertEqual(data[:97], b"\1" + A + B + C)
        self.assertEqual(struct.unpack_from("<qqQq", data, 97), (10, 100, 3, 5))
        self.assertEqual(data[129:], D + E)
        args = dict(forecast_id_hash=A, creator_hash=B, specification_hash=C,
                    open_at_ms=10, close_at_ms=100, revision=3, occurred_at_ms=5,
                    event_hash=D, snapshot_hash=E)
        for override in ({"revision": True}, {"revision": 0}, {"revision": 2**53},
                         {"open_at_ms": -1}, {"close_at_ms": 10}, {"occurred_at_ms": 100},
                         {"event_hash": ZERO}, {"snapshot_hash": bytearray(32)},
                         {"creator_hash": bytes(33)}, {"open_at_ms": 1.0}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                w.encode_register(**(args | override))

    def test_advance_exact_offsets_and_replay_rejection(self):
        args = dict(revision=4, occurred_at_ms=100, previous_event_hash=A,
                    event_hash=B, snapshot_hash=C, state=7, outcome=1,
                    resolution_hash=D, dispute_hash=E, pending_disputes=256,
                    challenge_until_ms=200)
        data = w.encode_advance(**args)
        self.assertEqual(len(data), 255)
        self.assertEqual(struct.unpack_from("<Qq", data, 1), (4, 100))
        self.assertEqual(data[17:113], A + B + C)
        self.assertEqual(data[113:115], b"\7\1")
        self.assertEqual(data[115:243], D + E + ZERO + ZERO)
        self.assertEqual(struct.unpack_from("<qHH", data, 243), (200, 256, 0))
        for override in ({"event_hash": A}, {"state": True}, {"outcome": 4},
                         {"state": 1}, {"material_disputes": 1}, {"dispute_hash": ZERO},
                         {"revision": -1}, {"challenge_until_ms": 2**53},
                         {"pending_disputes": -1}, {"resolution_hash": "a" * 32}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                w.encode_advance(**(args | override))

    def test_config_and_admin_instructions(self):
        self.assertEqual(w.encode_initialize(B), b"\0" + B)
        self.assertEqual(w.encode_set_relayer(C), b"\3" + C)
        self.assertEqual(w.encode_propose_admin(C), b"\4" + C)
        self.assertEqual(w.encode_accept_admin(), b"\5")
        config = w.decode_config(b"FNCONF01" + A + B + C)
        self.assertEqual((config.administrator, config.relayer, config.pending_administrator),
                         (A, B, C))
        with self.assertRaises(FrozenInstanceError):
            config.relayer = A
        for data in (b"FNCONF01" + A + A + ZERO, b"FNCONF01" + A + B + A,
                     b"FNCONF01" + ZERO + B + C, b"FNCONF02" + A + B + C,
                     b"FNCONF01" + A + B + C + b"x", b"FNCONF01" + A + B):
            with self.assertRaises(ValueError):
                w.decode_config(data)
        with self.assertRaises(ValueError):
            w.encode_initialize(ZERO)

    def test_forecast_decoder_rejects_structural_corruption(self):
        data = forecast_bytes()
        value = w.decode_forecast(data)
        self.assertEqual(value.revision, 3)
        self.assertEqual(value.specification_hash, C)
        with self.assertRaises(FrozenInstanceError):
            value.state = 10
        for bad in (data[:-1], data + b"x", b"FNFORE02" + data[8:], bytearray(data)):
            with self.assertRaises(ValueError):
                w.decode_forecast(bad)
        for changes in ({"reserved": 1}, {"revision": 0}, {"revision": 2**53},
                        {"state": 1}, {"state": 12}, {"event_hash": ZERO},
                        {"open_at_ms": -1}, {"open_at_ms": 100},
                        {"occurred_at_ms": 100}, {"trigger_hash": A},
                        {"outcome": 1}, {"paused_at_ms": 1}, {"paused_from": 4},
                        {"reputation_hash": A}, {"challenge_until_ms": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                w.decode_forecast(forecast_bytes(**changes))

    def test_forecast_semantic_challenge_pause_and_final_guards(self):
        challenge = dict(state=6, outcome=1, resolution_hash=A, occurred_at_ms=100,
                         challenge_until_ms=200, chain_finalize_not_before_ms=300)
        self.assertEqual(w.decode_forecast(forecast_bytes(**challenge)).state, 6)
        final = challenge | dict(state=10, occurred_at_ms=200, reputation_hash=B)
        self.assertEqual(w.decode_forecast(forecast_bytes(**final)).outcome, 1)
        pause = challenge | dict(state=9, paused_from=6, paused_at_ms=150)
        self.assertEqual(w.decode_forecast(forecast_bytes(**pause)).paused_from, 6)
        for changes in ({"pending_disputes": 1}, {"material_disputes": 1},
                        {"resolution_hash": ZERO}, {"outcome": 0},
                        {"chain_finalize_not_before_ms": 199}, {"challenge_until_ms": 0},
                        {"reputation_hash": B}, {"occurred_at_ms": 50},
                        {"state": 10, "occurred_at_ms": 199}, {"state": 8},
                        {"state": 9}, {"state": 9, "paused_from": 2, "paused_at_ms": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                w.decode_forecast(forecast_bytes(**(challenge | changes)))
        disputed = challenge | dict(state=7, dispute_hash=B, pending_disputes=256)
        self.assertEqual(w.decode_forecast(forecast_bytes(**disputed)).pending_disputes, 256)
        for changes in ({"material_disputes": 1}, {"dispute_hash": ZERO}):
            with self.assertRaises(ValueError):
                w.decode_forecast(forecast_bytes(**(disputed | changes)))
        early = challenge | dict(occurred_at_ms=50, trigger_hash=B)
        self.assertEqual(w.decode_forecast(forecast_bytes(**early)).outcome, 1)
        with self.assertRaises(ValueError):
            w.decode_forecast(forecast_bytes(**(early | {"outcome": 2})))


class LegacyTransactionTests(unittest.TestCase):
    def test_official_sdk_multisigner_legacy_message_vector(self):
        # Solana Message::new_with_blockhash().serialize(), solana-program 2.2.0.
        # Covers raw key ordering, privilege promotion, multiple instructions,
        # readonly signer and compact-u16 instruction length crossing 127.
        def key(n):
            return bytes([n]) * 32
        instructions = (
            w.Instruction(key(9), (w.AccountMeta(key(3), True),
                                   w.AccountMeta(key(4), False, True),
                                   w.AccountMeta(key(5))), bytes([1, 2, 3])),
            w.Instruction(key(8), (w.AccountMeta(key(3), False, True),
                                   w.AccountMeta(key(4)),
                                   w.AccountMeta(key(6), True)), bytes([42]) * 130),
        )
        result = w.compile_message(key(7), key(11), instructions)
        self.assertEqual(len(result.data), 407)
        self.assertEqual(sha256(result.data).hexdigest(),
                         "b6ed07ba935662bd85d2fe03df6f20ad34e901d91c6921c3b1f499b15aaa34ca")
        self.assertEqual(result.signer_keys, (key(7), key(3), key(6)))

    def test_shortvec_canonical_boundaries(self):
        for number, expected in ((0, b"\0"), (127, b"\x7f"), (128, b"\x80\1"),
                                 (16383, b"\xff\x7f"), (65535, b"\xff\xff\3")):
            self.assertEqual(w.shortvec(number), expected)
        for value in (-1, 65536, True, 1.0):
            with self.assertRaises(ValueError):
                w.shortvec(value)

    def test_privileges_merge_and_signatures_order_is_message_order(self):
        instructions = (w.Instruction(E, (w.AccountMeta(B, True), w.AccountMeta(C)), b"hello"),
                        w.Instruction(D, (w.AccountMeta(B, False, True),), b"world"))
        message = w.compile_message(A, D, instructions)
        self.assertEqual(message.signer_keys, (A, B))
        self.assertEqual(message.account_keys, (A, B, C, D, E))
        self.assertEqual(message.data[:4], bytes([2, 0, 3, 5]))
        wire = w.assemble_transaction(message, {B: b"b" * 64, A: b"a" * 64})
        self.assertEqual(wire, b"\2" + b"a" * 64 + b"b" * 64 + message.data)
        for signatures in ({A: b"a" * 64}, {A: b"a" * 64, B: bytes(64)},
                           {A: b"a" * 63, B: b"b" * 64},
                           {A: b"a" * 64, B: b"b" * 64, C: b"c" * 64}):
            with self.assertRaises(ValueError):
                w.assemble_transaction(message, signatures)
        for corrupt in (replace(message, signer_keys=(B, A)),
                        replace(message, data=b"\1" + message.data[1:]),
                        replace(message, account_keys=(A, B, D, C, E))):
            with self.assertRaises(ValueError):
                w.assemble_transaction(corrupt, {A: b"a" * 64, B: b"b" * 64})

    def test_packet_boundary_is_enforced_with_signature_overhead(self):
        message = w.compile_message(A, D, (w.Instruction(B, (), bytes(1062)),))
        self.assertEqual(len(w.assemble_transaction(message, {A: b"s" * 64})), 1232)
        with self.assertRaises(ValueError):
            w.compile_message(A, D, (w.Instruction(B, (), bytes(1063)),))

    def test_size_and_type_limits_fail_before_signing(self):
        for args in ((bytes(31), D, (w.Instruction(B, (), b""),)),
                     (A, ZERO, (w.Instruction(B, (), b""),)), (A, D, ()),
                     (A, D, [w.Instruction(B, (), b"")]),
                     (A, D, (w.Instruction(B, (), bytes(1232)),))):
            with self.assertRaises(ValueError):
                w.compile_message(*args)
        with self.assertRaises(ValueError):
            w.AccountMeta(A, 1, False)
        with self.assertRaises(ValueError):
            w.Instruction(B, [], b"")


if __name__ == "__main__":
    unittest.main()
