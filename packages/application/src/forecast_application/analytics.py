"""Read-only, event-backed product KPIs with explicit UTC denominators and maturity.

No wall clock, click collection, provider price inference or financial conversion.
The adapter takes one consistent D1/SQLite batch; pure aggregation is replayable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Literal

from forecast_domain.records import MAX_SAFE_INTEGER
from forecast_domain.serialization import content_hash

from .database import Database, Statement

VERSION = "product-analytics-v1"
DAY_MS = 86_400_000
MAX_ROWS = 200_000
PopulationKind = Literal["application", "fixture", "isolated-load"]
EXCLUDED_PREFIXES = ("staff:", "staff_", "test:", "test_", "load:", "load_", "sandbox:", "fixture:", "e2e:", "smoke:")
NON_PARTICIPANT_IDS = frozenset({"system_editorial"})


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _window(start: int, end: int, as_of: int) -> None:
    _check(all(type(v) is int and 0 <= v <= MAX_SAFE_INTEGER for v in (start, end, as_of)), "invalid KPI timestamp")
    _check(start < end <= as_of, "KPI windows must be complete and end by as_of_ms")
    _check(start % DAY_MS == end % DAY_MS == 0, "KPI windows must use UTC midnight boundaries")
    _check(end - start <= 366 * DAY_MS, "KPI window exceeds 366 days")


def _ratio(numerator: int, denominator: int, *, unit: str = "share") -> dict[str, Any]:
    scaled = (numerator * 10000 + denominator // 2) // denominator if denominator else None
    out_of_range = scaled is not None and scaled > MAX_SAFE_INTEGER
    if out_of_range:
        scaled = None
    return {"numerator": numerator, "denominator": denominator, "unit": unit,
            "valueBp": scaled if unit == "share" else None, "valueScaled": scaled, "scale": 10000,
            "status": "out_of_range" if out_of_range else "available" if denominator else "no_denominator"}


# Every accepted submission remains an immutable event. Current latest-choice views
# alone would lose prior active days when a user edits a forecast on a later day.
_ELIGIBLE = """
WITH eligible AS (
 SELECT e.forecast_id,e.revision,e.hash,e.created_at,
        json_extract(a.body,'$.forecaster_id') AS user_id
 FROM events e JOIN forecasts f ON f.id=e.forecast_id
 JOIN artifacts a ON a.hash=json_extract(e.event,'$.artifact_hash')
 LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=e.forecast_id AND d.created_at<?
 LEFT JOIN forecast_receipt_eligibility r ON r.forecast_id=e.forecast_id AND r.revision=e.revision
  AND r.decision_id=d.id
 WHERE json_extract(e.event,'$.command_name')='submit_forecast' AND e.created_at<?
 AND json_extract(a.body,'$.forecast_id')=e.forecast_id
 AND json_extract(a.body,'$.specification_hash')=f.specification_hash
 AND json_extract(a.body,'$.submitted_at_ms')=e.created_at
 AND json_extract(a.body,'$.forecaster_id') IS NOT NULL
 AND (d.id IS NULL OR (r.status='eligible' AND r.submitted_at=e.created_at
  AND EXISTS(SELECT 1 FROM forecast_eligibility_completions c WHERE c.decision_id=d.id AND c.created_at<?)))
 AND NOT EXISTS(SELECT 1 FROM participation_hold_events h WHERE h.forecast_id=f.id AND h.action='hold'
  AND h.created_at<? AND NOT EXISTS(SELECT 1 FROM participation_hold_events later WHERE later.forecast_id=h.forecast_id
    AND later.revision>h.revision AND later.created_at<?))
)
"""


async def product_analytics(
    db: Database, *, as_of_ms: int, window_start_ms: int, window_end_ms: int,
    cohort_start_ms: int | None = None, cohort_end_ms: int | None = None,
    population_kind: PopulationKind = "application", excluded_user_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Single read batch; root may expose this only through its authenticated admin API.

    Cost receipts and identity/exclusion admissions are separate operator writes.
    This function never writes or assumes an absent fee receipt means zero cost.
    """
    cohort_start = window_start_ms if cohort_start_ms is None else cohort_start_ms
    cohort_end = window_end_ms if cohort_end_ms is None else cohort_end_ms
    _window(window_start_ms, window_end_ms, as_of_ms)
    _window(cohort_start, cohort_end, as_of_ms)
    _check(population_kind in ("application", "fixture", "isolated-load"), "unknown KPI population")
    activity_start = min(window_start_ms, cohort_start)
    activity_end = min(as_of_ms, max(window_end_ms + 7 * DAY_MS, cohort_end + 31 * DAY_MS))
    at = (as_of_ms,) * 5
    statements: tuple[Statement, ...] = (
        ("SELECT id,created_at FROM users WHERE created_at<? ORDER BY id LIMIT ?", (as_of_ms, MAX_ROWS + 1)),
        ("SELECT f.id,f.creator_id,f.created_at,f.open_at,f.close_at,f.finalized_outcome,f.ai_forecast IS NOT NULL AS has_ai,"
         "CASE WHEN json_type(f.snapshot,'$.specification.ambiguity_score_bp')='integer' "
         "AND json_extract(f.snapshot,'$.specification.ambiguity_score_bp') BETWEEN 0 AND 10000 "
         "THEN 10000-json_extract(f.snapshot,'$.specification.ambiguity_score_bp') ELSE NULL END AS clarity_bp,"
         "EXISTS(SELECT 1 FROM participation_hold_events h WHERE h.forecast_id=f.id AND h.action='hold' AND h.created_at<? "
         "AND NOT EXISTS(SELECT 1 FROM participation_hold_events z WHERE z.forecast_id=h.forecast_id "
         "AND z.revision>h.revision AND z.created_at<?)) AS on_hold,"
         "COALESCE((SELECT json_extract(e.event,'$.new_state') FROM events e WHERE e.forecast_id=f.id AND e.created_at<? "
         "ORDER BY e.revision DESC LIMIT 1),'UNKNOWN') AS state_at,"
         "(SELECT MIN(e.created_at) FROM events e WHERE e.forecast_id=f.id AND e.created_at<? "
         "AND json_extract(e.event,'$.command_name')='finalize') AS finalized_at "
         "FROM forecasts f WHERE f.created_at<? ORDER BY f.id LIMIT ?", ((as_of_ms,) * 5 + (MAX_ROWS + 1,))),
        (_ELIGIBLE + "SELECT user_id,forecast_id,MIN(created_at) AS first_at FROM eligible "
         "GROUP BY user_id,forecast_id ORDER BY user_id,forecast_id LIMIT ?",
         (*at, MAX_ROWS + 1)),
        (_ELIGIBLE + "SELECT user_id,forecast_id,CAST(created_at/86400000 AS INTEGER)*86400000 AS day_ms,"
         "MIN(created_at) AS first_at,COUNT(*) AS submissions FROM eligible WHERE created_at>=? AND created_at<? "
         "GROUP BY user_id,forecast_id,day_ms ORDER BY user_id,forecast_id,day_ms LIMIT ?",
         (*at, activity_start, activity_end, MAX_ROWS + 1)),
        ("SELECT e.forecast_id,e.created_at,json_extract(e.event,'$.command_name') AS command_name,a.hash AS artifact_hash,"
         "json_extract(a.body,'$.disputant_id') AS user_id,json_extract(a.body,'$.dispute_hash') AS dispute_hash,"
         "json_extract(a.body,'$.material_conflict') AS material_conflict "
         "FROM events e JOIN artifacts a ON a.hash=json_extract(e.event,'$.artifact_hash') "
         "WHERE e.created_at<? AND json_extract(e.event,'$.command_name') IN ('submit_dispute','review_dispute') "
         "ORDER BY e.created_at,e.revision LIMIT ?", (as_of_ms, MAX_ROWS + 1)),
        ("SELECT * FROM product_analytics_exclusions ORDER BY subject_kind,subject_id,reason LIMIT ?", (MAX_ROWS + 1,)),
        ("SELECT alias_user_id,canonical_user_id FROM product_analytics_identity_links "
         "ORDER BY alias_user_id LIMIT ?", (MAX_ROWS + 1,)),
        ("SELECT c.* FROM product_cost_receipts c WHERE c.occurred_at>=? AND c.occurred_at<? AND c.recorded_at<? "
         "AND c.population_kind=? AND NOT EXISTS(SELECT 1 FROM product_cost_receipts later "
         "WHERE later.source_kind=c.source_kind AND later.operation_id=c.operation_id AND later.revision>c.revision "
         "AND later.recorded_at<?) ORDER BY c.source_kind,c.operation_id LIMIT ?",
         (window_start_ms, window_end_ms, as_of_ms, population_kind, as_of_ms, MAX_ROWS + 1)),
        ("SELECT forecast_id,signature,submitted_at,status FROM registry_delivery "
         "WHERE submitted_at>=? AND submitted_at<? AND signature IS NOT NULL ORDER BY signature LIMIT ?",
         (window_start_ms, window_end_ms, MAX_ROWS + 1)),
        ("SELECT COALESCE(SUM(reserved_lamports),0) AS reserved FROM registry_spend WHERE day>=? AND day<?",
         (window_start_ms // DAY_MS, window_end_ms // DAY_MS)),
    )
    results = await db.batch(statements)
    _check(len(results) == len(statements), "incomplete analytics snapshot")
    rows = [result.get("results", []) for result in results]
    _check(all(type(items) is list and len(items) <= MAX_ROWS for items in rows), "analytics snapshot exceeds row bound")
    names = ("users", "forecasts", "firsts", "activity", "disputes", "exclusions", "identities", "costs", "deliveries", "reserved")
    snapshot = dict(zip(names, rows, strict=True))
    return aggregate_product_analytics(snapshot, as_of_ms=as_of_ms, window_start_ms=window_start_ms,
                                      window_end_ms=window_end_ms, cohort_start_ms=cohort_start,
                                      cohort_end_ms=cohort_end, population_kind=population_kind,
                                      excluded_user_ids=excluded_user_ids)


def aggregate_product_analytics(
    snapshot: Mapping[str, list[dict[str, Any]]], *, as_of_ms: int, window_start_ms: int,
    window_end_ms: int, cohort_start_ms: int, cohort_end_ms: int,
    population_kind: PopulationKind = "application", excluded_user_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Pure counterpart for retained input replay. No identifiers leave the report."""
    _window(window_start_ms, window_end_ms, as_of_ms)
    _window(cohort_start_ms, cohort_end_ms, as_of_ms)
    _check(population_kind in ("application", "fixture", "isolated-load"), "unknown KPI population")
    aliases = {row["alias_user_id"]: row["canonical_user_id"] for row in snapshot["identities"]}

    def identity(value: str) -> str:
        seen: set[str] = set()
        while value in aliases:
            _check(value not in seen and len(seen) < 128, "cyclic or excessive analytics identity links")
            seen.add(value)
            value = aliases[value]
        return value

    for alias in aliases:
        identity(alias)
    users = {row["id"]: row for row in snapshot["users"]}
    excluded = set(excluded_user_ids) | NON_PARTICIPANT_IDS
    excluded.update(row["subject_id"] for row in snapshot["exclusions"] if row["subject_kind"] == "user")
    excluded.update(user for user in users if user.casefold().startswith(EXCLUDED_PREFIXES))
    # Deleting/excluding one alias excludes its merged identity, even for older windows.
    excluded = {identity(user) for user in excluded}
    excluded_forecasts = {row["subject_id"] for row in snapshot["exclusions"] if row["subject_kind"] == "forecast"}
    active_users = {identity(user) for user in users if identity(user) not in excluded and identity(user) in users}
    created: dict[str, int] = {}
    for user, row in users.items():
        canonical = identity(user)
        if canonical in active_users:
            created[canonical] = min(created.get(canonical, row["created_at"]), row["created_at"])
    forecasts = {row["id"]: row for row in snapshot["forecasts"]
                 if row["id"] not in excluded_forecasts and not row["id"].casefold().startswith(EXCLUDED_PREFIXES)}
    activity: dict[tuple[str, str, int], int] = {}
    for row in snapshot["activity"]:
        user, forecast = identity(row["user_id"]), row["forecast_id"]
        if user not in active_users or forecast not in forecasts:
            continue
        key = user, forecast, row["day_ms"]
        activity[key] = min(activity.get(key, row["first_at"]), row["first_at"])
    firsts: dict[str, int] = {}
    # First activation must be derived from allowed forecasts, not an excluded test question.
    for row in snapshot["firsts"]:
        user = identity(row["user_id"])
        if user in active_users and row["forecast_id"] in forecasts:
            firsts[user] = min(firsts.get(user, row["first_at"]), row["first_at"])
    window_activity = {(u, f, day) for u, f, day in activity if window_start_ms <= day < window_end_ms}
    predictors = {u for u, _, _ in window_activity}
    questions = {f for _, f, _ in window_activity}
    daily = []
    for day in range(window_start_ms, window_end_ms, DAY_MS):
        entries = {(u, f) for u, f, d in window_activity if d == day}
        daily.append({"dayStartMs": day, "dayEndMs": day + DAY_MS,
                      "activePredictors": len({u for u, _ in entries}),
                      "activeQuestions": len({f for _, f in entries}), "distinctPredictorQuestionDays": len(entries)})
    new_users = {u for u, timestamp in created.items() if window_start_ms <= timestamp < window_end_ms}
    mature_users = {u for u in new_users if created[u] + 7 * DAY_MS <= as_of_ms}
    activated = {u for u in mature_users if u in firsts and created[u] <= firsts[u] < created[u] + 7 * DAY_MS}
    new_questions = {f for f, row in forecasts.items() if window_start_ms <= row["created_at"] < window_end_ms}
    mature_questions = {f for f in new_questions if forecasts[f]["created_at"] + 7 * DAY_MS <= as_of_ms}
    participated = {f for (u, f, _), timestamp in activity.items() if f in mature_questions
                    and u != identity(forecasts[f]["creator_id"])
                    and forecasts[f]["created_at"] <= timestamp < forecasts[f]["created_at"] + 7 * DAY_MS}
    cohorts: list[dict[str, Any]] = []
    activity_days: dict[str, set[int]] = defaultdict(set)
    for u, _, day in activity:
        activity_days[u].add(day)
    for day in range(cohort_start_ms, cohort_end_ms, DAY_MS):
        members = {u for u, timestamp in firsts.items() if timestamp // DAY_MS * DAY_MS == day}
        retention = {}
        for offset in (1, 7, 30):
            target = day + offset * DAY_MS
            mature = target + DAY_MS <= as_of_ms
            numerator = len({u for u in members if target in activity_days[u]}) if mature else 0
            metric = _ratio(numerator, len(members))
            metric.update(mature=mature, targetStartMs=target, targetEndMs=target + DAY_MS)
            if not mature:
                metric.update(numerator=None, valueBp=None, valueScaled=None, status="immature")
            retention[f"D{offset}"] = metric
        cohorts.append({"cohortStartMs": day, "cohortEndMs": day + DAY_MS, "activatedUsers": len(members),
                        "retention": retention})
    retention_totals = {}
    for name in ("D1", "D7", "D30"):
        mature_metrics = [row["retention"][name] for row in cohorts if row["retention"][name]["mature"]]
        immature_users = sum(row["activatedUsers"] for row in cohorts if not row["retention"][name]["mature"])
        total_metric = _ratio(sum(item["numerator"] for item in mature_metrics), sum(item["denominator"] for item in mature_metrics))
        total_metric["immatureUsers"] = immature_users
        if total_metric["denominator"] == 0 and immature_users:
            total_metric["status"] = "immature"
        retention_totals[name] = total_metric
    weekly_days: dict[tuple[str, int], set[int]] = defaultdict(set)
    for u, _, day in window_activity:
        week = (day + 3 * DAY_MS) // (7 * DAY_MS) * (7 * DAY_MS) - 3 * DAY_MS
        if window_start_ms <= week and week + 7 * DAY_MS <= window_end_ms:
            weekly_days[u, week].add(day)
    finalized = {f for f, row in forecasts.items() if row["finalized_at"] is not None
                 and window_start_ms <= row["finalized_at"] < window_end_ms}
    known_finalized = {f for f in finalized if forecasts[f]["finalized_outcome"] in ("YES", "NO", "INVALID")}
    invalid = {f for f in finalized if forecasts[f]["finalized_outcome"] == "INVALID"}
    creator_quality = []
    creator_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for f in known_finalized:
        creator = identity(forecasts[f]["creator_id"])
        if creator not in active_users:
            continue
        count = creator_counts[creator]
        count[0] += 1
        count[1] += f in invalid
    for finalized_count, bad in creator_counts.values():
        creator_quality.append(_ratio(finalized_count - bad, finalized_count))
    disputes = {row["artifact_hash"]: row for row in snapshot["disputes"]
                if row["command_name"] == "submit_dispute" and row["forecast_id"] in forecasts
                and row["user_id"] in users and identity(row["user_id"]) in active_users}
    submitted_disputes = {key for key, row in disputes.items() if window_start_ms <= row["created_at"] < window_end_ms}
    reviews = {row["dispute_hash"]: row for row in snapshot["disputes"] if row["command_name"] == "review_dispute"
               and row["dispute_hash"] in disputes
               and row["forecast_id"] == disputes[row["dispute_hash"]]["forecast_id"]
               and window_start_ms <= row["created_at"] < window_end_ms}
    clarity = [forecasts[f]["clarity_bp"] for f in new_questions if forecasts[f]["clarity_bp"] is not None]
    # An operating expense does not cease to exist when its operator is excluded
    # from human engagement metrics. Population admission and question scope govern
    # expense inclusion; account deletion never retroactively erases paid costs.
    qualified_costs = [row for row in snapshot["costs"]
                       if row["population_kind"] == population_kind
                       and (row["forecast_id"] is None or row["forecast_id"] in forecasts)]
    costs = {}
    for kind, unit in (("provider", "USD_MICRO"), ("chain", "DEVNET_LAMPORT")):
        rows = [r for r in qualified_costs if r["source_kind"] == kind]
        known = sum(r["amount_atomic"] for r in rows if r["status"] == "known")
        estimated = sum(r["amount_atomic"] for r in rows if r["status"] == "estimated")
        _check(max(known, estimated) <= MAX_SAFE_INTEGER, "cost subtotal exceeds exact JSON integer range")
        costs[kind] = {"unit": unit, "knownRecordedSubtotalAtomic": known,
                       "knownOperations": sum(r["status"] == "known" for r in rows),
                       "unknownRecordedOperations": sum(r["status"] == "unknown" for r in rows),
                       "estimatedRecordedSubtotalAtomic": estimated,
                       "estimatedOperations": sum(r["status"] == "estimated" for r in rows),
                       "coverage": "partial", "completeActualTotalAtomic": None,
                       "knownSubtotalPerActivePredictor": _ratio(known, len(predictors), unit=unit + "_per_predictor"),
                       "knownSubtotalPerActiveQuestion": _ratio(known, len(questions), unit=unit + "_per_question")}
    signatures = {r["signature"] for r in snapshot["deliveries"] if r["forecast_id"] in forecasts}
    known_signatures = {r["operation_id"] for r in qualified_costs if r["source_kind"] == "chain" and r["status"] == "known"}
    costs["chain"]["deliverySignaturesWithoutActualFee"] = len(signatures - known_signatures)
    costs["chain"]["observedDeliverySignatures"] = len(signatures)
    costs["chain"]["reservedLamportsAllPopulations"] = snapshot["reserved"][0]["reserved"]
    costs["provider"]["retainedAiForecastRecordsInWindow"] = sum(forecasts[f]["has_ai"] for f in new_questions)
    costs["provider"]["historicalProviderOperationCount"] = None
    return {"formulaVersion": VERSION, "asOfMs": as_of_ms,
            "window": {"startMs": window_start_ms, "endMs": window_end_ms, "timezone": "UTC", "complete": True},
            "population": {"kind": population_kind, "evidenceClass": "application_database_observations"
                           if population_kind == "application" else "fixture_or_load_only",
                           "excludedIdentities": len(excluded & {identity(user) for user in users}),
                           "deduplicatedIdentities": len(active_users),
                           "exclusionPolicy": "explicit-permanent-rules-and-reserved-id-namespaces",
                           "unclassifiedAccounts": len(active_users), "humanIdentityVerified": False},
            "inputHash": content_hash({"snapshot": dict(snapshot), "asOfMs": as_of_ms,
                "window": [window_start_ms, window_end_ms], "cohorts": [cohort_start_ms, cohort_end_ms],
                "populationKind": population_kind, "excludedUserIds": sorted(excluded_user_ids)}),
            "activity": {"activePredictors": len(predictors), "activeQuestions": len(questions),
                         "distinctPredictorQuestionDays": len(window_activity), "daily": daily,
                         "openQuestionsAtAsOf": sum(r["state_at"] == "OPEN" and not r["on_hold"]
                                                      and r["open_at"] <= as_of_ms < r["close_at"]
                                                      for r in forecasts.values())},
            "activationWithin7Days": {**_ratio(len(activated), len(mature_users)), "newAccounts": len(new_users),
                                      "immatureAccounts": len(new_users - mature_users)},
            "creationToExternalParticipationWithin7Days": {**_ratio(len(participated), len(mature_questions)),
                "publishedQuestions": len(new_questions), "immatureQuestions": len(new_questions - mature_questions)},
            "cohortWindow": {"startMs": cohort_start_ms, "endMs": cohort_end_ms, "anchor": "first_eligible_prediction_UTC_day"},
            "cohorts": cohorts, "retention": retention_totals,
            "weeklyActiveDaysPerActiveUserWeek": _ratio(sum(map(len, weekly_days.values())), len(weekly_days), unit="days_per_active_user_week"),
            "quality": {"finalizedQuestions": len(finalized), "unavailableFinalizedOutcomes": len(finalized - known_finalized),
                        "invalidity": _ratio(len(invalid), len(known_finalized)),
                        "questionValidity": _ratio(len(known_finalized - invalid), len(known_finalized)),
                        "publishedQuestionClarity": {**_ratio(sum(clarity), 10000 * len(clarity)),
                            "measuredQuestions": len(clarity), "unavailableQuestions": len(new_questions) - len(clarity)},
                        "creatorsWithFinalizedResults": len(creator_quality), "creatorValidityRatios": sorted(creator_quality, key=lambda r: (r["denominator"], r["numerator"]))},
            "disputes": {"submitted": len(submitted_disputes), "reviewed": len(reviews),
                         "materialConflictAmongReviewed": _ratio(sum(r["material_conflict"] == 1 for r in reviews.values()), len(reviews)),
                         "disputedAmongFinalizedQuestions": _ratio(len(known_finalized & {d["forecast_id"] for d in disputes.values()}), len(known_finalized)),
                         "questionsWithDisputes": len({disputes[d]["forecast_id"] for d in submitted_disputes})},
            "costs": costs,
            "limitations": ["Later eligibility decisions, admitted identity links or deletion overlays may restate historical windows.",
                            "Identity merges require admitted account evidence; shared IPs are never merged.",
                            "No complete provider/chain expense ledger exists; known subtotals are not total cost.",
                            "Fixture retention tests do not prove real-user D30 retention."]}
