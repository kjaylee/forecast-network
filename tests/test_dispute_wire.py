from __future__ import annotations

import base64
import hashlib
import json
import struct
import unittest
from dataclasses import replace

from forecast_application import dispute_wire as d
from forecast_application import solana_wire as w


def gate() -> d.Accumulator:
    return d.Accumulator(bytes([7])*32, bytes([3])*32, 1, 1, 3, bytes([6])*32,
                         bytes([8])*32, 30, 100, 0, 0, 0, bytes([9])*32, 1)


def receipt(body: bytes = b"*") -> d.Receipt:
    g = gate()
    return d.Receipt(g.forecast, g.specification, bytes([10])*32, 1, 3, g.resolution,
                     g.proposal_event, bytes([11])*32, bytes([12])*32, d.evidence_hash(body), len(body), body)


def final() -> bytes:
    return w.encode_advance(revision=5, occurred_at_ms=101, previous_event_hash=bytes([4])*32,
        event_hash=bytes([13])*32, snapshot_hash=bytes([14])*32, state=10, outcome=1,
        resolution_hash=bytes([6])*32, reputation_hash=bytes([15])*32, challenge_until_ms=100)


class DisputeWireTests(unittest.TestCase):
    def test_rust_python_golden_commitments(self) -> None:
        self.assertEqual(gate().commitment().hex(), "27194d29a1041f5d1a7e4dec8b6001560b4f269fe5ee413dfc87e9964555ebed")
        self.assertEqual(receipt().commitment().hex(), "bb91319fb3146d0653cf42b84726fa5c51c0b9aa1d30276b01e9c7e85e76db6d")
        self.assertEqual(d.Reviewer(bytes([11])*32, 1).commitment().hex(), "b633e1b6358a9eb49d9758b46e3da9fe2f92cfe1d4db0a400125d3745bd29e01")
        self.assertEqual(d.advance_hash(final()).hex(), "cbc73db439e88aa792bee35cf094ec142913bbe37efbbba6ca120bb75a46e972")

    def test_exact_layout_offsets_and_roundtrip(self) -> None:
        g, r = gate(), receipt()
        self.assertEqual((len(g.encode()), len(r.encode()), len(d.Reviewer(bytes([11])*32, 1).encode())), (392, 425, 48))
        self.assertEqual(d.Accumulator.decode(g.encode()), g)
        self.assertEqual(d.Receipt.decode(r.encode()), r)
        self.assertEqual(g.encode()[104:112], struct.pack("<Q", 1))
        self.assertEqual(g.encode()[264], 1)
        self.assertEqual(r.encode()[312:320], struct.pack("<II", 1, 1))
        self.assertEqual(r.encode()[424:], b"*")
        for original, decoder, offsets in ((g.encode(), d.Accumulator.decode, (0, 72, 264, 265, 272)),
                                            (r.encode(), d.Receipt.decode, (0, 72, 316, 320, 321))):
            for offset in offsets:
                raw = bytearray(original)
                raw[offset] = (raw[offset] + 5) % 256
                with self.assertRaises(ValueError):
                    decoder(bytes(raw))
            for raw in (original[:-1], original + b"x"):
                with self.assertRaises(ValueError):
                    decoder(raw)

    def test_large_body_staging_and_immutable_acceptance(self) -> None:
        complete = receipt(b"*"*d.MAX_BODY)
        for written in (0, 1, 512, 10240, 32767, 32768):
            draft = replace(complete, body=complete.body[:written])
            self.assertEqual(len(draft.encode()), d.RECEIPT_HEADER + written)
            self.assertEqual(d.Receipt.decode(draft.encode()), draft)
            if written < d.MAX_BODY:
                with self.assertRaises(ValueError):
                    d.encode_submit(draft)
        self.assertEqual(len(d.encode_submit(complete)), 73)
        accepted = replace(complete, status=1, accepted_at=100, accepted_slot=1, deadline=100)
        self.assertEqual(d.Receipt.decode(accepted.encode()), accepted)
        for changed in (dict(body=b"x"*d.MAX_BODY), dict(accepted_at=101), dict(review=bytes([1])*32)):
            with self.assertRaises(ValueError):
                replace(accepted, **changed)

    def test_lengths_and_exact_seal_candidate(self) -> None:
        g, r = gate(), receipt()
        self.assertEqual(len(d.encode_draft(g, r.nonce, r.body)), 181)
        self.assertEqual(len(d.encode_append(0, b"*"*512)), 519)
        self.assertEqual(len(d.encode_reviewer(r.user)), 73)
        self.assertEqual(len(d.encode_seal(g, final())), 295)
        with self.assertRaises(ValueError):
            d.encode_advance(final())
        sealed = replace(g, phase=2, sealed_revision=5, sealed_event=bytes([13])*32,
                         sealed_snapshot=bytes([14])*32, sealed_payload=d.advance_hash(final()), sealed_at=101, sealed_slot=1)
        self.assertEqual(len(d.encode_finalize(sealed, final())), 287)
        changed = bytearray(final())
        changed[50] ^= 1
        with self.assertRaises(ValueError):
            d.encode_finalize(sealed, bytes(changed))
        with self.assertRaises(ValueError):
            replace(sealed, sealed_at=100)
        with self.assertRaises(ValueError):
            replace(sealed, pending=1, accepted=1)
        for encoded in (d.encode_draft(g, r.nonce, r.body), d.encode_append(0, b"*"),
                        d.encode_submit(r), d.encode_reviewer(r.user), d.encode_seal(g, final()),
                        d.encode_finalize(sealed, final())):
            self.assertEqual(d.decode_instruction(encoded)["tag"], encoded[0])
            for malformed in (encoded[:-1], encoded+b"x"):
                with self.assertRaises(ValueError):
                    d.decode_instruction(malformed)

    def test_review_scope_and_replay_bounds(self) -> None:
        accepted = replace(receipt(), status=1, accepted_at=100, accepted_slot=1, deadline=100)
        self.assertEqual(len(d.encode_review(accepted, bytes([16])*32, 2)), 74)
        for status in (0, 2, 3):
            r = receipt() if status == 0 else replace(accepted, status=status, review=bytes([16])*32,
                                                     reviewer=bytes([17])*32, reviewed_at=101)
            with self.assertRaises(ValueError):
                d.encode_review(r, bytes([16])*32, 2)
        for n in (True, -1, d.MAX + 1):
            with self.assertRaises(ValueError):
                replace(gate(), epoch=n)

    def test_pda_scope_and_instruction_roles(self) -> None:
        r, g = receipt(), gate()
        address = d.receipt_address(r.program, r.forecast, r.epoch, r.user)[0]
        self.assertNotEqual(address, d.receipt_address(r.program, r.forecast, 2, r.user)[0])
        ix = d.instruction(r.program, d.encode_append(0, b"*"), actor=r.user, receipt=address)
        self.assertEqual([(m.is_signer, m.is_writable) for m in ix.accounts], [(True, True), (False, True), (False, False)])
        accepted = replace(r, status=1, accepted_at=100, accepted_slot=1, deadline=100)
        ix = d.instruction(r.program, d.encode_review(accepted, bytes([16])*32, 2), actor=bytes([17])*32,
                           forecast=g.forecast, receipt=address)
        self.assertEqual(ix.accounts[2].pubkey, d.reviewer_address(r.program)[0])
        self.assertEqual(len(ix.accounts), 6)
        self.assertEqual(d.heap_frame().data, b"\x01\x00\x00\x04\x00")
        with self.assertRaises(ValueError):
            d.instruction(r.program, d.encode_append(0, b"*"), actor=r.user, receipt=r.user)

    def test_retained_evidence_semantics_are_explicit(self) -> None:
        raw = b"retained source bytes"
        value = {"version": "forecast-dispute-evidence-v1", "claim": "Claim", "rule_clause_id": "rule-1",
                 "explanation": "Explanation", "sources": [{"url": "https://example.org/source",
                 "body_base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest(),
                 "captured_at_ms": 1}]}
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(d.decode_evidence(body), value)
        for invalid in (body + b" ", body.replace(b'"Claim"', b'"Claim","claim":"other"'), b"["*2000):
            with self.assertRaises(ValueError):
                d.decode_evidence(invalid)
        value["sources"][0]["sha256"] = "0"*64
        with self.assertRaises(ValueError):
            d.decode_evidence(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


if __name__ == "__main__":
    unittest.main()
