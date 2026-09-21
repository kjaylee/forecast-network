#!/usr/bin/env python3
"""Export wallet sign-in, so a Rust port can be held to it.

This is the one flow where a signature *creates* an account, converts a guest profile, and
replaces a recovery credential with a wallet — so it is the flow where being almost right is
worst. Four things the vector pins:

  * The signed message is the authorization. It names the origin, the address, the chain, the
    purpose, the mode, and a keyed commitment to the profile it will produce. A port that
    recomposes that message slightly differently does not produce a wrong answer — it produces a
    message no wallet ever signed, or worse, one a *different* request's signature validates.
    The verifier's call log below records every message exactly as it was passed to it.
  * The guard batch re-checks, in SQL, everything the Rust code already checked in memory. That
    duplication is the point: the challenge is read, the signature is awaited, and the row is read
    again, and only the SQL can decide the third time.
  * `mode` is two different operations behind one word. `login` may create a profile; `migrate`
    may only ever convert an existing one, and the guest's own session has to still be the session
    that asked.
  * A wallet that has been retired is refused, not re-created. The address is known to have
    belonged to someone, and silently starting a second profile for it would strand the first.

The audit body is canonical JSON with `ensure_ascii`, and a second fixture runs the same sign-in
under a non-ASCII origin for exactly that reason: the reference escapes the origin's bytes, and a
port that emits raw UTF-8 writes a different audit row for the same event.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.auth import Authentication  # noqa: E402
from forecast_application.database import SQLiteDatabase  # noqa: E402
from forecast_application.errors import AppError  # noqa: E402
from forecast_application.wallet_login import WalletLogin  # noqa: E402
from forecast_application.wallets import encode_address  # noqa: E402
from golden_cli import golden_main  # noqa: E402

GOLDEN = ROOT / "tests/golden/wallet-login-golden.json"
SECRET = b"golden-secret"
NOW = 1_700_000_000_000
ORIGIN = "https://forecast.eastsea.xyz"

# Canonical encodings of real Ed25519 points: the base point and three others. `decode_address`
# refuses anything that is not canonical and not of full order, so a made-up string would test
# nothing but the refusal path.
POINTS = [
    "5866666666666666666666666666666666666666666666666666666666666666",
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
    "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    # y=3: a point found by search rather than quoted from anywhere, so that every address in
    # this vector is one `decode_address` actually accepts. Small-order encodings appear in the
    # refusal cases instead, where they belong.
    "0300000000000000000000000000000000000000000000000000000000000000",
    "c9a3f86aae465f0e56513864510f3997561fa2c9e85ea21dc2292309f3cd6022",
]
ADDRESSES = [encode_address(bytes.fromhex(point)) for point in POINTS]
GOOD_SIGNATURE = bytes([1]) * 64
BAD_SIGNATURE = bytes([2]) * 64

TABLES = [
    "users", "sessions", "wallet_login_contexts", "wallet_login_challenges",
    "wallet_login_audit", "wallet_identities", "wallet_links", "wallet_challenges", "wallet_audit",
]


def digest(value: str) -> str:
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()


class Fixture:
    """One worker: a store, a clock, and a deterministic verifier.

    The verifier answers `True` only for one exact signature, and every call is recorded with the
    message it was handed — which is the only way a port's recomposed message can be compared
    byte for byte.
    """

    def __init__(self, *, origin: str = ORIGIN) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now, self.counter, self.verified = NOW, 0, []
        self.auth = Authentication(self.db, self.now_ms, digest, self.token)
        self.login = WalletLogin(self.db, now_ms=self.now_ms, token_hash=digest, random_token=self.token,
                                 verify_signature=self.verify_signature, origin=origin,
                                 on_create=self.on_create)
        self.origin = origin
        self.calls: list[dict] = []
        self.creations = 0

    def now_ms(self) -> int:
        return self.now

    def token(self) -> str:
        self.counter += 1
        return f"wl{self.counter}".ljust(32, "0")

    async def verify_signature(self, key: bytes, message: bytes, signature: bytes) -> bool:
        self.verified.append({"key": key.hex(), "message": message.decode("utf-8", "replace"),
                              "signature": signature.hex(), "valid": signature == GOOD_SIGNATURE})
        return signature == GOOD_SIGNATURE

    async def on_create(self) -> None:
        self.creations += 1

    async def call(self, name: str, awaitable, **inputs) -> object:
        """One call, recorded with the method it was.

        The name is only a label: `verify:other-context` is a *context* call sitting in the middle
        of the verify group, and a replay that guessed the method from the name would run the wrong
        one against the right vector entry. Reading it off the coroutine cannot drift.
        """
        entry: dict = {"call": name, "kind": getattr(awaitable, "__qualname__", "?").split(".")[-1],
                       "input": inputs}
        try:
            result = await awaitable
        except AppError as exc:
            entry["error"] = {"status": exc.status, "code": exc.code, "message": exc.message}
            self.calls.append(entry)
            return None
        entry["result"] = result
        self.calls.append(entry)
        return result

    async def rows(self) -> dict:
        return {table: [dict(row) for row in await self.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in TABLES}

    async def retire(self, address: str, user_id: str) -> None:
        """Retire an address the way the migration does — an identity row written at insert time.

        Its status cannot be changed afterwards (the store aborts `immutable_wallet_identity`), so
        this is the only shape a retired wallet has, and a port has to read it the same way.
        """
        await self.db.execute(
            "INSERT INTO wallet_identities(address,user_id,status,created_at) VALUES(?,?,'tombstone',?)",
            (address, user_id, self.now))


async def sign_in(fixture: Fixture, label: str, address: str, *, mode: str = "login",
                  session: str | None = None, expected: str | None = None, context: str | None = None,
                  signature: bytes = GOOD_SIGNATURE, display_name: str | None = None) -> None:
    """One challenge and one verify, recorded under names that say which case they are."""
    body: dict = {"address": address, "mode": mode, "expectedUserId": expected}
    if display_name is not None:
        body["displayName"] = display_name
    issued = await fixture.call(f"{label}:challenge", fixture.login.challenge(context, body, session),
                                body=body, contextToken=context, sessionToken=session)
    if issued is None:
        return
    proof = {"challengeId": issued["challengeId"], "address": address,
             "signature": base64.b64encode(signature).decode()}
    await fixture.call(f"{label}:verify", fixture.login.verify(context, proof, session),
                       body=proof, contextToken=context, sessionToken=session)


async def build() -> dict:
    fixture = Fixture()
    login = fixture.login
    before = ADDRESSES[0]
    guest_address = ADDRESSES[1]
    returning = ADDRESSES[2]
    retired = ADDRESSES[3]
    historical = ADDRESSES[4]

    # --- context: reuse is the whole point, so a live one comes back unchanged and every way of
    # failing to supply one produces a fresh context rather than an error.
    first = await fixture.call("context:new", login.context(None), contextToken=None)
    await fixture.call("context:reuse", login.context(first["contextToken"]), contextToken=first["contextToken"])
    second = await fixture.call("context:empty", login.context(""), contextToken="")
    await fixture.call("context:short", login.context("short"), contextToken="short")
    await fixture.call("context:not-a-string", login.context(7), contextToken=7)
    await fixture.call("context:unknown", login.context("m" * 64), contextToken="m" * 64)
    await fixture.db.execute("UPDATE wallet_login_contexts SET revoked_at=? WHERE token_hash=?",
                             (fixture.now, digest("wallet-context:" + first["contextToken"])))
    third = await fixture.call("context:after-revocation", login.context(first["contextToken"]),
                               contextToken=first["contextToken"])
    await fixture.call("cancel:short", login.cancel("short"), contextToken="short")
    await fixture.call("cancel:none", login.cancel(None), contextToken=None)
    await fixture.call("cancel:live", login.cancel(second["contextToken"]), contextToken=second["contextToken"])
    await fixture.call("context:after-cancel", login.context(second["contextToken"]),
                       contextToken=second["contextToken"])

    ctx = third["contextToken"]

    # --- challenge: the body, the mode, and the address are all checked before anything is read.
    for label, body in [
        ("challenge:not-an-object", "not-a-body"),
        ("challenge:missing-key", {"address": before, "mode": "login"}),
        ("challenge:extra-key", {"address": before, "mode": "login", "expectedUserId": None, "extra": 1}),
        ("challenge:bad-mode", {"address": before, "mode": "signup", "expectedUserId": None}),
        ("challenge:mode-not-a-string", {"address": before, "mode": 7, "expectedUserId": None}),
        ("challenge:login-with-expected", {"address": before, "mode": "login", "expectedUserId": "u_someone"}),
    ]:
        await fixture.call(label, login.challenge(ctx, body, None), body=body, contextToken=ctx, sessionToken=None)

    # A non-canonical address and a small-order one are both refused by the address codec.
    for label, address in [("challenge:not-an-address", "0OIl"), ("challenge:small-order", "1" * 32)]:
        await fixture.call(label, login.challenge(ctx, {"address": address, "mode": "login", "expectedUserId": None}, None),
                           body={"address": address, "mode": "login", "expectedUserId": None},
                           contextToken=ctx, sessionToken=None)

    # --- a guest profile, so the migrate path has something real to convert.
    guest = await fixture.auth.register("Guest Migrator")
    guest_id, guest_session = guest["user"]["id"], guest["sessionToken"]
    await fixture.call("challenge:migrate-without-session",
                       login.challenge(ctx, {"address": guest_address, "mode": "migrate", "expectedUserId": guest_id}, None),
                       body={"address": guest_address, "mode": "migrate", "expectedUserId": guest_id},
                       contextToken=ctx, sessionToken=None)
    await fixture.call("challenge:migrate-wrong-user",
                       login.challenge(ctx, {"address": guest_address, "mode": "migrate", "expectedUserId": "u_someone"},
                                       guest_session),
                       body={"address": guest_address, "mode": "migrate", "expectedUserId": "u_someone"},
                       contextToken=ctx, sessionToken=guest_session)
    await fixture.call("challenge:migrate-expected-not-a-string",
                       login.challenge(ctx, {"address": guest_address, "mode": "migrate", "expectedUserId": 7},
                                       guest_session),
                       body={"address": guest_address, "mode": "migrate", "expectedUserId": 7},
                       contextToken=ctx, sessionToken=guest_session)

    # --- login: a brand-new address creates a profile, and the verifier sees the exact message.
    await sign_in(fixture, "login:new", before, context=ctx)
    created = fixture.calls[-1]["result"]
    user_id = created["user"]["id"]

    # --- verify: every refusal that can be reached without disturbing the created profile.
    fresh = await fixture.call("verify:context", login.context(None), contextToken=None)
    verify_ctx = fresh["contextToken"]
    issued = await fixture.call("verify:challenge",
                                login.challenge(verify_ctx, {"address": returning, "mode": "login", "expectedUserId": None}, None),
                                body={"address": returning, "mode": "login", "expectedUserId": None},
                                contextToken=verify_ctx, sessionToken=None)
    identifier = issued["challengeId"]
    for label, body, session in [
        ("verify:not-an-object", "not-a-body", None),
        ("verify:missing-key", {"challengeId": identifier, "address": returning}, None),
        ("verify:extra-key", {"challengeId": identifier, "address": returning,
                              "signature": base64.b64encode(GOOD_SIGNATURE).decode(), "extra": 1}, None),
        ("verify:bad-id", {"challengeId": "wl_" + "z" * 32, "address": returning,
                           "signature": base64.b64encode(GOOD_SIGNATURE).decode()}, None),
        ("verify:id-not-a-string", {"challengeId": 7, "address": returning,
                                    "signature": base64.b64encode(GOOD_SIGNATURE).decode()}, None),
        ("verify:wrong-address", {"challengeId": identifier, "address": before,
                                  "signature": base64.b64encode(GOOD_SIGNATURE).decode()}, None),
        ("verify:signature-not-base64", {"challengeId": identifier, "address": returning, "signature": "!" * 88}, None),
    ]:
        await fixture.call(label, login.verify(verify_ctx, body, session), body=body,
                           contextToken=verify_ctx, sessionToken=session)
    # The signature that does not match is refused, and nothing is written.
    bad_proof = {"challengeId": identifier, "address": returning,
                 "signature": base64.b64encode(BAD_SIGNATURE).decode()}
    await fixture.call("verify:bad-signature", login.verify(verify_ctx, bad_proof, None),
                       body=bad_proof, contextToken=verify_ctx, sessionToken=None)
    # A different context cannot present this challenge, even with the right signature.
    other = await fixture.call("verify:other-context", login.context(None), contextToken=None)
    replay = {"challengeId": identifier, "address": returning,
              "signature": base64.b64encode(GOOD_SIGNATURE).decode()}
    await fixture.call("verify:wrong-context", login.verify(other["contextToken"], replay, None),
                       body=replay, contextToken=other["contextToken"], sessionToken=None)

    # --- the returning wallet: the same address again, now that it owns a profile.
    await sign_in(fixture, "returning", returning, context=verify_ctx)
    # Replaying a challenge that has been used is not a second sign-in.
    await fixture.call("verify:replay", login.verify(verify_ctx, replay, None),
                       body=replay, contextToken=verify_ctx, sessionToken=None)

    # --- migrate: the guest's own session converts the profile it belongs to.
    migrate_context = await fixture.call("migrate:context", login.context(None), contextToken=None)
    await sign_in(fixture, "migrate", guest_address, mode="migrate", session=guest_session,
                  expected=guest_id, context=migrate_context["contextToken"], display_name="Named Migrator")
    # The guest's recovery code is gone: the wallet is the only way back in.
    await fixture.call("migrate:guest-code", fixture.auth.login(guest["recoveryCode"]),
                       recoveryCode=guest["recoveryCode"])

    # --- an expired challenge, which is a different refusal from a changed one.
    stale_context = await fixture.call("expired:context", login.context(None), contextToken=None)
    stale = await fixture.call("expired:challenge",
                               login.challenge(stale_context["contextToken"],
                                               {"address": before, "mode": "login", "expectedUserId": None}, None),
                               body={"address": before, "mode": "login", "expectedUserId": None},
                               contextToken=stale_context["contextToken"], sessionToken=None)
    # The store refuses to let an issued proof's expiry move — `immutable_wallet_login_proof`
    # — so the clock moves instead, which is what actually happens to an expired challenge.
    fixture.now = NOW + 6 * 60 * 1000
    await fixture.call("expired:verify", login.verify(stale_context["contextToken"], {
        "challengeId": stale["challengeId"], "address": before,
        "signature": base64.b64encode(GOOD_SIGNATURE).decode()}, None),
        body={"challengeId": stale["challengeId"], "address": before,
              "signature": base64.b64encode(GOOD_SIGNATURE).decode()},
        contextToken=stale_context["contextToken"], sessionToken=None)
    fixture.now = NOW

    # --- a retired wallet is refused rather than re-created, by both routes into `_owner`: an
    # identity row that is no longer active, and a history that has no identity row at all.
    await fixture.retire(retired, user_id)
    await fixture.db.execute(
        "INSERT INTO wallet_audit(id,user_id,address,kind,challenge_id,body,created_at) VALUES(?,?,?,?,?,?,?)",
        ("wa_retired", user_id, historical, "wallet_unlinked", None, "{}", fixture.now))
    retired_context = await fixture.call("retired:context", login.context(None), contextToken=None)
    for label, address in [("retired:identity", retired), ("retired:history", historical)]:
        await fixture.call(f"{label}:challenge",
                           login.challenge(retired_context["contextToken"],
                                           {"address": address, "mode": "login", "expectedUserId": None}, None),
                           body={"address": address, "mode": "login", "expectedUserId": None},
                           contextToken=retired_context["contextToken"], sessionToken=None)
    await fixture.call("retired:cancel", login.cancel(retired_context["contextToken"]),
                       contextToken=retired_context["contextToken"])

    # --- the same sign-in under an origin whose bytes have to be escaped in the audit body.
    escaped = Fixture(origin="https://éxample.test")
    escaped_context = await escaped.call("escaped:context", escaped.login.context(None), contextToken=None)
    await sign_in(escaped, "escaped", before, context=escaped_context["contextToken"])

    return {
        "description": "Wallet sign-in: browser-bound contexts, the exact signed message, the guarded "
                       "identity conversion, the migrate path, and every refusal between them.",
        "now": NOW,
        "origin": ORIGIN,
        "escapedOrigin": "https://éxample.test",
        "calls": fixture.calls,
        "verified": fixture.verified,
        "creations": fixture.creations,
        "rows": await fixture.rows(),
        "escapedCalls": escaped.calls,
        "escapedVerified": escaped.verified,
        "escapedRows": await escaped.rows(),
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
