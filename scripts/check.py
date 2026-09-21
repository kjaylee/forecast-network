#!/usr/bin/env python3
"""Dependency-free root verification; use --tools for installed Ruff and mypy."""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]
sys.dont_write_bytecode = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", action="store_true", help="Also require installed Ruff and mypy")
    args = parser.parse_args()
    task_tmp = ROOT / "tmp"
    task_tmp.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(task_tmp)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONPATH"] = os.pathsep.join([
        str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT),
    ])
    # Parse every source file without emitting bytecode outside tmp/.
    files = [
        *ROOT.glob("packages/**/*.py"), *ROOT.glob("tests/**/*.py"),
        *ROOT.glob("scripts/*.py"), *ROOT.glob("apps/web/src/**/*.py"),
    ]
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"Syntax verified: {len(files)} Python files", flush=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    commands = [[sys.executable, "scripts/generate_schemas.py", "--check"],
                # The edge Worker mirrors the Python queries by hand, so the two have to be
                # compared by something other than a person remembering to.
                [sys.executable, "scripts/sql_parity.py", "--check"],
                # The article parser decides whether evidence can be placed relative to
                # participation, which decides whether a reward may be credited.
                [sys.executable, "scripts/generate_article_golden.py", "--check"],
                # The two AI pipelines whose hashes the lifecycle commits. A port that reaches
                # the same verdict through a different conversation has not been ported.
                [sys.executable, "scripts/generate_resolution_golden.py", "--check"],
                [sys.executable, "scripts/generate_dispute_golden.py", "--check"],
                # The compiler wire decides the deadline every published question commits to.
                [sys.executable, "scripts/generate_compiler_golden.py", "--check"],
                # The front door end to end: four calls, one collection, ten artifacts a case.
                [sys.executable, "scripts/generate_compile_golden.py", "--check"],
                # The early-resolution path: the only way a positive result is reached without a
                # deadline passing, so its refusals matter as much as its acceptances.
                [sys.executable, "scripts/generate_early_golden.py", "--check"],
                # A display translation is presentation only, and this is what that costs.
                [sys.executable, "scripts/generate_translation_golden.py", "--check"],
                # The operator's own translation write: fifteen refusals, a replay that appends
                # nothing, and a correction that appends exactly one audit row.
                [sys.executable, "scripts/generate_translation_admin_golden.py", "--check"],
                # The market is where points are committed; a price that matches is not enough
                # if the fill that charged for it recorded a different receipt.
                [sys.executable, "scripts/generate_market_golden.py", "--check"],
                # Sandbox billing is nonbillable, but it is still a state machine over two pools
                # of capital that no sequence of events may leave owing more than it holds.
                [sys.executable, "scripts/generate_billing_golden.py", "--check"],
                # Product KPIs are definitions rather than arithmetic: a merged alias counted
                # twice, or an immature window reported as zero, is a plausible wrong answer.
                [sys.executable, "scripts/generate_analytics_golden.py", "--check"],
                # The Solana wire codecs: a port one byte out produces a rejected transaction,
                # or a different valid instruction.
                [sys.executable, "scripts/generate_solana_golden.py", "--check"],
                # The Seeker parsers read someone else's RPC reply, so leniency is the one
                # failure mode: it would report a verified Seeker the reference did not.
                [sys.executable, "scripts/generate_seeker_golden.py", "--check"],
                # The wallet codecs decide whether a sign-in is genuine: more permissive than
                # the reference is a security bug, not a compatibility one.
                [sys.executable, "scripts/generate_wallet_golden.py", "--check"],
                # Authentication is two statements that look like one. The session guard has two
                # branches with different meanings, and login has to tell a wrong code from a
                # lost race from an outage — all three are 401-shaped or 409-shaped otherwise.
                [sys.executable, "scripts/generate_auth_golden.py", "--check"],
                # Wallet sign-in is where a signature creates an account and replaces a recovery
                # credential, and the message it signs is the authorization — so the vector keeps
                # the verifier's call log, not just the outcomes.
                [sys.executable, "scripts/generate_wallet_login_golden.py", "--check"],
                # Python has two canonical-JSON encodings, one keyword argument apart, and the
                # reference uses both — for commitments and for the hashes that key rows. They
                # agree on every ASCII value, so picking the wrong one is invisible until the
                # first accent, and then it is a different digest for the same request.
                [sys.executable, "scripts/generate_canonical_json_golden.py", "--check"],
                # The signed feed is the live path: its cohorts decide a signed probability, and
                # its guard batch is the only thing standing between a raced snapshot and a
                # publication that never should have been signed.
                [sys.executable, "scripts/generate_reputation_golden.py", "--check"],
                # The publication lifecycle itself, including the bytes the signer was handed —
                # the signature is over a specific text, not over the outcome.
                [sys.executable, "scripts/generate_risk_feed_golden.py", "--check"],
                # v2 carries *typed* targets, so the failure this guards is not an error but a
                # feed that signs a probability about a window the question never asked about.
                [sys.executable, "scripts/generate_risk_feed_v2_golden.py", "--check"],
                # Recurring episodes publish and bind *before* their window opens, through the
                # ordinary seed path — so the cadence arithmetic and the retry backoff are the
                # whole of what can go quietly wrong.
                [sys.executable, "scripts/generate_risk_feed_series_golden.py", "--check"],
                # The Devnet memo attestation hands the phone an *incomplete* transaction: one real
                # signature and one zeroed slot the wallet fills. The zeroes are the contract.
                [sys.executable, "scripts/generate_attestation_golden.py", "--check"],
                # The AI failure → HTTP failure table. Its unknown codes are deliberately neutral,
                # and "say more" and "try again later" are different statuses.
                [sys.executable, "scripts/generate_ai_error_golden.py", "--check"],
                # The operator refresh: an AI call is awaited inside it, so the guard batch is what
                # keeps a revoked binding from being written under a lease that was already stale.
                [sys.executable, "scripts/generate_risk_refresh_golden.py", "--check"],
                # The automation layer's portable half: a *bounded* hint list, a mapping keyed on
                # host and path, and a status whose `enabled` is not the flag it is passed.
                [sys.executable, "scripts/generate_automation_golden.py", "--check"],
                # The only code in the port that spends money. Every method is a sequence of
                # refusals, and the vector records the RPC calls in order because the order is the
                # safety property.
                [sys.executable, "scripts/generate_solana_rpc_golden.py", "--check"],
                # The points read models: an assembled history, four onboarding states, and an
                # identifier rule that refuses rather than reporting a missing position.
                [sys.executable, "scripts/generate_points_golden.py", "--check"],
                # The profile card: one snapshot read, three metric divisions, and a publication the
                # public lookup verifies against the hash it is stored under.
                [sys.executable, "scripts/generate_profile_card_golden.py", "--check"],
                # The automation orchestration half: the pause `accept` requires, the upgrade that
                # re-reads retained bytes, the release only an observer's hold may earn, and the
                # poller driven through the automation object end to end.
                [sys.executable, "scripts/generate_automation_run_golden.py", "--check"],
                # The outbox and the sweep that drains it: the exactly-once selection, the blocked
                # forecast, the adapter hand-off, and the set-based settlement.
                [sys.executable, "scripts/generate_outbox_golden.py", "--check"],
                # The one place a *person* hands the watcher a page: two URL refusals that mean
                # different things, three statuses that are three facts, and a daily bound.
                [sys.executable, "scripts/generate_evidence_report_golden.py", "--check"],
                # The exceptional ADMIN path: a supplied verdict enters the record without a model,
                # and the receipt is what makes a retry a retry rather than a second adjudication.
                [sys.executable, "scripts/generate_adjudication_golden.py", "--check"],
                # The two DISPUTED outcomes the sweep reaches without asking anything: a material
                # conflict escalates, and its absence leaves the proposal standing.
                [sys.executable, "scripts/generate_dispute_sweep_golden.py", "--check"],
                # What the compiler is allowed to see: the ranking, the terms, and the byte budget
                # that decides which candidates reach the model at all.
                [sys.executable, "scripts/generate_candidates_golden.py", "--check"]]
    if args.tools:
        for tool in ("ruff", "mypy"):
            if shutil.which(tool) is None:
                parser.error(f"{tool} is unavailable; run dependency-free checks without --tools")
        commands.extend([
            ["ruff", "check", "packages", "tests", "scripts", "apps/web/src"],
            ["mypy", "--strict", "packages/domain/src", "packages/application/src"],
        ])
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
