#!/usr/bin/env python3
"""Export the authentication service, so a Rust port can be held to it.

Two things here are easy to port *almost* right, and both are worth a vector:

  * `authenticate` is one statement whose two branches mean different things. A session with no
    context authenticates only while its user has never converted to a wallet; a session with a
    context authenticates only while that context is still on the epoch the session recorded and
    still points at this session. Getting the second branch wrong re-admits a session that a
    later wallet sign-in was supposed to have replaced, so the vector walks both branches and
    every way each can fail: no context, the wrong context, a revoked context, an expired one,
    and a stale epoch.
  * `login` reads, then writes, and the write can lose a race. The reference does not retry it:
    it reads the store back to decide *which* refusal to report — a converted profile, a context
    that moved, or a genuine outage — and picking the wrong one either tells a user their code is
    wrong when it is not, or leaves them retrying a request that can never succeed.

Every call records its inputs beside its outcome. A vector whose inputs are only implicit in the
caller is one a port cannot replay, and the recovery-code and context paths both branch on the
exact inputs.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.auth import (  # noqa: E402
    SESSION_LIFETIME_MS,
    Authentication,
    public_user,
    text,
)
from forecast_application.database import SQLiteDatabase  # noqa: E402
from forecast_application.errors import AppError  # noqa: E402

GOLDEN = ROOT / "tests/golden/auth-golden.json"
SECRET = b"golden-secret"
NOW = 1_700_000_000_000

TABLES = ["users", "sessions", "wallet_login_contexts", "wallet_login_challenges", "wallet_identities"]


def digest(value: str) -> str:
    """The shape production uses: HMAC over the prefixed value. Which secret is irrelevant to the
    vector — the *prefix* is the part that has to survive the port, because it is what keeps a
    recovery code from being usable as a session token."""
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()


class Fixture:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now, self.counter = NOW, 0
        self.auth = Authentication(self.db, self.now_ms, digest, self.token)
        self.calls: list[dict] = []

    def now_ms(self) -> int:
        return self.now

    def token(self) -> str:
        """Deterministic and shaped: 32 url-safe characters, which is the floor the service
        enforces. A counter makes every derived id — the uid truncates it, the handle truncates it
        further — stable across regenerations."""
        self.counter += 1
        return f"token{self.counter}".ljust(32, "0")

    async def call(self, name: str, awaitable, **inputs) -> object:
        entry: dict = {"call": name, "input": inputs}
        try:
            result = await awaitable
        except AppError as exc:
            entry["error"] = {"status": exc.status, "code": exc.code, "message": exc.message}
            self.calls.append(entry)
            return None
        entry["result"] = result
        self.calls.append(entry)
        return result

    async def context(self, name: str, *, epoch: int = 1, expires_in: int = 600_000, revoked: bool = False,
                      latest: str | None = None, active: str | None = None) -> str:
        """A prepared sign-in context, written the way `WalletLogin` leaves one behind."""
        token = f"context{name}".ljust(32, "0")
        await self.db.execute(
            "INSERT INTO wallet_login_contexts(token_hash,epoch,latest_challenge_id,active_session_hash,"
            "created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,?)",
            (digest("wallet-context:" + token), epoch, latest, active,
             self.now - 1000, self.now + expires_in, self.now if revoked else None))
        return token

    async def rows(self) -> dict:
        return {table: [dict(row) for row in await self.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in TABLES}


async def build() -> dict:
    fixture = Fixture()
    auth = fixture.auth

    # --- register: what a display name has to be before an account exists at all.
    for index, value in enumerate(["  Ada Lovelace  ", None, 7, "", "x" * 41, "bad\nname", "   ", True]):
        await fixture.call(f"register:{index}", auth.register(value), displayName=value)
    # The first one succeeded, so its credentials are the ones every later call uses.
    first = fixture.calls[0]["result"]
    code, session = first["recoveryCode"], first["sessionToken"]

    # --- login without a context. The three ways a recovery code fails are told apart by message
    # alone: off-shape (which is what a wallet recovery *phrase* looks like), unknown, and absent.
    await fixture.call("login:code", auth.login(code), recoveryCode=code)
    await fixture.call("login:short", auth.login("z" * 31), recoveryCode="z" * 31)
    await fixture.call("login:long", auth.login("z" * 257), recoveryCode="z" * 257)
    await fixture.call("login:unknown", auth.login("z" * 64), recoveryCode="z" * 64)
    await fixture.call("login:not-a-string", auth.login(None), recoveryCode=None)
    phrase = " ".join(["abandon"] * 8)
    await fixture.call("login:phrase", auth.login(phrase), recoveryCode=phrase)

    # --- login with a context: every reason a prepared sign-in stops being usable.
    await fixture.call("login:context-short", auth.login(code, "short"), recoveryCode=code, contextToken="short")
    await fixture.call("login:context-not-a-string", auth.login(code, 7), recoveryCode=code, contextToken=7)
    await fixture.call("login:context-missing", auth.login(code, "m" * 64),
                       recoveryCode=code, contextToken="m" * 64)
    revoked = await fixture.context("revoked", revoked=True)
    await fixture.call("login:context-revoked", auth.login(code, revoked), recoveryCode=code, contextToken=revoked)
    stale = await fixture.context("expired", expires_in=-1)
    await fixture.call("login:context-expired", auth.login(code, stale), recoveryCode=code, contextToken=stale)
    good = await fixture.context("good", latest="challenge-1")
    bound = await fixture.call("login:context", auth.login(code, good), recoveryCode=code, contextToken=good)
    bound_session = bound["sessionToken"]

    # --- authenticate: the branch with no context survives, and the branch with one needs the
    # context to still be on the epoch the session recorded.
    await fixture.call("authenticate:plain", auth.authenticate(session), sessionToken=session)
    await fixture.call("authenticate:bound", auth.authenticate(bound_session, good),
                       sessionToken=bound_session, contextToken=good)
    await fixture.call("authenticate:bound-without-context", auth.authenticate(bound_session),
                       sessionToken=bound_session)
    await fixture.call("authenticate:plain-with-context", auth.authenticate(session, good),
                       sessionToken=session, contextToken=good)
    await fixture.call("authenticate:none", auth.authenticate(None), sessionToken=None)
    await fixture.call("authenticate:empty", auth.authenticate(""), sessionToken="")
    await fixture.call("authenticate:long", auth.authenticate("x" * 257), sessionToken="x" * 257)
    await fixture.call("authenticate:context-long", auth.authenticate(session, "y" * 257),
                       sessionToken=session, contextToken="y" * 257)
    await fixture.call("authenticate:unknown", auth.authenticate("z" * 64), sessionToken="z" * 64)
    # A context bumped to a new epoch invalidates the session that recorded the old one — this is
    # the statement that makes a wallet sign-in replace an earlier browser session rather than
    # sit alongside it.
    await fixture.db.execute("UPDATE wallet_login_contexts SET epoch=epoch+1 WHERE token_hash=?",
                             (digest("wallet-context:" + good),))
    await fixture.call("authenticate:stale-epoch", auth.authenticate(bound_session, good),
                       sessionToken=bound_session, contextToken=good)

    # --- the wallet conversion closes the recovery-code door behind it.
    created = fixture.calls[0]["result"]["user"]["id"]
    await fixture.db.execute(
        "INSERT INTO wallet_identities(address,user_id,status,created_at,converted_at) VALUES(?,?,?,?,?)",
        ("Wa11et" + "1" * 38, created, "active", fixture.now - 500, fixture.now))

    await fixture.call("authenticate:converted", auth.authenticate(session), sessionToken=session)
    converted_context = await fixture.context("converted")
    await fixture.call("login:converted-context", auth.login(code, converted_context),
                       recoveryCode=code, contextToken=converted_context)
    await fixture.call("login:converted", auth.login(code), recoveryCode=code)
    await fixture.call("register:after-conversion", auth.register("Someone Else"), displayName="Someone Else")
    await fixture.call("authenticate:converted-bound", auth.authenticate(bound_session, good),
                       sessionToken=bound_session, contextToken=good)
    # Restore the epoch so the bound branch is actually reached: the conversion does *not* close a
    # session that a wallet sign-in established, because the wallet is the credential there. Only
    # the recovery-code path is closed behind the conversion.
    await fixture.db.execute("UPDATE wallet_login_contexts SET epoch=epoch-1 WHERE token_hash=?",
                             (digest("wallet-context:" + good),))
    await fixture.call("authenticate:converted-bound-restored", auth.authenticate(bound_session, good),
                       sessionToken=bound_session, contextToken=good)

    # --- logout: the session's own bound context is revoked as well as the cookie's.
    await fixture.call("logout:bound", auth.logout(bound_session, good),
                       sessionToken=bound_session, contextToken=good)
    await fixture.call("logout:plain", auth.logout(session), sessionToken=session)
    await fixture.call("logout:none", auth.logout(None), sessionToken=None)
    await fixture.call("logout:unknown", auth.logout("z" * 64, "y" * 64), sessionToken="z" * 64, contextToken="y" * 64)
    await fixture.call("logout:twice", auth.logout(bound_session, good),
                       sessionToken=bound_session, contextToken=good)

    # --- the two pure helpers, which decide what a user is shown and what a name may be.
    #
    # The trim is the interesting part. Python's `str.strip()` strips everything `str.isspace()`
    # calls whitespace, and that set is *not* the Unicode White_Space property: it includes
    # U+001C..U+001F, which Unicode does not call whitespace at all. A port that trims with the
    # host language's own definition accepts `"\x1cAda\x1d"` as a name Python rejects \u2014 the
    # separator survives the trim and then trips the control-character check.
    names = []
    for value in ["  Grace  ", "", " ", "x" * 40, "x" * 41, "two words", "tab\there",
                  "line\nbreak", "nul\x00byte", "  \u00e9\u00e9  ", "\u2028separator",
                  "\u00a0Ada\u00a0", "\u3000Ada", "\x0bAda", 7, None, True, "  \u4e2d\u6587  ",
                  "\x1cseparator", "\x1cAda\x1d", "\x1f", "\x1c\x1d\x1e\x1f"]:
        try:
            names.append({"input": value, "result": text(value, 40)})
        except AppError as error:
            names.append({"input": value, "error": {"status": error.status, "code": error.code}})

    users = await fixture.db.all("SELECT id,display_name,handle,created_at FROM users ORDER BY rowid")
    projected = [{"row": dict(row), "public": public_user(dict(row))} for row in users]

    return {
        "description": "The authentication service: registration, recovery-code login with and "
                       "without a prepared wallet context, session authentication across both "
                       "branches, logout, and the conversion that closes the recovery path.",
        "now": NOW,
        "sessionLifetimeMs": SESSION_LIFETIME_MS,
        "calls": fixture.calls,
        "names": names,
        "projections": projected,
        "rows": await fixture.rows(),
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
