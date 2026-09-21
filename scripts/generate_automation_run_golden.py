#!/usr/bin/env python3
"""Export `ForecastAutomation`'s orchestration half, so a Rust port can be held to it.

The portable half — the family hints, the publisher-root→feed mapping and the operator view —
already has a vector. What is left is the half that *acts*, and it is the only place in the
reference where a retained official article can close a question before its deadline. Eleven
methods, and the wiring between them is what this pins:

  * `hold` pauses participation, and it is the *only* thing that may, so `accept` refuses unless a
    hold is already active. The order matters: a missing hold is refused before any eligibility
    work runs, so a refusal leaves no half-applied correction behind.
  * `accept` is where four subsystems meet under one revision CAS — eligibility classifies and
    adjusts receipts, the market refunds a proven late suffix, the completion barrier is checked,
    and only then does the lifecycle lock. The guard re-checks the hold *and* the completion inside
    the same batch as the lock, which is what makes a hold released mid-flight unable to slip an
    upgrade through.
  * `accept` is idempotent for the *same* trigger and refuses for a different one. That is the
    difference between a retry and a second, later event trying to claim a question it did not
    close.
  * `dismiss` releases only an observer-created hold, only on a distinctly counter-reviewed
    unrelated article, and only when no other unresolved review remains. An operator's hold is
    never auto-released.
  * `bootstrap` registers the *feed* rather than the newsroom page, and never rewrites the
    specification it was derived from.

The `run` cases are the capstone and the only place the poller is exercised through the automation
object: one where the watched feed is polled, retained, held, reviewed and dismissed in a single
pass, and one where the watcher is disabled and says so.

Every case runs on its own migrated database and the vector records **every table in the deployed
schema**, in rowid order. All of them rather than a chosen list: this layer writes through
eligibility, the market ledger and the points ledger, and a fixture that enumerated the tables it
*expected* to change would not notice the one it changed by accident.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.ai import CompileResult  # noqa: E402
from forecast_application.automation import ForecastAutomation  # noqa: E402
from forecast_application.errors import AppError  # noqa: E402
from forecast_application.source_watch import _json, article_content  # noqa: E402
from forecast_application.sources import SourceCollector, TextResponse  # noqa: E402
from forecast_domain.serialization import to_dict  # noqa: E402

from tests import model_fixtures as model  # noqa: E402
from tests.test_automation_dismissal import AutomationDismissalTests  # noqa: E402
from tests.test_automation_integration import (  # noqa: E402
    SOURCE_BODY,
    AutomationIntegrationTests,
)
from tests.test_source_watch import URL, SourceWatchAITests  # noqa: E402
from tests.test_web_ai import specification  # noqa: E402

GOLDEN = ROOT / "tests/golden/automation-run-golden.json"

# The article the early-resolution tests judge, retained exactly as they retain it.
ARTICLE = '<main>Apple officially announces Product X. The exact published official announcement and required specifications are confirmed.</main>'

APPLE_QUESTION = "Will Apple officially announce a foldable phone before the deadline?"
APPLE_ROOT = "https://www.apple.com/newsroom"
APPLE_FEED = "https://www.apple.com/newsroom/rss-feed.rss"


async def tables(case) -> dict:
    """Every table in the deployed schema, in rowid order."""
    names = [
        row["name"]
        for row in await case.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: [dict(row) for row in await case.db.all(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}


async def fixture(cls):
    """A fixture whose token stream is written down.

    The tokens are opaque, so a vector that recorded only the rows they produced could not be
    replayed: the hold id *is* a token. Recording the sequence turns an unverifiable value into a
    verified one, and — because the replay must consume them in the same order and the same number
    — a port that took one token where the reference took two fails rather than passing quietly.
    """
    case = cls(methodName="runTest")
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
    """One case: the state before the call, what it answered, and the state after.

    The *initial* rows travel with the vector because a replay has to start somewhere, and the only
    honest starting point is the reference's own. Reconstructing a fixture by hand is how a golden
    ends up testing the reconstruction.
    """
    # The clock travels with the case because a replay has to be *at* the same instant: several
    # refusals below are time-dependent, and a fixture that restored the rows but not the clock
    # would be replaying a different moment.
    compare = inputs.pop("compare", [])
    produced = getattr(case, "produced", None)
    start = len(produced) if produced is not None else 0
    entry: dict = {"call": name, "input": inputs, "compare": compare}
    if case is not None:
        entry["initial"] = await tables(case)
    try:
        entry["result"] = await action(case)
    except AppError as error:
        entry["error"] = {"status": error.status, "code": error.code, "message": error.message}
    # The clock is read *after* the call, because an action may move it: the reference's own
    # `_sweep` sets `self.now` before running, and a replay has to be at the instant the pass ran.
    entry["now"] = case.now if case is not None else None
    if produced is not None:
        entry["tokens"] = produced[start:]
    if case is not None:
        entry["rows"] = await tables(case)
    return entry


async def integration() -> AutomationIntegrationTests:
    return await fixture(AutomationIntegrationTests)


async def dismissal() -> AutomationDismissalTests:
    return await fixture(AutomationDismissalTests)


async def apple_forecast(case):
    """A published question whose official primary source is the Apple newsroom root.

    The feed mapping is keyed on the root, so reaching it at all needs a question whose published
    source is that root rather than an article under it.
    """

    original = case.ai.compile_question

    async def apple_compile(question, candidates, now_ms):
        result = await original(question, candidates, now_ms)
        policy = result.specification.source_policy
        source = replace(policy.primary_sources[0], url=APPLE_ROOT, is_official=True)
        spec = replace(result.specification, source_policy=replace(policy, primary_sources=(source,)))
        return CompileResult(spec, model.validation(spec, validated_at_ms=now_ms), result.artifacts)

    case.ai.compile_question = apple_compile
    draft = await case.app.compile_forecast(case.uid, APPLE_QUESTION)
    return (await case.app.publish_forecast(case.uid, draft["draftId"], "publish-apple-feed-test"))["forecast"]


async def closed(case) -> None:
    """Advance the fixture's forecast past its deadline so it is no longer OPEN."""
    case.now = case.card["closeAt"] + 1000
    await case.app.run_due_jobs()


def collector(responses: list):
    """A collector with no network, answering from a script."""

    async def fetch(url, method="GET", headers=None):
        for response in responses:
            if response[0] == url:
                return response[1]
        raise AssertionError(f"No scripted response for {url}")

    return SourceCollector(fetch)


def coordinator_for(outputs: list, body: str = ARTICLE, independent: bool = True, reader=None):
    """The early-resolution tests' own coordinator, over a scripted transport.

    Built on a scratch case because that helper lives on the AI suite and the wiring it sets up —
    two distinct providers, a reader that answers from the retained body — is exactly what the
    review contract is a contract *with*.

    `reader` replaces the reader with the application's own, which is what a review running inside
    the watcher needs: the observation it judges was stored by the watcher, not by this fixture.
    """
    scratch = SourceWatchAITests(methodName="runTest")
    ai = scratch.setup_ai(outputs, independent=independent, body=body)
    if reader is not None:
        ai.read_artifact = reader
    return ai, scratch.observation, scratch.forecast


def verified(relevant: bool = True):
    return {"verified": True, "relevant": relevant,
            "explanation": "Official article with substantive source evidence."}


def qualified():
    return {"positive_existential": True, "all_conditions_satisfied": True, "irreversible": True,
            "invalidation_clear": True,
            "explanation": "The official article establishes the exact immutable announcement conditions."}


def counter(agrees: bool = True):
    return {"agrees": agrees, "explanation": "Independent provider verified every exact condition.",
            "event_time_basis": "observed_upper_bound", "event_not_after_ms": 2000}


async def build() -> dict:
    cases: list[dict] = []

    # --- load: the record every other method reads, and the refusal when there is none.
    case = await integration()
    cases.append(await record(case, lambda c: c.app.automation.load(c.fid), "load",
                              forecastId=case.fid, compare=["result"]))
    cases.append(await record(case, lambda c: c.app.automation.load("f-none"), "load:missing",
                              forecastId="f-none", compare=["result", "error"]))
    case.connection.close()

    # --- bootstrap: the feed, registered once however often it is asked, and never rewriting the
    # specification it was derived from.
    case = await integration()
    card = await apple_forecast(case)
    before = await case.app._forecast(card["id"])
    case.app.ai.collector = collector([])
    watcher = ForecastAutomation(case.app, enabled=True)

    async def bootstrap(c):
        first = await watcher.bootstrap()
        second = await watcher.bootstrap()
        return {"first": first, "second": second,
                "sources": await c.db.all("SELECT * FROM official_watch_sources ORDER BY id"),
                "bindings": await c.db.all("SELECT * FROM official_watch_bindings ORDER BY forecast_id,source_id"),
                "specificationUnchanged": (await c.app._forecast(card["id"])) == before}

    cases.append(await record(case, bootstrap, "bootstrap", forecastId=card["id"]))
    case.connection.close()

    # bootstrap with a hold whose evidence is on the watched publisher: the article is registered
    # and pinned, so the page that closed the question is re-read rather than retired by the window.
    case = await integration()
    card = await apple_forecast(case)
    case.app.ai.collector = collector([])
    watcher = ForecastAutomation(case.app, enabled=True)
    await watcher.bootstrap()
    trigger = await case.reviewed_trigger(url=APPLE_ROOT + "/2026/09/apple-unveils-product-x/")
    await watcher.hold(card["id"], {"id": trigger.trigger_hash, "url": trigger.evidence[0].url})

    async def bootstrap_held(c):
        await watcher.bootstrap()
        return {"sources": await c.db.all("SELECT * FROM official_watch_sources ORDER BY id"),
                "bindings": await c.db.all("SELECT * FROM official_watch_bindings ORDER BY forecast_id,source_id")}

    cases.append(await record(case, bootstrap_held, "bootstrap:held", forecastId=card["id"]))
    case.connection.close()

    # A disabled watcher is disabled however strongly it is asked, and bootstrap is a no-op.
    case = await integration()
    card = await apple_forecast(case)
    disabled = ForecastAutomation(case.app, enabled=True)
    cases.append(await record(case, lambda c: disabled.bootstrap(), "bootstrap:disabled",
                              forecastId=card["id"], compare=["result"]))
    case.connection.close()

    # --- hold: the pause that accept requires, and its idempotence.
    case = await integration()
    trigger = await case.reviewed_trigger()

    watched = {"id": trigger.trigger_hash, "url": trigger.evidence[0].url}

    async def hold(c):
        await c.app.automation.hold(c.fid, watched)
        await c.app.automation.hold(c.fid, watched)
        return await c.app.participation_holds.status(c.fid)

    cases.append(await record(case, hold, "hold", forecastId=case.fid, observation=watched))
    case.connection.close()

    # A hold cannot be taken on a question that is no longer open: the pause that protects an
    # upgrade is a pause on *entry*, and entry has closed.
    case = await integration()
    trigger = await case.reviewed_trigger()
    await closed(case)
    cases.append(await record(
        case,
        lambda c: c.app.automation.hold(
            c.fid, {"id": trigger.trigger_hash, "url": trigger.evidence[0].url}),
        "hold:not-open", forecastId=case.fid,
        observation={"id": trigger.trigger_hash, "url": trigger.evidence[0].url}))
    case.connection.close()

    # --- accept: the upgrade, and one refusal at each gate in the order the reference checks them.
    case = await integration()
    trigger = await case.reviewed_trigger()
    await case.app.automation.hold(case.fid, {"id": trigger.trigger_hash, "url": trigger.evidence[0].url})

    async def accept(c):
        await c.app.automation.accept(c.fid, {"trigger": to_dict(trigger)})
        # A second accept of the same trigger is a retry, and a retry changes nothing.
        await c.app.automation.accept(c.fid, {"trigger": to_dict(trigger)})
        record_ = await c.app._forecast(c.fid)
        return {"state": record_.state.value,
                "revision": record_.revision,
                "earlyTrigger": to_dict(record_.early_trigger) if hasattr(record_, "early_trigger") else None}

    cases.append(await record(case, accept, "accept", forecastId=case.fid, trigger=to_dict(trigger)))
    case.connection.close()

    # A different event cannot claim a question another event already closed. The second trigger
    # binds its own evidence, so building one that validates means describing a different page.
    case = await integration()
    trigger = await case.reviewed_trigger()
    await case.app.automation.hold(case.fid, {"id": trigger.trigger_hash, "url": trigger.evidence[0].url})
    await case.app.automation.accept(case.fid, {"trigger": to_dict(trigger)})
    other = await case.reviewed_trigger(retain=False, content=SOURCE_BODY + " A later announcement.")
    cases.append(await record(
        case, lambda c: c.app.automation.accept(c.fid, {"trigger": to_dict(other)}),
        "accept:different-trigger", forecastId=case.fid, trigger=to_dict(other)))
    case.connection.close()

    # Without a hold, nothing is upgraded — and nothing is half-applied either.
    case = await integration()
    trigger = await case.reviewed_trigger()
    cases.append(await record(
        case, lambda c: c.app.automation.accept(c.fid, {"trigger": to_dict(trigger)}),
        "accept:no-hold", forecastId=case.fid, trigger=to_dict(trigger)))
    case.connection.close()

    # The evidence is re-read from retained bytes and re-hashed: a store that lost them cannot be
    # upgraded from, whatever the trigger says.
    case = await integration()
    trigger = await case.reviewed_trigger(retain=False)
    await case.app.automation.hold(case.fid, {"id": trigger.trigger_hash, "url": trigger.evidence[0].url})
    cases.append(await record(
        case, lambda c: c.app.automation.accept(c.fid, {"trigger": to_dict(trigger)}),
        "accept:evidence-missing", forecastId=case.fid, trigger=to_dict(trigger)))
    case.connection.close()

    # Past the deadline an accounting correction may still be recorded, but it cannot backdate an
    # early lock: the ordinary lifecycle keeps its own deadline and the key says so.
    case = await integration()
    trigger = await case.reviewed_trigger()
    await case.app.automation.hold(case.fid, {"id": trigger.trigger_hash, "url": trigger.evidence[0].url})
    case.now = case.card["closeAt"] + 1

    async def accept_late(c):
        await c.app.automation.accept(c.fid, {"trigger": to_dict(trigger)})
        record_ = await c.app._forecast(c.fid)
        return {"state": record_.state.value,
                "earlyTrigger": to_dict(record_.early_trigger) if hasattr(record_, "early_trigger") else None}

    cases.append(await record(case, accept_late, "accept:late", forecastId=case.fid, trigger=to_dict(trigger)))
    case.connection.close()

    # An `observed_upper_bound` event requires *zero* market receipts: the bound is the observation,
    # so a fill anywhere around it cannot be placed before the event. One active fill is therefore
    # enough to refuse the upgrade with `early_eligibility_review` and leave the correction on the
    # retry queue, which is what `retry_eligibility` exists to resume.
    case = await integration()
    await case.market_receipt()
    trigger = await case.reviewed_trigger(basis="observed_upper_bound")
    await case.app.automation.hold(case.fid, {"id": trigger.trigger_hash, "url": trigger.evidence[0].url})
    cases.append(await record(
        case, lambda c: c.app.automation.accept(c.fid, {"trigger": to_dict(trigger)}),
        "accept:eligibility-review", forecastId=case.fid, trigger=to_dict(trigger)))
    cases.append(await record(case, lambda c: c.app.automation.retry_eligibility(), "retry_eligibility:resumed",
                              limit=3, compare=["result"]))
    case.connection.close()

    # --- retry_eligibility: nothing to resume is not a failure.
    case = await integration()
    cases.append(await record(case, lambda c: c.app.automation.retry_eligibility(),
                              "retry_eligibility:empty", limit=3, compare=["result"]))
    case.connection.close()

    # --- dismiss: releasing an observer's hold, and every reason not to.
    case = await dismissal()
    forecast, review_id, result = await case.prepare()
    await case.app.automation.hold(forecast["id"], result["observation"])

    async def dismiss_released(c):
        await c.app.automation.dismiss(forecast["id"], result)
        # The second dismissal finds no hold of ours to release, so it is a no-op rather than a
        # second revision.
        await c.app.automation.dismiss(forecast["id"], result)
        return {"active": await c.app.participation_holds.active(forecast["id"]),
                "status": await c.app.participation_holds.status(forecast["id"])}

    cases.append(await record(case, dismiss_released, "dismiss:released",
                              forecastId=forecast["id"], result=result, reviewId=review_id))
    case.connection.close()

    case = await dismissal()
    forecast, _, result = await case.prepare()
    await case.app.participation_holds.change(forecast["id"], {
        "action": "hold", "expectedRevision": 0, "expectedHoldId": None,
        "specificationHash": forecast["specificationHash"], "reason": "known_outcome_review",
        "evidenceUrl": result["observation"]["url"], "idempotencyKey": "operator-hold-only"})

    async def dismiss_operator(c):
        await c.app.automation.dismiss(forecast["id"], result)
        await c.app.automation.dismiss(forecast["id"], {**result, "dismissible": False})
        return {"active": await c.app.participation_holds.active(forecast["id"]),
                "status": await c.app.participation_holds.status(forecast["id"])}

    cases.append(await record(case, dismiss_operator, "dismiss:operator-hold",
                              forecastId=forecast["id"], result=result))
    case.connection.close()

    # A second unresolved review keeps the hold until the last safe dismissal.
    case = await dismissal()
    forecast, _, first = await case.prepare()
    _, second_key, second = await case.prepare(suffix="b", forecast=forecast)
    await case.db.execute("UPDATE official_source_reviews SET state='pending',result=NULL WHERE id=?", (second_key,))
    await case.app.automation.hold(forecast["id"], first["observation"])

    async def dismiss_unresolved(c):
        await c.app.automation.dismiss(forecast["id"], first)
        held = await c.app.participation_holds.active(forecast["id"])
        await c.db.execute("UPDATE official_source_reviews SET state='reviewed',result=? WHERE id=?",
                           (json.dumps(second), second_key))
        await c.app.automation.dismiss(forecast["id"], second)
        return {"afterFirst": held,
                "afterSecond": await c.app.participation_holds.active(forecast["id"]),
                "status": await c.app.participation_holds.status(forecast["id"])}

    cases.append(await record(case, dismiss_unresolved, "dismiss:unresolved", forecastId=forecast["id"],
                              first=first, second=second, secondReviewId=second_key,
                              # The exact text the harness writes back, because the fixture's own
                              # `json.dumps` is neither sorted nor compact and a replay that
                              # re-encoded it would be testing its encoder.
                              secondStored=json.dumps(second)))
    case.connection.close()

    # A poll in flight is not evidence: the release is refused while the source is leased.
    case = await dismissal()
    forecast, _, result = await case.prepare()
    await case.app.automation.hold(forecast["id"], result["observation"])
    await case.db.execute("UPDATE official_watch_sources SET lease_until=?", (case.now + 10000,))

    async def dismiss_polling(c):
        try:
            await c.app.automation.dismiss(forecast["id"], result)
            refusal = None
        except AppError as error:
            refusal = {"status": error.status, "code": error.code, "message": error.message}
        await c.db.execute("UPDATE official_watch_sources SET lease_until=0")
        await c.app.automation.dismiss(forecast["id"], result)
        return {"refusal": refusal, "afterRetry": await c.app.participation_holds.active(forecast["id"])}

    cases.append(await record(case, dismiss_polling, "dismiss:polling", forecastId=forecast["id"],
                              result=result))
    case.connection.close()

    # --- check_creation: the compile gate, and the retained article that closes it.
    case = await integration()
    card = await apple_forecast(case)
    case.app.ai.collector = collector([])
    watcher = ForecastAutomation(case.app, enabled=True)
    spec = (await case.app._forecast(card["id"])).specification
    cases.append(await record(case, lambda c: watcher.check_creation(spec), "check_creation:unknown",
                              forecastId=card["id"], question=spec.canonical_question))

    # A retained observation from the published official host, whose excerpt the family hints match.
    # The gate then asks the model whether the event is already established.
    artifact = hashlib.sha256(ARTICLE.encode()).hexdigest()
    await case.db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,'source',?,'text/plain',?)",
                          (artifact, ARTICLE, case.now))
    # The content commitment is the watch's own: recomputed from the retained bytes exactly as the
    # freshness gate recomputes it. A hand-picked hash is not a commitment, and the gate says so.
    text, publication, precision = article_content(ARTICLE)
    commitment = json.dumps({"text": text, "publicationDate": publication, "datePrecision": precision},
                            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    observation = {"id": "observation-freshness", "sourceId": "source", "url": URL,
                   "contentHash": hashlib.sha256(commitment.encode()).hexdigest(), "artifactHash": artifact,
                   "excerpt": "Apple officially announces a folding display in its new phone.",
                   "observedAt": case.now}
    await case.db.execute(
        "INSERT OR IGNORE INTO official_watch_sources(id,url,kind,interval_ms,next_poll,checked_at) "
        "VALUES('source',?,'index',300000,?,?)", (URL, case.now, case.now))
    await case.db.execute(
        "INSERT INTO official_source_observations VALUES(?,?,?,?,?,?,?)",
        (observation["id"], "source", URL, observation["contentHash"], artifact,
         json.dumps(observation), case.now))
    ai, _, _ = coordinator_for([{"status": "uncertain", "monotonic_positive": False,
                                 "all_conditions_satisfied": False,
                                 "explanation": "The official page only mentions a related product."}],
                               reader=case.app.read_artifact)
    case.app.ai = ai
    case.app.ai.collector = collector([])
    watcher = ForecastAutomation(case.app, enabled=True)
    cases.append(await record(case, lambda c: watcher.check_creation(spec), "check_creation:known",
                              forecastId=card["id"], question=spec.canonical_question,
                              responses=[{"status": "uncertain", "monotonic_positive": False,
                                          "all_conditions_satisfied": False,
                                          "explanation": "The official page only mentions a related product."}]))
    case.connection.close()

    # --- status: the operator view, through the automation object rather than the free function.
    case = await integration()
    card = await apple_forecast(case)
    case.app.ai.collector = collector([])
    enabled = ForecastAutomation(case.app, enabled=True)
    await enabled.bootstrap()
    cases.append(await record(case, lambda c: enabled.status(), "status", compare=["result"]))
    case.connection.close()

    # --- review: the delegation, and the exact record the watcher stores for it.
    ai, observation, forecast = coordinator_for([verified(), qualified(), counter()])

    class Bare:
        """Only the two attributes `review` reads, so the delegation is what is under test."""

        now_ms = staticmethod(lambda: 3000)  # REVIEWED_AT_MS

        class _AI:
            pass

        ai = _AI()

    Bare.ai = ai
    watcher = ForecastAutomation(Bare(), enabled=False)

    async def review(c):
        result = dict(await watcher.review(forecast, observation))
        artifacts = tuple(result.pop("artifacts", ()))
        if result.get("trigger") is not None:
            result["trigger"] = to_dict(result["trigger"])
        # `stored` is the exact bytes the review row would hold, which is the contract the port has
        # to meet: the accept branch carries a `trigger` and no `dismissible` at all, and a port
        # that filled the key in would store a different row.
        return {"stored": _json(result), "keys": sorted(result.keys()),
                "artifacts": [{"kind": item.kind, "hash": item.content_hash, "body": item.body}
                              for item in artifacts]}

    cases.append(await record(None, review, "review:accepted",
                              forecast=forecast, observation=observation,
                              responses=[verified(), qualified(), counter()]))

    # A third shape: the page's own date is outside the question's window, so the event cannot be
    # placed and the answer is an uncertainty rather than a qualification. Note what it does *not*
    # carry — no `trigger`, and no `dismissible` either, because a page whose timing is unknown is
    # not a fact a second provider can certify. The `dismissible` shape is pinned by the `run` case,
    # whose stored review record is the reference's own.
    dated = ('<html><meta property="article:published_time" content="2000-01-01">'
             '<main>Apple officially announces Product X.</main></html>')
    ai, observation, forecast = coordinator_for([], body=dated)

    class Dated(Bare):
        _AI = None

    Dated.ai = ai
    watcher = ForecastAutomation(Dated(), enabled=False)
    cases.append(await record(None, review, "review:uncertain",
                              forecast=forecast, observation=observation, responses=[]))

    # --- run: the capstone. Disabled it reports why rather than pretending to have polled.
    case = await integration()
    card = await apple_forecast(case)
    cases.append(await record(case, lambda c: ForecastAutomation(c.app, enabled=False).run(),
                              "run:disabled", limit=2, compare=["result"]))
    case.connection.close()

    # Enabled, over a registered feed that publishes one relevant article: bootstrap, poll, retain,
    # hold, review, and the release a distinctly dismissible review earns.
    #
    # The article is dated before the question opened, which is the deterministic branch: an event
    # that predates the question can never resolve it, so the review is dismissible without asking a
    # model. The `dismissible` branch is reached before any provider call, which is why this case
    # needs no scripted model output — and why the scripted *collector* below is the whole
    # environment the replay needs.
    case = await integration()
    card = await apple_forecast(case)
    feed_body = ('<rss version="2.0"><channel><title>Apple Newsroom</title><item>'
                 f'<title>Apple unveils a foldable phone</title>'
                 f'<link>{APPLE_ROOT}/2026/09/apple-unveils-product-x/</link>'
                 f'<pubDate>Wed, 10 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>')
    article_body = ('<html><meta property="article:published_time" content="2026-09-10">'
                    '<main>Apple officially announces a foldable phone.</main></html>')
    # The response travels whole — status and content type with the body. A collector that got
    # only bytes could not tell the feed from the article, and the watch treats those differently.
    script = {
        APPLE_FEED: {"status": 200, "contentType": "application/rss+xml", "body": feed_body},
        APPLE_ROOT + "/2026/09/apple-unveils-product-x/": {
            "status": 200, "contentType": "text/html", "body": article_body},
    }
    reviewer, _, _ = coordinator_for([], reader=case.app.read_artifact)
    reviewer.collector = collector([
        (APPLE_FEED, TextResponse(200, feed_body, {"content-type": "application/rss+xml"})),
        (APPLE_ROOT + "/2026/09/apple-unveils-product-x/",
         TextResponse(200, article_body, {"content-type": "text/html"})),
    ])
    case.app.ai = reviewer
    watcher = ForecastAutomation(case.app, enabled=True)

    async def run(c):
        # Twice, because the article is *discovered* by the first pass — the feed index carries no
        # announcement of its own, so there is nothing relevant to retain until the article it links
        # is polled.
        summary = [await watcher.run(), await watcher.run()]
        return {"summary": summary,
                "hold": await c.app.participation_holds.active(card["id"]),
                "reviews": await c.db.all(
                    "SELECT id,state,result,last_error FROM official_source_reviews ORDER BY id")}

    cases.append(await record(case, run, "run", limit=2, fetcher=script, compare=["reviews", "summary"]))
    case.connection.close()

    # --- run_automation: the operator's one call, and what it does when there is nothing to do.
    case = await integration()
    card = await apple_forecast(case)
    cases.append(await record(case, lambda c: c.app.run_automation(limit=2), "run_automation:idle",
                              forecastId=card["id"], limit=2, compare=["result"]))
    case.connection.close()

    # And with a market whose question has finalized: the last step is the one that would stay
    # broken quietly, because a settled market and an unsettled one look the same until a holder
    # tries to withdraw.
    case = await integration()
    await case.market_receipt()
    forecast = case.card
    case.now = forecast["closeAt"] + 1000
    await case.app.run_due_jobs()
    record_ = await case.app._forecast(forecast["id"])
    case.now = record_.challenge_until_ms + 1
    await case.app.run_due_jobs()
    cases.append(await record(case, lambda c: c.app.run_automation(limit=2), "run_automation:settle",
                              forecastId=forecast["id"], limit=2, compare=["result"]))
    case.connection.close()

    return {
        "description": "The automation orchestration half: the hold that pauses participation, the "
                       "upgrade that reads retained bytes and refuses without a pause, the dismissal "
                       "that releases only an observer's hold, and the watcher's own lifecycle.",
        "article": ARTICLE,
        "sourceBody": SOURCE_BODY,
        "appleQuestion": APPLE_QUESTION,
        "appleFeed": APPLE_FEED,
        "specification": to_dict(specification()),
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
