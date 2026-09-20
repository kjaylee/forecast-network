#!/usr/bin/env python3
"""Export the Devnet memo attestation service, so a Rust port can be held to it.

The Worker cannot reach public Devnet RPC, so the *phone* does the network work: it fetches a
blockhash, asks this service for a relayer-fee-paid transaction, has the Seed Vault wallet co-sign
it, submits it, and reports the signature. So the transaction this returns is deliberately
incomplete, and the zeroed signature slot is part of the contract rather than a placeholder.

Four things the vector pins:

  * The memo names the forecast and the hash of the *retained* submission, so the on-chain
    statement is about the receipt that exists rather than about anything re-sent.
  * The rate limit is taken before the receipt is read. A stamp is a chain write the relayer pays
    for, so the cost is bounded whether or not the request turns out to be valid.
  * `confirm` is idempotent for the same signature and a conflict for a different one: an
    attestation is a statement about one transaction, and reporting a second one against it would
    make the memo a claim about whichever landed.
  * `status` with no user is `None` — nothing to report — while a user with nothing stamped is
    `{"status": "none"}`. A port that collapsed those would tell the caller an anonymous request
    had been stamped.

The relayer key and the signing function are deterministic and synthetic: this module only carries
the relayer's signature through, and a signer whose output depends on its input is a stronger check
on the compiled message than a real key would be, without the `cryptography` dependency.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.attestation import Attestations  # noqa: E402
from forecast_application.database import SQLiteDatabase  # noqa: E402
from forecast_application.errors import AppError  # noqa: E402
from forecast_application.solana_wire import base58_encode  # noqa: E402

GOLDEN = ROOT / "tests/golden/attestation-golden.json"
NOW = 1_800_000_000_000
RELAYER = bytes(range(100, 132))
WALLET = bytes(range(200, 232))
# A real, decodable blockhash: base58 of 32 bytes.
BLOCKHASH = base58_encode(bytes(range(50, 82)))
SIGNATURE = base58_encode(bytes(range(1, 65)))
OTHER_SIGNATURE = base58_encode(bytes(range(2, 66)))

TABLES = ["forecast_attestations"]


def sign(message: bytes) -> bytes:
    """Sixty-four bytes that depend on every byte of the message, so the recorded transaction is a
    check on the compiled message rather than on a constant."""
    return hashlib.sha256(message).digest() + hashlib.sha256(b"second:" + message).digest()


class Fixture:
    def __init__(self, *, available: bool = True) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now, self.counter, self.limited = NOW, 0, []
        self.attestations = Attestations(
            self.db, relayer=RELAYER if available else None,
            sign=self.sign if available else None, now_ms=self.now_ms,
            random_token=self.token, rate_limit=self.rate_limit)
        self.calls: list[dict] = []

    def now_ms(self) -> int:
        return self.now

    def token(self) -> str:
        self.counter += 1
        return f"attestationtoken{self.counter}".ljust(32, "0")

    async def sign(self, message: bytes) -> bytes:
        return sign(message)

    async def rate_limit(self, scope: str, limit: int, window_ms: int) -> None:
        self.limited.append({"scope": scope, "limit": limit, "windowMs": window_ms})

    async def call(self, name: str, awaitable, **inputs) -> object:
        entry: dict = {"call": name, "input": inputs}
        try:
            result = await awaitable
        except AppError as error:
            entry["error"] = {"status": error.status, "code": error.code, "message": error.message}
            self.calls.append(entry)
            return None
        entry["result"] = result
        self.calls.append(entry)
        return result

    async def rows(self) -> dict:
        return {table: [dict(row) for row in await self.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in TABLES}


async def seeded(fixture: Fixture) -> None:
    """One forecaster with a wallet and a retained submission, and one without each."""
    for user in ("u-wallet", "u-nowallet", "u-noreceipt", "u-receipt-nowallet"):
        await fixture.db.execute(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
            (user, user, user, f"recovery:{user}", NOW - 1000))
    await fixture.db.execute(
        "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
        "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) "
        "VALUES('f-att','u-wallet','d-att','{}',1,'OPEN','CRYPTO','t','q','q',?,0,?,1,1,'k')",
        ("a" * 64, NOW + 86_400_000))
    await fixture.db.execute(
        "INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,"
        "revision,body) VALUES('f-att','u-wallet','YES',80,80,?,1,'{\"outcome\":\"YES\"}')", (NOW - 500,))
    # The wallet-less user has a receipt, which is what makes the *wallet* check the one that
    # refuses: a user with neither would never reach it.
    await fixture.db.execute(
        "INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,"
        "revision,body) VALUES('f-att','u-receipt-nowallet','YES',80,80,?,1,'{\"outcome\":\"YES\"}')",
        (NOW - 400,))
    await fixture.db.execute(
        "INSERT INTO wallet_identities(address,user_id,status,created_at) VALUES(?,?,'active',?)",
        (base58_encode(WALLET), "u-wallet", NOW - 500))


async def build() -> dict:
    fixture = Fixture()
    await seeded(fixture)
    attestations = fixture.attestations

    # --- prepare: everything that has to be true before the relayer signs anything.
    await fixture.call("prepare:no-blockhash", attestations.prepare("u-wallet", "f-att", {}))
    await fixture.call("prepare:blockhash-not-a-string", attestations.prepare("u-wallet", "f-att", {"blockhash": 7}))
    await fixture.call("prepare:blockhash-not-base58", attestations.prepare("u-wallet", "f-att", {"blockhash": "0OIl"}))
    await fixture.call("prepare:no-receipt", attestations.prepare("u-noreceipt", "f-att", {"blockhash": BLOCKHASH}))
    await fixture.call("prepare:no-wallet", attestations.prepare("u-receipt-nowallet", "f-att", {"blockhash": BLOCKHASH}))
    first = await fixture.call("prepare:ok", attestations.prepare("u-wallet", "f-att", {"blockhash": BLOCKHASH}))
    assert first is not None

    # --- confirm: idempotent for the same signature, a conflict for a different one.
    await fixture.call("confirm:not-a-signature", attestations.confirm("u-wallet", "f-att", {
        "attestationId": first["attestationId"], "signature": "too-short"}))
    await fixture.call("confirm:unknown", attestations.confirm("u-wallet", "f-att", {
        "attestationId": "at_unknown", "signature": SIGNATURE}))
    await fixture.call("confirm:negative-slot", attestations.confirm("u-wallet", "f-att", {
        "attestationId": first["attestationId"], "signature": SIGNATURE, "slot": -1}))
    await fixture.call("confirm:ok", attestations.confirm("u-wallet", "f-att", {
        "attestationId": first["attestationId"], "signature": SIGNATURE, "slot": 12345}))
    # The device reporting again is the same statement, not a second one.
    await fixture.call("confirm:again", attestations.confirm("u-wallet", "f-att", {
        "attestationId": first["attestationId"], "signature": SIGNATURE, "slot": 12345}))
    await fixture.call("confirm:different-signature", attestations.confirm("u-wallet", "f-att", {
        "attestationId": first["attestationId"], "signature": OTHER_SIGNATURE, "slot": 12346}))

    # --- status: no user is nothing to report; a user with nothing stamped says so.
    await fixture.call("status:anonymous", attestations.status(None, "f-att"))
    await fixture.call("status:stamped", attestations.status("u-wallet", "f-att"))
    await fixture.call("status:unstamped", attestations.status("u-nowallet", "f-att"))

    # --- unavailable: a service with no relayer refuses rather than building a transaction
    # nobody can pay for.
    unavailable = Fixture(available=False)
    await seeded(unavailable)
    await unavailable.call("unavailable:prepare", unavailable.attestations.prepare(
        "u-wallet", "f-att", {"blockhash": BLOCKHASH}))
    await unavailable.call("unavailable:status", unavailable.attestations.status("u-wallet", "f-att"))

    return {
        "description": "The Devnet memo attestation service: the relayer-paid transaction with the "
                       "wallet's signature slot zeroed, the idempotent confirm, and the status "
                       "shapes.",
        "now": NOW,
        "relayer": base58_encode(RELAYER),
        "wallet": base58_encode(WALLET),
        "blockhash": BLOCKHASH,
        "rateLimited": fixture.limited,
        "calls": fixture.calls,
        "rows": await fixture.rows(),
        "unavailableCalls": unavailable.calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(asyncio.run(build()), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if arguments.write:
        GOLDEN.write_text(document)
        print(f"wrote {GOLDEN.relative_to(ROOT)}")
        return 0
    if arguments.check:
        current = GOLDEN.read_text() if GOLDEN.exists() else ""
        if current != document:
            print(f"{GOLDEN.relative_to(ROOT)} is stale; regenerate with --write", file=sys.stderr)
            return 1
        print(f"{GOLDEN.relative_to(ROOT)} is current")
        return 0
    print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
