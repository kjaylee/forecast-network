#!/usr/bin/env python3
"""Offline research estimates from a dated mainnet rent snapshot, never a wallet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs/research/evidence/solana-rent-2026-09-09.json"
LAMPORTS_PER_SOL = 1_000_000_000
MARKET_BYTES = 360
DISPUTE_BYTES = 160
PAGE_HEADER_BYTES = 64
SLOTS_PER_PAGE = 16


def nonnegative(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def rent(snapshot: dict[str, Any], data_bytes: int) -> int:
    nonnegative(data_bytes, "data_bytes")
    try:
        value = snapshot["rent_by_data_bytes"][str(data_bytes)]
    except KeyError as exc:
        raise ValueError(f"No observed rent quote for {data_bytes} bytes; fetch a new quote") from exc
    nonnegative(value, "quoted rent")
    return int(value)


def tree_bytes(depth: int, buffer_size: int, canopy_depth: int = 0) -> int:
    """SPL CMT v1 SDK serialized-size formula; deployment must check supported pairs."""
    for name, value in (("depth", depth), ("buffer_size", buffer_size), ("canopy", canopy_depth)):
        nonnegative(value, name)
    if not 1 <= depth <= 30 or buffer_size == 0 or canopy_depth > depth:
        raise ValueError("Invalid tree dimensions")
    if buffer_size & (buffer_size - 1):
        raise ValueError("Changelog buffer must be a power of two")
    return 56 + 24 + (buffer_size + 1) * (32 * depth + 40) + 32 * ((1 << (canopy_depth + 1)) - 2)


def light_v2_cost(accounts: int, updates_each: int) -> int:
    """One signature/instruction/tree/leaf per operation; excludes app deploy and services."""
    nonnegative(accounts, "accounts")
    nonnegative(updates_each, "updates_each")
    create = 5_000 + 5_000 + 300 + 10_000
    update = 5_000 + 5_000 + 300
    return accounts * (create + updates_each * update)


def estimate(
    snapshot: dict[str, Any], *, program_kib: int, active_markets: int,
    live_disputes: int, paged: bool = False, fee_reserve: int = 50_000_000,
) -> dict[str, Any]:
    """Hypothetical full program sizes. No rent reclamation is subtracted up front."""
    for name, value in (("program_kib", program_kib), ("active_markets", active_markets),
                        ("live_disputes", live_disputes), ("fee_reserve", fee_reserve)):
        nonnegative(value, name)
    if program_kib == 0:
        raise ValueError("Program size must be positive")
    code_rent = rent(snapshot, program_kib * 1024 + 45)
    accounts = (active_markets + SLOTS_PER_PAGE - 1) // SLOTS_PER_PAGE if paged else active_markets
    size = PAGE_HEADER_BYTES + SLOTS_PER_PAGE * MARKET_BYTES if paged else MARKET_BYTES
    components = {
        "program_data": code_rent,
        "program_account": rent(snapshot, 36),
        "active_market_accounts": accounts * rent(snapshot, size),
        "live_dispute_receipts": live_disputes * rent(snapshot, DISPUTE_BYTES),
        "config_and_four_commitment_accounts": rent(snapshot, 256) + 4 * rent(snapshot, 128),
        "planned_fee_reserve": fee_reserve,
    }
    total = sum(components.values())
    return {
        "layout": "16_per_page" if paged else "individual_pda",
        "hypothetical_program_kib": program_kib, "active_markets": active_markets,
        "funded_market_accounts": accounts, "live_disputes": live_disputes,
        "lamports": components, "initial_budget_lamports": total,
        # Conservative same-size upgrade liquidity; CLI may fund buffer at ProgramData size.
        "upgrade_buffer_reserve_lamports": code_rent,
        "initial_plus_one_upgrade_buffer_lamports": total + code_rent,
        "initial_within_two_sol": total <= 2 * LAMPORTS_PER_SOL,
        "including_upgrade_buffer_within_two_sol": total + code_rent <= 2 * LAMPORTS_PER_SOL,
    }


def research_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    cases = [(64, 100, 5, False), (64, 300, 15, False), (64, 500, 25, False),
             (64, 500, 25, True), (48, 500, 25, True), (96, 300, 15, False),
             (64, 300, 256, False)]
    return {
        "schema_version": 1, "observed_at_utc": snapshot["observed_at_utc"],
        "assumptions": [
            "Program sizes and binary layouts are design scenarios, not measured Forecast builds.",
            "0.05 SOL fee reserve is a planning allocation, not an observed fee quote or lifetime budget.",
            "Global config 256 bytes plus four 128-byte commitment accounts are planning allocations.",
            "No account-rent refund is counted before safe archival and closure.",
            "Excluded: servers, storage, indexer/prover/RPC charges, audits and continuing transaction spend.",
            "Compression does not eliminate deployment rent for the custom Forecast program.",
        ],
        "scenarios": [estimate(snapshot, program_kib=k, active_markets=n,
                               live_disputes=d, paged=p) for k, n, d, p in cases],
        "spl_tree_examples": [
            {"depth": 14, "buffer": 64, "canopy": c, "bytes": tree_bytes(14, 64, c),
             "rent_lamports": rent(snapshot, tree_bytes(14, 64, c))} for c in (0, 10)
        ],
        "light_v2_examples": [
            {"accounts": count, "updates_each": updates,
             "documented_fee_model_lamports": light_v2_cost(count, updates)}
            for count, updates in ((1000, 0), (1000, 11), (10000, 0))
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    print(json.dumps(research_summary(snapshot), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
