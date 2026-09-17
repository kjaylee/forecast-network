"""Versioned, cutoff-aware quality projections over explicitly eligible records.

These functions perform no queries. Adapters must supply eligibility at the cutoff,
not infer historical eligibility from a current materialized view.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from fractions import Fraction
from typing import Any

DAY_MS = 86_400_000
QUALITY_VERSION = "forecast-quality-v2"
EXPERT_WINDOW_MS = 365 * DAY_MS
CONSISTENCY_WINDOW_MS = 30 * DAY_MS
EXPERT_MIN_SAMPLES = 20
EXPERT_MIN_DAYS = 3
CONSISTENCY_MIN_PER_WINDOW = 5


def _integer(value: Any, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Invalid {name}")
    return value


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"Invalid {name}")
    return value


def eligible_history(
    rows: Iterable[Mapping[str, Any]], *, as_of_ms: int,
    window_ms: int | None = None, exclude_forecast_id: str | None = None,
) -> list[dict[str, Any]]:
    """Export safe scoring history for display and signed forecasting feeds.

    Required joined SQL columns: user_id, forecast_id, category, probability
    (integer YES percent), outcome, finalized_outcome, state, submitted_at,
    finalized_at, eligibility_at, eligible. Missing evidence fails closed. The
    adapter's eligible flag must reflect all holds/decisions at as_of_ms.
    """
    _integer(as_of_ms, "as_of_ms")
    if window_ms is not None:
        _integer(window_ms, "window_ms", 1)
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if (type(row.get("eligible")) not in (bool, int) or row.get("eligible") != 1) or row.get("state") not in ("FINALIZED", "ARCHIVED"):
            continue
        if row.get("outcome") not in ("YES", "NO") or row.get("finalized_outcome") != row.get("outcome"):
            continue
        times = [row.get(name) for name in ("submitted_at", "finalized_at", "eligibility_at")]
        if any(type(value) is not int or not 0 <= value <= as_of_ms for value in times):
            continue
        submitted, finalized, eligibility = (_integer(value, "history timestamp") for value in times)
        if submitted > finalized or eligibility < finalized:
            continue
        if window_ms is not None and finalized < as_of_ms - window_ms:
            continue
        if row.get("forecast_id") == exclude_forecast_id:
            continue
        probability = row.get("probability")
        if type(probability) is not int or not 0 <= probability <= 100:
            continue
        user = _identifier(row.get("user_id"), "user_id")
        forecast = _identifier(row.get("forecast_id"), "forecast_id")
        category = _identifier(row.get("category"), "category").lower()
        key = (user, forecast)
        if key in seen:
            raise ValueError("Duplicate scoring history: one eligible result per user and forecast")
        seen.add(key)
        result.append({"userId": user, "forecastId": forecast, "category": category,
                       "probabilityBp": probability * 100, "outcome": row["outcome"],
                       "submittedAt": submitted, "finalizedAt": finalized,
                       "eligibilityAt": eligibility})
    return sorted(result, key=lambda row: (row["finalizedAt"], row["forecastId"], row["userId"]))


def _metrics(history: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(history)
    if not count:
        return {"count": 0, "brierScore": None, "calibrationScore": None}
    loss = 0
    bins: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for row in history:
        probability = row["probabilityBp"]
        actual = 10000 if row["outcome"] == "YES" else 0
        loss += (probability - actual) ** 2
        bucket = bins[min(9, probability // 1000)]
        bucket[0] += probability
        bucket[1] += actual
    return {"count": count, "brierScore": float(Fraction(loss, 100_000_000 * count)),
            "calibrationScore": float(1 - Fraction(sum(abs(p - y) for p, y in bins.values()), 10000 * count))}


def _qualification(history: list[dict[str, Any]], category: str, as_of_ms: int) -> dict[str, Any]:
    sample = [row for row in history if row["category"] == category
              and row["finalizedAt"] >= as_of_ms - EXPERT_WINDOW_MS]
    metrics = _metrics(sample)
    days = len({row["finalizedAt"] // DAY_MS for row in sample})
    enough = metrics["count"] >= EXPERT_MIN_SAMPLES and days >= EXPERT_MIN_DAYS
    qualified = enough and metrics["brierScore"] <= 0.20 and metrics["calibrationScore"] >= 0.70
    return {"category": category, **metrics, "distinctFinalizationDays": days,
            "status": "qualified" if qualified else "not-qualified" if enough else "provisional" if sample else "new",
            "qualified": qualified, "windowStart": max(0, as_of_ms - EXPERT_WINDOW_MS),
            "minimumSamples": EXPERT_MIN_SAMPLES, "minimumDays": EXPERT_MIN_DAYS,
            "maximumBrier": 0.20, "minimumCalibration": 0.70}


def reputation_quality(
    rows: Iterable[Mapping[str, Any]], *, as_of_ms: int, category: str | None = None,
) -> dict[str, Any]:
    """One user's quality supplement; existing accuracy/dispute scores stay owned by projections."""
    history = eligible_history(rows, as_of_ms=as_of_ms)
    if len({row["userId"] for row in history}) > 1:
        raise ValueError("reputation_quality requires one user's history")
    if category is not None:
        category = _identifier(category, "category").lower()
        history = [row for row in history if row["category"] == category]
    windows = []
    for offset in (3, 2, 1):
        start = as_of_ms - offset * CONSISTENCY_WINDOW_MS
        end = start + CONSISTENCY_WINDOW_MS
        sample = [row for row in history if start <= row["finalizedAt"]
                  and (row["finalizedAt"] < end or (offset == 1 and row["finalizedAt"] == end))]
        windows.append({"start": max(0, start), "end": max(0, end), **_metrics(sample)})
    enough = all(window["count"] >= CONSISTENCY_MIN_PER_WINDOW for window in windows)
    losses = [window["brierScore"] for window in windows if window["brierScore"] is not None]
    categories = [category] if category is not None else sorted({row["category"] for row in history})
    return {"methodologyVersion": QUALITY_VERSION, "asOf": as_of_ms,
            "consistencyScore": 1 - (max(losses) - min(losses)) if enough else None,
            "consistency": {"status": "established" if enough else "provisional" if losses else "new",
                            "minimumPerWindow": CONSISTENCY_MIN_PER_WINDOW,
                            "windowDays": 30, "windows": windows,
                            "meaning": "Stability of mean Brier loss across three windows; consistency does not imply accuracy."},
            "expertise": [_qualification(history, item, as_of_ms) for item in categories],
            "eligibleHistoryCount": len(history)}


def qualified_cohorts(
    history_rows: Iterable[Mapping[str, Any]], *, forecast_id: str, category: str, as_of_ms: int,
) -> dict[str, tuple[str, ...]]:
    """Canonical application-internal member IDs; do not sum overlapping cohorts."""
    category = _identifier(category, "category").lower()
    history = eligible_history(history_rows, as_of_ms=as_of_ms, exclude_forecast_id=forecast_id)
    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in history:
        by_user[record["userId"]].append(record)
    qualified = {user for user, sample in by_user.items()
                 if _qualification(sample, category, as_of_ms)["qualified"]}
    ranked = sorted(((_metrics(sample)["brierScore"], user) for user, sample in by_user.items()
                     if len(sample) >= 10))
    top = {user for _, user in ranked[:(len(ranked) + 9) // 10]}
    return {"top": tuple(sorted(top)), "expert": tuple(sorted(qualified))}


def cohort_statistics(
    submissions: Iterable[Mapping[str, Any]], history_rows: Iterable[Mapping[str, Any]], *,
    forecast_id: str, category: str, as_of_ms: int, ai: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Independent counts/means; latest eligible participant receipt, no target-result leakage.

    Submission columns: forecast_id,user_id,yes_probability,submitted_at,revision,
    eligible,eligibility_at. AI requires probability (0..100), created_at, provider,
    model. Absence/future/invalid AI evidence yields a null probability and zero count.
    """
    category = _identifier(category, "category").lower()
    members = qualified_cohorts(history_rows, forecast_id=forecast_id, category=category, as_of_ms=as_of_ms)
    top, qualified = set(members["top"]), set(members["expert"])
    latest: dict[str, Mapping[str, Any]] = {}
    for row in submissions:
        if row.get("forecast_id") != forecast_id or (type(row.get("eligible")) not in (bool, int) or row.get("eligible") != 1):
            continue
        if any(type(row.get(name)) is not int or not 0 <= row[name] <= as_of_ms
               for name in ("submitted_at", "eligibility_at")):
            continue
        probability = row.get("yes_probability")
        if type(probability) is not int or not 0 <= probability <= 100:
            continue
        user = _identifier(row.get("user_id"), "user_id")
        revision = _integer(row.get("revision"), "revision")
        prior = latest.get(user)
        key = (row["submitted_at"], revision)
        if prior is not None and key == (prior["submitted_at"], prior["revision"]) and probability != prior["yes_probability"]:
            raise ValueError("Conflicting participant receipts")
        if prior is None or key > (prior["submitted_at"], prior["revision"]):
            latest[user] = row

    def group(users: set[str]) -> dict[str, Any]:
        values = [row["yes_probability"] for user, row in latest.items() if user in users]
        return {"probability": sum(values) / len(values) if values else None, "count": len(values)}

    ai_result: dict[str, Any] = {"probability": None, "count": 0, "provider": None, "model": None}
    if ai is not None and type(ai.get("probability")) in (int, float) and not isinstance(ai.get("probability"), bool):
        probability = ai["probability"]
        if (0 <= probability <= 100 and type(ai.get("created_at")) is int
                and 0 <= ai["created_at"] <= as_of_ms
                and type(ai.get("provider")) is str and bool(ai["provider"])
                and type(ai.get("model")) is str and bool(ai["model"])):
            ai_result = {"probability": probability, "count": 1, "provider": ai["provider"], "model": ai["model"]}
    return {"methodologyVersion": QUALITY_VERSION, "asOf": as_of_ms,
            "crowd": group(set(latest)), "top": group(top), "expert": group(qualified), "ai": ai_result,
            "qualification": {"category": category, "qualifiedUsers": len(qualified),
                              "meaning": "Demonstrated category forecasting record, not professional credentials."}}
