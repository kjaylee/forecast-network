"""Durable signed probability publications; HTTP authorization belongs to the caller."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from forecast_domain.errors import ValidationError
from forecast_domain.risk_feed import (
    RiskFeedBinding,
    RiskFeedPayload,
    RiskFeedSignal,
    SignedRiskFeed,
    require,
    signing_bytes,
)
from forecast_domain.serialization import content_hash, dumps, loads

from .database import Database, Statement
from .reputation import qualified_cohorts

# Reused verbatim in the CAS guard, including every eligibility/body change that
# could alter the signed aggregate while an asynchronous signer is running.
SNAPSHOT_SQL = """
SELECT json_object('id',f.id,'specification_hash',f.specification_hash,'category',f.category,
 'state',f.state,'open_at',f.open_at,'close_at',f.close_at,'revision',f.revision,
 'ai', (SELECT body FROM artifacts WHERE hash=json_extract(f.ai_forecast,'$.artifactHash')),
 'submissions',json((SELECT json_group_array(json_object('user_id',u.user_id,'probability',u.yes_probability,
   'submitted_at',u.submitted_at,'revision',u.revision,'body',u.body)) FROM
   (SELECT * FROM eligible_user_forecasts WHERE forecast_id=f.id AND submitted_at>=? AND submitted_at<=?
    ORDER BY user_id) u)),
 'history',json((SELECT json_group_array(json_object('user_id',h.user_id,'forecast_id',h.forecast_id,
  'category',h.category,'probability',h.probability,'outcome',h.outcome,'state',h.state,
  'finalized_outcome',h.finalized_outcome,'submitted_at',h.submitted_at,'finalized_at',h.finalized_at,
  'eligibility_at',h.eligibility_at,'eligible',h.eligible)) FROM
  (SELECT * FROM forecast_quality_history WHERE finalized_at<=? AND eligibility_at<=?
   AND user_id IN(SELECT user_id FROM eligible_user_forecasts WHERE forecast_id=f.id)
   ORDER BY user_id,forecast_id LIMIT 100001) h))) AS snapshot
FROM forecasts f WHERE f.id=? AND f.state='OPEN' AND f.open_at<=? AND f.close_at>?
 AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id)
 AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=f.id
  AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_completions c WHERE c.decision_id=d.id))
"""


async def approve_binding(
    db: Database, *, feed_id: str, binding: RiskFeedBinding, approved_by: str, now_ms: int
) -> None:
    """Call only after admin authentication; never infer a binding from question text."""
    binding.__post_init__()
    require(
        bool(approved_by.strip()) and len(approved_by) <= 128, "authenticated approver required"
    )
    require(binding.valid_from_ms <= now_ms < binding.valid_until_ms, "binding is not current")
    row = await db.first(
        "SELECT specification_hash,category,open_at,close_at,state FROM forecasts WHERE id=?",
        (binding.forecast_id,),
    )
    require(row is not None, "unknown canonical forecast")
    if row is None:
        raise ValidationError("unknown canonical forecast")
    require(
        row["specification_hash"] == binding.specification_hash
        and row["category"] == binding.category.value
        and row["state"] == "OPEN"
        and row["open_at"] <= binding.valid_from_ms
        and binding.valid_until_ms <= row["close_at"],
        "binding does not match the published specification window",
    )
    # Validate feed identifier with the same field contract before persisting it.
    require(
        type(feed_id) is str
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", feed_id) is not None,
        "invalid feed ID",
    )
    await db.batch(
        (
            (
                "INSERT INTO risk_feed_bindings(binding_id,feed_id,binding_json,approved_by,approved_at) VALUES(?,?,?,?,?)",
                (binding.binding_id, feed_id, dumps(binding), approved_by, now_ms),
            ),
            (
                "INSERT INTO risk_feed_heads(feed_id,sequence) VALUES(?,0) ON CONFLICT(feed_id) DO NOTHING",
                (feed_id,),
            ),
        )
    )


def _signals(
    binding: RiskFeedBinding, snapshot: dict[str, Any], now_ms: int, window_ms: int
) -> list[RiskFeedSignal]:
    require(
        snapshot["specification_hash"] == binding.specification_hash
        and snapshot["category"] == binding.category.value,
        "immutable binding changed",
    )
    rows = snapshot["submissions"]
    require(type(rows) is list and len(rows) <= 1000000, "invalid source population")
    history = snapshot["history"]
    require(
        type(history) is list and len(history) <= 100000,
        "qualification history exceeds source bound",
    )
    cohorts = qualified_cohorts(
        history, forecast_id=binding.forecast_id, category=binding.category.value, as_of_ms=now_ms
    )
    signals = []
    # Conservative group: all channels consuming the same service observations
    # share an upstream family. AI/crowd never manufacture independent quorum.
    dependence = content_hash({"upstream": "forecast-network-service-v1"})
    if rows:
        for row in rows:
            require(
                type(row["probability"]) is int and 0 <= row["probability"] <= 100,
                "malformed eligible probability",
            )
        count = len(rows)
        probability = (sum(r["probability"] for r in rows) * 200 + count) // (2 * count)
        signals.append(
            RiskFeedSignal(
                binding_id=binding.binding_id,
                source="crowd",
                probability_bp=probability,
                confidence_bp=min(9000, 3000 + count * 100),
                sample_count=count,
                observed_at_ms=max(r["submitted_at"] for r in rows),
                evidence_hash=content_hash({"binding": binding, "eligible_rows": rows}),
                dependence_group=dependence,
                calibration_status="provisional",
            )
        )
    top_rows = [row for row in rows if row["user_id"] in cohorts["top"]]
    if top_rows:
        count = len(top_rows)
        probability = (sum(r["probability"] for r in top_rows) * 200 + count) // (2 * count)
        signals.append(
            RiskFeedSignal(
                binding_id=binding.binding_id,
                source="top",
                probability_bp=probability,
                confidence_bp=min(9000, 3000 + count * 100),
                sample_count=count,
                observed_at_ms=max(r["submitted_at"] for r in top_rows),
                evidence_hash=content_hash(
                    {
                        "binding": binding,
                        "eligible_rows": top_rows,
                        "qualification_history": history,
                    }
                ),
                dependence_group=dependence,
                calibration_status="provisional",
            )
        )
    if snapshot["ai"] is not None:
        ai = json.loads(snapshot["ai"])
        require(type(ai) is dict, "invalid retained AI artifact")
        probability, observed = ai.get("yes_probability_bp"), ai.get("as_of_ms")
        require(
            ai.get("specification_hash") == binding.specification_hash
            and type(probability) is int
            and 0 <= probability <= 10000
            and type(observed) is int,
            "invalid AI probability provenance",
        )
        if max(binding.valid_from_ms, now_ms - window_ms) <= observed <= now_ms:
            signals.append(
                RiskFeedSignal(
                    binding_id=binding.binding_id,
                    source="ai",
                    probability_bp=probability,
                    confidence_bp=3000,
                    sample_count=1,
                    observed_at_ms=observed,
                    evidence_hash=content_hash(ai),
                    dependence_group=dependence,
                    calibration_status="provisional",
                )
            )
    return signals


async def active_bindings(
    db: Database, *, feed_id: str, now_ms: int
) -> tuple[RiskFeedBinding, ...]:
    """Current immutable approvals for publishing or operator-owned AI refresh.

    Historical approvals remain retained. Root may refresh retained AI artifacts
    only for these exact forecast/specification identities before publishing.
    """
    require(type(now_ms) is int and 0 <= now_ms < 2**53, "invalid binding selection time")
    rows = await db.all(
        "SELECT binding_json FROM risk_feed_bindings b WHERE feed_id=? "
        "AND json_extract(binding_json,'$.valid_from_ms')<=? "
        "AND json_extract(binding_json,'$.valid_until_ms')>? "
        "AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations r WHERE r.binding_id=b.binding_id) "
        "ORDER BY binding_id LIMIT 13",
        (feed_id, now_ms, now_ms),
    )
    require(len(rows) <= 12, "feed binding capacity exceeded")
    return tuple(loads(RiskFeedBinding, row["binding_json"]) for row in rows)


async def publish_feed(
    db: Database,
    *,
    feed_id: str,
    genesis_hash: str,
    key_id: str,
    public_key_hex: str,
    signer: Callable[[bytes], Awaitable[bytes]],
    now_ms: int,
    weight_set_hash: str,
    weight_set_version: str,
    window_ms: int = 86_400_000,
) -> SignedRiskFeed:
    require(
        type(window_ms) is int and 0 < window_ms <= 31_536_000_000,
        "invalid feed observation window",
    )
    head = await db.first(
        "SELECT h.sequence,COALESCE((SELECT MAX(created_at) FROM risk_feed_publications p WHERE p.feed_id=h.feed_id),0) last_time FROM risk_feed_heads h WHERE feed_id=?",
        (feed_id,),
    )
    require(head is not None, "feed has no approved canonical bindings")
    if head is None:
        raise ValidationError("unknown feed")
    require(type(now_ms) is int and now_ms >= head["last_time"], "publication time moved backwards")
    active = await active_bindings(db, feed_id=feed_id, now_ms=now_ms)
    bindings, signals, snapshots = [], [], []
    for binding in active:
        params = (
            max(binding.valid_from_ms, now_ms - window_ms),
            now_ms,
            now_ms,
            now_ms,
            binding.forecast_id,
            now_ms,
            now_ms,
        )
        found = await db.first(SNAPSHOT_SQL, params)
        if found is None:
            continue
        snapshot = str(found["snapshot"])
        values = _signals(binding, json.loads(snapshot), now_ms, window_ms)
        if values:
            bindings.append(binding)
            signals.extend(values)
            snapshots.append((params, snapshot))
    require(bool(signals), "no eligible current canonical risk observations")
    payload = RiskFeedPayload(
        genesis_hash=genesis_hash,
        feed_id=feed_id,
        sequence=head["sequence"] + 1,
        key_id=key_id,
        issued_at_ms=now_ms,
        expires_at_ms=min(now_ms + 120000, *(b.valid_until_ms for b in bindings)),
        bindings=tuple(bindings),
        signals=tuple(sorted(signals, key=lambda s: (s.binding_id, s.source))),
        weight_set_hash=weight_set_hash,
        weight_set_version=weight_set_version,
    )
    signature = await signer(signing_bytes(payload))
    require(
        type(signature) is bytes and len(signature) == 64,
        "signer returned invalid Ed25519 signature",
    )
    envelope = SignedRiskFeed(
        payload=payload, public_key_hex=public_key_hex, signature_hex=signature.hex()
    )
    digest = content_hash(payload)
    guard = "risk-feed:" + digest
    statements: list[Statement] = [
        (
            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM risk_feed_heads "
            "WHERE feed_id=? AND sequence=?) THEN 1 ELSE 0 END",
            (guard, feed_id, head["sequence"]),
        ),
    ]
    for binding in bindings:
        statements.append(
            (
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN NOT EXISTS("
                "SELECT 1 FROM risk_feed_binding_revocations WHERE binding_id=?) THEN 1 ELSE 0 END",
                (guard + ":binding:" + binding.binding_id, binding.binding_id),
            )
        )
    for index, (params, snapshot) in enumerate(snapshots):
        statements.append(
            (
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN ("
                + SNAPSHOT_SQL
                + ")=? THEN 1 ELSE 0 END",
                (f"{guard}:{index}", *params, snapshot),
            )
        )
    statements.extend(
        (
            (
                "INSERT INTO risk_feed_publications(feed_id,sequence,payload_hash,envelope_json,created_at) VALUES(?,?,?,?,?)",
                (feed_id, payload.sequence, digest, dumps(envelope), now_ms),
            ),
            (
                "UPDATE risk_feed_heads SET sequence=? WHERE feed_id=? AND sequence=?",
                (payload.sequence, feed_id, head["sequence"]),
            ),
            ("DELETE FROM mutation_guards WHERE token=? OR token LIKE ?", (guard, guard + ":%")),
        )
    )
    await db.batch(statements)
    return envelope


async def latest_feed(db: Database, *, feed_id: str) -> SignedRiskFeed | None:
    row = await db.first(
        "SELECT envelope_json FROM risk_feed_publications WHERE feed_id=? ORDER BY sequence DESC LIMIT 1",
        (feed_id,),
    )
    return loads(SignedRiskFeed, row["envelope_json"]) if row else None


async def revoke_binding(
    db: Database, *, binding_id: str, revoked_by: str, now_ms: int, reason: str
) -> None:
    """Append an admin-authorized revocation; published evidence remains immutable."""
    require(
        bool(revoked_by.strip()) and len(revoked_by) <= 128 and 1 <= len(reason.strip()) <= 1000,
        "revocation requires authenticated actor and bounded reason",
    )
    await db.execute(
        "INSERT INTO risk_feed_binding_revocations(binding_id,revoked_by,revoked_at,reason) VALUES(?,?,?,?)",
        (binding_id, revoked_by, now_ms, reason),
    )
