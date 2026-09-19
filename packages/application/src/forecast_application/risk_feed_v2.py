"""v2 risk feed producer: typed [start,end) targets, reviewed profiles, separated clocks.

Every approval is authenticated by the HTTP caller. Nothing here edits a published
question, rewrites a retained estimate's timestamps, or lets a payload authorize
its own mapping profile. v1 producer code and tables are untouched.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from forecast_domain.risk_feed import (
    FEED_TTL_MS,
    CanonicalRiskDefinitionV2,
    Channel,
    ChannelCoverageV2,
    RiskFeedBindingV2,
    RiskFeedPayloadV2,
    RiskFeedSignalV2,
    RiskMappingProfileV2,
    SignedRiskFeedV2,
    Source,
    freshness_as_of_ms,
    require,
    signing_bytes_v2,
)
from forecast_domain.serialization import content_hash, dumps, loads

from .ai import measurement_window
from .database import Database, Statement
from .reputation import qualified_cohorts
from .risk_feed import SNAPSHOT_SQL

CHANNELS: tuple[Channel, ...] = ("depegRisk1d", "depegRisk7d", "depegRisk30d", "reserveLossRisk",
                                 "liquidityStressRisk", "stableCollateralRisk", "btcCrashRisk",
                                 "ethCrashRisk", "solCrashRisk", "oracleFailureRisk",
                                 "bridgeFailureRisk", "counterpartyRisk")
FEED_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
CLOCK_SQL = ("SELECT body FROM artifacts WHERE hash=(SELECT clock_artifact_hash FROM risk_prediction_clocks_v2 "
             "WHERE estimate_artifact_hash=?)")


def _actor(approved_by: str) -> None:
    require(bool(approved_by.strip()) and len(approved_by) <= 128, "authenticated approver required")


def _feed(feed_id: str) -> None:
    require(type(feed_id) is str and FEED_ID.fullmatch(feed_id) is not None, "invalid feed ID")


async def admit_definition(db: Database, *, feed_id: str, definition: CanonicalRiskDefinitionV2,
                           approved_by: str, now_ms: int) -> str:
    definition.__post_init__()
    _actor(approved_by)
    _feed(feed_id)
    digest = content_hash(definition)
    await db.execute(
        "INSERT INTO risk_feed_definitions_v2(definition_hash,feed_id,channel,definition_json,approved_by,approved_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(definition_hash) DO NOTHING",
        (digest, feed_id, definition.channel, dumps(definition), approved_by, now_ms))
    return digest


async def admit_profile(db: Database, *, feed_id: str, profile: RiskMappingProfileV2,
                        approved_by: str, now_ms: int) -> str:
    profile.__post_init__()
    _actor(approved_by)
    _feed(feed_id)
    digest = content_hash(profile)
    await db.execute(
        "INSERT INTO risk_feed_profiles_v2(profile_hash,feed_id,profile_json,approved_by,approved_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(profile_hash) DO NOTHING",
        (digest, feed_id, dumps(profile), approved_by, now_ms))
    return digest


async def _approved(db: Database, *, feed_id: str, binding: RiskFeedBindingV2
                    ) -> tuple[CanonicalRiskDefinitionV2, RiskMappingProfileV2]:
    definition_row = await db.first(
        "SELECT definition_json FROM risk_feed_definitions_v2 WHERE definition_hash=? AND feed_id=?",
        (binding.definition_hash, feed_id))
    profile_row = await db.first(
        "SELECT profile_json FROM risk_feed_profiles_v2 WHERE profile_hash=? AND feed_id=?",
        (binding.mapping_profile_hash, feed_id))
    require(definition_row is not None and profile_row is not None, "binding references unadmitted records")
    assert definition_row is not None and profile_row is not None
    definition = loads(CanonicalRiskDefinitionV2, definition_row["definition_json"])
    profile = loads(RiskMappingProfileV2, profile_row["profile_json"])
    require(content_hash(definition) == binding.definition_hash
            and content_hash(profile) == binding.mapping_profile_hash, "retained record identity mismatch")
    require(
        definition.channel == binding.channel and definition.asset == binding.asset
        and definition.policy_horizon_ms == binding.policy_horizon_ms
        and definition.mapping_kind == binding.mapping_kind == profile.mapping_kind
        and definition.mapping_profile_id == binding.mapping_profile_id == profile.profile_id
        and definition.mapping_profile_version == binding.mapping_profile_version == profile.profile_version,
        "binding disagrees with its admitted definition or profile",
    )
    return definition, profile


async def approve_binding_v2(db: Database, *, feed_id: str, binding: RiskFeedBindingV2,
                             approved_by: str, now_ms: int) -> None:
    """Typed target bounds must equal the immutable published question's own UTC interval."""
    binding.__post_init__()
    _actor(approved_by)
    _feed(feed_id)
    require(binding.authorization_valid_from_ms <= now_ms < binding.authorization_valid_until_ms,
            "binding authorization is not current")
    row = await db.first(
        "SELECT specification_hash,category,open_at,close_at,state,snapshot FROM forecasts WHERE id=?",
        (binding.forecast_id,))
    require(row is not None, "unknown canonical forecast")
    assert row is not None
    require(row["specification_hash"] == binding.specification_hash and row["category"] == binding.category.value
            and row["state"] == "OPEN", "binding does not match the published specification")
    specification = json.loads(row["snapshot"])["specification"]
    window = measurement_window(str(specification["canonical_question"]))
    require(window is not None, "published question has no explicit [start, end) measurement interval")
    assert window is not None
    require(window["start_at_ms"] == binding.target_start_ms and window["end_at_ms"] == binding.target_end_ms,
            "typed target disagrees with the published measurement interval")
    require(binding.target_end_ms == row["close_at"], "measurement end must equal the published deadline")
    require(binding.question_event_definition_hash == content_hash(
        {"specification_hash": binding.specification_hash, "window": window["canonical_expression"]}),
        "question event definition hash mismatch")
    await _approved(db, feed_id=feed_id, binding=binding)
    await db.batch((
        ("INSERT INTO risk_feed_bindings_v2(binding_id,feed_id,forecast_id,binding_json,approved_by,approved_at) "
         "VALUES(?,?,?,?,?,?)", (binding.binding_id, feed_id, binding.forecast_id, dumps(binding), approved_by, now_ms)),
        ("INSERT INTO risk_feed_heads(feed_id,sequence) VALUES(?,0) ON CONFLICT(feed_id) DO NOTHING", (feed_id,)),
    ))


async def revoke_binding_v2(db: Database, *, binding_id: str, revoked_by: str, now_ms: int, reason: str) -> None:
    require(bool(revoked_by.strip()) and len(revoked_by) <= 128 and 1 <= len(reason.strip()) <= 1000,
            "revocation requires authenticated actor and bounded reason")
    await db.execute(
        "INSERT INTO risk_feed_binding_revocations_v2(binding_id,revoked_by,revoked_at,reason) VALUES(?,?,?,?)",
        (binding_id, revoked_by, now_ms, reason))


async def operational_bindings_v2(db: Database, *, feed_id: str, now_ms: int) -> tuple[RiskFeedBindingV2, ...]:
    """Bindings whose operational validity contains now; authorization-only bindings stay out."""
    require(type(now_ms) is int and 0 <= now_ms < 2**53, "invalid binding selection time")
    rows = await db.all(
        "SELECT binding_json FROM risk_feed_bindings_v2 b WHERE feed_id=? "
        "AND json_extract(binding_json,'$.operational_valid_from_ms')<=? "
        "AND json_extract(binding_json,'$.operational_valid_until_ms')>? "
        "AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id) "
        "ORDER BY binding_id LIMIT 65", (feed_id, now_ms, now_ms))
    require(len(rows) <= 64, "feed binding capacity exceeded")
    bindings = [loads(RiskFeedBindingV2, row["binding_json"]) for row in rows]
    # Deterministic per-channel order: newest target start first, then identity.
    # Publication takes the first episode with eligible estimates; probability
    # values never influence which episode is selected.
    return tuple(sorted(bindings, key=lambda b: (b.channel, -b.target_start_ms, b.binding_id)))


def _selection_rank(binding: RiskFeedBindingV2, definition: CanonicalRiskDefinitionV2, now_ms: int
                    ) -> tuple[str, bool, int, str]:
    """Per-channel publication order: mature episodes first, then newest target start, then identity.

    A consumer covers a window from its start only once the first sampling-grid candle has
    completed, so an episode younger than one grid step is published only when no older
    overlapping episode is operational; otherwise the channel would be withheld at every
    episode boundary. Probability values never influence the order.
    """
    warming = now_ms < binding.target_start_ms + definition.sampling_grid_ms
    return (binding.channel, warming, -binding.target_start_ms, binding.binding_id)


def _pool(binding: RiskFeedBindingV2, source: Source, rows: list[dict[str, Any]], evidence: dict[str, Any],
          dependence: str) -> RiskFeedSignalV2:
    count = len(rows)
    probability = (sum(r["probability"] for r in rows) * 200 + count) // (2 * count)
    times = sorted(r["submitted_at"] for r in rows)
    # The pool's information set closes with its newest member; freshness is judged
    # from its oldest member through freshness_as_of_ms on both sides of the feed.
    return RiskFeedSignalV2(
        binding_id=binding.binding_id, source=source,
        question_probability_bp=probability, confidence_bp=min(9000, 3000 + count * 100), sample_count=count,
        forecast_as_of_ms=times[-1], information_cutoff_ms=times[-1],
        evaluation_started_at_ms=times[0], evaluation_completed_at_ms=times[-1],
        source_capture_started_at_ms=times[0], source_capture_completed_at_ms=times[-1], source_watermark_ms=None,
        source_bundle_hash=content_hash(rows), estimate_hash=content_hash(evidence),
        coverage_evidence_hash=content_hash({"members": [r["user_id"] for r in rows]}),
        evidence_hash=content_hash(evidence), dependence_group=dependence,
        estimator_version="eligible-mean-v1", oldest_member_as_of_ms=times[0], newest_member_as_of_ms=times[-1],
        constituent_dataset_hash=content_hash(rows))


def _ai_signal(binding: RiskFeedBindingV2, ai: dict[str, Any], clock: dict[str, Any] | None,
               dependence: str) -> RiskFeedSignalV2 | None:
    require(type(ai) is dict and ai.get("specification_hash") == binding.specification_hash
            and type(ai.get("yes_probability_bp")) is int and 0 <= ai["yes_probability_bp"] <= 10000
            and type(ai.get("as_of_ms")) is int, "invalid AI probability provenance")
    if clock is None:
        return None  # compile-time estimates carry no capture/completion clocks; never invent them
    require(clock.get("version") == "risk-prediction-clock-v1"
            and clock.get("specification_hash") == binding.specification_hash
            and clock.get("forecast_as_of_ms") == ai["as_of_ms"], "clock artifact does not describe this estimate")
    return RiskFeedSignalV2(
        binding_id=binding.binding_id, source="ai",
        question_probability_bp=ai["yes_probability_bp"], confidence_bp=3000, sample_count=1,
        forecast_as_of_ms=ai["as_of_ms"], information_cutoff_ms=clock["information_cutoff_ms"],
        evaluation_started_at_ms=clock["evaluation_started_at_ms"],
        evaluation_completed_at_ms=clock["evaluation_completed_at_ms"],
        source_capture_started_at_ms=clock["source_capture_started_at_ms"],
        source_capture_completed_at_ms=clock["source_capture_completed_at_ms"],
        source_watermark_ms=clock.get("source_watermark_ms"),
        source_bundle_hash=clock["source_bundle_hash"], estimate_hash=content_hash(ai),
        coverage_evidence_hash=clock["source_bundle_hash"], evidence_hash=content_hash(ai),
        dependence_group=dependence, estimator_version=f"{ai.get('provider')}:{ai.get('model')}:refresh-v2"[:128])


def signals_v2(binding: RiskFeedBindingV2, snapshot: dict[str, Any], clock: dict[str, Any] | None,
               now_ms: int) -> list[RiskFeedSignalV2]:
    require(snapshot["specification_hash"] == binding.specification_hash
            and snapshot["category"] == binding.category.value, "immutable binding changed")
    rows, history = snapshot["submissions"], snapshot["history"]
    require(type(rows) is list and len(rows) <= 1000000 and type(history) is list and len(history) <= 100000,
            "invalid source population")
    for row in rows:
        require(type(row["probability"]) is int and 0 <= row["probability"] <= 100
                and type(row["submitted_at"]) is int, "malformed eligible submission")
    cohorts = qualified_cohorts(history, forecast_id=binding.forecast_id, category=binding.category.value,
                                as_of_ms=now_ms)
    dependence = content_hash({"upstream": "forecast-network-service-v1"})
    signals: list[RiskFeedSignalV2] = []
    if snapshot["ai"] is not None:
        signal = _ai_signal(binding, json.loads(snapshot["ai"]), clock, dependence)
        if signal is not None:
            signals.append(signal)
    if rows:
        signals.append(_pool(binding, "crowd", rows, {"binding": binding, "eligible_rows": rows}, dependence))
    top_rows = [row for row in rows if row["user_id"] in cohorts["top"]]
    if top_rows:
        signals.append(_pool(binding, "top", top_rows, {"binding": binding, "eligible_rows": top_rows,
                                                        "qualification_history": history}, dependence))
    return [s for s in signals if binding.authorization_valid_from_ms <= s.forecast_as_of_ms
            and s.evaluation_completed_at_ms <= now_ms
            and (binding.mapping_kind != "exact_dated" or s.forecast_as_of_ms <= binding.target_start_ms)]


async def _coverage(db: Database, *, feed_id: str, covered: dict[str, str], withheld: dict[str, str]
                    ) -> tuple[ChannelCoverageV2, ...]:
    rows = await db.all("SELECT DISTINCT channel FROM risk_feed_definitions_v2 WHERE feed_id=?", (feed_id,))
    defined = {row["channel"] for row in rows}
    coverage = []
    for channel in sorted(CHANNELS):
        if channel in covered:
            coverage.append(ChannelCoverageV2(channel=channel, status="covered", binding_id=covered[channel]))
        elif channel in defined:
            coverage.append(ChannelCoverageV2(channel=channel, status="unavailable",
                                              reason=withheld.get(channel, "no operational episode")))
        else:
            coverage.append(ChannelCoverageV2(channel=channel, status="unsupported", reason="no admitted definition"))
    return tuple(coverage)


async def profile_set_hash(db: Database, *, feed_id: str) -> str:
    rows = await db.all("SELECT profile_hash FROM risk_feed_profiles_v2 WHERE feed_id=? ORDER BY profile_hash",
                        (feed_id,))
    return content_hash({"feed_id": feed_id, "profiles": [row["profile_hash"] for row in rows]})


async def publish_feed_v2(
    db: Database, *, feed_id: str, genesis_hash: str, key_id: str, public_key_hex: str,
    signer: Callable[[bytes], Awaitable[bytes]], now_ms: int, weight_set_hash: str, weight_set_version: str,
    calibration_cohort_id: str,
) -> SignedRiskFeedV2:
    head = await db.first(
        "SELECT h.sequence,COALESCE((SELECT MAX(created_at) FROM risk_feed_publications_v2 p "
        "WHERE p.feed_id=h.feed_id),0) last_time FROM risk_feed_heads h WHERE feed_id=?", (feed_id,))
    require(head is not None, "feed has no approved canonical bindings")
    assert head is not None
    require(type(now_ms) is int and now_ms >= head["last_time"], "publication time moved backwards")
    bindings: list[RiskFeedBindingV2] = []
    signals: list[RiskFeedSignalV2] = []
    snapshots: list[tuple[tuple[Any, ...], str]] = []
    expiry = now_ms + FEED_TTL_MS
    withheld: dict[str, str] = {}
    selected: set[str] = set()
    operational = await operational_bindings_v2(db, feed_id=feed_id, now_ms=now_ms)
    approved = {b.binding_id: await _approved(db, feed_id=feed_id, binding=b) for b in operational}
    for binding in sorted(operational, key=lambda b: _selection_rank(b, approved[b.binding_id][0], now_ms)):
        if binding.channel in selected:
            continue  # an older overlapping episode stays retained but is not published twice
        _, profile = approved[binding.binding_id]
        params = (binding.authorization_valid_from_ms, now_ms, now_ms, now_ms, binding.forecast_id, now_ms, now_ms)
        found = await db.first(SNAPSHOT_SQL, params)
        if found is None:
            withheld[binding.channel] = "question not currently eligible"
            continue
        snapshot = str(found["snapshot"])
        decoded = json.loads(snapshot)
        clock = None
        if decoded["ai"] is not None:
            # The retained estimate artifact's identity is the hash of its own canonical body.
            clock_row = await db.first(CLOCK_SQL, (content_hash(json.loads(decoded["ai"])),))
            clock = json.loads(clock_row["body"]) if clock_row else None
        values = [s for s in signals_v2(binding, decoded, clock, now_ms)
                  if now_ms - freshness_as_of_ms(s) < profile.max_forecast_age_ms]
        if not values:
            withheld[binding.channel] = "no fresh estimate with complete clock provenance"
            continue
        bindings.append(binding)
        selected.add(binding.channel)
        withheld.pop(binding.channel, None)
        signals.extend(values)
        snapshots.append((params, snapshot))
        expiry = min(expiry, binding.operational_valid_until_ms,
                     *(freshness_as_of_ms(s) + profile.max_forecast_age_ms for s in values))
    require(expiry > now_ms, "every current estimate expires before publication")
    require(len(bindings) <= 12, "feed binding capacity exceeded")
    bindings.sort(key=lambda b: b.binding_id)
    coverage = await _coverage(db, feed_id=feed_id, covered={b.channel: b.binding_id for b in bindings},
                               withheld=withheld)
    payload = RiskFeedPayloadV2(
        genesis_hash=genesis_hash, feed_id=feed_id, sequence=head["sequence"] + 1, key_id=key_id,
        issued_at_ms=now_ms, expires_at_ms=expiry, bindings=tuple(bindings),
        signals=tuple(sorted(signals, key=lambda s: (s.binding_id, s.source))), channel_coverage=coverage,
        profile_set_hash=await profile_set_hash(db, feed_id=feed_id), weight_set_hash=weight_set_hash,
        weight_set_version=weight_set_version, calibration_cohort_id=calibration_cohort_id)
    signature = await signer(signing_bytes_v2(payload))
    require(type(signature) is bytes and len(signature) == 64, "signer returned invalid Ed25519 signature")
    envelope = SignedRiskFeedV2(payload=payload, public_key_hex=public_key_hex, signature_hex=signature.hex())
    digest = content_hash(payload)
    guard = "risk-feed-v2:" + digest
    statements: list[Statement] = [(
        "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM risk_feed_heads "
        "WHERE feed_id=? AND sequence=?) THEN 1 ELSE 0 END", (guard, feed_id, head["sequence"]))]
    for binding in bindings:
        statements.append((
            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN NOT EXISTS("
            "SELECT 1 FROM risk_feed_binding_revocations_v2 WHERE binding_id=?) THEN 1 ELSE 0 END",
            (guard + ":binding:" + binding.binding_id, binding.binding_id)))
    for index, (params, snapshot) in enumerate(snapshots):
        statements.append((
            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN (" + SNAPSHOT_SQL + ")=? THEN 1 ELSE 0 END",
            (f"{guard}:{index}", *params, snapshot)))
    tokens = tuple(str(statement[1][0]) for statement in statements)
    statements.extend((
        ("INSERT INTO risk_feed_publications_v2(feed_id,sequence,payload_hash,envelope_json,created_at) "
         "VALUES(?,?,?,?,?)", (feed_id, payload.sequence, digest, dumps(envelope), now_ms)),
        ("UPDATE risk_feed_heads SET sequence=? WHERE feed_id=? AND sequence=?",
         (payload.sequence, feed_id, head["sequence"])),
        # Every guard token is known here; an explicit list avoids LIKE pattern handling in D1.
        ("DELETE FROM mutation_guards WHERE token IN (" + ",".join("?" * len(tokens)) + ")", tokens),
    ))
    await db.batch(statements)
    return envelope


LATEST_CLOCK_SQL = """
SELECT json_extract(a.body,'$.forecast_as_of_ms') AS as_of FROM forecasts f
 JOIN risk_prediction_clocks_v2 c ON c.estimate_artifact_hash=json_extract(f.ai_forecast,'$.artifactHash')
 JOIN artifacts a ON a.hash=c.clock_artifact_hash WHERE f.id=?
"""


async def configure_operation(db: Database, *, feed_id: str, weight_set_hash: str, weight_set_version: str,
                              calibration_cohort_id: str, enabled: bool, configured_by: str, now_ms: int) -> None:
    _actor(configured_by)
    _feed(feed_id)
    require(re.fullmatch(r"[0-9a-f]{64}", weight_set_hash) is not None, "invalid weight set hash")
    require(all(type(v) is str and FEED_ID.fullmatch(v) is not None for v in (weight_set_version, calibration_cohort_id)),
            "invalid weight version or cohort identity")
    weight = await db.first("SELECT body FROM artifacts WHERE hash=? AND kind='risk-weight-set'", (weight_set_hash,))
    require(weight is not None and json.loads(weight["body"]).get("version") == weight_set_version,
            "weight reference is not admitted")
    await db.execute(
        "INSERT INTO risk_feed_operations_v2(feed_id,weight_set_hash,weight_set_version,calibration_cohort_id,enabled,"
        "configured_by,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(feed_id) DO UPDATE SET weight_set_hash=excluded."
        "weight_set_hash,weight_set_version=excluded.weight_set_version,calibration_cohort_id=excluded."
        "calibration_cohort_id,enabled=excluded.enabled,configured_by=excluded.configured_by,updated_at=excluded.updated_at",
        (feed_id, weight_set_hash, weight_set_version, calibration_cohort_id, 1 if enabled else 0, configured_by, now_ms))


async def stale_bindings_v2(db: Database, *, feed_id: str, now_ms: int) -> list[str]:
    """Bindings operational now or within half a forecast age whose clock-backed estimate is missing or aging."""
    rows = await db.all(
        "SELECT binding_json,profile_json FROM risk_feed_bindings_v2 b JOIN risk_feed_profiles_v2 p "
        "ON p.profile_hash=json_extract(b.binding_json,'$.mapping_profile_hash') WHERE b.feed_id=? "
        "AND json_extract(b.binding_json,'$.operational_valid_until_ms')>? "
        "AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id) "
        "ORDER BY json_extract(b.binding_json,'$.operational_valid_from_ms') LIMIT 64", (feed_id, now_ms))
    stale = []
    for row in rows:
        binding = loads(RiskFeedBindingV2, row["binding_json"])
        profile = loads(RiskMappingProfileV2, row["profile_json"])
        half_age = profile.max_forecast_age_ms // 2
        if binding.operational_valid_from_ms - half_age > now_ms:
            continue
        if binding.mapping_kind == "exact_dated" and now_ms > binding.target_start_ms:
            continue  # a later estimate could never be as of the target start
        latest = await db.first(LATEST_CLOCK_SQL, (binding.forecast_id,))
        if latest is None or type(latest["as_of"]) is not int or now_ms - latest["as_of"] >= half_age:
            stale.append(binding.binding_id)
    return stale


async def operate_feeds_v2(
    db: Database, *, now_ms: int, refresh: Callable[[str], Awaitable[Any]],
    publish: Callable[[str, str, str, str], Awaitable[SignedRiskFeedV2]],
    seed: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """One scheduled tick: due episodes, at most one budgeted refresh per feed, then a signed publication."""
    from .risk_feed_series import create_due_episodes

    # Each phase is timed and retained with the tick. A tick was measured taking 130 seconds
    # with no refresh and no episode due, so the time was not being spent where the work was,
    # and there was no way to tell where it was. These phases say where, and they say it from
    # inside the Worker, which is the only place that can see a cold start.
    started = time.monotonic()
    episodes = await create_due_episodes(db, now_ms=now_ms, seed=seed) if seed is not None else []
    episodes_ms = int((time.monotonic() - started) * 1000)
    listed = time.monotonic()
    feeds = await db.all("SELECT * FROM risk_feed_operations_v2 WHERE enabled=1 ORDER BY feed_id LIMIT 8")
    outcomes = []
    for feed in feeds:
        mark = time.monotonic()
        outcome: dict[str, Any] = {"feedId": feed["feed_id"], "refreshed": None, "published": None,
                                   "episodes": [e for e in episodes if e.get("created") or e.get("failure")],
                                   "phaseMs": {"episodes": episodes_ms,
                                               "list": int((time.monotonic() - listed) * 1000)}}
        mark = time.monotonic()
        stale = await stale_bindings_v2(db, feed_id=feed["feed_id"], now_ms=now_ms)
        outcome["phaseMs"]["stale"] = int((time.monotonic() - mark) * 1000)
        if stale:
            mark = time.monotonic()
            try:
                await refresh(stale[0])
                outcome["refreshed"] = stale[0]
            except Exception as exc:  # budget, lease or provider failure; publication still reports honestly
                outcome["refreshFailure"] = type(exc).__name__
            outcome["phaseMs"]["refresh"] = int((time.monotonic() - mark) * 1000)
        mark = time.monotonic()
        try:
            envelope = await publish(feed["feed_id"], feed["weight_set_hash"], feed["weight_set_version"],
                                     feed["calibration_cohort_id"])
            outcome["published"] = envelope.payload.sequence
            outcome["covered"] = [c.channel for c in envelope.payload.channel_coverage if c.status == "covered"]
        except Exception as exc:
            outcome["publishFailure"] = type(exc).__name__
        outcome["phaseMs"]["publish"] = int((time.monotonic() - mark) * 1000)
        await db.execute("INSERT OR IGNORE INTO risk_feed_operation_log_v2(feed_id,tick_at,outcome,detail) VALUES(?,?,?,?)",
                         (feed["feed_id"], now_ms, "published" if outcome["published"] else "withheld",
                          json.dumps(outcome, sort_keys=True)))
        outcomes.append(outcome)
    return outcomes


async def operations_health(db: Database, *, now_ms: int) -> dict[str, Any]:
    """Operator view of the v2 pipeline: feed age, tick failures, series schedule, source watch staleness."""
    feeds = []
    for feed in await db.all("SELECT * FROM risk_feed_operations_v2 ORDER BY feed_id LIMIT 8"):
        latest = await db.first("SELECT sequence,created_at,envelope_json FROM risk_feed_publications_v2 WHERE feed_id=? "
                                "ORDER BY sequence DESC LIMIT 1", (feed["feed_id"],))
        recent = await db.all("SELECT outcome,detail FROM risk_feed_operation_log_v2 WHERE feed_id=? AND tick_at>? "
                              "ORDER BY tick_at DESC LIMIT 30", (feed["feed_id"], now_ms - 1_800_000))
        covered = None
        if latest is not None:
            payload = json.loads(latest["envelope_json"])["payload"]
            covered = [c["channel"] for c in payload["channel_coverage"] if c["status"] == "covered"]
        feeds.append({
            "feedId": feed["feed_id"], "enabled": bool(feed["enabled"]),
            "latestSequence": latest["sequence"] if latest else None,
            "latestAgeMs": now_ms - latest["created_at"] if latest else None,
            "coveredChannels": covered,
            "ticksLast30m": len(recent),
            "failedTicksLast30m": sum(1 for r in recent if "Failure" in r["detail"]),
        })
    series = []
    for row in await db.all("SELECT series_id,enabled,series_json FROM risk_feed_series_v2 ORDER BY series_id LIMIT 16"):
        latest_start = await db.first(
            "SELECT MAX(json_extract(binding_json,'$.target_start_ms')) AS start FROM risk_feed_bindings_v2 b "
            "WHERE json_extract(binding_json,'$.series_id')=? AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r "
            "WHERE r.binding_id=b.binding_id)", (row["series_id"],))
        # Attempts that failed for an episode which then published are the retry working, not
        # a problem: the series logs `published` for the same target start and the count drops
        # to zero. Counting them for a further 24 hours held the pipeline at degraded while
        # both series were producing episodes.
        failures = await db.first(
            "SELECT COUNT(*) AS n FROM risk_feed_series_log_v2 l WHERE l.series_id=? AND l.attempted_at>? "
            "AND l.outcome LIKE 'failed:%' AND NOT EXISTS(SELECT 1 FROM risk_feed_bindings_v2 b "
            "WHERE json_extract(b.binding_json,'$.series_id')=l.series_id "
            "AND json_extract(b.binding_json,'$.target_start_ms')=l.target_start_ms)",
            (row["series_id"], now_ms - 86_400_000))
        cadence = json.loads(row["series_json"])["cadence_ms"]
        series.append({"seriesId": row["series_id"], "enabled": bool(row["enabled"]),
                       "latestEpisodeStartMs": latest_start["start"] if latest_start else None,
                       "nextEpisodeStartMs": (latest_start["start"] + cadence) if latest_start and latest_start["start"] else None,
                       "failedUnpublishedAttemptsLast24h": failures["n"] if failures else 0})
    # The grace is a quarter of each source's own interval, never less than five minutes. A
    # flat five minutes on a sixty-minute article is a jitter detector rather than a health
    # signal: it flips on the sweep landing a couple of minutes late, and the monitor alerts
    # on every flip. Scaling it keeps the tight case tight -- a fifteen-minute publisher is
    # still stale after twenty -- while a slow source is judged against its own period, which
    # is what the forty-seven-hour outage looked like.
    sources = await db.first(
        "SELECT COUNT(*) AS total, SUM(failure_count>0) AS failing, "
        "SUM(failure_count=0 AND (checked_at IS NULL OR checked_at<?-interval_ms-MAX(300000,interval_ms/4))) AS stale "
        "FROM official_watch_sources WHERE enabled=1",
        (now_ms,))
    # A forecast carrying a job_error has failed an attempt and not been cleared since. These
    # accumulate silently: nothing counts them, and three have been retrying for days because
    # the state they are in cannot reach the path that would resolve them. Reported, not
    # alerted on, because it needs a decision rather than a wake-up call.
    stuck = await db.first("SELECT COUNT(*) AS n FROM forecasts WHERE job_error IS NOT NULL")
    return {"serverTime": now_ms, "feeds": feeds, "series": series,
            "stuckForecasts": stuck["n"] if stuck else 0,
            "sourceWatch": {k: (sources[k] or 0) for k in ("total", "failing", "stale")} if sources else None}


async def training_export(db: Database, *, feed_id: str, now_ms: int, limit: int = 500) -> dict[str, Any]:
    """Finalized, unheld canonical questions with the exact signed estimates the feed carried.

    Labels come only from the finalization event and the finalized outcome; eligibility
    waits for any open eligibility decision. Every retained publication signal for a
    finalized binding is exported so the consumer can deduplicate by source/question and
    train against the exact numbers it once consumed, never re-derived estimates.
    """
    require(type(limit) is int and 1 <= limit <= 2000, "invalid export bound")
    rows = await db.all(
        "SELECT b.binding_id,b.binding_json,f.id AS forecast_id,f.finalized_outcome,z.finalized_at,"
        " MAX(z.finalized_at,COALESCE(c.created_at,0)) AS eligibility_at "
        "FROM risk_feed_bindings_v2 b JOIN forecasts f ON f.id=b.forecast_id "
        "JOIN forecast_quality_finalizations z ON z.forecast_id=f.id "
        "LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=f.id "
        "LEFT JOIN forecast_eligibility_completions c ON c.decision_id=d.id "
        "WHERE b.feed_id=? AND f.state IN ('FINALIZED','ARCHIVED') AND f.finalized_outcome IN ('YES','NO') "
        "AND (d.id IS NULL OR c.decision_id IS NOT NULL) "
        "AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id) "
        "ORDER BY z.finalized_at,b.binding_id LIMIT ?", (feed_id, limit))
    bindings = []
    for row in rows:
        signals = await db.all(
            "SELECT sequence,created_at,envelope_json FROM risk_feed_publications_v2 WHERE feed_id=? "
            "AND envelope_json LIKE ? ORDER BY sequence LIMIT 5000", (feed_id, '%"' + row["binding_id"] + '"%'))
        seen: dict[str, dict[str, Any]] = {}
        for publication in signals:
            payload = json.loads(publication["envelope_json"])["payload"]
            for signal in payload["signals"]:
                if signal["binding_id"] == row["binding_id"] and signal["source"] not in seen:
                    seen[signal["source"]] = {**signal, "sequence": publication["sequence"]}
        bindings.append({"binding": json.loads(row["binding_json"]), "forecastId": row["forecast_id"],
                         "outcome": row["finalized_outcome"], "finalizedAtMs": row["finalized_at"],
                         "eligibilityAtMs": row["eligibility_at"], "firstSignals": sorted(seen.values(), key=lambda s: s["source"])})
    return {"feedId": feed_id, "exportedAtMs": now_ms, "bindings": bindings, "policy": "first retained signal per source"}


async def latest_feed_v2(db: Database, *, feed_id: str) -> SignedRiskFeedV2 | None:
    row = await db.first(
        "SELECT envelope_json FROM risk_feed_publications_v2 WHERE feed_id=? ORDER BY sequence DESC LIMIT 1",
        (feed_id,))
    return loads(SignedRiskFeedV2, row["envelope_json"]) if row else None
