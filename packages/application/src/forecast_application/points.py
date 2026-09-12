"""Atomic participation-point statement builders and authenticated read models.

These are non-purchasable, non-transferable, non-redeemable product points.
Nothing here weights crowd probability, reputation, or authorizes a transaction.
Builders never execute SQL: callers append every statement to their existing
domain/wallet D1 batch. Triggers apply only newly appended immutable ledger rows.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from .database import Database, Statement
from .errors import AppError

POLICY_VERSION = "participation-points-v1"
PROFILE_GRANT = 1000
WALLET_GRANT = 500
MAX_STAKE = 1000
MAX_BALANCE = 9_007_199_254_740_991


def _identifier(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 128 or any(ord(char) < 32 for char in value):
        raise AppError(400, "invalid_points_request", "Invalid participation points request.")
    return value


def reservation_sql(user_id: str, forecast_id: str, newamount: int, outcome: str,
                    forecast_revision: int, operation_id: str, now: int) -> list[Statement]:
    """Reserve/release an explicit total stake after the caller's accepted CAS.

    forecast_revision is the new accepted revision, not the pre-command revision.
    Exact operation retries emit no changes, even after a later stake/settlement.
    Balance checks execute inside the batch, protecting different forecast races.
    """
    for value in (user_id, forecast_id, operation_id):
        _identifier(value)
    if type(newamount) is not int or not 0 <= newamount <= MAX_STAKE:
        raise AppError(400, "invalid_stake", "Use zero practice points or a whole-number stake from 1 to 1,000.")
    if type(outcome) is not str or outcome not in {"YES", "NO"}:
        raise AppError(400, "invalid_stake", "A points stake must have a YES or NO forecast choice.")
    if type(forecast_revision) is not int or not 1 <= forecast_revision <= MAX_BALANCE \
            or type(now) is not int or not 0 <= now <= MAX_BALANCE:
        raise AppError(400, "invalid_points_request", "Invalid participation points revision or timestamp.")
    identity = hashlib.sha256(json.dumps([user_id, operation_id], separators=(",", ":")).encode()).hexdigest()
    ledger_id = "reservation:"+identity
    request_hash = hashlib.sha256(json.dumps([user_id, forecast_id, newamount, outcome, forecast_revision],
                                            separators=(",", ":")).encode()).hexdigest()
    guards = [ledger_id+":"+kind for kind in ("operation", "position", "balance")]
    return [
        ("INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'operation',CASE WHEN "
         "NOT EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM point_ledger "
         "WHERE id=? AND user_id=? AND kind='reservation' AND request_hash=?) THEN 1 ELSE 0 END",
         (guards[0], ledger_id, ledger_id, user_id, request_hash)),
        ("INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'position',CASE WHEN "
         "EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM forecasts f "
         "LEFT JOIN point_positions p ON p.user_id=? AND p.forecast_id=f.id "
         "WHERE f.id=? AND f.state='OPEN' AND f.open_at<=? AND ?<f.close_at AND f.revision=? "
         "AND (p.user_id IS NULL OR (p.status IN ('practice','committed') AND p.forecast_revision<?))) "
         "THEN 1 ELSE 0 END", (guards[1], ledger_id, user_id, forecast_id, now, now, forecast_revision, forecast_revision)),
        ("INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'balance',CASE WHEN "
         "EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM point_accounts a "
         "LEFT JOIN point_positions p ON p.user_id=a.user_id AND p.forecast_id=? WHERE a.user_id=? "
         "AND a.available+COALESCE(p.amount,0)-?>=0 AND a.committed+?-COALESCE(p.amount,0)>=0) "
         "THEN 1 ELSE 0 END", (guards[2], ledger_id, forecast_id, user_id, newamount, newamount)),
        ("INSERT INTO point_ledger(id,user_id,kind,forecast_id,operation_id,request_hash,available_delta,"
         "committed_delta,available_after,committed_after,stake,outcome,forecast_revision,policy_version,created_at) "
         "SELECT ?,a.user_id,'reservation',?,?,?,COALESCE(p.amount,0)-?,?-COALESCE(p.amount,0),"
         "a.available+COALESCE(p.amount,0)-?,a.committed+?-COALESCE(p.amount,0),?,?,?,COALESCE(p.policy_version,?),? "
         "FROM point_accounts a LEFT JOIN point_positions p ON p.user_id=a.user_id AND p.forecast_id=? "
         "WHERE a.user_id=? AND NOT EXISTS(SELECT 1 FROM point_ledger WHERE id=?)",
         (ledger_id, forecast_id, operation_id, request_hash, newamount, newamount, newamount, newamount,
          newamount, outcome, forecast_revision, POLICY_VERSION, now, forecast_id, user_id, ledger_id)),
        ("DELETE FROM point_write_guards WHERE id IN (?,?,?)", tuple(guards)),
    ]


def settlement_sql(forecast_id: str, now: int) -> list[Statement]:
    """Set-based exact-once settlement inside the caller's existing atomic batch.

    Each newly inserted ledger row atomically credits that account and changes
    its position from committed to settled. A replay selects no committed rows;
    it never aggregates previous settlement rows or mutates an applied flag.
    """
    _identifier(forecast_id)
    if type(now) is not int or not 0 <= now <= MAX_BALANCE:
        raise AppError(400, "invalid_points_request", "Invalid points settlement timestamp.")
    return [(
        "INSERT INTO point_ledger(id,user_id,kind,forecast_id,available_delta,committed_delta,"
        "available_after,committed_after,stake,returned,outcome,resolved_outcome,forecast_revision,policy_version,created_at) "
        "SELECT 'settlement:'||length(p.forecast_id)||':'||p.forecast_id||':'||p.user_id,p.user_id,'settlement',p.forecast_id,"
        "CASE WHEN f.finalized_outcome='INVALID' THEN p.amount*policy.invalid_return_multiplier "
        "WHEN f.finalized_outcome=p.outcome THEN p.amount*policy.win_return_multiplier ELSE 0 END,-p.amount,"
        "a.available+CASE WHEN f.finalized_outcome='INVALID' THEN p.amount*policy.invalid_return_multiplier "
        "WHEN f.finalized_outcome=p.outcome THEN p.amount*policy.win_return_multiplier ELSE 0 END,"
        "a.committed-p.amount,p.amount,CASE WHEN f.finalized_outcome='INVALID' THEN p.amount*policy.invalid_return_multiplier "
        "WHEN f.finalized_outcome=p.outcome THEN p.amount*policy.win_return_multiplier ELSE 0 END,"
        "p.outcome,f.finalized_outcome,p.forecast_revision,p.policy_version,? "
        "FROM point_positions p JOIN forecasts f ON f.id=p.forecast_id JOIN point_accounts a ON a.user_id=p.user_id "
        "JOIN point_policies policy ON policy.version=p.policy_version "
        "WHERE p.forecast_id=? AND p.status='committed' AND f.state IN ('FINALIZED','ARCHIVED') "
        "AND f.finalized_outcome IN ('YES','NO','INVALID') "
        "AND EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=f.id AND json_extract(e.event,'$.command_name')='finalize' AND e.created_at<=?) "
        "AND NOT EXISTS(SELECT 1 FROM point_ledger l WHERE l.id='settlement:'||length(p.forecast_id)||':'||p.forecast_id||':'||p.user_id)",
        (now, forecast_id, now))]


def _position(row: dict[str, Any] | None) -> dict[str, Any]:
    return {"amount": row["amount"] if row else 0, "status": row["status"] if row else "practice",
            "policyVersion": row["policy_version"] if row else POLICY_VERSION,
            "returned": row["returned"] if row else None, "outcome": row["outcome"] if row else None,
            "forecastRevision": row["forecast_revision"] if row else None}


class PointsService:
    """Read models; caller authentication is required and enforced by the Worker."""

    def __init__(self, db: Database):
        self.db = db

    async def position(self, user_id: str, forecast_id: str) -> dict[str, Any]:
        result = await self.positions(user_id, (forecast_id,))
        return result[forecast_id]

    async def positions(self, user_id: str, forecast_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        _identifier(user_id)
        if len(forecast_ids) > 100:
            raise AppError(400, "invalid_points_request", "Too many forecast positions were requested.")
        identifiers = tuple(dict.fromkeys(_identifier(identifier) for identifier in forecast_ids))
        if not identifiers:
            return {}
        rows = await self.db.all("SELECT * FROM point_positions WHERE user_id=? "
                                 "AND forecast_id IN (SELECT value FROM json_each(?))",
                                 (user_id, json.dumps(identifiers)))
        by_id = {row["forecast_id"]: row for row in rows}
        return {identifier: _position(by_id.get(identifier)) for identifier in identifiers}

    async def summary(self, user_id: str) -> dict[str, Any]:
        _identifier(user_id)
        # Older migration snapshots remain readable during controlled upgrades; one probe covers all optional tables.
        installed = {row["name"] for row in await self.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('market_account_ledger','point_eligibility_adjustments','market_fill_voids')")}
        markets_installed = "market_account_ledger" in installed
        eligibility_installed = "point_eligibility_adjustments" in installed
        voids_installed = "market_fill_voids" in installed
        history = ("SELECT id,kind,available_delta,committed_delta,available_after,committed_after,stake,returned,forecast_id,created_at "
                   "FROM point_ledger l WHERE l.user_id=a.user_id ")
        fraction = "0"
        if markets_installed:
            history += ("UNION ALL SELECT ml.id,ml.kind,ml.available_delta,ml.committed_delta,ml.available_after,ml.committed_after,"
                        "ABS(ml.committed_delta), ( CASE WHEN ml.kind='market_settlement' THEN ml.available_delta ELSE NULL END ) ,"
                        "ml.forecast_id,ml.created_at FROM market_account_ledger ml WHERE ml.user_id=a.user_id AND ml.mode='active' ")
            fraction = "COALESCE((SELECT remainder_atomic FROM point_fractions WHERE user_id=a.user_id AND mode='active'),0)"
        if eligibility_installed:
            history += ("UNION ALL SELECT e.id, ( CASE WHEN e.available_delta>=0 THEN 'evidence_refund' ELSE 'evidence_restore' END ) ,"
                "e.available_delta,e.committed_delta,e.available_after,e.committed_after,e.old_amount,"
                "MAX(0,e.available_delta),e.forecast_id,e.created_at FROM point_eligibility_adjustments e "
                "WHERE e.user_id=a.user_id AND e.available_delta!=0 ")
        if voids_installed:
            history += ("UNION ALL SELECT 'market-void:'||v.fill_id,'market_void_refund',v.spend,-v.spend,"
                "v.available_before+v.spend,v.committed_before-v.spend,v.spend,v.spend,v.forecast_id,v.created_at "
                "FROM market_fill_voids v WHERE v.user_id=a.user_id AND v.mode='active' ")
        history += "ORDER BY created_at DESC,id DESC LIMIT 30"
        row = await self.db.first(
            "SELECT a.*,p.profile_grant,p.wallet_grant,p.max_stake,p.win_return_multiplier,p.invalid_return_multiplier,"
            "profile.created_at AS profile_awarded_at,wallet.created_at AS wallet_awarded_at,"
            "w.address AS linked_address,EXISTS(SELECT 1 FROM point_awards old WHERE old.wallet_address=w.address) AS address_rewarded,"
            "(SELECT json_group_array(json_object('id',id,'kind',kind,'amount',available_delta,"
            "'availableDelta',available_delta,'committedDelta',committed_delta,'availableAfter',available_after,"
            "'committedAfter',committed_after,'stake',stake,'returned',returned,'forecastId',forecast_id,'at',created_at)) "
            "FROM ("+history+")) AS entries_json, "+fraction+" AS fraction_atomic "
            "FROM point_accounts a JOIN point_policies p ON p.version=? "
            "LEFT JOIN point_awards profile ON profile.user_id=a.user_id AND profile.kind='profile' "
            "LEFT JOIN point_awards wallet ON wallet.user_id=a.user_id AND wallet.kind='wallet' "
            "LEFT JOIN wallet_links w ON w.user_id=a.user_id WHERE a.user_id=?", (POLICY_VERSION, user_id))
        if row is None:
            raise AppError(401, "points_account_missing", "Sign in to view your participation points.")
        rewarded = row["wallet_awarded_at"] is not None
        linked = row["linked_address"] is not None
        eligible = not rewarded and not bool(row["address_rewarded"])
        reason = "awarded" if rewarded else "wallet_already_rewarded" if row["address_rewarded"] \
            else "eligible" if linked else "connect_wallet"
        return {
            "userId": user_id, "available": row["available"], "committed": row["committed"],
            "total": row["available"]+row["committed"], "fractionAtomic": str(row["fraction_atomic"]), "atomicScale": 1_000_000,
            "policy": {"version": POLICY_VERSION, "profileGrant": row["profile_grant"],
                "walletGrant": row["wallet_grant"], "minStake": 0, "maxStake": row["max_stake"],
                "winReturnMultiplier": row["win_return_multiplier"], "invalidReturnMultiplier": row["invalid_return_multiplier"],
                "practiceAllowed": True, "purchasable": False, "transferable": False,
                "redeemable": False, "reputationWeighted": False},
            "onboarding": {"profile": {"completed": row["profile_awarded_at"] is not None,
                "reward": row["profile_grant"], "awardedAt": row["profile_awarded_at"]},
                "wallet": {"completed": rewarded, "linked": linked, "reward": row["wallet_grant"],
                    "eligible": eligible, "reason": reason, "awardedAt": row["wallet_awarded_at"]}},
            "entries": json.loads(row["entries_json"] or "[]"),
        }
