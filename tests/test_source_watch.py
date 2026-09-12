"""Official observation persistence, network boundary and real AI contracts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

from forecast_application.ai import AIUnavailable, ProviderConfig
from forecast_application.database import SQLiteDatabase
from forecast_application.source_watch import (
    LEASE_MS,
    SourceWatch,
    article_content,
    discover_articles,
    relevant,
)
from forecast_application.sources import (
    MAX_EXCERPT_BYTES,
    MAX_SOURCE_BYTES,
    SourceCollector,
    TextResponse,
)
from forecast_domain.serialization import to_dict

from tests.test_web_ai import Transport, coordinator, specification

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.apple.com/newsroom/2026/09/apple-unveils-iphone-duo/"
BODY = '<html><meta property="article:published_time" content="2026-09-10"><main>Apple today announces iPhone Duo with a folding display. Official specifications and product announcement information.</main></html>'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class SourceWatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now = 1800000000000
        self.seq = 0
        self.forecast = {"id": "f1", "specificationHash": "a"*64, "state": "OPEN",
                         "officialSourceUrls": ["https://www.apple.com/newsroom/"], "families": ["iphone"]}
        await self.db.execute("INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u1','User','u1','recovery',0)")
        await self.db.execute("INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) VALUES('f1','u1','draft','{}',0,'OPEN','TECH','iPhone','Question','question',?,0,9999999999999,0,0,'initial')", ("a"*64,))
        self.responses = []
        self.requests = []
        self.events = []
        self.result = {"accepted": False, "reason": "not_qualified"}
        self.review_error = None
        self.review_gate = None
        self.on_review = None
        self.accept_error = None
        self.watch = SourceWatch(self.db, SourceCollector(self.fetch), lambda: self.now, self.token,
                                 load_forecast=self.load, hold=self.hold, review=self.review, accept=self.accept)

    async def asyncTearDown(self):
        self.connection.close()

    def token(self):
        self.seq += 1
        return "token"+str(self.seq)

    async def load(self, fid):
        return self.forecast

    async def fetch(self, url, method, headers):
        self.requests.append((url, dict(headers)))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return TextResponse(200, BODY, {"content-type": "text/html", "etag": '"v1"'})

    async def hold(self, fid, observation):
        self.events.append("hold")

    async def review(self, forecast, observation):
        self.events.append("review")
        if self.review_gate:
            await self.review_gate.wait()
        if self.on_review:
            await self.on_review()
        if self.review_error:
            raise self.review_error
        return dict(self.result)

    async def accept(self, fid, result):
        self.events.append("accept")
        if self.accept_error:
            raise self.accept_error

    async def registered(self):
        await self.watch.register("apple", URL, kind="article", interval_ms=60000)
        await self.watch.bind("f1", "apple", ("iphone",))

    async def count(self, table):
        return (await self.db.first("SELECT COUNT(*) AS n FROM " + table))["n"]

    async def test_hold_precedes_review_and_no_settlement(self):
        await self.registered()
        points_before = await self.count("point_ledger")
        self.assertEqual(await self.watch.run(), {"polled": 1, "reviewed": 1, "failed": 0})
        self.assertLess(self.events.index("hold"), self.events.index("review"))
        self.assertNotIn("accept", self.events)
        self.assertEqual(await self.count("point_ledger"), points_before)
        self.assertEqual(await self.count("official_source_observations"), 1)
        self.assertEqual((await self.db.first("SELECT snapshot FROM forecasts"))["snapshot"], "{}")

    async def test_conditional_304_reuses_cache_no_model(self):
        await self.registered()
        await self.watch.run()
        self.now += 60000
        self.responses.append(TextResponse(304, "", {}))
        await self.watch.run()
        self.assertEqual(self.requests[-1][1]["If-None-Match"], '"v1"')
        self.assertEqual(self.events.count("review"), 1)
        self.assertEqual(await self.count("artifacts"), 1)
        self.assertEqual((await self.db.first("SELECT checked_at FROM official_watch_sources"))["checked_at"], self.now)

    async def test_same_article_cosmetic_markup_deduplicates_review(self):
        await self.registered()
        await self.watch.run()
        self.now += 60000
        self.responses.append(TextResponse(200, BODY.replace("<main>", "<nav>updated promo</nav><main class='new'>"), {"content-type": "text/html"}))
        await self.watch.run()
        self.assertEqual(self.events.count("review"), 1)
        self.assertEqual(await self.count("official_source_observations"), 1)

    async def test_unrelated_article_does_not_call_ai_or_hold(self):
        await self.registered()
        self.responses.append(TextResponse(200, BODY.replace("iPhone Duo", "Apple Watch"), {"content-type": "text/html"}))
        await self.watch.run()
        self.assertEqual(self.events, [])
        self.assertEqual(await self.count("official_source_reviews"), 0)

    async def test_alias_family_matches_without_exact_product_name(self):
        self.assertTrue(relevant("Introducing iPhone Duo, our folding phone", ("iphone",)))
        self.assertFalse(relevant("The fictional megaiPhonecase", ("iphone",)))

    async def test_index_discovers_exact_allowlisted_article_only_no_ai(self):
        await self.watch.register("apple-index", "https://www.apple.com/newsroom/")
        self.responses.append(TextResponse(200, f'<html><body>Latest Apple announcements news feed published today.<a href="{URL}">iPhone</a><a href="https://evil.example/x">bad</a></body></html>', {"content-type": "text/html"}))
        await self.watch.run()
        self.assertEqual(await self.count("official_watch_sources"), 2)
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.requests), 1)

    async def test_redirect_to_other_official_host_rejected_before_fetch(self):
        await self.registered()
        self.responses.append(TextResponse(302, "", {"location": "https://www.microsoft.com/news/"}))
        self.assertEqual((await self.watch.run())["failed"], 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.events, [])

    async def test_published_host_binding_rejects_unrelated_source(self):
        await self.watch.register("microsoft", "https://blogs.microsoft.com/")
        with self.assertRaises(ValueError):
            await self.watch.bind("f1", "microsoft", ("iphone",))

    async def test_known_cache_gate_finds_relevant_announcements(self):
        await self.registered()
        await self.watch.run()
        known = await self.watch.check_known(self.forecast)
        self.assertEqual(len(known), 1)
        self.assertEqual(known[0]["datePrecision"], "date")
        self.assertEqual(known[0]["publicationDate"], "2026-09-10")
        self.assertNotIn("eventAt", known[0])

    async def test_failed_reviews_bounded_and_never_release_hold(self):
        await self.registered()
        self.review_error = AIUnavailable("no independent provider")
        for _ in range(3):
            await self.watch.run()
            self.now += 300000
        job = await self.db.first("SELECT * FROM official_source_reviews")
        self.assertEqual(job["state"], "exhausted")
        self.assertEqual(job["attempts"], 3)
        await self.watch.run()
        self.assertEqual(self.events.count("review"), 3)
        self.assertNotIn("accept", self.events)

    async def test_accept_retry_uses_retained_decision_without_new_ai(self):
        await self.registered()
        self.result = {"accepted": True, "reason": "qualified"}
        self.accept_error = RuntimeError("uncertain acknowledgement")
        await self.watch.run()
        self.accept_error = None
        self.now += 60000
        await self.watch.run()
        self.assertEqual(self.events.count("review"), 1)
        self.assertEqual(self.events.count("accept"), 2)
        self.assertEqual((await self.db.first("SELECT state FROM official_source_reviews"))["state"], "complete")

    async def test_review_lease_expiry_prevents_accept(self):
        await self.registered()
        self.result = {"accepted": True, "reason": "qualified"}
        async def expire():
            self.now += LEASE_MS+1
        self.on_review = expire
        await self.watch.run()
        self.assertNotIn("accept", self.events)

    async def test_daily_budget_defers_without_spending_retry(self):
        await self.registered()
        await self.db.execute("INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES('official-watch-ai',?,72,?)", (self.now//86400000, self.now+86400000))
        await self.watch.run()
        job = await self.db.first("SELECT * FROM official_source_reviews")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(job["last_error"], "daily_budget")
        self.assertNotIn("review", self.events)
        self.assertIn("hold", self.events)

    async def test_review_lease_excludes_concurrent_model_call(self):
        await self.registered()
        self.review_gate = asyncio.Event()
        task = asyncio.create_task(self.watch.run())
        for _ in range(20):
            if "review" in self.events:
                break
            await asyncio.sleep(0)
        await self.watch.run()
        self.review_gate.set()
        await task
        self.assertEqual(self.events.count("review"), 1)

    async def test_observations_immutable(self):
        await self.registered()
        await self.watch.run()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE official_source_observations SET observed_at=0")

    def test_dates_and_feed_discovery(self):
        self.assertEqual(article_content(BODY)[1:], ("2026-09-10", "date"))
        self.assertEqual(article_content(BODY.replace("2026-09-10", "invalid"))[1:], (None, "unknown"))
        self.assertEqual(discover_articles(f"<rss><channel><link>{URL}</link></channel></rss>", "https://www.apple.com/newsroom/"), (URL,))


    async def test_unknown_304_is_rejected_and_never_marks_fresh(self):
        await self.registered()
        self.responses.append(TextResponse(304, "", {}))
        result = await self.watch.run()
        self.assertEqual(result["failed"], 1)
        self.assertIsNone((await self.db.first("SELECT checked_at FROM official_watch_sources"))["checked_at"])

    async def test_rss_mime_is_bounded_xml_and_discovers_articles(self):
        await self.watch.register("apple-feed", "https://www.apple.com/newsroom/rss-feed.rss")
        self.responses.append(TextResponse(200, f"<rss><channel><title>Official Apple announcement feed with latest product news</title><item><link>{URL}</link></item></channel></rss>", {"content-type": "application/rss+xml"}))
        self.assertEqual((await self.watch.run())["failed"], 0)
        self.assertEqual(await self.count("official_watch_sources"), 2)
        self.assertEqual(self.events, [])

    async def test_uncertain_decision_commit_reuses_stored_ai_result(self):
        await self.registered()
        original = self.db.batch
        failed = False
        async def uncertain(statements):
            nonlocal failed
            result = await original(statements)
            if not failed and any("state='reviewed'" in sql for sql, _ in statements):
                failed = True
                raise RuntimeError("commit acknowledgement lost")
            return result
        self.db.batch = uncertain
        await self.watch.run()
        self.now += 60000
        await self.watch.run()
        self.assertEqual(self.events.count("review"), 1)
        self.assertEqual((await self.db.first("SELECT state FROM official_source_reviews"))["state"], "complete")

    async def test_response_byte_limit_blocks_storage_and_ai(self):
        await self.registered()
        self.responses.append(TextResponse(200, "x"*(MAX_SOURCE_BYTES+1), {"content-type": "text/plain"}))
        self.assertEqual((await self.watch.run())["failed"], 1)
        self.assertEqual(await self.count("official_source_observations"), 0)
        self.assertEqual(self.events, [])


    async def test_feed_window_retires_old_articles_but_preserves_pinned_incident(self):
        await self.watch.register("apple-index", "https://www.apple.com/newsroom/", interval_ms=60000)
        pinned_id = "article-"+digest(URL)[:32]
        await self.watch.register(pinned_id, URL, kind="article", parent_id="apple-index", pinned=True)
        old = "https://www.apple.com/newsroom/2026/08/old-article/"
        await self.watch.register("old-article", old, kind="article", parent_id="apple-index")
        fresh = "https://www.apple.com/newsroom/2026/09/latest-article/"
        self.responses.append(TextResponse(200, f'<main>Latest official announcement feed with verified publisher context.<a href="{fresh}">New product story</a></main>', {"content-type": "text/html"}))
        await self.watch.run(limit=1)
        self.assertEqual((await self.db.first("SELECT enabled FROM official_watch_sources WHERE id=?", (pinned_id,)))["enabled"], 1)
        self.assertEqual((await self.db.first("SELECT enabled FROM official_watch_sources WHERE id='old-article'"))["enabled"], 0)
        self.assertEqual((await self.db.first("SELECT enabled FROM official_watch_sources WHERE url=?", (fresh,)))["enabled"], 1)


    async def test_certified_dismissal_retry_reuses_cached_review(self):
        await self.registered()
        self.result = {"accepted": False, "dismissible": True, "reason": "unrelated_official_article"}
        calls = 0
        async def dismiss(fid, result):
            nonlocal calls
            calls += 1
            self.events.append("dismiss")
            self.assertIn("observation", result)
            self.assertEqual((await self.db.first("SELECT state FROM official_source_reviews"))["state"], "reviewed" if calls == 1 else "pending")
            if calls == 1:
                raise RuntimeError("release acknowledgement lost")
        self.watch.dismiss = dismiss
        await self.watch.run()
        self.now += 60000
        await self.watch.run()
        self.assertEqual(calls, 2)
        self.assertEqual(self.events.count("review"), 1)
        self.assertEqual((await self.db.first("SELECT state FROM official_source_reviews"))["state"], "complete")
        self.assertNotIn("accept", self.events)

    async def test_ambiguous_result_never_calls_dismissal(self):
        await self.registered()
        self.result = {"accepted": False, "dismissible": False, "reason": "conditions_not_qualified"}
        async def dismiss(fid, result):
            self.fail("Ambiguous reviews must not dismiss holds")
        self.watch.dismiss = dismiss
        await self.watch.run()
        self.assertIn("hold", self.events)


    async def test_large_official_html_retains_complete_source_without_expanding_excerpt(self):
        await self.registered()
        padded = BODY.replace("<main>", "<style>"+"x"*333000+"</style><main>")
        self.responses.append(TextResponse(200, padded, {"content-type": "text/html", "content-length": str(len(padded.encode()))}))
        result = await self.watch.run()
        self.assertEqual(result["failed"], 0)
        observed = json.loads((await self.db.first("SELECT body FROM official_source_observations"))["body"])
        self.assertEqual(observed["artifactHash"], digest(padded))
        retained = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (digest(padded),))
        self.assertEqual(retained["body"], padded)
        self.assertLessEqual(len(observed["excerpt"].encode()), MAX_EXCERPT_BYTES)
        self.assertNotIn("x"*100, observed["excerpt"])

    async def test_exact_source_limit_accepted_and_one_byte_more_rejected(self):
        await self.registered()
        prefix = BODY+"<!--"
        padded = prefix+"x"*(MAX_SOURCE_BYTES-len(prefix.encode())-3)+"-->"
        self.assertEqual(len(padded.encode()), MAX_SOURCE_BYTES)
        self.responses.append(TextResponse(200, padded, {"content-type": "text/html"}))
        self.assertEqual((await self.watch.run())["failed"], 0)
        self.now += 60000
        self.responses.append(TextResponse(200, padded+"x", {"content-type": "text/html"}))
        self.assertEqual((await self.watch.run())["failed"], 1)
        self.assertEqual(await self.count("official_source_observations"), 1)


    def test_microsoft_discovery_excludes_real_uploads_and_keeps_real_article(self):
        article = "https://news.microsoft.com/source/2026/09/09/aft-uft-and-microsoft-announce-national-ai-safety-privacy-standard-for-schools-to-protect-students-families-and-educators/"
        uploads = [
            "https://news.microsoft.com/source/wp-content/uploads/2022/10/cropped-Microsoft_logo.svg_-300x300-1.png",
            "https://news.microsoft.com/source/wp-content/uploads/2022/10/cropped-Microsoft_logo.svg_-128x120.png",
            "https://news.microsoft.com/source/2026/09/09/logo.png",
            "https://news.microsoft.com/source/2026/09/09/logo.svg",
            "https://news.microsoft.com/source/2026/09/09/video.mp4",
            "https://news.microsoft.com/other/path/2026/09/09/unverified-article/",
        ]
        markup = "".join(f'<a href="{url}">Publisher content</a>' for url in [*uploads, article])
        self.assertEqual(discover_articles(markup, "https://news.microsoft.com/source/"), (article,))

    def test_microsoft_blog_supports_only_canonical_dated_article_roots(self):
        article = "https://blogs.microsoft.com/blog/2026/09/09/product-announcement/"
        bad = "https://blogs.microsoft.com/blog/wp-content/uploads/2026/09/logo.png"
        self.assertEqual(discover_articles(f'<a href="{bad}">Logo</a><a href="{article}">Announcement</a>',
                                           "https://blogs.microsoft.com/"), (article,))


    def test_apple_jsonld_day_z_preserves_date_only_precision(self):
        metadata = {"@type": "NewsArticle", "datePublished": "2026-09-09Z",
                    "dateModified": "2026-09-09T19:09:28Z"}
        html = '<script type="application/ld+json">'+json.dumps(metadata)+'</script><main>Apple announces iPhone Duo in its official newsroom article.</main>'
        text, publication, precision = article_content(html)
        self.assertEqual((publication, precision), ("2026-09-09", "date"))
        self.assertNotIn("19:09:28", text)
        self.assertNotIn("datePublished", text)

    def test_jsonld_graph_and_list_extract_only_publication_instants(self):
        for metadata in [
            [{"@type": "Organization", "datePublished": "1999-01-01"},
             {"@type": "BlogPosting", "datePublished": "2026-09-09T17:30:00Z"}],
            {"@graph": [{"@type": ["Article", "CreativeWork"], "datePublished": "2026-09-09T17:30:00Z"}]},
            {"@type": "https://schema.org/NewsArticle", "datePublished": "2026-09-09T17:30:00Z"},
        ]:
            with self.subTest(metadata=metadata):
                html = '<script type="application/ld+json">'+json.dumps(metadata)+'</script><main>Official article.</main>'
                self.assertEqual(article_content(html)[1:], ("2026-09-09T17:30:00Z", "instant"))

    def test_jsonld_malformed_modified_only_and_conflicting_dates_stay_unknown(self):
        for raw in [
            '{bad json',
            json.dumps({"@type": "NewsArticle", "dateModified": "2026-09-09T19:09:28Z"}),
            json.dumps({"@type": "Article", "datePublished": "2026-02-30Z"}),
            json.dumps({"@graph": [{"@type": "Article", "datePublished": "2026-09-08"},
                                  {"@type": "NewsArticle", "datePublished": "2026-09-09"}]}),
            json.dumps({"@type": "Organization", "datePublished": "2026-09-09"}),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(article_content('<script type="application/ld+json">'+raw+'</script><main>Article.</main>')[1:], (None, "unknown"))

    def test_jsonld_script_count_and_bytes_are_bounded(self):
        empty = '<script type="application/ld+json">{}</script>'
        valid = '<script type="application/ld+json">'+json.dumps({"@type": "NewsArticle", "datePublished": "2026-09-09Z"})+'</script>'
        self.assertEqual(article_content(empty*8+valid)[1:], (None, "unknown"))
        oversized = '<script type="application/ld+json">'+json.dumps({"@type": "NewsArticle", "datePublished": "2026-09-09Z", "description": "x"*65536})+'</script>'
        self.assertEqual(article_content(oversized+valid)[1:], (None, "unknown"))


class SourceWatchAITests(unittest.IsolatedAsyncioTestCase):
    def setup_ai(self, outputs, *, independent=True, body=None):
        self.body = body or '<main>Apple officially announces Product X. The exact published official announcement and required specifications are confirmed.</main>'
        text, date, precision = article_content(self.body)
        content = json.dumps({"text": text, "publicationDate": date, "datePrecision": precision}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.observation = {"url": URL, "artifactHash": digest(self.body), "contentHash": digest(content), "observedAt": 2000}
        self.forecast = {"id": "forecast-observer", "specificationHash": specification().specification_hash,
                         "specification": to_dict(specification())}
        self.transport = Transport(outputs)
        async def read(key):
            return self.body if key == digest(self.body) else None
        providers = [ProviderConfig("gemini", "test-model", "test-key")]
        if independent:
            providers.append(ProviderConfig("openai", "test-model", "test-key"))
        return coordinator(self.transport, providers=providers, read_artifact=read)

    def counter_output(self, agrees=True, explanation="Independent provider verified every exact condition."):
        return {"agrees": agrees, "explanation": explanation,
                "event_time_basis": "observed_upper_bound", "event_not_after_ms": 2000}

    def positive_outputs(self):
        return [{"verified": True, "relevant": True, "explanation": "Official article with substantive source evidence."},
                {"positive_existential": True, "all_conditions_satisfied": True, "irreversible": True,
                 "invalidation_clear": True, "explanation": "The official article establishes the exact immutable announcement conditions."},
                self.counter_output()]

    async def test_real_qualification_binds_distinct_providers_and_observation_bound(self):
        ai = self.setup_ai(self.positive_outputs())
        result = await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertTrue(result["accepted"])
        trigger = result["trigger"]
        trigger.validate_for(specification())
        self.assertEqual(trigger.event_time_basis, "observed_upper_bound")
        self.assertEqual(trigger.event_at_ms, 2000)
        self.assertEqual(trigger.qualifier.provider, "gemini")
        self.assertEqual(trigger.counter_qualifier.provider, "openai")
        self.assertEqual(len(self.transport.calls), 3)
        self.assertEqual(self.transport.source_calls, [])

    async def test_false_positive_stops_after_source_review(self):
        ai = self.setup_ai([{"verified": False, "relevant": False, "explanation": "This is an unrelated source page and not evidence for the question."}])
        result = await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertFalse(result["accepted"])
        self.assertEqual(len(self.transport.calls), 1)

    async def test_no_invented_independent_provider(self):
        ai = self.setup_ai(self.positive_outputs(), independent=False)
        with self.assertRaises(AIUnavailable) as ctx:
            await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertEqual(len(ctx.exception.artifacts), 2)
        self.assertEqual(len(self.transport.calls), 2)

    async def test_independent_disagreement_keeps_unqualified(self):
        outputs = self.positive_outputs()
        outputs[-1]["agrees"] = False
        ai = self.setup_ai(outputs)
        result = await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertFalse(result["accepted"])
        self.assertIsNone(result["trigger"])

    async def test_source_corruption_never_calls_model(self):
        ai = self.setup_ai([])
        self.observation["artifactHash"] = "f"*64
        with self.assertRaises(AIUnavailable):
            await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertEqual(self.transport.calls, [])

    async def test_early_proposal_uses_exact_trigger_bytes_and_distinct_judges(self):
        from forecast_domain.early_resolution import LockEarly
        from forecast_domain.lifecycle import (
            BeginResolution,
            BeginValidation,
            Publish,
            create_forecast,
        )

        from tests.model_fixtures import validation
        from tests.test_early_resolution_domain import step
        from tests.test_web_ai import resolution_outputs

        ai = self.setup_ai(self.positive_outputs()+[resolution_outputs()[1], self.counter_output()])
        result = await ai.review_source_observation(self.forecast, self.observation, 3000)
        draft = create_forecast(forecast_id=self.forecast["id"], creator_id="creator-watch",
                                specification=specification(), now_ms=0)
        validating = step(draft, BeginValidation(), 10).forecast
        opened = step(validating, Publish(assessment=validation(specification())), 60).forecast
        locked = step(opened, LockEarly(trigger=result["trigger"]), 3001).forecast
        resolving = step(locked, BeginResolution(), 3002).forecast
        proposal = await ai.propose_early_resolution(resolving, 3003)
        proposal.resolution.require_proposable(specification())
        self.assertEqual(proposal.resolution.schema_version, 2)
        self.assertEqual(proposal.resolution.evidence, result["trigger"].evidence)
        self.assertEqual(proposal.resolution.counter_judge.provider, "openai")
        self.assertEqual(self.transport.source_calls, [])
        self.assertEqual(len(self.transport.calls), 5)
        schema = self.transport.calls[3]["body"]["generationConfig"]["responseJsonSchema"]
        matches = schema["properties"]["rule_matches"]
        self.assertEqual(matches["items"]["enum"], [result["trigger"].clause_id])
        self.assertEqual((matches["minItems"], matches["maxItems"]), (0, 1))
        self.assertIn("UNRESOLVED", schema["properties"]["conflict_status"]["enum"])
        self.assertEqual(schema["properties"]["proposed_outcome"]["enum"], ["YES", "NO", "INVALID"])
        payload = json.loads(self.transport.calls[3]["body"]["contents"][0]["parts"][0]["text"])
        self.assertIn('rule_matches:["yes-rule"]', payload["policy"])

    async def test_freshness_rejects_already_completed_event_with_audit(self):
        from forecast_application.ai import AIRejected
        ai = self.setup_ai([{"status": "known_true", "monotonic_positive": True,
                             "all_conditions_satisfied": True,
                             "explanation": "The retained announcement already establishes every exact YES condition."}])
        with self.assertRaises(AIRejected) as ctx:
            await ai.check_question_freshness(specification(), [self.observation], 3000)
        self.assertEqual(ctx.exception.code, "question_already_resolved")
        self.assertEqual(len(ctx.exception.artifacts), 1)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.source_calls, [])

    async def test_freshness_uncertainty_returns_audit_without_rejection(self):
        ai = self.setup_ai([{"status": "uncertain", "monotonic_positive": False,
                             "all_conditions_satisfied": False,
                             "explanation": "The official page only mentions a related product and does not settle this question."}])
        artifacts = await ai.check_question_freshness(specification(), [self.observation], 3000)
        self.assertEqual(len(artifacts), 1)

    async def test_freshness_empty_candidates_no_model_call(self):
        ai = self.setup_ai([])
        self.assertEqual(await ai.check_question_freshness(specification(), [], 3000), ())
        self.assertEqual(self.transport.calls, [])


    async def test_unrelated_article_certified_by_two_actual_providers(self):
        ai = self.setup_ai([
            {"verified": True, "relevant": False, "explanation": "The genuine official article describes an unrelated product family."},
            self.counter_output(explanation="The exact article is unrelated to every immutable event condition.")])
        result = await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertFalse(result["accepted"])
        self.assertTrue(result["dismissible"])
        self.assertIsNone(result["trigger"])
        self.assertEqual(len(result["artifacts"]), 2)
        self.assertEqual(result["dismissalProof"]["sourceVerifier"]["provider"], "gemini")
        self.assertEqual(result["dismissalProof"]["counterReviewer"]["provider"], "openai")
        self.assertEqual(result["dismissalProof"]["specificationHash"], self.forecast["specificationHash"])
        self.assertEqual(len(self.transport.calls), 2)

    async def test_unrelated_disagreement_does_not_certify_dismissal(self):
        ai = self.setup_ai([
            {"verified": True, "relevant": False, "explanation": "The official article appears to describe another product."},
            self.counter_output(False, "The marketing alias could satisfy the exact question and remains ambiguous.")])
        result = await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertFalse(result["dismissible"])
        self.assertEqual(result["reason"], "unrelatedness_disagreement")
        self.assertEqual(len(result["artifacts"]), 2)

    async def test_unrelated_without_independent_provider_keeps_hold_and_audit(self):
        ai = self.setup_ai([
            {"verified": True, "relevant": False, "explanation": "The official article covers a different product family."}], independent=False)
        with self.assertRaises(AIUnavailable) as ctx:
            await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertEqual(len(ctx.exception.artifacts), 1)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_unrelated_counter_outage_retains_first_review(self):
        ai = self.setup_ai([
            {"verified": True, "relevant": False, "explanation": "The official article covers a different product family."},
            RuntimeError("provider unavailable")])
        with self.assertRaises(AIUnavailable) as ctx:
            await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertGreaterEqual(len(ctx.exception.artifacts), 2)


    async def test_large_retained_html_ai_context_stays_bounded(self):
        body = "<style>"+"x"*333000+"</style><main>Apple officially announces Product X. Exact official product specifications and announcement details are confirmed.</main>"
        ai = self.setup_ai(self.positive_outputs(), body=body)
        result = await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertTrue(result["accepted"])
        for request in self.transport.calls:
            serialized = json.dumps(request["body"])
            self.assertLess(len(serialized.encode()), 65536)
            self.assertNotIn("x"*100, serialized)

    async def test_early_counter_schema_is_concise_and_time_bound(self):
        ai = self.setup_ai(self.positive_outputs())
        await ai.review_source_observation(self.forecast, self.observation, 3000)
        request = self.transport.calls[-1]["body"]
        schema = request["text"]["format"]["schema"]["properties"]
        self.assertEqual(schema["explanation"]["maxLength"], 1000)
        self.assertEqual(schema["event_time_basis"]["const"], "observed_upper_bound")
        self.assertEqual(schema["event_not_after_ms"]["const"], 2000)
        user_input = json.loads(request["input"][1]["content"])
        self.assertIn("four concise sentences", user_input["counter_time_binding"]["response_policy"])
        self.assertIn("retained_text", user_input)

    async def test_counter_agreement_cannot_upgrade_date_only_to_midnight(self):
        from forecast_application.ai import AIRejected
        outputs = self.positive_outputs()
        outputs[-1]["explanation"] = "The announcement happened at 1970-01-01T00:00:00Z, so the event qualifies."
        body = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"1970-01-01Z"}</script><main>Apple officially announces Product X with all published product specifications established.</main>'
        ai = self.setup_ai(outputs, body=body)
        with self.assertRaises(AIRejected) as ctx:
            await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertEqual(ctx.exception.code, "early_counter_time_mismatch")
        self.assertEqual(len(ctx.exception.artifacts), 3)

    async def test_counter_may_quote_only_actual_bound_and_deadline_instants(self):
        outputs = self.positive_outputs()
        outputs[-1]["explanation"] = "The observation bound is 1970-01-01T00:00:02Z and precedes the deadline 1970-01-01T00:06:40Z."
        ai = self.setup_ai(outputs)
        self.assertTrue((await ai.review_source_observation(self.forecast, self.observation, 3000))["accepted"])

    async def test_counter_wrong_typed_binding_is_rejected(self):
        from forecast_application.ai import AIRejected
        outputs = self.positive_outputs()
        outputs[-1]["event_time_basis"] = "published_instant"
        ai = self.setup_ai(outputs)
        with self.assertRaises(AIRejected):
            await ai.review_source_observation(self.forecast, self.observation, 3000)

    async def test_counter_explanation_length_limit_is_enforced(self):
        from forecast_application.ai import AIRejected
        outputs = self.positive_outputs()
        outputs[-1]["explanation"] = "x"*1001
        ai = self.setup_ai(outputs)
        with self.assertRaises(AIRejected):
            await ai.review_source_observation(self.forecast, self.observation, 3000)

    async def test_unrelated_counter_invented_time_cannot_dismiss_hold(self):
        from forecast_application.ai import AIRejected
        ai = self.setup_ai([
            {"verified": True, "relevant": False, "explanation": "The official article is about another product entirely."},
            self.counter_output(explanation="This unrelated event happened at 1970-01-01T00:00:00Z.")])
        with self.assertRaises(AIRejected) as ctx:
            await ai.review_source_observation(self.forecast, self.observation, 3000)
        self.assertEqual(ctx.exception.code, "early_counter_time_mismatch")
        self.assertEqual(len(ctx.exception.artifacts), 2)


    async def test_early_proposal_rejects_explanation_instead_of_clause_id(self):
        from forecast_application.ai import AIRejected
        from forecast_domain.early_resolution import LockEarly
        from forecast_domain.lifecycle import (
            BeginResolution,
            BeginValidation,
            Publish,
            create_forecast,
        )

        from tests.model_fixtures import validation
        from tests.test_early_resolution_domain import step
        from tests.test_web_ai import resolution_outputs

        incorrect = resolution_outputs()[1]
        incorrect["rule_matches"] = ["The official announcement satisfies the YES clause because the product is confirmed."]
        ai = self.setup_ai(self.positive_outputs()+[incorrect])
        qualified = await ai.review_source_observation(self.forecast, self.observation, 3000)
        draft = create_forecast(forecast_id=self.forecast["id"], creator_id="creator-watch",
                                specification=specification(), now_ms=0)
        validating = step(draft, BeginValidation(), 10).forecast
        opened = step(validating, Publish(assessment=validation(specification())), 60).forecast
        locked = step(opened, LockEarly(trigger=qualified["trigger"]), 3001).forecast
        resolving = step(locked, BeginResolution(), 3002).forecast
        with self.assertRaises(AIRejected):
            await ai.propose_early_resolution(resolving, 3003)
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual(self.transport.source_calls, [])
