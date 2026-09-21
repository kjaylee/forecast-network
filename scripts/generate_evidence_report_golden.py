#!/usr/bin/env python3
"""Export `report_evidence`, so a Rust port can be held to it.

A report is the one place a *person* can hand the watcher a page, so three things about it are the
whole rule:

  * The URL has to belong to one of the question's **published** official sources — the
    specification's own list, not a model's suggestion — and the two refusals are different
    answers: "this is not a public https page" and "this is a public https page, but this question
    does not cite it". Collapsing them tells a reporter nothing about what to do next.
  * The three statuses are three facts. `held` means a pause is actually in force; `unrelated`
    means the page **predates the question** and can never be its resolving event; `received`
    means the watcher is switched off and nothing was fetched at all. A port that reported `held`
    whenever an observation came back would pause participation for a review that must reject it.
  * A report never resolves anything by itself. It enters the same hold-before-review path as the
    automatic watcher, and the reward is paid later, by the upgrade, to the *earliest* report whose
    retained evidence the accepted trigger cites.

The vector keeps the fetches the collector was asked for, because what a report fetches is part of
what it does: the publisher's feed is registered and the *article* is fetched, and nothing else.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from golden_cli import golden_main  # noqa: E402

from tests import test_automation_integration as fixtures  # noqa: E402
from tests.test_automation_integration import EvidenceReportTests  # noqa: E402

GOLDEN = ROOT / "tests/golden/evidence-report-golden.json"


async def tables(case) -> dict:
    names = [
        row["name"]
        for row in await case.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: [dict(row) for row in await case.db.all(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}


async def fixture(enabled: bool = True, with_watcher: bool = True):
    case = EvidenceReportTests(methodName="runTest")
    original = case.random_token
    case.produced = []

    def token():
        value = original()
        case.produced.append(value)
        return value

    case.random_token = token
    await case.asyncSetUp()
    if not with_watcher:
        return case
    case.apple = await case.apple_forecast_with_watcher()
    if not enabled:
        from forecast_application.automation import ForecastAutomation

        case.app.automation = ForecastAutomation(case.app, enabled=False)
    return case


async def plain_fixture():
    """The bare application fixture, without the Apple question this file's other cases need.

    `AutomationIntegrationTests` publishes during its own setup, which makes the fixture's fixed
    publish key unusable a second time — so a case that wants a question carried all the way to
    finalized starts from the fixture that has not published yet.
    """
    case = fixtures.application_tests.ApplicationTests(methodName="runTest")
    original = case.random_token
    case.produced = []

    def token():
        value = original()
        case.produced.append(value)
        return value

    case.random_token = token
    await case.asyncSetUp()
    case.ARTICLE_HTML = EvidenceReportTests.ARTICLE_HTML
    return case


async def record(case, action, name: str, **inputs) -> dict:
    compare = inputs.pop("compare", [])
    produced = getattr(case, "produced", None)
    start = len(produced) if produced is not None else 0
    entry: dict = {"call": name, "input": inputs, "compare": compare}
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


async def build() -> dict:
    cases: list[dict] = []
    article = EvidenceReportTests.ARTICLE

    # A URL that is not a public https page on any official publisher.
    case = await fixture()
    cases.append(await record(
        case, lambda c: c.app.report_evidence(c.other, c.apple["id"], "https://example.com/news/product-x"),
        "report:url", userId=case.other, forecastId=case.apple["id"],
        url="https://example.com/news/product-x", compare=["result"]))
    case.connection.close()

    # A public https page on an official publisher this question does not cite.
    case = await fixture()
    cases.append(await record(
        case,
        lambda c: c.app.report_evidence(c.other, c.apple["id"],
                                        "https://news.microsoft.com/source/2026/09/09/product-x/"),
        "report:source", forecastId=case.apple["id"], compare=["result"], userId=case.other, body=case.ARTICLE_HTML,
        url="https://news.microsoft.com/source/2026/09/09/product-x/"))
    case.connection.close()

    # The real thing: the publisher's feed is registered, the article is fetched, and entry pauses.
    case = await fixture()

    async def held(c):
        report = await c.app.report_evidence(c.other, c.apple["id"], article)
        held = await c.app.participation_holds.active(c.apple["id"])
        return {"report": report, "held": held is not None, "fetched": c.fetched}

    cases.append(await record(case, held, "report:held", forecastId=case.apple["id"], compare=["result"], userId=case.other, body=case.ARTICLE_HTML, url=article))
    case.connection.close()

    # The same report again is the same report, not a second one.
    case = await fixture()

    async def duplicate(c):
        first = await c.app.report_evidence(c.other, c.apple["id"], article)
        second = await c.app.report_evidence(c.other, c.apple["id"], article)
        return {"first": first, "second": second, "fetched": c.fetched}

    cases.append(await record(case, duplicate, "report:duplicate", forecastId=case.apple["id"], compare=["result"], userId=case.other, body=case.ARTICLE_HTML, url=article))
    case.connection.close()

    # A page published before the question opened cannot be its resolving event, so it is neither
    # retained for review nor allowed to pause anyone.
    case = await fixture()
    case.ARTICLE_HTML = case.ARTICLE_HTML.replace("2027-01-15T14:00:00Z", "2020-01-15T14:00:00Z")

    async def predates(c):
        report = await c.app.report_evidence(c.other, c.apple["id"], article)
        return {"report": report,
                "held": await c.app.participation_holds.active(c.apple["id"]),
                "reviews": await c.db.all("SELECT id FROM official_source_reviews")}

    cases.append(await record(case, predates, "report:predates", forecastId=case.apple["id"], compare=["result"], userId=case.other, body=case.ARTICLE_HTML, url=article))
    case.connection.close()

    # Ten a day, and the eleventh is a refusal rather than a silent drop.
    case = await fixture()

    async def limited(c):
        seen = []
        for index in range(10):
            seen.append(await c.app.report_evidence(
                c.other, c.apple["id"], f"https://www.apple.com/newsroom/2026/09/other-{index}/"))
        return seen

    cases.append(await record(case, limited, "report:ten-a-day", forecastId=case.apple["id"], compare=["result"], userId=case.other, body=case.ARTICLE_HTML))
    cases.append(await record(
        case,
        lambda c: c.app.report_evidence(c.other, c.apple["id"], "https://www.apple.com/newsroom/2026/09/eleventh/"),
        "report:rate-limited", forecastId=case.apple["id"], compare=["result"], userId=case.other,
        body=case.ARTICLE_HTML, url="https://www.apple.com/newsroom/2026/09/eleventh/"))
    case.connection.close()

    # A question that is no longer taking participation is not taking evidence for it either.
    #
    # Carried there by the application rather than written into place: the domain refuses a
    # finalized snapshot without a resolution, and a fixture that forged one would be testing the
    # forging.
    #
    # Reported against the *fixture's own* question rather than the Apple one, because the state is
    # checked before the URL is — a question that has closed refuses every report, not only the
    # ones from its own sources.
    case = await plain_fixture()
    forecast = await fixtures.application_tests.ApplicationTests.challenge(case)
    case.now = forecast.challenge_until_ms + 1
    await case.app.run_due_jobs()
    case.apple = {"id": forecast.forecast_id}
    cases.append(await record(
        case, lambda c: c.app.report_evidence(c.other, c.apple["id"], article),
        "report:closed", forecastId=case.apple["id"], compare=["result"], userId=case.other, body=case.ARTICLE_HTML, url=article))
    case.connection.close()

    # A watcher that is switched off records the report and fetches nothing.
    case = await fixture(enabled=False)
    cases.append(await record(
        case, lambda c: c.app.report_evidence(c.other, c.apple["id"], article),
        "report:disabled", forecastId=case.apple["id"], compare=["result"], userId=case.other, body=case.ARTICLE_HTML, url=article))
    case.connection.close()

    return {
        "description": "`report_evidence`: the two URL refusals, the hold that pauses entry, the "
                       "page that predates the question, the daily bound, and the watcher that is "
                       "switched off.",
        "article": article,
        "articleHtml": EvidenceReportTests.ARTICLE_HTML,
        "appleQuestion": "Will Apple officially announce Product X before the deadline?",
        "cases": cases,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
