#!/usr/bin/env python3
"""Reproducible research checks for a proposed buy-only point LMSR; not execution code."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from decimal import Decimal, localcontext
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def cost(yes: float, no: float, b: float) -> float:
    return max(yes, no) + b * math.log1p(math.exp(-abs(yes-no)/b))


def quote(p: float, spend: float, b: float = 1000) -> dict[str, float]:
    claims = b * math.log1p(math.expm1(spend/b)/p)
    return {"before_probability": p, "spend": spend, "winning_total": claims,
            "total_return_multiple": claims/spend, "after_probability": 1-(1-p)*math.exp(-spend/b)}


def simulate(seed: int, scenarios: int) -> dict[str, object]:
    require(scenarios >= 10000, "At least 10000 scenarios are required")
    rng = random.Random(seed)
    minimum_surplus = math.inf
    maximum_loss_fraction = 0.0
    total_fills = 0
    maximum_void_error = 0.0
    for scenario in range(scenarios):
        b = rng.choice((100.0, 1000.0, 10000.0))
        subsidy = math.ceil(b*math.log(2))
        yes = no = deposits = 0.0
        # Independent market paths, including one-sided and alternating orders.
        for step in range(rng.randint(5, 60)):
            side = scenario % 2 if scenario % 5 == 0 else rng.randrange(2)
            if scenario % 7 == 0:
                side = step % 2
            quantity = rng.uniform(0.001, 3*b)
            old = cost(yes, no, b)
            if side == 0:
                yes += quantity
            else:
                no += quantity
            paid = cost(yes, no, b)-old
            require(paid >= -1e-9, "Buy-only cost must not be negative")
            deposits += paid
            reserve = subsidy+deposits
            surplus = reserve-max(yes, no)
            tolerance = 1e-8 * max(1.0, b/1000)
            require(surplus >= -tolerance, "Outcome liability exceeds reserve")
            require(reserve+ tolerance >= deposits, "INVALID refunds exceed reserve")
            minimum_surplus = min(minimum_surplus, surplus)
            maximum_loss_fraction = max(maximum_loss_fraction, (max(yes, no)-deposits)/(b*math.log(2)))
            maximum_void_error = max(maximum_void_error, abs((reserve-deposits)-subsidy))
            total_fills += 1
    # High precision spot checks compare the finite-order formula with integrated cost.
    precision_checks = []
    with localcontext() as ctx:
        ctx.prec = 80
        b = Decimal(1000)
        for probability in ('0.05', '0.2', '0.5', '0.8', '0.95'):
            p = Decimal(probability)
            for amount in ('0.000001', '1', '100', '1000'):
                x = Decimal(amount)
                claims = b*(1+((x/b).exp()-1)/p).ln()
                recovered = b*(p*(claims/b).exp()+1-p).ln()
                require(abs(recovered-x) < Decimal('1e-65'), "High precision quote mismatch")
                precision_checks.append({"p": probability, "spend": amount, "claims": str(claims)})
    example_rows = [quote(p, 100) for p in (0.2, 0.5, 0.8)]
    donor_spend = 1000.0
    manipulated_yes = 0.5*math.exp(-donor_spend/1000)
    beneficiary = quote(manipulated_yes, 100)
    # Simple terminal-budget checks; these are not database integration tests.
    market_count, subsidy_per_market = 20, 700
    total_subsidy = market_count*subsidy_per_market
    require(total_subsidy == 14000, "Independent reserves must add under correlation")
    # Full reserve cannot be pledged twice even if outcomes are thought independent.
    require(total_subsidy > 10000, "A 10000 treasury cannot authorize 20 reserves of700")
    # Integer-cent reference proof for full refund plus remaining service work.
    # Payment/attempt billing concurrency and actual processor settlement are NOT modeled.
    invoice_paths = 0
    for refund_after in range(5):
        assets, refundable, remaining = 315, 200, 115
        expenses = (40, 24, 48, 3)  # Payment/refund allowance and bounded service costs.
        for stage in range(5):
            if stage == refund_after:
                assets -= refundable
                refundable = 0
            require(assets >= refundable+remaining, "Refund/work cash double-counted")
            if stage < len(expenses):
                assets -= expenses[stage]
                remaining -= expenses[stage]
        require((assets, refundable, remaining) == (0, 0, 0), "Refunded job left unbacked costs")
        invoice_paths += 1
    reward_units = 10_000_000  # Ten points; one point = one million atomic units.
    payouts = {report: tuple(reward_units*(10000-(report-100*outcome)**2)//10000
                            for outcome in (0, 1)) for report in range(101)}
    for values in payouts.values():
        require(all(0 <= value <= reward_units for value in values), "Reward exceeds slot reserve")
    for belief in range(101):
        expected = {report: (100-belief)*values[0]+belief*values[1]
                    for report, values in payouts.items()}
        require(max(expected, key=lambda report: expected[report]) == belief,
                "Integer-grid expected reward does not peak at truthful belief")
    return {
        "schema_version": 1, "seed": seed, "scenarios": scenarios, "sampled_fills": total_fills,
        "model": "binary zero-inventory uniform-prior buy-only LMSR; no fees or sells",
        "status": "passed", "minimum_outcome_surplus": minimum_surplus,
        "maximum_loss_divided_by_b_ln_2": maximum_loss_fraction,
        "maximum_float_invalid_refund_error": maximum_void_error,
        "precision_decimal_digits": 80, "precision_checks": len(precision_checks),
        "examples_spend_100": example_rows,
        "size_impact_at_half": [quote(0.5, amount) for amount in (100, 500, 1000)],
        "subsidy_bound_b1000": 1000*math.log(2),
        "prior_10_90_subsidy_bound_b1000": 1000*math.log(10),
        "all_yes_buy_of_1000": quote(0.5, 1000),
        "collusion_counterexample": {"donor_no_spend": donor_spend,
            "beneficiary_yes": beneficiary,
            "within_proposed_caps": {"donor_no_fills": [100, 100, 100],
                "beneficiary_yes": quote(0.5*math.exp(-300/1000), 100)}, "beneficiary_unmanipulated": quote(0.5, 100),
            "warning": "No transfer/sell endpoint does not prevent indirect migration from disposable accounts."},
        "portfolio": {"markets": market_count, "subsidy_each": subsidy_per_market,
            "locked": total_subsidy, "treasury": 20000, "unallocated": 20000-total_subsidy,
            "at_300_markets": 300*subsidy_per_market, "at_500_markets": 500*subsidy_per_market},
        "service_invoice_reference": {"checked_refund_stages": invoice_paths,
            "invoice_cents": 200, "operator_risk_capital_cents": 115,
            "maximum_cost_cents": 115, "earned_contribution_cents": 85,
            "break_even_at_fixed_10_dollars": math.ceil(1000/85),
            "break_even_at_fixed_30_dollars": math.ceil(3000/85),
            "twenty_simultaneous_refunds": {"customer_principal_cents": 4000,
                "operator_capital_required_cents": 2300, "refund_principal_cents": 4000},
            "uncertain_billing": "Do not release an attempt's cost reservation until reconciled; maximum cost remains reserved."},
        "independent_reward_reference": {"max_reward_atomic": reward_units,
            "atomic_units_per_point": 1000000, "outcome_report_checks": 202,
            "truthful_grid_maxima": 101, "p70_no_atomic": payouts[70][0],
            "p70_yes_atomic": payouts[70][1], "slots_per_question": 100,
            "reserve_per_question_atomic": 100*reward_units},
        "not_tested": ["production interval arithmetic or atomic units", "transaction concurrency/CAS",
            "provider or D1 runtime", "real user price quality", "Sybil prevention", "endogenous behavioral accuracy"],
        "method_note": "Random paths use floating arithmetic with stated tolerance; Decimal checks are reference checks, not certified interval proofs. The algebraic reserve proof is separate. No production policy changed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=20260910)
    parser.add_argument('--scenarios', type=int, default=10000)
    parser.add_argument('--output', type=Path, default=Path('tmp/point-pricing-simulation.json'))
    args = parser.parse_args()
    result = simulate(args.seed, args.scenarios)
    result['source_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps({key: result[key] for key in ('status', 'scenarios', 'sampled_fills', 'precision_checks')}))


if __name__ == '__main__':
    main()
