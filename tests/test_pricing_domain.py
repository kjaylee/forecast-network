"""Integer conservation, certified bounds and immutable LMSR quote contracts."""
from __future__ import annotations

import random
import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import ROUND_DOWN, Decimal, localcontext

from forecast_domain.errors import ValidationError
from forecast_domain.models import ForecastChoice, Outcome
from forecast_domain.pricing import (
    ATOMIC_UNITS_PER_POINT,
    PricingFill,
    PricingPolicy,
    PricingQuote,
    PricingReceipt,
    PricingState,
    _claims_for_spend,
    accept_quote,
    close_market,
    cost_interval,
    initialize_market,
    market_probability_bp,
    minimum_subsidy_atomic,
    payout_atomic,
    quote_buy,
    terminal_liability_atomic,
)
from forecast_domain.records import MAX_SAFE_INTEGER
from forecast_domain.serialization import content_hash, dumps, loads

POINT = ATOMIC_UNITS_PER_POINT


def policy() -> PricingPolicy:
    return PricingPolicy()


def market(selected_policy: PricingPolicy | None = None) -> PricingState:
    return initialize_market(selected_policy or policy(), 'market-1', 'a'*64)


def quote(state: PricingState, side: ForecastChoice = ForecastChoice.YES,
          spend: int = 100*POINT, now: int = 100) -> PricingQuote:
    return quote_buy(state, owner_id='alice', side=side, spend_atomic=spend, now_ms=now)


def receipt(state: PricingState, side: ForecastChoice = ForecastChoice.YES,
            spend: int = 100*POINT, now: int = 100, gross: int = 0) -> PricingReceipt:
    offered = quote(state, side, spend, now)
    return accept(state, offered, owner_id='alice', now_ms=now,
                        minimum_claims_atomic=offered.claims_atomic,
                        owner_gross_atomic=gross, owner_unsettled_atomic=gross)



def accept(state: PricingState, offered: PricingQuote, *, owner_id: str, now_ms: int,
           minimum_claims_atomic: int, owner_gross_atomic: int = 0,
           owner_unsettled_atomic: int = 0) -> PricingReceipt:
    """Test fixtures explicitly model a new owner unless a test supplies totals."""
    return accept_quote(state, offered, owner_id=owner_id, now_ms=now_ms,
                        minimum_claims_atomic=minimum_claims_atomic,
                        owner_gross_atomic=owner_gross_atomic,
                        owner_unsettled_atomic=owner_unsettled_atomic)


def pricing_records() -> tuple[PricingPolicy | PricingState | PricingQuote | PricingFill | PricingReceipt, ...]:
    state = market()
    accepted = receipt(state)
    return state.policy, state, accepted.fill.quote, accepted.fill, accepted


class PricingDomainTests(unittest.TestCase):
    def test_fixed_default_and_finite_fill_match_design(self) -> None:
        state = market()
        offered = quote(state)
        self.assertEqual(minimum_subsidy_atomic(state.policy.liquidity_atomic), 693_147_181)
        self.assertEqual(state.policy.subsidy_atomic, 700*POINT)
        self.assertEqual(offered.claims_atomic, 190_902_828)
        self.assertEqual(offered.net_gain_atomic, 90_902_828)
        self.assertEqual((offered.before_probability_bp, offered.after_probability_bp), (5000, 5476))
        self.assertEqual(offered.winning_total_atomic, offered.claims_atomic)
        self.assertEqual(market_probability_bp(state), 5000)
        advanced = receipt(state).state
        self.assertEqual(market_probability_bp(advanced), offered.after_probability_bp)
        self.assertEqual(market_probability_bp(close_market(advanced, Outcome.YES)), offered.after_probability_bp)

    def test_records_round_trip_frozen_and_versioned(self) -> None:
        for record in pricing_records():
            with self.subTest(record=type(record).__name__):
                self.assertEqual(loads(type(record), dumps(record)), record)
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, 'schema_version', 2)
                with self.assertRaises(ValidationError):
                    replace(record, schema_version=2)

    def test_every_accepted_asset_and_liability_is_conserved(self) -> None:
        state = market()
        fills = []
        users_spendable = 10_000*POINT
        initial_assets = users_spendable+state.reserve_atomic
        for index in range(80):
            accepted = receipt(state, ForecastChoice.YES if index%3 else ForecastChoice.NO,
                               (index%99+1)*POINT, 100+index)
            state = accepted.state
            fills.append(accepted.fill)
            users_spendable -= accepted.fill.quote.spend_atomic
            self.assertEqual(users_spendable+state.reserve_atomic, initial_assets)
            for outcome in Outcome:
                liability = terminal_liability_atomic(state, outcome)
                self.assertEqual(sum(payout_atomic(fill, outcome) for fill in fills), liability)
                self.assertLessEqual(liability, state.reserve_atomic)
        self.assertEqual(state.reserve_atomic-terminal_liability_atomic(state, Outcome.INVALID),
                         state.policy.subsidy_atomic)

    def test_opposite_side_holdings_are_distinct_and_gross_capped(self) -> None:
        first = receipt(market())
        second = receipt(first.state, ForecastChoice.NO, gross=100*POINT)
        third = receipt(second.state, ForecastChoice.YES, gross=200*POINT)
        self.assertGreater(second.state.no_claims_atomic, 0)
        self.assertEqual(second.state.yes_claims_atomic, first.state.yes_claims_atomic)
        with self.assertRaisesRegex(ValidationError, 'gross market'):
            receipt(third.state, ForecastChoice.NO, gross=300*POINT)
        self.assertGreater(payout_atomic(first.fill, Outcome.YES), 0)
        self.assertEqual(payout_atomic(second.fill, Outcome.YES), 0)

    def test_global_unsettled_limit_cannot_be_offset_by_a_hedge(self) -> None:
        state = market()
        offered = quote(state, ForecastChoice.NO)
        with self.assertRaisesRegex(ValidationError, 'unsettled gross'):
            accept(state, offered, owner_id='alice', now_ms=100,
                         minimum_claims_atomic=1, owner_gross_atomic=0,
                         owner_unsettled_atomic=950*POINT)
        accept(state, offered, owner_id='alice', now_ms=100,
                     minimum_claims_atomic=1, owner_gross_atomic=200*POINT,
                     owner_unsettled_atomic=900*POINT)

    def test_time_equality_and_minimum_claim_boundary(self) -> None:
        state = market()
        offered = quote(state)
        for now in (offered.quoted_at_ms, offered.expires_at_ms-1):
            accept(state, offered, owner_id='alice', now_ms=now,
                         minimum_claims_atomic=offered.claims_atomic)
        for now in (offered.quoted_at_ms-1, offered.expires_at_ms):
            with self.assertRaisesRegex(ValidationError, 'expired'):
                accept(state, offered, owner_id='alice', now_ms=now, minimum_claims_atomic=1)
        with self.assertRaisesRegex(ValidationError, 'minimum claims'):
            accept(state, offered, owner_id='alice', now_ms=100,
                         minimum_claims_atomic=offered.claims_atomic+1)

    def test_stale_revision_owner_policy_spec_and_forged_quote_rejected_without_mutation(self) -> None:
        state = market()
        original = dumps(state)
        offered = quote(state)
        candidates = (
            replace(offered, claims_atomic=offered.claims_atomic+1,
                    winning_total_atomic=offered.claims_atomic+1, net_gain_atomic=offered.net_gain_atomic+1),
            replace(offered, state_revision=1),
            replace(offered, specification_hash='b'*64),
            replace(offered, policy_hash='b'*64),
            replace(offered, state_hash='b'*64),
            replace(offered, before_probability_bp=4999, price_impact_bp=477),
        )
        for forged in candidates:
            with self.subTest(forged=forged), self.assertRaises(ValidationError):
                accept(state, forged, owner_id='alice', now_ms=100, minimum_claims_atomic=1)
        with self.assertRaisesRegex(ValidationError, 'another owner'):
            accept(state, offered, owner_id='bob', now_ms=100, minimum_claims_atomic=1)
        with self.assertRaisesRegex(ValidationError, 'revision changed'):
            accept(receipt(state).state, offered, owner_id='alice', now_ms=100,
                         minimum_claims_atomic=1)
        asymmetric = receipt(state).state
        wrong_side = replace(quote(asymmetric), side=ForecastChoice.NO)
        with self.assertRaisesRegex(ValidationError, 'forged'):
            accept(asymmetric, wrong_side, owner_id='alice', now_ms=100, minimum_claims_atomic=1)
        self.assertEqual(dumps(state), original)

    def test_policy_and_snapshot_semantics_survive_decoding(self) -> None:
        state = market()
        for mutation in (
            {'subsidy_atomic': 693_147_180}, {'maximum_fill_atomic': 0},
            {'minimum_fill_atomic': 101*POINT}, {'atomic_units_per_point': 100},
            {'quote_lifetime_ms': 300_001}, {'liquidity_atomic': True},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                replace(state.policy, **mutation)
        for mutation in (
            {'yes_claims_atomic': 1}, {'deposits_atomic': 1}, {'reserve_atomic': 1},
            {'revision': 1}, {'closed_outcome': 'YES'},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                replace(state, **mutation)
        accepted = receipt(state)
        with self.assertRaises(ValidationError):
            replace(accepted, state=replace(accepted.state, deposits_atomic=101*POINT,
                                          reserve_atomic=801*POINT))
        with self.assertRaises(ValidationError):
            loads(PricingState, dumps(state).replace('"reserve_atomic":700000000', '"reserve_atomic":1'))

    def test_no_free_claims_or_out_of_range_atomic_inputs(self) -> None:
        state = market()
        for spend in (0, -1, True, 0.1, POINT-1, 100*POINT+1, MAX_SAFE_INTEGER+1):
            with self.subTest(spend=spend), self.assertRaises(ValidationError):
                quote(state, spend=spend)
        for now in (-1, True, MAX_SAFE_INTEGER):
            with self.subTest(now=now), self.assertRaises(ValidationError):
                quote(state, now=now)
        with self.assertRaises(ValidationError):
            quote_buy(state, owner_id='alice', side='YES', spend_atomic=POINT, now_ms=100)

    def test_terminal_closure_is_irreversible_and_payouts_are_exact(self) -> None:
        accepted = receipt(market())
        for outcome in Outcome:
            closed = close_market(accepted.state, outcome)
            self.assertEqual(close_market(closed, outcome), closed)
            with self.assertRaisesRegex(ValidationError, 'closed'):
                quote(closed)
            with self.assertRaisesRegex(ValidationError, 'cannot change'):
                close_market(closed, Outcome.NO if outcome is Outcome.YES else Outcome.YES)
        self.assertEqual(payout_atomic(accepted.fill, Outcome.INVALID), 100*POINT)
        self.assertEqual(payout_atomic(accepted.fill, Outcome.YES), 190_902_828)
        self.assertEqual(payout_atomic(accepted.fill, Outcome.NO), 0)

    def test_both_sides_move_price_and_never_reprice_old_fills(self) -> None:
        first = receipt(market())
        original = dumps(first.fill)
        second = receipt(first.state)
        self.assertGreater(second.fill.quote.before_probability_bp, 5000)
        self.assertLess(second.fill.quote.claims_atomic, first.fill.quote.claims_atomic)
        opposite = receipt(second.state, ForecastChoice.NO)
        self.assertLess(opposite.fill.quote.before_probability_bp, 5000)
        self.assertGreater(opposite.fill.quote.claims_atomic, first.fill.quote.claims_atomic)
        self.assertEqual(dumps(first.fill), original)

    def test_order_splitting_does_not_create_rounding_profit(self) -> None:
        state = market()
        whole = quote(state)
        for parts in (2, 4, 10, 100):
            split = state
            total_claims = 0
            for step in range(parts):
                accepted = receipt(split, spend=100*POINT//parts, now=100+step)
                split = accepted.state
                total_claims += accepted.fill.quote.claims_atomic
            self.assertLessEqual(total_claims, whole.claims_atomic)
            self.assertLess(whole.claims_atomic-total_claims, parts*2)
            self.assertEqual(split.deposits_atomic, whole.spend_atomic)

    def test_certified_intervals_contain_160_digit_reference(self) -> None:
        cases = ((0, 0, 1), (1, 0, 1), (10**12, 10**12+1, 10**9),
                 (MAX_SAFE_INTEGER, MAX_SAFE_INTEGER-1, MAX_SAFE_INTEGER),
                 (256_000, 0, 1000), (MAX_SAFE_INTEGER, 0, 1))
        for yes, no, liquidity in cases:
            lo, hi = cost_interval(yes, no, liquidity)
            with localcontext() as ctx:
                ctx.prec = 160
                tail = (-Decimal(abs(yes-no))/Decimal(liquidity)).exp() if abs(yes-no)/liquidity < 1000 else Decimal(0)
                reference = Decimal(max(yes, no))+Decimal(liquidity)*(1+tail).ln()
                self.assertLessEqual(lo, reference)
                self.assertGreaterEqual(hi, reference)
                self.assertLess(hi-lo, Decimal('1e-55'))

    def test_ambient_decimal_context_cannot_change_quotes(self) -> None:
        state = market()
        expected = quote(state)
        with localcontext() as ctx:
            ctx.prec = 3
            ctx.rounding = ROUND_DOWN
            ctx.Emax = 12
            self.assertEqual(quote(state), expected)
            self.assertEqual(receipt(state).fill.quote, expected)

    def test_micro_atomic_and_extreme_inventory_fail_closed(self) -> None:
        micro = replace(policy(), minimum_fill_atomic=1)
        offered = quote(market(micro), spend=1)
        self.assertEqual(offered.claims_atomic, 1)
        # Very one-sided inventory is solvent but the safe finite quote can no
        # longer certify a nonnegative net gain at atomic precision.
        tiny = PricingPolicy(liquidity_atomic=1, subsidy_atomic=1,
                             minimum_fill_atomic=1, maximum_fill_atomic=100,
                             maximum_owner_gross_atomic=300, maximum_owner_unsettled_atomic=1000)
        extreme = PricingState(market_id='tail', specification_hash='a'*64, policy=tiny,
                               yes_claims_atomic=10**12, no_claims_atomic=0,
                               deposits_atomic=10**12, reserve_atomic=10**12+1, revision=1)
        with self.assertRaisesRegex(ValidationError, 'positive conservatively priced|saturated'):
            quote(extreme, spend=1)
        opposite = quote(extreme, ForecastChoice.NO, spend=1)
        self.assertGreater(opposite.claims_atomic, 10**11)
        self.assertLessEqual(opposite.claims_atomic, extreme.reserve_atomic+1)

    def test_seeded_ten_thousand_integer_fills_are_fully_reserved(self) -> None:
        rng = random.Random(20260910)
        checked = 0
        for scenario in range(400):
            state = market()
            for step in range(25):
                side = ForecastChoice.YES if (scenario%5 == 0 or rng.randrange(2)) else ForecastChoice.NO
                spend = rng.randint(POINT, 100*POINT)
                claims = _claims_for_spend(state, side, spend)
                own = state.yes_claims_atomic if side is ForecastChoice.YES else state.no_claims_atomic
                other = state.no_claims_atomic if side is ForecastChoice.YES else state.yes_claims_atomic
                before_low, before_high = cost_interval(own, other, state.policy.liquidity_atomic)
                after_low, after_high = cost_interval(own+claims, other, state.policy.liquidity_atomic)
                with localcontext() as ctx:
                    ctx.prec = 100
                    self.assertLessEqual(after_high-before_low, spend)
                    self.assertGreater(after_low-before_high, 0)
                    if step == 24:
                        _, next_high = cost_interval(own+claims+1, other, state.policy.liquidity_atomic)
                        self.assertGreater(next_high-before_low, spend)
                state = replace(state, yes_claims_atomic=state.yes_claims_atomic+(claims if side is ForecastChoice.YES else 0),
                                no_claims_atomic=state.no_claims_atomic+(claims if side is ForecastChoice.NO else 0),
                                deposits_atomic=state.deposits_atomic+spend,
                                reserve_atomic=state.reserve_atomic+spend, revision=state.revision+1)
                self.assertEqual(state.reserve_atomic-state.deposits_atomic, 700*POINT)
                self.assertGreaterEqual(state.reserve_atomic, max(state.yes_claims_atomic, state.no_claims_atomic))
                if step == 24:
                    self.assertEqual(content_hash(loads(PricingState, dumps(state))), content_hash(state))
                checked += 1
        self.assertEqual(checked, 10_000)


if __name__ == '__main__':
    unittest.main()
