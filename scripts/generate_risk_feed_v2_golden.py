#!/usr/bin/env python3
"""Export the v2 risk producer, so a Rust port can be held to it.

The v2 feed is the one that carries *typed* targets: a binding names a `[start, end)` interval and
has to agree with the interval the published question itself states, field by field, down to the
hash that commits to both. So the vector is built around the ways that agreement can fail, because
the failure is not a visible error — it is a feed that signs a probability about a window the
question never asked about.

Five things it is built to expose:

  * The approval gate. A binding with a plausible start, a plausible end, an unadmitted definition,
    a mismatched hash or a different asset is refused, and each refusal names itself.
  * The compile-time estimate. It carries no capture or completion clocks, and the reference
    refuses to invent them: the channel reports `unavailable` with the reason, signed, rather than
    manufacturing a probability. A port that defaulted those clocks to the estimate's own as-of
    would publish a number nobody measured.
  * The ordering. `operational_bindings_v2` publishes the *first* eligible episode per channel, so a
    different tie order is a different signed envelope under the same key.
  * The guard batch. It is evaluated after the signer is awaited, and the guard tokens are deleted
    by an explicit list rather than a `LIKE` pattern — a binding id may contain `%`.
  * Coverage. Every channel in the vocabulary is reported, including the ones with no admitted
    definition: a channel that disappears from the list cannot be noticed as missing.

The signer is deterministic and not a real Ed25519 key: the module checks only the signature's
length, and a synthetic signer whose output depends on its input is a stronger check on the signed
bytes than a real key would be, without the `cryptography` dependency the dependency-free CI job
does not install.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from forecast_application.risk_feed_v2 import (  # noqa: E402
    admit_definition,
    admit_profile,
    approve_binding_v2,
    latest_feed_v2,
    operational_bindings_v2,
    publish_feed_v2,
    revoke_binding_v2,
    stale_bindings_v2,
)
from forecast_domain.errors import ValidationError  # noqa: E402
from forecast_domain.models import Category  # noqa: E402
from forecast_domain.risk_feed import RiskFeedBindingV2  # noqa: E402
from forecast_domain.serialization import content_hash, to_dict  # noqa: E402
from golden_cli import golden_main  # noqa: E402

from tests import test_web_application as fixtures  # noqa: E402
from tests.risk_feed_fixtures import GENESIS, golden_definition, golden_profile  # noqa: E402

GOLDEN = ROOT / "tests/golden/risk-feed-v2-golden.json"
HOUR = 3_600_000

# The tables the producer writes, and the ones its approval gate reads. The second group is why
# this list is not just the risk-feed tables: a port has to replay the approval against the *same*
# published question, or it is approving a different one.
TABLES = [
    "users", "forecasts", "artifacts",
    "risk_feed_definitions_v2", "risk_feed_profiles_v2", "risk_feed_bindings_v2",
    "risk_feed_binding_revocations_v2", "risk_feed_publications_v2", "risk_feed_heads",
    "mutation_guards",
]


def spell(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign(data: bytes) -> bytes:
    """Sixty-four bytes that depend on every byte of the input, so the recorded signature is a
    check on the text rather than on the envelope."""
    return hashlib.sha256(data).digest() + hashlib.sha256(b"second:" + data).digest()


class Fixture:
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
            entry["error"] = {"type": type(error).__name__,
                              "message": getattr(error, "message", None) or str(error)}
            self.calls.append(entry)
            return None
        entry["result"] = result
        self.calls.append(entry)
        return result

    async def rows(self) -> dict:
        return {table: [dict(row) for row in await self.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in TABLES}


async def build() -> dict:
    case = fixtures.ApplicationTests(methodName="runTest")
    await case.asyncSetUp()
    fixture = Fixture(case)
    db = fixture.db

    # The episode opens now and becomes operational two hours later, which is what makes the
    # compile-time estimate's missing clocks observable: before the start there is no episode at
    # all, and after it there is one whose estimate still carries no capture clock.
    start = (case.now // 1000 + 2 * 3600) * 1000
    end = start + 48 * HOUR
    question = (f"During [{spell(start)}, {spell(end)}), will USDC/USD close strictly below USD 0.9900 "
                f"on both Kraken and Bitstamp in any same completed 5-minute candle? Submissions close exactly "
                f"{spell(end)}.")
    original_spec = fixtures.fixtures.specification
    policy = fixtures.fixtures.specification().source_policy
    with patch.object(fixtures.fixtures, "specification", side_effect=lambda **kw: original_spec(
            **{**kw, "source_policy": policy, "close_at_ms": end, "category": Category.CRYPTO})):
        draft = await case.app.compile_forecast(case.uid, question)
        published = (await case.app.publish_forecast(case.uid, draft["draftId"], "publish-risk-v2"))["forecast"]
    row = await db.first("SELECT * FROM forecasts WHERE id=?", (published["id"],))

    definition = golden_definition()
    profile = golden_profile()

    # --- admission: the records a binding is allowed to cite.
    await fixture.call("admit:definition-bad-actor", "admit_definition",
                       admit_definition(db, feed_id="risk-v2", definition=definition, approved_by="  ",
                                        now_ms=case.now),
                       feedId="risk-v2")
    await fixture.call("admit:definition-bad-feed", "admit_definition",
                       admit_definition(db, feed_id="risk v2", definition=definition, approved_by="admin",
                                        now_ms=case.now),
                       feedId="risk v2")
    await fixture.call("admit:definition", "admit_definition",
                       admit_definition(db, feed_id="risk-v2", definition=definition,
                                        approved_by="authenticated-admin", now_ms=case.now),
                       feedId="risk-v2")
    await fixture.call("admit:definition-again", "admit_definition",
                       admit_definition(db, feed_id="risk-v2", definition=definition,
                                        approved_by="authenticated-admin", now_ms=case.now),
                       feedId="risk-v2")
    await fixture.call("admit:profile", "admit_profile",
                       admit_profile(db, feed_id="risk-v2", profile=profile,
                                     approved_by="authenticated-admin", now_ms=case.now),
                       feedId="risk-v2")

    specification_hash = row["specification_hash"]
    binding = RiskFeedBindingV2(
        binding_id="usdc-depeg-1d-episode-1", forecast_id=published["id"],
        specification_hash=specification_hash, channel="depegRisk1d", asset="USDC",
        category=Category.CRYPTO, series_id="usdc-depeg-1d-w48", episode_id=spell(start),
        target_start_ms=start, target_end_ms=end, policy_horizon_ms=24 * HOUR,
        definition_hash=content_hash(definition), mapping_profile_id=profile.profile_id,
        mapping_profile_version=profile.profile_version, mapping_profile_hash=content_hash(profile),
        mapping_kind="containing_upper_estimate",
        question_event_definition_hash=content_hash({
            "specification_hash": specification_hash, "window": f"[{spell(start)}, {spell(end)})"}),
        approval_artifact_hash="1" * 64,
        authorization_valid_from_ms=row["open_at"], authorization_valid_until_ms=end,
        operational_valid_from_ms=start, operational_valid_until_ms=end - 24 * HOUR)

    # --- the approval gate: every way the typed target can disagree with the question.
    for name, changes in [
        # The record has to stay *valid* for the approval gate to be what refuses it: a binding
        # that violates its own interval invariants never reaches the question comparison.
        ("approve:start-moved", dict(binding_id="b1", target_start_ms=start + 300_000,
                                     operational_valid_from_ms=start + 300_000)),
        ("approve:end-moved", dict(binding_id="b2", target_end_ms=end - 300_000,
                                   operational_valid_until_ms=end - 300_000 - 24 * HOUR)),
        ("approve:definition-unadmitted", dict(binding_id="b3", definition_hash="0" * 64)),
        ("approve:profile-unadmitted", dict(binding_id="b4", mapping_profile_hash="0" * 64)),
        ("approve:event-hash", dict(binding_id="b5", question_event_definition_hash="0" * 64)),
        ("approve:asset", dict(binding_id="b6", asset="USDT")),
    ]:
        await fixture.call(name, "approve_binding_v2",
                           approve_binding_v2(db, feed_id="risk-v2", binding=replace(binding, **changes),
                                              approved_by="admin", now_ms=case.now),
                           feedId="risk-v2")
    await fixture.call("approve:not-current", "approve_binding_v2",
                       approve_binding_v2(db, feed_id="risk-v2", binding=binding, approved_by="admin",
                                          now_ms=binding.authorization_valid_from_ms - 1),
                       feedId="risk-v2")
    await fixture.call("approve:ok", "approve_binding_v2",
                       approve_binding_v2(db, feed_id="risk-v2", binding=binding,
                                          approved_by="authenticated-admin", now_ms=case.now),
                       feedId="risk-v2")

    # --- before the operational window there is no episode at all.
    async def produce(name: str, **overrides) -> object:
        arguments = dict(feed_id="risk-v2", genesis_hash=GENESIS, key_id="test-key",
                         public_key_hex="a" * 64, signer=fixture.signer, now_ms=case.now,
                         weight_set_hash="d" * 64, weight_set_version="source-calibration-v2",
                         calibration_cohort_id="usdc-depeg-1d-w48-containing")
        arguments.update(overrides)
        return await fixture.call(name, "publish_feed_v2", publish_feed_v2(db, **arguments), feedId="risk-v2")

    await fixture.call("operational:before", "operational_bindings_v2",
                       operational_bindings_v2(db, feed_id="risk-v2", now_ms=case.now), feedId="risk-v2")
    # Before the operational window there is an admitted, approved binding and still no episode:
    # the channel is `unavailable` for that reason, and the publication is signed anyway.
    await produce("publish:no-operational-episode")
    case.now = start + 1000
    await fixture.call("operational:after", "operational_bindings_v2",
                       operational_bindings_v2(db, feed_id="risk-v2", now_ms=case.now), feedId="risk-v2")
    # The compile-time estimate has no capture or completion clock, which is a signed
    # "unavailable" rather than a manufactured probability.
    await produce("publish:no-clock-provenance")
    await fixture.call("stale:none-yet", "stale_bindings_v2",
                       stale_bindings_v2(db, feed_id="risk-v2", now_ms=case.now), feedId="risk-v2")
    await fixture.call("latest", "latest_feed_v2", latest_feed_v2(db, feed_id="risk-v2"), feedId="risk-v2")

    # --- revocation stops the next publication from carrying the binding.
    await fixture.call("revoke:bad-actor", "revoke_binding_v2",
                       revoke_binding_v2(db, binding_id=binding.binding_id, revoked_by="", now_ms=case.now,
                                         reason="operator"),
                       bindingId=binding.binding_id)
    await fixture.call("revoke:ok", "revoke_binding_v2",
                       revoke_binding_v2(db, binding_id=binding.binding_id, revoked_by="authenticated-admin",
                                         now_ms=case.now, reason="operator withdrew the mapped question"),
                       bindingId=binding.binding_id)
    await fixture.call("operational:after-revocation", "operational_bindings_v2",
                       operational_bindings_v2(db, feed_id="risk-v2", now_ms=case.now), feedId="risk-v2")

    rows = await fixture.rows()
    await case.asyncTearDown()

    return {
        "description": "The v2 risk producer: admitted definitions and profiles, the typed-target "
                       "approval gate, the operational window, the compile-time estimate that has no "
                       "clock provenance, and revocation.",
        "genesis": GENESIS,
        "signed": fixture.signed,
        "calls": fixture.calls,
        "rows": rows,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__, default=to_dict))
