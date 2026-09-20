#!/usr/bin/env python3
"""Export what the sweep does with a disputed question whose disputes are all reviewed.

Two of the three outcomes a `DISPUTED` forecast can reach are decided **without asking anything**:
when no dispute is waiting to be reviewed, a material conflict among the reviews escalates the
question, and the absence of one leaves the proposal standing. That is the pair this vector pins —
and it is pinned at the *sweep* level, because the decision is not a function of the review alone:
it is a function of the reviews the forecast holds against the disputes it recorded.

The third outcome — a pending dispute being reviewed — is not here. The reference's fixture AI is a
hand-written stub that builds the review directly, so the conversation a port with a real
coordinator would have to replay does not exist; `ai-dispute-golden` holds `review_dispute` itself,
and the sweep's wiring of it is transcribed without a vector. Saying so is the point of this
paragraph: the case that is missing is named rather than absent.

The review is planted through the application's own command rather than written into the rows, so
the state the sweep starts from is one the reference can actually produce.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from forecast_domain.lifecycle import ReviewDispute  # noqa: E402

from tests import model_fixtures as model  # noqa: E402
from tests import test_web_application as fixtures  # noqa: E402

GOLDEN = ROOT / "tests/golden/dispute-sweep-golden.json"


async def tables(case) -> dict:
    names = [
        row["name"]
        for row in await case.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: [dict(row) for row in await case.db.all(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}


async def fixture():
    case = fixtures.ApplicationTests(methodName="runTest")
    original = case.random_token
    case.produced = []

    def token():
        value = original()
        case.produced.append(value)
        return value

    case.random_token = token
    await case.asyncSetUp()
    return case


async def record(case, action, name: str, **inputs) -> dict:
    compare = inputs.pop("compare", [])
    produced = getattr(case, "produced", None)
    start = len(produced) if produced is not None else 0
    entry: dict = {"call": name, "input": inputs, "compare": compare, "nowBefore": case.now}
    entry["initial"] = await tables(case)
    try:
        entry["result"] = await action(case)
    except AppError as error:
        entry["error"] = {"status": error.status, "code": error.code, "message": error.message}
    entry["now"] = case.now
    if produced is not None:
        entry["tokens"] = produced[start:]
    entry["rows"] = await tables(case)
    return entry


async def disputed(case, *, material: bool):
    """A question in DISPUTED whose only dispute has already been reviewed.

    The review is applied as the *command* it is, so the row the sweep reads is one the reference
    produced — and so that the review's own hash bindings are the domain's rather than a fixture's.
    """
    forecast = await case.challenge()
    case.now += 10
    await case.app.submit_dispute(
        case.uid, forecast.forecast_id, "판정에 중요한 반대 근거가 있습니다.",
        "https://acme.example/news", "yes-rule", "독립 심사가 필요한 근거 충돌입니다.",
        forecast.revision, f"dispute-key-{material}")
    case.now += 10
    current = await case.app._forecast(forecast.forecast_id)
    review = model.review(current.disputes[0], current.resolution, current.specification,
                          material_conflict=material, reviewed_at_ms=case.now)
    await case.app._mutate(current, ReviewDispute(review=review),
                           key=f"planted-review-{material}", now=case.now)
    case.now += 10
    return forecast.forecast_id


async def build() -> dict:
    cases: list[dict] = []

    # A material conflict among the reviews is what escalation is for.
    case = await fixture()
    forecast_id = await disputed(case, material=True)

    async def escalated(c):
        sweep = await c.app.run_due_jobs()
        return {"sweep": sweep, "state": (await c.app._forecast(forecast_id)).state.value}

    cases.append(await record(case, escalated, "dispute:escalate", forecastId=forecast_id, compare=["result"]))
    case.connection.close()

    # Without one, the proposal stands: escalation is not a thing that happens on its own.
    case = await fixture()
    forecast_id = await disputed(case, material=False)

    async def retained(c):
        sweep = await c.app.run_due_jobs()
        return {"sweep": sweep, "state": (await c.app._forecast(forecast_id)).state.value}

    cases.append(await record(case, retained, "dispute:retain", forecastId=forecast_id, compare=["result"]))
    case.connection.close()

    return {
        "description": "What the sweep decides about a disputed question whose disputes are all "
                       "reviewed: a material conflict escalates it, and the absence of one leaves "
                       "the proposal standing.",
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(asyncio.run(build()), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if arguments.write:
        GOLDEN.write_text(document)
        print(f"wrote {GOLDEN.relative_to(ROOT)}")
        return 0
    if arguments.check:
        current = GOLDEN.read_text() if GOLDEN.exists() else ""
        if current != document:
            print(f"{GOLDEN.relative_to(ROOT)} is stale; regenerate with --write", file=sys.stderr)
            return 1
        print(f"{GOLDEN.relative_to(ROOT)} is current")
        return 0
    print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
