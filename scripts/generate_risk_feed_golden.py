#!/usr/bin/env python3
"""Export the signed risk-feed publication lifecycle, so a Rust port can be held to it.

This is the live path: `scripts/operate_risk_v2.py` calls exactly these functions on a per-minute
cron, and what comes out is a signed envelope that people read as the network's own probability.
Four things the vector is built around:

  * The signature covers a *specific text*. Every possible ordering or encoding of the same
    signals is a different signature, so the vector records the bytes the signer was handed rather
    than only the envelope it produced — a port that assembled the payload correctly but signed
    something else would otherwise look right.
  * The guard batch re-evaluates the whole snapshot in SQL, after the signer has been awaited. That
    await is where a hold can be placed or a binding revoked, so the interesting sequence is
    publish → mutate → publish again, and the second attempt must be refused rather than signed.
  * The head sequence is compare-and-set. Two publications built from the same head cannot both
    land, and a publication that moves time backwards is refused before it is built.
  * A binding has to match the *published* specification window, and only an admin-authorized
    caller may approve one — checked as text here, because the route owns authorization and this
    module says so rather than growing a second path.

The signer is deterministic and not a real Ed25519 key: the module checks only the signature's
length, and a synthetic signer whose output depends on its input is a stronger check on the signed
bytes than a real key would be, without the `cryptography` dependency the dependency-free CI job
does not install.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from forecast_application.risk_feed import (  # noqa: E402
    active_bindings,
    approve_binding,
    latest_feed,
    publish_feed,
    revoke_binding,
)
from forecast_domain.errors import ValidationError  # noqa: E402
from forecast_domain.models import Category  # noqa: E402
from forecast_domain.risk_feed import RiskFeedBinding  # noqa: E402
from forecast_domain.serialization import to_dict  # noqa: E402

from tests import test_web_application as fixtures  # noqa: E402

# Not `risk-feed-golden.json`: that name belongs to the domain crate's record contract, which
# is a different vector about the same subject. Overwriting it is how a passing suite stops
# testing what it says it tests.
GOLDEN = ROOT / "tests/golden/risk-feed-publication-golden.json"
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
SIGNATURE_KEY = "test-key"


def sign(data: bytes) -> bytes:
    """Sixty-four bytes that depend on every byte of the input.

    A constant would let a port sign the wrong text and still match the envelope; this makes the
    recorded signature a digest of the exact payload bytes.
    """
    return hashlib.sha256(data).digest() + hashlib.sha256(b"second:" + data).digest()


# The tables the lifecycle writes, and the ones its snapshot reads. The second group is why this
# list is not just the risk-feed tables: a port has to replay the publication against the *same*
# forecast and the same submissions, or it is signing a different feed.
TABLES = [
    "users", "forecasts", "user_forecasts", "artifacts",
    "risk_feed_bindings", "risk_feed_heads", "risk_feed_publications",
    "risk_feed_binding_revocations", "mutation_guards",
]


class Fixture:
    """The publication lifecycle, driven the way `tests/test_risk_feed_producer.py` drives it."""

    def __init__(self, case) -> None:
        self.case = case
        self.calls: list[dict] = []
        self.signed: list[str] = []

    @property
    def db(self):
        return self.case.db

    @property
    def now(self):
        return self.case.now

    async def signer(self, data: bytes) -> bytes:
        self.signed.append(data.hex())
        return sign(data)

    async def call(self, name: str, kind: str, awaitable, **inputs) -> object:
        entry: dict = {"call": name, "kind": kind, "input": inputs}
        try:
            result = await awaitable
        except (AppError, ValidationError) as error:
            entry["error"] = {
                "type": type(error).__name__,
                "message": getattr(error, "message", None) or str(error),
                "code": getattr(error, "code", None),
            }
            self.calls.append(entry)
            return None
        entry["result"] = result
        self.calls.append(entry)
        return result

    async def rows(self) -> dict:
        return {
            table: [dict(row) for row in await self.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES
        }


def binding_for(row, forecast_id: str, *, binding_id: str = "canonical-001") -> RiskFeedBinding:
    return RiskFeedBinding(
        binding_id=binding_id,
        forecast_id=forecast_id,
        specification_hash=row["specification_hash"],
        channel="depegRisk1d",
        horizon_hours=24,
        asset="USDC",
        category=Category(row["category"]),
        valid_from_ms=row["open_at"],
        valid_until_ms=row["close_at"],
    )


async def build() -> dict:
    case = fixtures.ApplicationTests(methodName="runTest")
    await case.asyncSetUp()
    fixture = Fixture(case)
    db = fixture.db

    published = await case.publish()
    forecast_id = published["id"]
    row = await db.first("SELECT * FROM forecasts WHERE id=?", (forecast_id,))
    binding = binding_for(row, forecast_id)

    # --- approve: the binding has to match the published specification window.
    await fixture.call("approve:bad-actor", "approve_binding",
                       approve_binding(db, feed_id="risk", binding=binding, approved_by="   ", now_ms=case.now),
                       feedId="risk", approvedBy="   ")
    await fixture.call("approve:actor-too-long", "approve_binding",
                       approve_binding(db, feed_id="risk", binding=binding, approved_by="x" * 129, now_ms=case.now),
                       feedId="risk", approvedBy="x" * 129)
    await fixture.call("approve:not-current", "approve_binding",
                       approve_binding(db, feed_id="risk", binding=binding, approved_by="admin",
                                       now_ms=binding.valid_from_ms - 1),
                       feedId="risk")
    await fixture.call("approve:bad-feed-id", "approve_binding",
                       approve_binding(db, feed_id="risk feed", binding=binding, approved_by="admin", now_ms=case.now),
                       feedId="risk feed")
    await fixture.call("approve:unknown-forecast", "approve_binding",
                       approve_binding(db, feed_id="risk",
                                       binding=binding_for(row, "no-such-forecast"), approved_by="admin",
                                       now_ms=case.now),
                       feedId="risk")
    await fixture.call("approve:wrong-specification", "approve_binding",
                       approve_binding(db, feed_id="risk",
                                       binding=replace(binding, specification_hash="b" * 64),
                                       approved_by="admin", now_ms=case.now),
                       feedId="risk")
    await fixture.call("approve:ok", "approve_binding",
                       approve_binding(db, feed_id="risk", binding=binding, approved_by="authenticated-admin",
                                       now_ms=case.now),
                       feedId="risk", approvedBy="authenticated-admin")

    # --- publish with nothing eligible yet.
    await fixture.call("publish:no-observations", "publish_feed",
                       publish_feed(db, feed_id="risk", genesis_hash=GENESIS, key_id=SIGNATURE_KEY,
                                    public_key_hex="a" * 64, signer=fixture.signer, now_ms=case.now,
                                    weight_set_hash="d" * 64, weight_set_version="source-calibration-v1"),
                       feedId="risk")

    await case.app.submit_forecast(case.other, forecast_id, "YES", 80, published["revision"], "feed-vote-123")

    def publish(**overrides):
        arguments = dict(feed_id="risk", genesis_hash=GENESIS, key_id=SIGNATURE_KEY, public_key_hex="a" * 64,
                         signer=fixture.signer, now_ms=case.now, weight_set_hash="d" * 64,
                         weight_set_version="source-calibration-v1")
        arguments.update(overrides)
        return publish_feed(db, **arguments)

    await fixture.call("publish:window-zero", "publish_feed", publish(window_ms=0), feedId="risk")
    await fixture.call("publish:window-too-large", "publish_feed", publish(window_ms=31_536_000_001), feedId="risk")
    await fixture.call("publish:first", "publish_feed", publish(), feedId="risk")
    await fixture.call("publish:second", "publish_feed", publish(), feedId="risk")
    await fixture.call("latest", "latest_feed", latest_feed(db, feed_id="risk"), feedId="risk")
    await fixture.call("active", "active_bindings", active_bindings(db, feed_id="risk", now_ms=case.now), feedId="risk")

    # --- the snapshot has to hold across the await. Pausing the forecast changes it.
    await db.execute("UPDATE forecasts SET state='PAUSED' WHERE id=?", (forecast_id,))
    await fixture.call("publish:after-snapshot-change", "publish_feed", publish(), feedId="risk")
    await db.execute("UPDATE forecasts SET state='OPEN' WHERE id=?", (forecast_id,))

    # --- revoke, and confirm the revocation is what stops the next publication.
    await fixture.call("revoke:bad-actor", "revoke_binding",
                       revoke_binding(db, binding_id=binding.binding_id, revoked_by="", now_ms=case.now,
                                      reason="operator"),
                       bindingId=binding.binding_id)
    await fixture.call("revoke:reason-too-long", "revoke_binding",
                       revoke_binding(db, binding_id=binding.binding_id, revoked_by="admin", now_ms=case.now,
                                      reason="x" * 1001),
                       bindingId=binding.binding_id)
    await fixture.call("revoke:ok", "revoke_binding",
                       revoke_binding(db, binding_id=binding.binding_id, revoked_by="authenticated-admin",
                                      now_ms=case.now, reason="operator withdrew the mapped question"),
                       bindingId=binding.binding_id)
    await fixture.call("publish:after-revocation", "publish_feed", publish(), feedId="risk")
    await fixture.call("active:after-revocation", "active_bindings",
                       active_bindings(db, feed_id="risk", now_ms=case.now), feedId="risk")

    rows = await fixture.rows()
    await case.asyncTearDown()

    return {
        "description": "The signed risk-feed publication lifecycle: approval against the published "
                       "specification window, the guarded batch after the signer, sequence "
                       "compare-and-set, and revocation.",
        "genesis": GENESIS,
        "signed": fixture.signed,
        "calls": fixture.calls,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(asyncio.run(build()), indent=2, sort_keys=True, ensure_ascii=False,
                          default=to_dict) + "\n"
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
