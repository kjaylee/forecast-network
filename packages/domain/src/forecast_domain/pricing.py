"""Certified, buy-only binary LMSR research domain; not a live-market authorization.

All ledger values are safe integer atomic units. Decimal exp/ln are documented by
Python as correctly rounded (ROUND_HALF_EVEN). At fixed precision their adjacent
representable numbers enclose the exact result; basic operations round outward.
Consequently interval subtraction encloses the finite cost, even when cancelling
nearly equal costs. Charging only when the upper bound fits the exact integer
spend can under-allocate a rounding atom, but cannot undercharge a fill.

For x <= -256, exp(x) < 2**-256 < 10**-77 (e > 2). This explicit tail bound avoids
underflow and unbounded exponent work. It contributes less than 10**-61 atomic
units over our entire safe-integer range. Never replace these bounds with floats,
ambient Decimal contexts, or an arbitrary epsilon acceptance tolerance.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal

from .errors import ValidationError
from .models import BP, HASH, ID, ForecastChoice, Outcome
from .records import MAX_SAFE_INTEGER, Record
from .serialization import content_hash

ATOMIC_UNITS_PER_POINT = 1_000_000
_PRECISION = 80
_DOWN = Context(prec=_PRECISION, rounding=ROUND_FLOOR)
_UP = Context(prec=_PRECISION, rounding=ROUND_CEILING)
_NEAR = Context(prec=_PRECISION, rounding=ROUND_HALF_EVEN)
_ZERO, _ONE = Decimal(0), Decimal(1)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _integer(value: int, name: str, *, positive: bool = False) -> None:
    _require(type(value) is int and int(positive) <= value <= MAX_SAFE_INTEGER,
             f"{name} must be a {'positive ' if positive else ''}safe integer")


@dataclass(frozen=True, slots=True)
class _Interval:
    lo: Decimal
    hi: Decimal

    @classmethod
    def exact(cls, value: int) -> _Interval:
        return cls(Decimal(value), Decimal(value))

    def add(self, other: _Interval) -> _Interval:
        return _Interval(_DOWN.add(self.lo, other.lo), _UP.add(self.hi, other.hi))

    def sub(self, other: _Interval) -> _Interval:
        return _Interval(_DOWN.subtract(self.lo, other.hi), _UP.subtract(self.hi, other.lo))

    def mul_positive(self, value: int) -> _Interval:
        _require(value > 0, "interval multiplier must be positive")
        return _Interval(_DOWN.multiply(self.lo, Decimal(value)),
                         _UP.multiply(self.hi, Decimal(value)))

    def div_positive(self, value: int) -> _Interval:
        _require(value > 0, "interval divisor must be positive")
        return _Interval(_DOWN.divide(self.lo, Decimal(value)),
                         _UP.divide(self.hi, Decimal(value)))

    def exp_negative(self) -> _Interval:
        _require(self.hi <= 0, "stable exponential requires a nonpositive interval")

        def bounds(value: Decimal) -> tuple[Decimal, Decimal]:
            if value == 0:
                return _ONE, _ONE
            if value <= -256:
                return _ZERO, Decimal('1e-77')
            result = _NEAR.exp(value)
            return _NEAR.next_minus(result), _NEAR.next_plus(result)

        return _Interval(bounds(self.lo)[0], bounds(self.hi)[1])

    def ln(self) -> _Interval:
        _require(self.lo > 0, "logarithm requires a positive interval")

        def bounds(value: Decimal) -> tuple[Decimal, Decimal]:
            if value == 1:
                return _ZERO, _ZERO
            result = _NEAR.ln(value)
            return _NEAR.next_minus(result), _NEAR.next_plus(result)

        return _Interval(bounds(self.lo)[0], bounds(self.hi)[1])


def _softplus(value: _Interval) -> _Interval:
    """Monotone log(1 + exp(x)), bounded at the two interval endpoints."""
    def endpoint(x: Decimal) -> _Interval:
        negative = x.copy_abs().copy_negate()
        tail = _Interval(negative, negative).exp_negative()
        base = _Interval(max(_ZERO, x), max(_ZERO, x))
        return base.add(_Interval.exact(1).add(tail).ln())

    return _Interval(endpoint(value.lo).lo, endpoint(value.hi).hi)


def cost_interval(yes_atomic: int, no_atomic: int, liquidity_atomic: int) -> tuple[Decimal, Decimal]:
    """Public diagnostic bounds for C(q); no floating point ledger conversion."""
    for value, name in ((yes_atomic, 'YES inventory'), (no_atomic, 'NO inventory')):
        _integer(value, name)
    _integer(liquidity_atomic, 'liquidity', positive=True)
    tail = _Interval.exact(-abs(yes_atomic-no_atomic)).div_positive(liquidity_atomic)
    result = _Interval.exact(max(yes_atomic, no_atomic)).add(
        _Interval.exact(1).add(tail.exp_negative()).ln().mul_positive(liquidity_atomic))
    return result.lo, result.hi


def _cost(yes: int, no: int, liquidity: int) -> _Interval:
    return _Interval(*cost_interval(yes, no, liquidity))


def minimum_subsidy_atomic(liquidity_atomic: int) -> int:
    _integer(liquidity_atomic, 'liquidity', positive=True)
    return int(_cost(0, 0, liquidity_atomic).hi.to_integral_value(rounding=ROUND_CEILING))


def _price_bp(own: int, other: int, liquidity: int) -> int:
    if own == other:
        return 5000
    exp = _Interval.exact(-abs(own-other)).div_positive(liquidity).exp_negative()
    denominator = _Interval.exact(1).add(exp)
    low = _DOWN.divide(_ONE, denominator.hi)
    high = _UP.divide(_ONE, denominator.lo)
    if own < other:
        low, high = _DOWN.subtract(_ONE, high), _UP.subtract(_ONE, low)
    # Presentation only. Interval midpoint rounds to the nearest basis point;
    # payouts are calculated from the certified cost, never from this display.
    midpoint = _NEAR.divide(_NEAR.add(low, high), Decimal(2))
    return int(_NEAR.multiply(midpoint, Decimal(10000)).to_integral_value(rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True, slots=True, kw_only=True)
class PricingPolicy(Record):
    policy_id: str = field(default='binary-lmsr-shadow-v1', metadata=ID)
    atomic_units_per_point: int = field(default=ATOMIC_UNITS_PER_POINT, metadata={'const': ATOMIC_UNITS_PER_POINT})
    liquidity_atomic: int = field(default=1_000*ATOMIC_UNITS_PER_POINT, metadata={'minimum': 1})
    subsidy_atomic: int = field(default=700*ATOMIC_UNITS_PER_POINT, metadata={'minimum': 1})
    minimum_fill_atomic: int = field(default=ATOMIC_UNITS_PER_POINT, metadata={'minimum': 1})
    maximum_fill_atomic: int = field(default=100*ATOMIC_UNITS_PER_POINT, metadata={'minimum': 1})
    maximum_owner_gross_atomic: int = field(default=300*ATOMIC_UNITS_PER_POINT, metadata={'minimum': 1})
    maximum_owner_unsettled_atomic: int = field(default=1_000*ATOMIC_UNITS_PER_POINT, metadata={'minimum': 1})
    quote_lifetime_ms: int = field(default=30_000, metadata={'minimum': 1, 'maximum': 300_000})

    def validate(self) -> None:
        _require(self.subsidy_atomic >= minimum_subsidy_atomic(self.liquidity_atomic),
                 'uniform-prior subsidy does not cover b ln(2)')
        _require(self.minimum_fill_atomic <= self.maximum_fill_atomic
                 <= self.maximum_owner_gross_atomic <= self.maximum_owner_unsettled_atomic,
                 'fill and gross limits must be ordered')


@dataclass(frozen=True, slots=True, kw_only=True)
class PricingState(Record):
    market_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    policy: PricingPolicy
    yes_claims_atomic: int = 0
    no_claims_atomic: int = 0
    deposits_atomic: int = 0
    reserve_atomic: int
    revision: int = 0
    closed_outcome: Outcome | None = None

    def validate(self) -> None:
        _require(self.reserve_atomic == self.policy.subsidy_atomic+self.deposits_atomic,
                 'reserve must equal the subsidy plus exact acquisition costs')
        _require(self.reserve_atomic >= max(self.yes_claims_atomic, self.no_claims_atomic,
                                           self.deposits_atomic), 'terminal liability exceeds reserve')
        if self.revision == 0:
            _require(self.yes_claims_atomic == self.no_claims_atomic == self.deposits_atomic == 0,
                     'initial inventory and deposits must be zero')
        else:
            _require(self.deposits_atomic > 0 and max(self.yes_claims_atomic, self.no_claims_atomic) > 0,
                     'accepted fills require deposits and claims')
            cost = _cost(self.yes_claims_atomic, self.no_claims_atomic, self.policy.liquidity_atomic)
            initial = _cost(0, 0, self.policy.liquidity_atomic)
            _require(Decimal(self.deposits_atomic) >= cost.sub(initial).lo,
                     'deposits cannot be below integrated acquisition cost')


@dataclass(frozen=True, slots=True, kw_only=True)
class PricingQuote(Record):
    market_id: str = field(metadata=ID)
    owner_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    policy_hash: str = field(metadata=HASH)
    state_hash: str = field(metadata=HASH)
    state_revision: int
    side: ForecastChoice
    spend_atomic: int = field(metadata={'minimum': 1})
    claims_atomic: int = field(metadata={'minimum': 1})
    winning_total_atomic: int = field(metadata={'minimum': 1})
    net_gain_atomic: int
    before_probability_bp: int = field(metadata=BP)
    after_probability_bp: int = field(metadata=BP)
    price_impact_bp: int = field(metadata=BP)
    quoted_at_ms: int
    expires_at_ms: int

    def validate(self) -> None:
        _require(self.winning_total_atomic == self.claims_atomic
                 and self.net_gain_atomic+self.spend_atomic == self.claims_atomic,
                 'payout display must match the immutable claims and exact spend')
        _require(self.after_probability_bp >= self.before_probability_bp
                 and self.price_impact_bp == self.after_probability_bp-self.before_probability_bp,
                 'buy price impact must be nonnegative and consistent')
        _require(self.expires_at_ms > self.quoted_at_ms, 'quote must have a positive lifetime')


@dataclass(frozen=True, slots=True, kw_only=True)
class PricingFill(Record):
    quote: PricingQuote
    accepted_at_ms: int
    state_after_hash: str = field(metadata=HASH)

    def validate(self) -> None:
        _require(self.quote.quoted_at_ms <= self.accepted_at_ms < self.quote.expires_at_ms,
                 'fill acceptance must occur within the quote lifetime')


@dataclass(frozen=True, slots=True, kw_only=True)
class PricingReceipt(Record):
    state: PricingState
    fill: PricingFill

    def validate(self) -> None:
        quote = self.fill.quote
        _require(content_hash(self.state) == self.fill.state_after_hash,
                 'receipt state hash must match accepted fill')
        _require(self.state.market_id == quote.market_id
                 and self.state.specification_hash == quote.specification_hash
                 and content_hash(self.state.policy) == quote.policy_hash
                 and self.state.revision == quote.state_revision+1
                 and self.state.closed_outcome is None, 'receipt market context mismatch')
        before = replace(self.state,
                         yes_claims_atomic=self.state.yes_claims_atomic-(quote.claims_atomic if quote.side is ForecastChoice.YES else 0),
                         no_claims_atomic=self.state.no_claims_atomic-(quote.claims_atomic if quote.side is ForecastChoice.NO else 0),
                         deposits_atomic=self.state.deposits_atomic-quote.spend_atomic,
                         reserve_atomic=self.state.reserve_atomic-quote.spend_atomic,
                         revision=self.state.revision-1)
        _require(content_hash(before) == quote.state_hash, 'receipt does not conserve claims or assets')
        _require(quote_buy(before, owner_id=quote.owner_id, side=quote.side,
                           spend_atomic=quote.spend_atomic, now_ms=quote.quoted_at_ms) == quote,
                 'receipt includes a forged quote')


def market_probability_bp(state: PricingState) -> int:
    """Marginal YES price for presentation; never a finite-fill payout."""
    state.__post_init__()
    return _price_bp(state.yes_claims_atomic, state.no_claims_atomic, state.policy.liquidity_atomic)


def initialize_market(policy: PricingPolicy, market_id: str, specification_hash: str) -> PricingState:
    return PricingState(market_id=market_id, specification_hash=specification_hash,
                        policy=policy, reserve_atomic=policy.subsidy_atomic)


def _claims_for_spend(state: PricingState, side: ForecastChoice, spend: int) -> int:
    own, other = ((state.yes_claims_atomic, state.no_claims_atomic) if side is ForecastChoice.YES
                  else (state.no_claims_atomic, state.yes_claims_atomic))
    liquidity = state.policy.liquidity_atomic
    # Exact inverse: d = s + b softplus((other-own)/b + ln(1-exp(-s/b))).
    spent = _Interval.exact(spend)
    tail = _Interval.exact(-spend).div_positive(liquidity).exp_negative()
    fraction = _Interval.exact(1).sub(tail)
    shifted = _Interval.exact(other-own).div_positive(liquidity).add(fraction.ln())
    quantity = spent.add(_softplus(shifted).mul_positive(liquidity))
    candidate = int(quantity.hi.to_integral_value(rounding=ROUND_FLOOR))
    _require(candidate <= MAX_SAFE_INTEGER-own, 'claim capacity exhausted')
    before = _cost(own, other, liquidity)
    # Upper inverse bound leaves at most a few atoms to inspect. Fail closed on
    # any unexpectedly wide interval instead of an unbounded search.
    low = int(quantity.lo.to_integral_value(rounding=ROUND_FLOOR))
    _require(candidate-low <= 2, 'pricing precision insufficient')
    while candidate > 0:
        upper_cost = _cost(own+candidate, other, liquidity).sub(before).hi
        if upper_cost <= Decimal(spend):
            return candidate
        candidate -= 1
        _require(candidate >= low-2, 'pricing precision insufficient')
    raise ValidationError('spend cannot purchase a positive conservatively priced claim')


def quote_buy(state: PricingState, *, owner_id: str, side: ForecastChoice,
              spend_atomic: int, now_ms: int) -> PricingQuote:
    state.__post_init__()
    _integer(spend_atomic, 'spend', positive=True)
    _integer(now_ms, 'quote time')
    _require(type(side) is ForecastChoice, 'side must be YES or NO')
    _require(state.closed_outcome is None, 'market is closed')
    policy = state.policy
    _require(policy.minimum_fill_atomic <= spend_atomic <= policy.maximum_fill_atomic,
             'spend outside published fill limits')
    _require(state.reserve_atomic+spend_atomic <= MAX_SAFE_INTEGER, 'reserve capacity exhausted')
    claims = _claims_for_spend(state, side, spend_atomic)
    own, other = ((state.yes_claims_atomic, state.no_claims_atomic) if side is ForecastChoice.YES
                  else (state.no_claims_atomic, state.yes_claims_atomic))
    _require(state.reserve_atomic+spend_atomic >= max(own+claims, other),
             'fill would exceed fully reserved terminal claims')
    # A buy has positive convex price below one. At saturation conservative
    # rounding may produce exactly spend-1; decline such negative-net fills.
    _require(claims >= spend_atomic, 'price is too saturated for this atomic spend')
    before_bp, after_bp = (_price_bp(own, other, policy.liquidity_atomic),
                           _price_bp(own+claims, other, policy.liquidity_atomic))
    return PricingQuote(market_id=state.market_id, owner_id=owner_id,
                        specification_hash=state.specification_hash, policy_hash=content_hash(policy),
                        state_hash=content_hash(state), state_revision=state.revision, side=side,
                        spend_atomic=spend_atomic, claims_atomic=claims, winning_total_atomic=claims,
                        net_gain_atomic=claims-spend_atomic, before_probability_bp=before_bp,
                        after_probability_bp=after_bp, price_impact_bp=after_bp-before_bp,
                        quoted_at_ms=now_ms, expires_at_ms=now_ms+policy.quote_lifetime_ms)


def accept_quote(state: PricingState, quote: PricingQuote, *, owner_id: str,
                 now_ms: int, minimum_claims_atomic: int, owner_gross_atomic: int,
                 owner_unsettled_atomic: int) -> PricingReceipt:
    """Pure acceptance; adapters must CAS state and authoritative owner totals.

    Caller-provided totals here are trusted domain inputs, never client fields.
    This function does not promise authentication, cross-market CAS, or balances.
    """
    quote.__post_init__()
    for value, name in ((now_ms, 'acceptance time'), (minimum_claims_atomic, 'minimum claims'),
                        (owner_gross_atomic, 'gross cost'), (owner_unsettled_atomic, 'unsettled cost')):
        _integer(value, name)
    _require(owner_id == quote.owner_id, 'quote belongs to another owner')
    _require(quote.quoted_at_ms <= now_ms < quote.expires_at_ms, 'quote expired or not yet valid')
    expected = quote_buy(state, owner_id=owner_id, side=quote.side,
                         spend_atomic=quote.spend_atomic, now_ms=quote.quoted_at_ms)
    _require(expected == quote, 'quote is forged or market revision changed')
    _require(quote.claims_atomic >= minimum_claims_atomic, 'minimum claims not satisfied')
    _require(owner_gross_atomic+quote.spend_atomic <= state.policy.maximum_owner_gross_atomic,
             'owner gross market cost limit exceeded')
    _require(owner_unsettled_atomic+quote.spend_atomic <= state.policy.maximum_owner_unsettled_atomic,
             'owner unsettled gross cost limit exceeded')
    updated = replace(state,
                      yes_claims_atomic=state.yes_claims_atomic+(quote.claims_atomic if quote.side is ForecastChoice.YES else 0),
                      no_claims_atomic=state.no_claims_atomic+(quote.claims_atomic if quote.side is ForecastChoice.NO else 0),
                      deposits_atomic=state.deposits_atomic+quote.spend_atomic,
                      reserve_atomic=state.reserve_atomic+quote.spend_atomic, revision=state.revision+1)
    return PricingReceipt(state=updated, fill=PricingFill(quote=quote, accepted_at_ms=now_ms,
                                                        state_after_hash=content_hash(updated)))


def close_market(state: PricingState, outcome: Outcome) -> PricingState:
    """Only an authenticated finalization adapter may supply a final outcome."""
    state.__post_init__()
    _require(type(outcome) is Outcome, 'invalid final outcome')
    _require(state.closed_outcome is None or state.closed_outcome is outcome,
             'final outcome cannot change')
    return replace(state, closed_outcome=outcome)


def payout_atomic(fill: PricingFill, outcome: Outcome) -> int:
    fill.__post_init__()
    _require(type(outcome) is Outcome, 'invalid final outcome')
    if outcome is Outcome.INVALID:
        return fill.quote.spend_atomic
    return fill.quote.claims_atomic if fill.quote.side.value == outcome.value else 0


def terminal_liability_atomic(state: PricingState, outcome: Outcome) -> int:
    state.__post_init__()
    _require(type(outcome) is Outcome, 'invalid final outcome')
    if outcome is Outcome.INVALID:
        return state.deposits_atomic
    return state.yes_claims_atomic if outcome is Outcome.YES else state.no_claims_atomic
