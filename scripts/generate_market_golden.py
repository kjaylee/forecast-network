#!/usr/bin/env python3
"""Export a point-market lifecycle, so a Rust port can be held to it.

The market is where points are actually committed, and its arithmetic is already ported —
`forecast-domain::pricing` reproduces the 80-digit LMSR. What is not ported is the
*transaction*: the treasury admission, the quote, the fill, the settlement, and the guards
that hold across all of them. A share price that matches is not enough if the fill that
charged for it recorded a different receipt, so this vector exports both — every call's
result and the rows it left behind.

The sequence is deliberately ordinary, because the ordinary path is the one that has to be
right first: fund a treasury, create a shadow market and an active one, buy both sides,
look at the book, then settle against a finalized outcome. The interesting races have
their own tests in `tests/test_markets.py`; this vector is what tells a port that it has
the plain case before it can be trusted with the hard ones.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.database import SQLiteDatabase  # noqa: E402
from forecast_application.errors import AppError  # noqa: E402
from forecast_application.markets import SCALE, PointMarkets  # noqa: E402

from tests import test_points  # noqa: E402

GOLDEN = ROOT / "tests/golden/market-golden.json"

# Every table a market lifecycle writes to, in a fixed order so the dump is comparable.
TABLES = [
    # The fixture the market reads from is part of the vector: a port that seeds a different
    # forecast is not reproducing the lifecycle, it is running a different one.
    "users", "forecasts",
    "market_treasuries", "market_funding", "point_markets", "market_shadow_accounts",
    "point_fractions", "market_quotes", "market_fills", "market_positions",
    "market_settlements", "market_closures", "market_account_ledger", "point_accounts",
    "point_positions",
]


class Fixture:
    """The lifecycle, driven the way `tests/test_markets.py` drives it."""

    opened = test_points.PointsTests.opened
    user = test_points.PointsTests.user
    event_sql = staticmethod(test_points.PointsTests.event_sql)
    finalize = test_points.PointsTests.finalize
    reserve = test_points.PointsTests.reserve

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now, self.counter = 1_000_000, 0
        self.markets = PointMarkets(self.db, lambda: self.now, self.token, live_enabled=True)
        self.calls: list[dict] = []

    def token(self) -> str:
        self.counter += 1
        return "test-market-token-" + str(self.counter)

    async def call(self, name: str, awaitable) -> dict:
        """Record the outcome either way.

        A refusal is part of the lifecycle: a replayed receipt, a stale quote and a
        double-funded treasury are the guards doing their job, and a port that only
        reproduces the happy path has not reproduced the module.
        """
        try:
            result = await awaitable
        except AppError as exc:
            self.calls.append({"call": name, "error": {"status": exc.status, "code": exc.code, "message": exc.message}})
            raise
        self.calls.append({"call": name, "result": result})
        return result

    async def maybe(self, name: str, awaitable) -> None:
        """The same, for the calls that are expected to be refused."""
        try:
            await self.call(name, awaitable)
        except AppError:
            pass

    async def source(self, fid: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO official_watch_sources(id,url,kind,interval_ms,next_poll,checked_at) "
            "VALUES(?,?,'index',60000,?,?)",
            ("source:" + fid, "https://example.com/" + fid, self.now + 60000, self.now),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO official_watch_bindings(forecast_id,source_id,families) VALUES(?,?,?)",
            (fid, "source:" + fid, "[]"),
        )

    async def rows(self) -> dict:
        dumped = {}
        for table in TABLES:
            found = await self.db.all(f"SELECT * FROM {table} ORDER BY rowid")
            dumped[table] = [dict(row) for row in found]
        return dumped


async def seed_only(fixture: Fixture) -> dict:
    for uid in ("user-a", "user-b", "user-c"):
        await fixture.user(uid)
    for fid in ("market-one", "market-two", "market-three"):
        await fixture.opened(fid)
    return await fixture.rows()


async def build_lifecycle(fixture: Fixture) -> dict:

    await fixture.call("budget", fixture.markets.budget("shadow"))
    await fixture.call("fund_treasury", fixture.markets.fund_treasury(700, fixture.token(), "shadow"))
    # A request key is user-supplied text, and both funding and fills key their rows on a hash of
    # it. The reference hashes with `json.dumps`'s default `ensure_ascii`, so a key with a
    # non-ASCII character names a different row than one that only escaped it — and a port that
    # emitted raw UTF-8 would accept the same key twice as two different requests.
    await fixture.call("fund_treasury_unicode", fixture.markets.fund_treasury(50, "treasury-café-1", "shadow"))
    await fixture.call("fund_treasury_unicode_replay", fixture.markets.fund_treasury(50, "treasury-café-1", "shadow"))
    # Funding twice with the same request key must not fund twice.
    await fixture.maybe("fund_treasury_again", fixture.markets.fund_treasury(700, fixture.token(), "shadow"))

    specification_hash = (await fixture.db.first("SELECT specification_hash FROM forecasts WHERE id=?", ("market-one",)))[
        "specification_hash"
    ]
    await fixture.call("create", fixture.markets.create("market-one", None, "shadow", specification_hash))
    await fixture.call("get", fixture.markets.get("market-one"))
    await fixture.call("preview", fixture.markets.preview("market-one", "YES", 100))

    quote = await fixture.call("quote", fixture.markets.quote("user-a", "market-one", "YES", 100))
    # The receipt is keyed by the request that produced it, so reading it back needs the same
    # key: a receipt that could be read under any key would not be a receipt.
    accept_key = fixture.token()
    await fixture.call(
        "accept",
        fixture.markets.accept("user-a", "market-one", quote["quoteId"], int(quote["claimsAtomic"]), accept_key),
    )
    # The same quote, spent again: the receipt is what stops it.
    await fixture.maybe(
        "accept_replay",
        fixture.markets.accept("user-a", "market-one", quote["quoteId"], int(quote["claimsAtomic"]), fixture.token()),
    )

    # A fill is keyed by the request that produced it, and that key is user-supplied text. The
    # reference hashes it with `json.dumps`'s default `ensure_ascii`, so a key with a non-ASCII
    # character names a different row than the same key escaped — and a port that wrote raw UTF-8
    # would treat one request as two.
    unicode_quote = await fixture.call("quote_unicode_key", fixture.markets.quote("user-b", "market-one", "YES", 20))
    unicode_key = "fill-café-1"
    await fixture.call(
        "accept_unicode_key",
        fixture.markets.accept("user-b", "market-one", unicode_quote["quoteId"],
                               int(unicode_quote["claimsAtomic"]), unicode_key),
    )
    await fixture.maybe(
        "accept_unicode_key_replay",
        fixture.markets.accept("user-b", "market-one", unicode_quote["quoteId"],
                               int(unicode_quote["claimsAtomic"]), unicode_key),
    )

    other = await fixture.call("quote_other_side", fixture.markets.quote("user-b", "market-one", "NO", 60))
    await fixture.call(
        "accept_other_side",
        fixture.markets.accept("user-b", "market-one", other["quoteId"], int(other["claimsAtomic"]), fixture.token()),
    )

    await fixture.call("positions", fixture.markets.positions("user-a", "market-one"))
    await fixture.call(
        "receipt_status",
        fixture.markets.receipt_status("user-a", "market-one", quote["quoteId"], accept_key),
    )
    return {"quote": quote, "other": other, "specificationHash": specification_hash}


async def build_settlement(fixture: Fixture) -> dict:
    """Settle a market against a finalized outcome, on the active path."""
    specification_hash = (
        await fixture.db.first("SELECT specification_hash FROM forecasts WHERE id=?", ("market-three",))
    )["specification_hash"]
    await fixture.call("fund_treasury_settle", fixture.markets.fund_treasury(700, fixture.token(), "active"))
    await fixture.call("create_settle", fixture.markets.create("market-three", None, "active", specification_hash))
    await fixture.source("market-three")
    quote = await fixture.call("quote_settle", fixture.markets.quote("user-c", "market-three", "YES", 90))
    await fixture.call(
        "accept_settle",
        fixture.markets.accept("user-c", "market-three", quote["quoteId"], int(quote["claimsAtomic"]), fixture.token()),
    )
    await fixture.finalize("market-three")
    await fixture.call("settle", fixture.markets.settle("market-three"))
    # Settling again is a replay, not a second payout.
    await fixture.maybe("settle_replay", fixture.markets.settle("market-three"))
    return {"quote": quote, "specificationHash": specification_hash}


async def build() -> dict:
    fixture = Fixture()
    # The state the market reads from, captured before it writes anything: a port seeds exactly
    # this, so a divergence in the fixture cannot be mistaken for a divergence in the port.
    fixture_rows = await seed_only(fixture)
    lifecycle = await build_lifecycle(fixture)
    settlement = await build_settlement(fixture)
    return {
        "fixtureRows": fixture_rows,
        "description": "A point market's ordinary path: treasury, create, quote, fill, look, settle.",
        "scale": str(SCALE),
        "calls": fixture.calls,
        "lifecycle": lifecycle,
        "settlement": settlement,
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
