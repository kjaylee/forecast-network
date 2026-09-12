"""Funded, buy-only LMSR persistence with isolated rehearsal accounts.

Quotes never authorize money, transferable assets, or economic redemption. The
active adapter is disabled unless its host explicitly enables it. Every balance,
market revision, safety gate and receipt is committed in one database batch.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from forecast_domain import content_hash, dumps, loads
from forecast_domain.errors import ValidationError
from forecast_domain.models import ForecastChoice, Outcome
from forecast_domain.pricing import (
    ATOMIC_UNITS_PER_POINT,
    PricingPolicy,
    PricingQuote,
    PricingReceipt,
    PricingState,
    accept_quote,
    close_market,
    initialize_market,
    market_probability_bp,
    quote_buy,
)

from .database import Database, Statement
from .errors import AppError

SCALE = ATOMIC_UNITS_PER_POINT
TREASURY_ISSUANCE_CAP = 20_000*SCALE
GLOBAL_GROSS_CAP = 10_000*SCALE
MARKET_GROSS_CAP = 2_000*SCALE
MAX_BATCH = 25


def _invalid() -> AppError:
    return AppError(400, 'market_invalid_request', 'Check the market request and try again.')


def _conflict() -> AppError:
    return AppError(409, 'market_conflict', 'The market or your balance changed. Request a fresh quote.')


def _identifier(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise _invalid()
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _guard(identifier: str, condition: str, params: tuple[Any, ...] = ()) -> Statement:
    return ('INSERT INTO market_write_guards(id,passed) SELECT ?, ( CASE WHEN '+condition+
            ' THEN 1 ELSE 0 END )', (identifier, *params))


class PointMarkets:
    """Authentication/admin authorization belongs to the host routing adapter."""

    def __init__(self, db: Database, clock: Callable[[], int], token: Callable[[], str],
                 live_enabled: bool = False):
        self.db, self.clock, self.token = db, clock, token
        self.live_enabled = live_enabled is True

    def _mode(self, mode: str) -> str:
        if mode not in ('shadow', 'active'):
            raise _invalid()
        if mode == 'active' and not self.live_enabled:
            raise AppError(403, 'market_unavailable', 'Live point markets are not enabled.')
        return mode

    @staticmethod
    def _state(row: dict[str, Any]) -> PricingState:
        state = loads(PricingState, row['state'])
        if content_hash(state) != row['state_hash'] or state.revision != row['revision'] \
                or state.specification_hash != row['specification_hash'] \
                or state.market_id != row['forecast_id'] or content_hash(state.policy) != row['policy_hash'] \
                or dumps(state.policy) != row['policy']:
            raise _conflict()
        return state

    async def _row(self, forecast_id: str) -> dict[str, Any]:
        row = await self.db.first('SELECT * FROM point_markets WHERE forecast_id=?', (_identifier(forecast_id),))
        if row is None:
            raise AppError(404, 'market_unavailable', 'This forecast does not have a funded market.')
        return row

    async def budget(self, mode: str = 'shadow') -> dict[str, Any]:
        if mode not in ('active', 'shadow'):
            raise _invalid()
        row = await self.db.first('SELECT * FROM market_treasuries WHERE mode=?', (mode,))
        if row is None:
            raise _conflict()
        reserved = await self.db.first('SELECT COALESCE(SUM(reserve_atomic),0) amount FROM point_markets WHERE mode=?', (mode,))
        return {'mode': mode, 'issuedAtomic': str(row['issued_atomic']),
                'availableAtomic': str(row['available_atomic']), 'reservedAtomic': str(reserved['amount'] if reserved else 0),
                'issuanceCapAtomic': str(TREASURY_ISSUANCE_CAP), 'liveEnabled': self.live_enabled}

    async def fund_treasury(self, amount: int, request_key: str, mode: str = 'shadow') -> dict[str, Any]:
        """Explicit administrative grant; never automatic or purchasable."""
        self._mode(mode)
        _identifier(request_key)
        if type(amount) is not int or not 1 <= amount <= 20_000:
            raise _invalid()
        identity = 'fund:'+_hash([mode, request_key])
        old = await self.db.first('SELECT * FROM market_funding WHERE id=?', (identity,))
        if old:
            if old['amount_atomic'] != amount*SCALE or old['mode'] != mode:
                raise _conflict()
            return await self.budget(mode)
        guard = self.token()
        try:
            await self.db.batch([
                _guard(guard, 'EXISTS(SELECT 1 FROM market_treasuries WHERE mode=? AND issued_atomic+?<=?)',
                       (mode, amount*SCALE, TREASURY_ISSUANCE_CAP)),
                ('INSERT INTO market_funding(id,mode,amount_atomic,created_at) VALUES(?,?,?,?)',
                 (identity, mode, amount*SCALE, self.clock())),
                ('UPDATE market_treasuries SET issued_atomic=issued_atomic+?,available_atomic=available_atomic+?,revision=revision+1 WHERE mode=?',
                 (amount*SCALE, amount*SCALE, mode)),
                ('DELETE FROM market_write_guards WHERE id=?', (guard,)),
            ])
        except Exception as exc:
            old = await self.db.first('SELECT * FROM market_funding WHERE id=?', (identity,))
            if old and old['amount_atomic'] == amount*SCALE and old['mode'] == mode:
                return await self.budget(mode)
            raise _conflict() from exc
        return await self.budget(mode)

    async def create(self, forecast_id: str, policy: PricingPolicy | None = None, mode: str = 'shadow',
                     expected_specification_hash: str | None = None) -> dict[str, Any]:
        self._mode(mode)
        _identifier(forecast_id)
        policy = policy or PricingPolicy()
        if type(policy) is not PricingPolicy or type(expected_specification_hash) is not str \
                or len(expected_specification_hash) != 64 or any(c not in '0123456789abcdef' for c in expected_specification_hash):
            raise _invalid()
        policy.__post_init__()
        # Policy records support diagnostics beyond the deliberately small pilot.
        if policy.maximum_fill_atomic > 100*SCALE or policy.maximum_owner_gross_atomic > 300*SCALE \
                or policy.maximum_owner_unsettled_atomic > 1000*SCALE:
            raise _invalid()
        state = initialize_market(policy, forecast_id, expected_specification_hash)
        previous = await self.db.first('SELECT * FROM point_markets WHERE forecast_id=?', (forecast_id,))
        if previous:
            if previous['policy_hash'] != content_hash(policy) or previous['mode'] != mode \
                    or previous['specification_hash'] != expected_specification_hash:
                raise _conflict()
            return await self._view(previous)
        now, guard = self.clock(), self.token()
        try:
            await self.db.batch([
                _guard(guard, "EXISTS(SELECT 1 FROM forecasts WHERE id=? AND specification_hash=? AND state='OPEN' AND open_at<=? AND close_at>?) "
                       "AND (?='shadow' OR NOT EXISTS(SELECT 1 FROM point_positions WHERE forecast_id=? AND amount>0)) "
                       'AND EXISTS(SELECT 1 FROM market_treasuries WHERE mode=? AND available_atomic>=?) '
                       "AND (SELECT COUNT(*) FROM point_markets WHERE mode=? AND status!='settled')<20 "
                       'AND (SELECT COUNT(*) FROM point_markets WHERE mode=? AND created_at>=?)<5',
                       (forecast_id, expected_specification_hash, now, now, mode, forecast_id, mode, policy.subsidy_atomic, mode, mode, now-now%86_400_000)),
                ('INSERT INTO point_markets(forecast_id,mode,specification_hash,policy_hash,policy,state,state_hash,revision,reserve_atomic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                 (forecast_id, mode, expected_specification_hash, content_hash(policy), dumps(policy), dumps(state), content_hash(state), 0, policy.subsidy_atomic, now)),
                ('UPDATE market_treasuries SET available_atomic=available_atomic-?,revision=revision+1 WHERE mode=?', (policy.subsidy_atomic, mode)),
                ('DELETE FROM market_write_guards WHERE id=?', (guard,)),
            ])
        except Exception as exc:
            old = await self.db.first('SELECT * FROM point_markets WHERE forecast_id=?', (forecast_id,))
            if old and old['policy_hash'] == content_hash(policy) and old['mode'] == mode \
                    and old['specification_hash'] == expected_specification_hash:
                return await self._view(old)
            raise _conflict() from exc
        return await self._view(await self._row(forecast_id))

    async def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        state = self._state(row)
        decision = await self.db.first('SELECT * FROM forecast_eligibility_decisions WHERE forecast_id=?', (row['forecast_id'],))
        probability: int | None = market_probability_bp(state)
        probability_revision: int | None = state.revision
        probability_status = 'current'
        if decision:
            probability_status = 'frozen_before_evidence'
            prefix = await self._eligible_prefix(row, decision)
            if prefix is None:
                probability, probability_revision, probability_status = None, None, 'eligibility_review'
            else:
                probability, probability_revision = market_probability_bp(prefix), prefix.revision
        voids = await self.db.first('SELECT COUNT(*) n,COALESCE(SUM(spend),0) amount FROM market_fill_voids WHERE forecast_id=?', (row['forecast_id'],))
        return {'forecastId': row['forecast_id'], 'mode': row['mode'], 'status': row['status'],
                'yesProbabilityBps': probability, 'probabilityStatus': probability_status,
                'probabilityRevision': probability_revision, 'revision': row['revision'],
                'voidedFillCount': voids['n'] if voids else 0, 'refundedPoints': voids['amount'] if voids else 0,
                'atomicScale': SCALE, 'maxSpendPoints': min(100, state.policy.maximum_fill_atomic//SCALE),
                'policyHash': row['policy_hash'], 'specificationHash': row['specification_hash'],
                'liveEnabled': self.live_enabled, 'reserveAtomic': str(row['reserve_atomic'])}

    async def _eligible_prefix(self, row: dict[str, Any], decision: dict[str, Any]) -> PricingState | None:
        """Read the unchanged receipt at the cutoff, never reprice earlier fills."""
        if decision['event_time_basis'] != 'published_instant' or decision['specification_hash'] != row['specification_hash']:
            return None
        inversion = await self.db.first('SELECT 1 FROM market_fills late JOIN market_fills early '
                                        'ON early.forecast_id=late.forecast_id AND early.revision>late.revision '
                                        'WHERE late.forecast_id=? AND late.created_at>=? AND early.created_at<?',
                                        (row['forecast_id'], decision['cutoff_at'], decision['cutoff_at']))
        if inversion:
            return None
        before = await self.db.first('SELECT * FROM market_fills WHERE forecast_id=? AND created_at<? ORDER BY revision DESC LIMIT 1',
                                    (row['forecast_id'], decision['cutoff_at']))
        if before is None:
            return initialize_market(self._state(row).policy, row['forecast_id'], row['specification_hash'])
        try:
            state = loads(PricingReceipt, before['body']).state
            count = await self.db.first('SELECT COUNT(*) n FROM market_fills WHERE forecast_id=? AND revision<=?',
                                       (row['forecast_id'], before['revision']))
            if state.market_id != row['forecast_id'] or state.specification_hash != row['specification_hash'] \
                    or content_hash(state.policy) != row['policy_hash'] or state.revision != before['revision'] \
                    or not count or count['n'] != state.revision:
                return None
            return state
        except ValidationError:
            return None

    async def get(self, forecast_id: str) -> dict[str, Any] | None:
        row = await self.db.first('SELECT * FROM point_markets WHERE forecast_id=?', (_identifier(forecast_id),))
        return await self._view(row) if row else None

    @staticmethod
    def _quote_view(quote: PricingQuote, quote_id: str | None, mode: str) -> dict[str, Any]:
        return {'quoteId': quote_id, 'forecastId': quote.market_id, 'userId': quote.owner_id,
                'mode': mode, 'side': quote.side.value, 'spendPoints': quote.spend_atomic//SCALE,
                'claimsAtomic': str(quote.claims_atomic), 'priceBeforeBps': quote.before_probability_bp,
                'priceAfterBps': quote.after_probability_bp, 'expiresAt': quote.expires_at_ms,
                'revision': quote.state_revision, 'policyHash': quote.policy_hash,
                'specificationHash': quote.specification_hash}

    @staticmethod
    def _buy_input(side: str, spend: int) -> ForecastChoice:
        if type(side) is not str or side not in ('YES', 'NO') or type(spend) is not int or not 1 <= spend <= 100:
            raise _invalid()
        return ForecastChoice(side)

    async def preview(self, forecast_id: str, side: str, spend: int) -> dict[str, Any]:
        choice = self._buy_input(side, spend)
        forecast = await self.db.first('SELECT specification_hash FROM forecasts WHERE id=?', (_identifier(forecast_id),))
        if not forecast:
            raise AppError(404, 'market_unavailable', 'Forecast not found.')
        row = await self.db.first('SELECT * FROM point_markets WHERE forecast_id=?', (forecast_id,))
        state = self._state(row) if row else initialize_market(PricingPolicy(), forecast_id, forecast['specification_hash'])
        try:
            quote = quote_buy(state, owner_id='preview', side=choice, spend_atomic=spend*SCALE, now_ms=self.clock())
        except ValidationError as exc:
            raise _conflict() from exc
        return {**self._quote_view(quote, None, 'preview'), 'nonbinding': True, 'atomicScale': SCALE}

    async def _account(self, user_id: str, mode: str) -> dict[str, Any]:
        _identifier(user_id)
        if mode == 'shadow':
            await self.db.execute('INSERT OR IGNORE INTO market_shadow_accounts(user_id,updated_at) SELECT id,? FROM users WHERE id=?',
                                  (self.clock(), user_id))
        table = 'market_shadow_accounts' if mode == 'shadow' else 'point_accounts'
        account = await self.db.first('SELECT a.*,COALESCE(f.remainder_atomic,0) fraction FROM '+table+
                                      ' a LEFT JOIN point_fractions f ON f.user_id=a.user_id AND f.mode=? WHERE a.user_id=?', (mode, user_id))
        if not account:
            raise AppError(401, 'market_unavailable', 'Sign in to use a market.')
        return account

    async def _totals(self, user_id: str, fid: str, mode: str) -> tuple[int, int]:
        row = await self.db.first('SELECT COALESCE(SUM(p.gross),0) unsettled,COALESCE(SUM( CASE WHEN p.forecast_id=? THEN p.gross ELSE 0 END ),0) gross '
                                 'FROM market_positions p JOIN point_markets m ON m.forecast_id=p.forecast_id WHERE p.user_id=? AND m.mode=? AND p.settled=0',
                                 (fid, user_id, mode))
        return (row['gross']*SCALE, row['unsettled']*SCALE) if row else (0, 0)

    async def quote(self, user_id: str, forecast_id: str, side: str, spend: int) -> dict[str, Any]:
        choice = self._buy_input(side, spend)
        row = await self._row(forecast_id)
        self._mode(row['mode'])
        account = await self._account(user_id, row['mode'])
        if account['available'] < spend:
            raise AppError(409, 'market_balance_insufficient', 'There are not enough available points.')
        state, now = self._state(row), self.clock()
        try:
            quote = quote_buy(state, owner_id=user_id, side=choice, spend_atomic=spend*SCALE, now_ms=now)
            gross, unsettled = await self._totals(user_id, forecast_id, row['mode'])
            accept_quote(state, quote, owner_id=user_id, now_ms=now, minimum_claims_atomic=quote.claims_atomic,
                         owner_gross_atomic=gross, owner_unsettled_atomic=unsettled)
        except ValidationError as exc:
            raise _conflict() from exc
        identity, guard = self.token(), self.token()
        try:
            await self.db.batch([
                self._open_guard(guard, row, now),
                ('INSERT INTO market_quotes(id,user_id,forecast_id,body,quote_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                 (identity, user_id, forecast_id, dumps(quote), content_hash(quote), now, quote.expires_at_ms)),
                ('DELETE FROM market_write_guards WHERE id=?', (guard,)),
            ])
        except Exception as exc:
            raise _conflict() from exc
        return self._quote_view(quote, identity, row['mode'])

    @staticmethod
    def _open_guard(guard: str, row: dict[str, Any], now: int) -> Statement:
        # This repeats the host's containment gates *inside* the fill transaction.
        # Shadow mode is a rehearsal, but known outcomes still stop new positions.
        return _guard(guard, "EXISTS(SELECT 1 FROM point_markets m JOIN forecasts f ON f.id=m.forecast_id WHERE m.forecast_id=? "
                      "AND m.status='open' AND m.revision=? AND m.state_hash=? AND m.policy_hash=? AND f.specification_hash=m.specification_hash "
                      "AND f.state='OPEN' AND f.open_at<=? AND f.close_at>?) "
                      'AND NOT EXISTS(SELECT 1 FROM active_participation_holds WHERE forecast_id=?) '
                      "AND NOT EXISTS(SELECT 1 FROM official_source_reviews WHERE forecast_id=? AND specification_hash=? AND state!='complete') "
                      "AND (?='shadow' OR (EXISTS(SELECT 1 FROM official_watch_bindings WHERE forecast_id=?) "
                      'AND NOT EXISTS(SELECT 1 FROM official_watch_bindings b JOIN official_watch_sources s ON (s.id=b.source_id OR (s.parent_id=b.source_id AND s.enabled=1)) '
                      'WHERE b.forecast_id=? AND (s.enabled!=1 OR s.failure_count>0 OR s.checked_at IS NULL '
                      'OR s.lease_until>? OR s.checked_at<?-s.interval_ms-60000))))',
                      (row['forecast_id'], row['revision'], row['state_hash'], row['policy_hash'], now, now,
                       row['forecast_id'], row['forecast_id'], row['specification_hash'], row['mode'],
                       row['forecast_id'], row['forecast_id'], now, now))

    async def _receipt_view(self, row: dict[str, Any], mode: str) -> dict[str, Any]:
        receipt = loads(PricingReceipt, row['body'])
        void = await self.db.first('SELECT spend,created_at,decision_id FROM market_fill_voids WHERE fill_id=?', (row['id'],))
        return {**self._quote_view(receipt.fill.quote, row['quote_id'], mode), 'id': row['id'],
                'status': 'void' if void else 'accepted', 'acceptedAt': receipt.fill.accepted_at_ms,
                'refundedPoints': void['spend'] if void else 0,
                'voidedAt': void['created_at'] if void else None,
                'eligibilityDecisionId': void['decision_id'] if void else None}

    async def receipt_status(self, user_id: str, forecast_id: str, quote_id: str,
                             idempotency_key: str) -> dict[str, Any]:
        """Reconcile an uncertain response without submitting or retrying a buy.

        Absence is definitive only when the same database read sees a persisted
        cutoff: its insert trigger prevents an in-flight buy from landing later.
        An open market can only report pending when no receipt is visible yet.
        """
        for value in (user_id, forecast_id, quote_id, idempotency_key):
            _identifier(value)
        row = await self.db.first(
            'SELECT f.*,q.id owned_quote_id,q.forecast_id owned_forecast_id,m.mode,qf.id quoted_fill_id,d.id cutoff_decision_id '
            'FROM market_quotes q JOIN point_markets m ON m.forecast_id=q.forecast_id '
            'LEFT JOIN market_fills f ON f.user_id=q.user_id AND f.idempotency_key=? '
            'LEFT JOIN market_fills qf ON qf.quote_id=q.id '
            'LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=q.forecast_id '
            'WHERE q.id=? AND q.user_id=? AND q.forecast_id=?',
            (idempotency_key, quote_id, user_id, forecast_id))
        if row is None:
            raise AppError(404, 'market_unavailable', 'Market receipt not found.')
        if row['id'] is not None:
            if row['quote_id'] != quote_id or row['forecast_id'] != forecast_id:
                raise _conflict()
            receipt = await self._receipt_view(row, row['mode'])
            return {'forecastId': forecast_id, 'userId': user_id, 'quoteId': quote_id,
                    'status': receipt['status'], 'receipt': receipt}
        if row['quoted_fill_id'] is not None:
            raise _conflict()
        return {'forecastId': forecast_id, 'userId': user_id, 'quoteId': quote_id,
                'status': 'not_accepted' if row['cutoff_decision_id'] is not None else 'pending', 'receipt': None}

    async def accept(self, user_id: str, forecast_id: str, quote_id: str, min_claims_atomic: int,
                     idempotency_key: str) -> dict[str, Any]:
        for value in (user_id, forecast_id, quote_id, idempotency_key):
            _identifier(value)
        if type(min_claims_atomic) is not int or not 0 <= min_claims_atomic <= 9_007_199_254_740_991:
            raise _invalid()
        request_hash = _hash([user_id, forecast_id, quote_id, min_claims_atomic])
        # Accepted receipts remain retrievable after expiry, closure or a kill switch.
        old = await self.db.first('SELECT f.*,m.mode FROM market_fills f JOIN point_markets m ON m.forecast_id=f.forecast_id '
                                  'WHERE f.user_id=? AND f.idempotency_key=?', (user_id, idempotency_key))
        if old:
            if old['request_hash'] != request_hash:
                raise _conflict()
            return await self._receipt_view(old, old['mode'])
        row = await self._row(forecast_id)
        self._mode(row['mode'])
        issued = await self.db.first('SELECT * FROM market_quotes WHERE id=? AND user_id=? AND forecast_id=?',
                                     (quote_id, user_id, forecast_id))
        if not issued:
            raise _conflict()
        quote = loads(PricingQuote, issued['body'])
        if content_hash(quote) != issued['quote_hash'] or quote.owner_id != user_id or quote.market_id != forecast_id:
            raise _conflict()
        account = await self._account(user_id, row['mode'])
        gross, unsettled = await self._totals(user_id, forecast_id, row['mode'])
        state, now = self._state(row), self.clock()
        try:
            receipt = accept_quote(state, quote, owner_id=user_id, now_ms=now, minimum_claims_atomic=min_claims_atomic,
                                   owner_gross_atomic=gross, owner_unsettled_atomic=unsettled)
        except ValidationError as exc:
            raise _conflict() from exc
        spend = quote.spend_atomic//SCALE
        if quote.spend_atomic % SCALE or account['available'] < spend:
            raise AppError(409, 'market_balance_insufficient', 'There are not enough available points.')
        fill_id = 'fill:'+_hash([user_id, idempotency_key])
        guard, caps = self.token(), self.token()
        policy = state.policy
        try:
            await self.db.batch([
                self._open_guard(guard, row, now),
                _guard(caps, 'COALESCE((SELECT gross FROM market_positions WHERE user_id=? AND forecast_id=?),0)*?+?<=? '
                       'AND COALESCE((SELECT SUM(p.gross)*? FROM market_positions p JOIN point_markets m ON m.forecast_id=p.forecast_id '
                       'WHERE p.user_id=? AND m.mode=? AND p.settled=0),0)+?<=? '
                       'AND COALESCE((SELECT SUM(p.gross)*? FROM market_positions p JOIN point_markets m ON m.forecast_id=p.forecast_id '
                       'WHERE m.mode=? AND p.settled=0),0)+?<=? AND (SELECT gross_atomic FROM point_markets WHERE forecast_id=?)+?<=?',
                       (user_id, forecast_id, SCALE, quote.spend_atomic, policy.maximum_owner_gross_atomic,
                        SCALE, user_id, row['mode'], quote.spend_atomic, policy.maximum_owner_unsettled_atomic,
                        SCALE, row['mode'], quote.spend_atomic, GLOBAL_GROSS_CAP, forecast_id, quote.spend_atomic, MARKET_GROSS_CAP)),
                ('INSERT INTO market_fills(id,quote_id,user_id,forecast_id,idempotency_key,request_hash,body,revision,side,spend,claims_atomic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                 (fill_id, quote_id, user_id, forecast_id, idempotency_key, request_hash, dumps(receipt), receipt.state.revision,
                  quote.side.value, spend, quote.claims_atomic, now)),
                self._ledger(fill_id, user_id, row, 'market_buy', -spend, spend, account, account['fraction'], now),
                ('INSERT INTO market_positions(user_id,forecast_id,gross,yes_claims_atomic,no_claims_atomic) VALUES(?,?,?,?,?) '
                 'ON CONFLICT(user_id,forecast_id) DO UPDATE SET gross=gross+excluded.gross,yes_claims_atomic=yes_claims_atomic+excluded.yes_claims_atomic,'
                 'no_claims_atomic=no_claims_atomic+excluded.no_claims_atomic',
                 (user_id, forecast_id, spend, quote.claims_atomic if quote.side is ForecastChoice.YES else 0,
                  quote.claims_atomic if quote.side is ForecastChoice.NO else 0)),
                ('UPDATE point_markets SET state=?,state_hash=?,revision=?,reserve_atomic=reserve_atomic+?,gross_atomic=gross_atomic+? WHERE forecast_id=?',
                 (dumps(receipt.state), content_hash(receipt.state), receipt.state.revision, quote.spend_atomic, quote.spend_atomic, forecast_id)),
                ('DELETE FROM market_write_guards WHERE id IN (?,?)', (guard, caps)),
            ])
        except Exception as exc:
            old = await self.db.first('SELECT * FROM market_fills WHERE user_id=? AND idempotency_key=?', (user_id, idempotency_key))
            if old and old['request_hash'] == request_hash:
                return await self._receipt_view(old, row['mode'])
            if 'eligibility_account_hold' in str(exc):
                raise AppError(409, 'point_correction_pending',
                               'A previous stake correction must finish before you can commit more points.') from exc
            raise _conflict() from exc
        result = await self.db.first('SELECT * FROM market_fills WHERE id=?', (fill_id,))
        if result is None:
            raise _conflict()
        return await self._receipt_view(result, row['mode'])

    @staticmethod
    def _ledger(identity: str, uid: str, row: dict[str, Any], kind: str, available: int, committed: int,
                account: dict[str, Any], fraction: int, now: int) -> Statement:
        return ('INSERT INTO market_account_ledger(id,user_id,mode,forecast_id,kind,available_delta,committed_delta,'
                'available_after,committed_after,fraction_before,fraction_after,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity, uid, row['mode'], row['forecast_id'], kind, available, committed,
                 account['available']+available, account['committed']+committed, account['fraction'], fraction, now))

    async def positions(self, user_id: str, forecast_id: str) -> dict[str, Any]:
        row = await self._row(forecast_id)
        account = await self._account(user_id, row['mode'])
        position = await self.db.first('SELECT * FROM market_positions WHERE user_id=? AND forecast_id=?', (user_id, forecast_id))
        voids = await self.db.first('SELECT COUNT(*) n,COALESCE(SUM(spend),0) amount FROM market_fill_voids WHERE user_id=? AND forecast_id=?', (user_id, forecast_id))
        return {'forecastId': forecast_id, 'mode': row['mode'], 'availablePoints': account['available'],
                'committedPoints': account['committed'], 'fractionAtomic': str(account['fraction']),
                'grossPoints': position['gross'] if position else 0,
                'yesClaimsAtomic': str(position['yes_claims_atomic'] if position else 0),
                'noClaimsAtomic': str(position['no_claims_atomic'] if position else 0),
                'voidedFillCount': voids['n'] if voids else 0, 'refundedPoints': voids['amount'] if voids else 0,
                'settled': bool(position and position['settled'])}

    async def void_after_evidence(self, forecast_id: str, decision_id: str, limit: int = MAX_BATCH) -> dict[str, Any]:
        """Refund a proven late suffix without modifying any accepted receipt.

        The coordinator retains the intake/finalization hold until its separate
        completion barrier passes. Unknown earlier timing remains under review.
        """
        _identifier(forecast_id)
        _identifier(decision_id)
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise _invalid()
        decision = await self.db.first('SELECT * FROM forecast_eligibility_decisions WHERE id=? AND forecast_id=?', (decision_id, forecast_id))
        if not decision:
            raise _conflict()
        row = await self.db.first('SELECT * FROM point_markets WHERE forecast_id=?', (forecast_id,))
        if not row:
            return {'status': 'completed', 'processed': 0, 'remaining': 0}
        try:
            self._state(row)
        except (ValidationError, AppError):
            return {'status': 'review', 'processed': 0, 'reason': 'market_reconciliation'}
        anomaly = await self.db.first('SELECT 1 FROM market_eligibility_anomalies WHERE forecast_id=?', (forecast_id,))
        if anomaly:
            return {'status': 'review', 'processed': 0, 'reason': 'market_reconciliation'}
        fills = await self.db.all('SELECT * FROM market_effective_fills WHERE forecast_id=? AND created_at>=? ORDER BY revision DESC LIMIT ?',
                                 (forecast_id, decision['cutoff_at'], limit))
        processed = 0
        for fill in fills:
            account = await self._account(fill['user_id'], row['mode'])
            current = await self._row(forecast_id)
            try:
                await self.db.execute('INSERT INTO market_fill_voids(fill_id,decision_id,user_id,forecast_id,mode,spend,claims_atomic,side,'
                                      'available_before,committed_before,reserve_before_atomic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (fill['id'], decision_id, fill['user_id'], forecast_id, row['mode'], fill['spend'], fill['claims_atomic'],
                                       fill['side'], account['available'], account['committed'], current['reserve_atomic'], self.clock()))
                processed += 1
            except Exception as exc:
                # Lost acknowledgements and competing coordinators cannot refund
                # twice. Other races retry against fresh authoritative balances.
                old = await self.db.first('SELECT decision_id FROM market_fill_voids WHERE fill_id=?', (fill['id'],))
                if old and old['decision_id'] == decision_id:
                    continue
                anomaly = await self.db.first('SELECT 1 FROM market_eligibility_anomalies WHERE forecast_id=?', (forecast_id,))
                if anomaly:
                    return {'status': 'review', 'processed': processed, 'reason': 'market_reconciliation'}
                raise _conflict() from exc
        remaining = await self.db.first('SELECT COUNT(*) n FROM market_effective_fills WHERE forecast_id=? AND created_at>=?',
                                        (forecast_id, decision['cutoff_at']))
        ambiguous = await self.db.first('SELECT 1 FROM market_effective_fills WHERE forecast_id=? AND created_at<?',
                                        (forecast_id, decision['cutoff_at'])) if decision['event_time_basis'] == 'observed_upper_bound' else None
        return {'status': 'pending' if remaining and remaining['n'] else 'review' if ambiguous else 'completed',
                'processed': processed, 'remaining': remaining['n'] if remaining else 0,
                'reason': 'publication_time_uncertain' if ambiguous else None}

    async def settle(self, forecast_id: str, limit: int = MAX_BATCH) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise _invalid()
        row = await self._row(forecast_id)
        # A disabled buy switch must not strand already accepted active liabilities.
        final = await self.db.first("SELECT * FROM forecasts f WHERE f.id=? AND f.state IN ('FINALIZED','ARCHIVED') "
                                    "AND f.finalized_outcome IN ('YES','NO','INVALID') AND EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=f.id "
                                    "AND json_extract(e.event,'$.command_name')='finalize' AND e.created_at<=?)", (forecast_id, self.clock()))
        if not final or final['specification_hash'] != row['specification_hash']:
            raise _conflict()
        if row['status'] == 'settled':
            return {**(await self._view(row)), 'processed': 0, 'remaining': 0}
        state = close_market(self._state(row), Outcome(final['finalized_outcome']))
        people = await self.db.all('SELECT * FROM market_positions WHERE forecast_id=? AND settled=0 ORDER BY user_id LIMIT ?',
                                  (forecast_id, limit))
        now, guard = self.clock(), self.token()
        statements: list[Statement] = [
            _guard(guard, "EXISTS(SELECT 1 FROM point_markets m JOIN forecasts f ON f.id=m.forecast_id WHERE m.forecast_id=? "
                   "AND m.revision=? AND m.state_hash=? AND m.status!='settled' AND f.specification_hash=m.specification_hash "
                   "AND f.state IN ('FINALIZED','ARCHIVED') AND f.finalized_outcome=? "
                   "AND EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=f.id AND json_extract(e.event,'$.command_name')='finalize' AND e.created_at<=?))",
                   (forecast_id, row['revision'], row['state_hash'], final['finalized_outcome'], now)),
            ("UPDATE point_markets SET state=?,state_hash=?,status='settling',final_outcome=?,closed_at=COALESCE(closed_at,?) WHERE forecast_id=?",
             (dumps(state), content_hash(state), final['finalized_outcome'], now, forecast_id)),
        ]
        payout_total = 0
        for position in people:
            uid = position['user_id']
            payout = position['gross']*SCALE if state.closed_outcome is Outcome.INVALID else \
                position['yes_claims_atomic'] if state.closed_outcome is Outcome.YES else position['no_claims_atomic']
            payout_total += payout
            account = await self._account(uid, row['mode'])
            whole, fraction = divmod(payout+account['fraction'], SCALE)
            identity = 'market-settlement:'+_hash([forecast_id, uid])
            statements.extend([
                ('INSERT INTO market_settlements(id,user_id,forecast_id,outcome,gross,payout_atomic,created_at) VALUES(?,?,?,?,?,?,?)',
                 (identity, uid, forecast_id, final['finalized_outcome'], position['gross'], payout, now)),
                self._ledger(identity, uid, row, 'market_settlement', whole, -position['gross'], account, fraction, now),
                ('UPDATE market_positions SET settled=1 WHERE user_id=? AND forecast_id=? AND settled=0', (uid, forecast_id)),
            ])
        statements.extend([
            ('UPDATE point_markets SET reserve_atomic=reserve_atomic-? WHERE forecast_id=?', (payout_total, forecast_id)),
            ('INSERT INTO market_closures(forecast_id,returned_atomic,created_at) SELECT forecast_id,reserve_atomic,? FROM point_markets m '
             'WHERE forecast_id=? AND NOT EXISTS(SELECT 1 FROM market_positions WHERE forecast_id=m.forecast_id AND settled=0)', (now, forecast_id)),
            ('UPDATE market_treasuries SET available_atomic=available_atomic+COALESCE((SELECT returned_atomic FROM market_closures WHERE forecast_id=?),0),'
             'revision=revision+1 WHERE mode=? AND EXISTS(SELECT 1 FROM market_closures WHERE forecast_id=?)', (forecast_id, row['mode'], forecast_id)),
            ("UPDATE point_markets SET status='settled',reserve_atomic=0 WHERE forecast_id=? AND EXISTS(SELECT 1 FROM market_closures WHERE forecast_id=?)", (forecast_id, forecast_id)),
            ('DELETE FROM market_write_guards WHERE id=?', (guard,)),
        ])
        try:
            await self.db.batch(statements)
        except Exception as exc:
            # Concurrent batches or a lost acknowledgement are safe to replay.
            current = await self._row(forecast_id)
            completed = await self.db.first('SELECT COUNT(*) n FROM market_positions WHERE forecast_id=? AND settled=0', (forecast_id,))
            accepted = [await self.db.first('SELECT 1 FROM market_settlements WHERE forecast_id=? AND user_id=?',
                                            (forecast_id, p['user_id'])) for p in people]
            if current['status'] == 'settled' or (people and all(accepted)):
                return {**(await self._view(current)), 'processed': 0, 'remaining': completed['n'] if completed else 0}
            raise _conflict() from exc
        remaining = await self.db.first('SELECT COUNT(*) n FROM market_positions WHERE forecast_id=? AND settled=0', (forecast_id,))
        return {**(await self._view(await self._row(forecast_id))), 'processed': len(people), 'remaining': remaining['n'] if remaining else 0}
