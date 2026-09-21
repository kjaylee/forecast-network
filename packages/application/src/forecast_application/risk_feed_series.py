"""Recurring canonical episodes: publish and bind the next question before its start.

Series are operator-approved templates (shared `RiskFeedSeriesV2`). Each tick creates at
most one due episode per series through the ordinary canonical seed path (compiler,
review, retained artifacts) and the ordinary typed-target approval; nothing bypasses
those gates, and a failed attempt is logged and retried on the next tick.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from forecast_domain.models import Category
from forecast_domain.risk_feed import RiskFeedBindingV2, RiskFeedSeriesV2, require
from forecast_domain.serialization import content_hash, dumps, loads

from .ai import measurement_window
from .database import Database
from .risk_feed_v2 import _actor, _feed, approve_binding_v2

RETRY_MS = 600_000
CATEGORY_BY_CHANNEL = {"depegRisk1d": Category.CRYPTO, "depegRisk7d": Category.CRYPTO, "depegRisk30d": Category.CRYPTO,
                       "btcCrashRisk": Category.CRYPTO, "ethCrashRisk": Category.CRYPTO, "solCrashRisk": Category.CRYPTO}


def spell(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def configure_series(db: Database, *, series: RiskFeedSeriesV2, enabled: bool, configured_by: str,
                           now_ms: int) -> str:
    series.__post_init__()
    _actor(configured_by)
    _feed(series.feed_id)
    definition = await db.first("SELECT channel,definition_json FROM risk_feed_definitions_v2 WHERE definition_hash=? AND feed_id=?",
                                (series.definition_hash, series.feed_id))
    profile = await db.first("SELECT profile_json FROM risk_feed_profiles_v2 WHERE profile_hash=? AND feed_id=?",
                             (series.mapping_profile_hash, series.feed_id))
    require(definition is not None and profile is not None, "series references unadmitted records")
    assert definition is not None and profile is not None
    require(definition["channel"] == series.channel and json.loads(definition["definition_json"])["asset"] == series.asset
            and json.loads(profile["profile_json"])["mapping_kind"] == series.mapping_kind,
            "series disagrees with its admitted definition or profile")
    require(series.channel in CATEGORY_BY_CHANNEL, "series channel has no publication category")
    digest = content_hash(series)
    await db.execute(
        "INSERT INTO risk_feed_series_v2(series_id,feed_id,series_hash,series_json,enabled,configured_by,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(series_id) DO UPDATE SET feed_id=excluded.feed_id,series_hash=excluded."
        "series_hash,series_json=excluded.series_json,enabled=excluded.enabled,configured_by=excluded.configured_by,"
        "updated_at=excluded.updated_at",
        (series.series_id, series.feed_id, digest, dumps(series), 1 if enabled else 0, configured_by, now_ms))
    return digest


async def latest_episode_start(db: Database, *, series_id: str) -> int | None:
    row = await db.first(
        "SELECT MAX(json_extract(binding_json,'$.target_start_ms')) AS start FROM risk_feed_bindings_v2 b "
        "WHERE json_extract(binding_json,'$.series_id')=? "
        "AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id)",
        (series_id,))
    return int(row["start"]) if row and row["start"] is not None else None


def next_episode_start(series: RiskFeedSeriesV2, *, latest_start_ms: int | None, now_ms: int) -> int:
    """The start after the latest episode if it is still ahead, else the next boundary strictly after now.

    A missed start is not the next start. When every attempt at an episode failed until its start
    passed (2026-09-19: the D1 daily read limit), `latest + cadence` was an instant in the past,
    the lead window could never hold again, and the series stalled for 33 hours answering
    `episodes: []`. The episode that can still be published before it starts is the next one.
    """
    if latest_start_ms is not None and latest_start_ms + series.cadence_ms > now_ms:
        return latest_start_ms + series.cadence_ms
    boundary = -((-now_ms) // series.cadence_ms) * series.cadence_ms
    return boundary + series.cadence_ms if boundary == now_ms else boundary


def episode_question(series: RiskFeedSeriesV2, start_ms: int) -> str:
    end_ms = start_ms + series.window_ms
    question = series.question_template.format(start=spell(start_ms), end=spell(end_ms), since=start_ms // 1000,
                                               candles=series.window_ms // 300_000)
    require(len(question) <= 1000, "episode question exceeds the compiler bound")
    return question


def episode_binding(series: RiskFeedSeriesV2, card: dict[str, Any], start_ms: int, *,
                    profile_id: str, profile_version: str) -> RiskFeedBindingV2:
    window = measurement_window(str(card["question"]))
    require(window is not None, "published episode lacks its measurement interval")
    assert window is not None
    horizon = series.policy_horizon_ms if series.mapping_kind == "containing_upper_estimate" else 0
    return RiskFeedBindingV2(
        binding_id=f"{series.series_id}-{spell(start_ms)}", forecast_id=str(card["id"]),
        specification_hash=str(card["specificationHash"]), channel=series.channel, asset=series.asset,
        category=CATEGORY_BY_CHANNEL[series.channel], series_id=series.series_id, episode_id=spell(start_ms),
        target_start_ms=start_ms, target_end_ms=start_ms + series.window_ms,
        policy_horizon_ms=series.policy_horizon_ms, definition_hash=series.definition_hash,
        mapping_profile_id=profile_id, mapping_profile_version=profile_version,
        mapping_profile_hash=series.mapping_profile_hash,
        mapping_kind=series.mapping_kind,
        question_event_definition_hash=content_hash({"specification_hash": card["specificationHash"],
                                                     "window": window["canonical_expression"]}),
        approval_artifact_hash=content_hash({"series": content_hash(series), "forecast_id": card["id"]}),
        authorization_valid_from_ms=int(card["openAt"]), authorization_valid_until_ms=start_ms + series.window_ms,
        operational_valid_from_ms=start_ms, operational_valid_until_ms=start_ms + series.window_ms - horizon)


async def create_due_episodes(
    db: Database, *, now_ms: int, seed: Callable[[str], Awaitable[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Publish and bind every episode whose lead window has opened; one attempt per series per tick."""
    outcomes = []
    for row in await db.all("SELECT * FROM risk_feed_series_v2 WHERE enabled=1 ORDER BY series_id LIMIT 16"):
        series = loads(RiskFeedSeriesV2, row["series_json"])
        latest = await latest_episode_start(db, series_id=series.series_id)
        start = next_episode_start(series, latest_start_ms=latest, now_ms=now_ms)
        outcome: dict[str, Any] = {"seriesId": series.series_id, "nextStartMs": start, "created": None}
        if not start - series.lead_ms <= now_ms < start:
            outcomes.append(outcome)
            continue
        last = await db.first("SELECT attempted_at,outcome FROM risk_feed_series_log_v2 WHERE series_id=? AND target_start_ms=? "
                              "ORDER BY attempted_at DESC LIMIT 1", (series.series_id, start))
        if last is not None and str(last["outcome"]).startswith("failed") and now_ms - int(last["attempted_at"]) < RETRY_MS:
            outcome["backoffUntilMs"] = int(last["attempted_at"]) + RETRY_MS  # compile failures cost AI budget; pace retries
            outcomes.append(outcome)
            continue
        try:
            profile = await db.first("SELECT profile_json FROM risk_feed_profiles_v2 WHERE profile_hash=?",
                                     (series.mapping_profile_hash,))
            require(profile is not None, "series profile no longer admitted")
            assert profile is not None
            decoded = json.loads(profile["profile_json"])
            card = (await seed(episode_question(series, start)))["forecast"]
            binding = episode_binding(series, card, start, profile_id=decoded["profile_id"],
                                      profile_version=decoded["profile_version"])
            existing = await db.first("SELECT binding_id FROM risk_feed_bindings_v2 WHERE binding_id=?",
                                      (binding.binding_id,))
            if existing is None:
                await approve_binding_v2(db, feed_id=series.feed_id, binding=binding,
                                         approved_by="series:" + series.series_id, now_ms=now_ms)
            outcome.update(created=binding.binding_id, forecastId=card["id"])
            detail = "published"
        except Exception as exc:  # compiler review, budget or approval failure: retry next tick
            # The class name alone made this log undiagnosable: six failed attempts for one
            # series read as "ValidationError" with nothing to act on. The stable code names
            # the cause, the type separates a refusal from a crash, and the message is kept
            # for the operator rather than the user.
            reason = str(getattr(exc, "code", "") or type(exc).__name__).splitlines()[0][:120]
            outcome["failure"] = reason
            outcome["failureType"] = type(exc).__name__
            outcome["failureDetail"] = " ".join(str(exc).split())[:400]
            detail = "failed:" + reason
        await db.execute("INSERT OR IGNORE INTO risk_feed_series_log_v2(series_id,target_start_ms,attempted_at,outcome,detail)"
                         " VALUES(?,?,?,?,?)", (series.series_id, start, now_ms, detail, json.dumps(outcome, sort_keys=True)))
        outcomes.append(outcome)
    return outcomes
