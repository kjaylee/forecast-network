"""Operator-only fresh estimates for exact approved canonical bindings."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from forecast_domain.early_resolution import loads_forecast
from forecast_domain.risk_feed import RiskFeedBinding, RiskFeedBindingV2
from forecast_domain.serialization import canonical_bytes, content_hash, loads

from .database import Statement
from .errors import AppError, conflict

if TYPE_CHECKING:
    from .service import Application


CURRENT = """
SELECT f.*,b.binding_json FROM risk_feed_bindings b JOIN forecasts f
 ON f.id=json_extract(b.binding_json,'$.forecast_id')
WHERE b.binding_id=? AND f.specification_hash=json_extract(b.binding_json,'$.specification_hash')
 AND f.state='OPEN' AND f.open_at<=? AND f.close_at>?
 AND json_extract(b.binding_json,'$.valid_from_ms')<=?
 AND json_extract(b.binding_json,'$.valid_until_ms')>?
 AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations r WHERE r.binding_id=b.binding_id)
 AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id)
 AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=f.id
  AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_completions c WHERE c.decision_id=d.id))
"""

# v2 approvals separate authorization (advance forecasting allowed) from operational use.
CURRENT_V2 = CURRENT.replace("risk_feed_bindings b", "risk_feed_bindings_v2 b").replace(
    "risk_feed_binding_revocations r", "risk_feed_binding_revocations_v2 r").replace(
    "'$.valid_from_ms'", "'$.authorization_valid_from_ms'").replace(
    "'$.valid_until_ms'", "'$.authorization_valid_until_ms'")


def _clock_statements(artifacts: Sequence[Any], *, forecast_id: str, specification_hash: str,
                      started_ms: int, completed_ms: int) -> list[Statement]:
    """Retain every clock role of one refresh; the estimate artifact itself is never edited."""
    kinds = {artifact.kind: artifact for artifact in artifacts}
    estimate, sources = kinds.get("ai-forecast"), kinds.get("risk-prediction-sources")
    if estimate is None or sources is None:
        return []
    as_of = json.loads(estimate.body)["as_of_ms"]
    clock = {"version": "risk-prediction-clock-v1", "specification_hash": specification_hash,
             "estimate_artifact_hash": estimate.content_hash, "source_bundle_hash": sources.content_hash,
             "forecast_as_of_ms": as_of, "information_cutoff_ms": as_of,
             "source_capture_started_at_ms": started_ms, "source_capture_completed_at_ms": as_of,
             "evaluation_started_at_ms": as_of, "evaluation_completed_at_ms": completed_ms,
             "source_watermark_ms": None}
    digest = content_hash(clock)
    return [("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
             (digest, "risk-prediction-clock", canonical_bytes(clock).decode("utf-8"), "application/json",
              completed_ms)),
            ("INSERT OR IGNORE INTO risk_prediction_clocks_v2(estimate_artifact_hash,forecast_id,clock_artifact_hash,"
             "recorded_at) VALUES(?,?,?,?)", (estimate.content_hash, forecast_id, digest, completed_ms))]


async def refresh_bound_prediction(app: Application, binding_id: str) -> dict[str, Any]:
    """The HTTP caller authenticates; budget, lease and guarded persistence live here."""
    return await _refresh(app, binding_id, current=CURRENT, record=RiskFeedBinding)


async def refresh_bound_prediction_v2(app: Application, binding_id: str) -> dict[str, Any]:
    return await _refresh(app, binding_id, current=CURRENT_V2, record=RiskFeedBindingV2)


async def _refresh(app: Application, binding_id: str, *, current: str,
                   record: type[RiskFeedBinding] | type[RiskFeedBindingV2]) -> dict[str, Any]:
    started = app.now_ms()
    row = await app.db.first(current, (binding_id, started, started, started, started))
    if row is None:
        raise AppError(409, "risk_binding_not_current", "The approved risk question is not available for refresh.")
    binding = loads(record, row["binding_json"])
    forecast = loads_forecast(row["snapshot"])
    owner = "risk-prediction:" + binding.forecast_id
    lease = await app._ai_lease(owner)
    try:
        result = await app._bounded_ai(app.ai.refresh_prediction(forecast.specification, started, app.now_ms))
        now = app.now_ms()
        estimate = result.ai_forecast
        previous = json.loads(row["ai_forecast"]) if row["ai_forecast"] else None
        if (estimate.get("specificationHash") != binding.specification_hash
                or type(estimate.get("asOf")) is not int
                or not started <= estimate["asOf"] <= now
                or (previous is not None and estimate["asOf"] <= previous["asOf"])):
            raise conflict()
        if await app.db.first(current, (binding_id, now, now, now, now)) is None:
            raise conflict()
        serialized = json.dumps(estimate, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        guard = "risk-refresh:" + lease
        condition = "SELECT 1 FROM (" + current + ") f WHERE f.revision=? AND f.ai_forecast IS ? AND f.binding_json=?"
        statements = [(
            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(" + condition + ") "
            "AND EXISTS(SELECT 1 FROM ai_leases WHERE owner=? AND token=? AND expires_at>?) THEN 1 ELSE 0 END",
            (guard, binding_id, now, now, now, now, row["revision"], row["ai_forecast"], row["binding_json"],
             owner, lease, now),
        ), *app._artifact_sql(result.artifacts),
            *_clock_statements(result.artifacts, forecast_id=binding.forecast_id,
                               specification_hash=binding.specification_hash, started_ms=started, completed_ms=now),
            ("UPDATE forecasts SET ai_forecast=? WHERE id=?", (serialized, binding.forecast_id)),
            ("DELETE FROM mutation_guards WHERE token=?", (guard,))]
        await app.db.batch(statements)
        return {"status": "refreshed", "bindingId": binding_id, "forecastId": binding.forecast_id,
                "aiForecast": estimate}
    except AppError:
        raise
    except Exception as exc:
        await app._retain_rejected(exc)
        if "mutation_guards" in str(exc) or "valid=1" in str(exc):
            raise conflict() from exc
        raise app._ai_error(exc) from exc
    finally:
        await app._release_ai(owner, lease)
