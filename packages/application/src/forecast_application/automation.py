"""Application orchestration for retained official events and explicit v2 upgrades."""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit

from forecast_domain import ForecastSpecification
from forecast_domain.early_resolution import EarlyResolutionTrigger, ForecastV2, LockEarly
from forecast_domain.lifecycle import LifecycleState
from forecast_domain.serialization import from_dict, to_dict

from .errors import AppError
from .source_watch import SourceWatch


def families(specification: ForecastSpecification) -> tuple[str, ...]:
    """Bounded family hints, never semantic proof that an event happened."""
    value = (specification.canonical_question + ' ' + specification.share_title).casefold()
    if 'fold' in value or '폴더블' in value or '접는' in value:
        return ('foldable', 'folding')
    if 'm6' in value:
        return ('M6',)
    if 'windows 12' in value or '윈도우 12' in value:
        return ('Windows 12',)
    for name in ('iphone', 'macbook', 'nvidia', 'spacex', 'nasa', 'windows'):
        if name in value:
            return (name,)
    return ()


PUBLISHER_FEEDS = {
    # Newsroom roots are not crawlable indexes; their feeds expose dated article links.
    ('www.apple.com', '/newsroom'): 'https://www.apple.com/newsroom/rss-feed.rss',
    ('apple.com', '/newsroom'): 'https://apple.com/newsroom/rss-feed.rss',
    ('news.microsoft.com', ''): 'https://news.microsoft.com/source/feed/',
    ('news.microsoft.com', '/source'): 'https://news.microsoft.com/source/feed/',
}


def publisher_feed_url(url: str) -> str:
    """Map a published official-source root to the feed the watcher can actually read."""
    parts = urlsplit(url)
    return PUBLISHER_FEEDS.get((parts.hostname or '', parts.path.rstrip('/')), url)


class ForecastAutomation:
    def __init__(self, app: Any, *, enabled: bool = False):
        self.app = app
        collector = getattr(app.ai, 'collector', None)
        self.enabled = enabled and collector is not None
        self.watch = SourceWatch(app.db, collector, app.now_ms, app.random_token,
            load_forecast=self.load, hold=self.hold, review=self.review, accept=self.accept, dismiss=self.dismiss,
            artifact_sql=app._artifact_sql) if self.enabled and collector is not None else None

    async def load(self, forecast_id: str) -> dict[str, Any]:
        record = await self.app._forecast(forecast_id)
        spec = record.specification
        return {'id': record.forecast_id, 'specificationHash': record.specification_hash,
                'state': record.state.value, 'createdAt': record.published_at_ms,
                'specification': to_dict(spec), 'families': families(spec),
                'officialSourceUrls': [source.url for source in spec.source_policy.primary_sources if source.is_official]}

    async def bootstrap(self, *, limit: int = 30) -> int:
        if self.watch is None:
            return 0
        rows = await self.app.db.all("SELECT id FROM forecasts WHERE state='OPEN' AND close_at>? ORDER BY created_at,id LIMIT ?",
                                     (self.app.now_ms(), max(1, min(30, limit))))
        count = 0
        for row in rows:
            forecast = await self.load(row['id'])
            if not forecast['families']:
                continue
            for url in forecast['officialSourceUrls'][:3]:
                host = urlsplit(url).hostname
                if host not in {'www.apple.com', 'apple.com', 'news.microsoft.com', 'blogs.microsoft.com', 'www.microsoft.com'}:
                    continue
                url = publisher_feed_url(url)
                index_id = 'publisher-' + hashlib.sha256(url.encode()).hexdigest()[:32]
                await self.watch.register(index_id, url, kind='index')
                await self.watch.bind(row['id'], index_id, forecast['families'])
                current = await self.app.participation_holds.active(row['id'])
                if current and urlsplit(current['evidenceUrl']).hostname == host:
                    article_url = current['evidenceUrl']
                    article_id = 'article-' + hashlib.sha256(article_url.encode()).hexdigest()[:32]
                    await self.watch.register(article_id, article_url, kind='article', interval_ms=3600000, parent_id=index_id, pinned=True)
                count += 1
        return count

    async def hold(self, forecast_id: str, observation: dict[str, Any]) -> None:
        forecast = await self.app._forecast(forecast_id)
        if forecast.state != LifecycleState.OPEN:
            return
        status = await self.app.participation_holds.status(forecast_id)
        if status['hold']:
            return
        key = 'source-watch:' + hashlib.sha256((forecast_id+observation['id']).encode()).hexdigest()
        try:
            await self.app.participation_holds.change(forecast_id, {
                'action': 'hold', 'expectedRevision': status['revision'], 'expectedHoldId': None,
                'specificationHash': forecast.specification_hash, 'reason': 'known_outcome_review',
                'evidenceUrl': observation['url'], 'idempotencyKey': key})
        except AppError as error:
            if error.code != 'participation_hold_changed' or not await self.app.participation_holds.active(forecast_id):
                raise

    async def review(self, forecast: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        return dict(await self.app.ai.review_source_observation(forecast, observation, self.app.now_ms()))

    async def accept(self, forecast_id: str, result: dict[str, Any]) -> None:
        trigger = from_dict(EarlyResolutionTrigger, result['trigger'])
        current = await self.app._forecast(forecast_id)
        if isinstance(current, ForecastV2):
            if current.early_trigger.trigger_hash != trigger.trigger_hash:
                raise AppError(409, 'early_trigger_changed', 'A different reviewed event already closed this forecast.')
            return
        if current.state != LifecycleState.OPEN or current.specification_hash != trigger.specification_hash:
            raise AppError(409, 'early_trigger_changed', 'The forecast changed while the event was reviewed.')
        trigger.validate_for(current.specification)
        for evidence in trigger.evidence:
            retained = await self.app.read_artifact(evidence.content_sha256)
            if retained is None or hashlib.sha256(retained.encode()).hexdigest() != evidence.content_sha256:
                raise AppError(503, 'early_evidence_unavailable', 'The original official evidence could not be verified.')
        if not await self.app.participation_holds.active(forecast_id):
            raise AppError(409, 'early_trigger_changed', 'Participation must be paused before an observed-event upgrade.')
        # Compensation is an append-only overlay. The original accepted events
        # remain replayable; only eligible receipts contribute to scoring/payment.
        await self.app.eligibility.apply(trigger)
        await self.app.markets.void_after_evidence(forecast_id, trigger.trigger_hash)
        eligibility = await self.app.eligibility.finish(trigger)
        if eligibility['status'] != 'complete':
            raise AppError(409, 'early_eligibility_review',
                           'Receipt timing or point restoration is still being reviewed.')
        guard = self.app.random_token()
        extra = [
            ('INSERT INTO mutation_guards(token,valid) SELECT ?, ( CASE WHEN '
             'EXISTS(SELECT 1 FROM active_participation_holds WHERE forecast_id=?) '
             'AND EXISTS(SELECT 1 FROM forecast_eligibility_completions WHERE decision_id=?) '
             'THEN 1 ELSE 0 END )', (guard, forecast_id, trigger.trigger_hash)),
            self.app._record_artifact(trigger, 'early-resolution-trigger'),
            ('DELETE FROM mutation_guards WHERE token=?', (guard,)),
        ]
        if self.app.now_ms() < current.specification.close_at_ms:
            await self.app._mutate(current, LockEarly(trigger=trigger),
                key='early:' + trigger.trigger_hash, extra=extra)
            # The first community report that surfaced this exact retained evidence earns the fixed reward.
            await self.app.reward_evidence_report(forecast_id, [evidence.content_sha256 for evidence in trigger.evidence])
        else:
            # A delayed accounting correction cannot backdate an early lock.
            # The ordinary lifecycle still waits for its original deadline.
            from forecast_domain.lifecycle import Lock
            await self.app._mutate(current, Lock(),
                key='eligibility-lock:' + trigger.trigger_hash, extra=extra)

    async def retry_eligibility(self, limit: int = 3) -> dict[str, int]:
        """Resume retained compensation after crashes or later account credits.

        No new model call or invented publication time is involved. Ambiguous
        receipts stay under review until separately adjudicated.
        """
        from forecast_domain.serialization import loads
        rows = await self.app.db.all(
            "SELECT d.id,d.body,j.attempts FROM forecast_eligibility_decisions d JOIN forecasts f ON f.id=d.forecast_id "
            "JOIN forecast_eligibility_retry j ON j.decision_id=d.id "
            "WHERE f.state='OPEN' AND j.next_attempt<=? AND NOT EXISTS (SELECT 1 FROM forecast_receipt_eligibility r "
            "WHERE r.decision_id=d.id AND r.status='review') "
            "ORDER BY j.next_attempt,d.created_at,d.id LIMIT ?", (self.app.now_ms(), max(1, min(3, limit))))
        completed = 0
        for row in rows:
            claim = await self.app.db.execute("UPDATE forecast_eligibility_retry SET attempts=attempts+1,next_attempt=? "
                "WHERE decision_id=? AND next_attempt<=? RETURNING decision_id",
                (self.app.now_ms()+300000, row['id'], self.app.now_ms()))
            if not claim.get('results'):
                continue
            trigger = loads(EarlyResolutionTrigger, row['body'])
            try:
                await self.accept(trigger.forecast_id, {'trigger': to_dict(trigger)})
                completed += 1
            except Exception:
                # Fixed bounded backoff; a permanently unfunded restoration must
                # not monopolize the first page and starve newer corrections.
                await self.app.db.execute("UPDATE forecast_eligibility_retry SET next_attempt=? WHERE decision_id=?",
                    (self.app.now_ms()+min(21600000, 60000*2**min(row['attempts'], 8)), row['id']))
        return {'considered': len(rows), 'completed': completed}

    async def dismiss(self, forecast_id: str, result: dict[str, Any]) -> None:
        # Only a distinctly counter-reviewed irrelevant article can release an
        # observer-created hold. A manual/operator hold is never auto-released.
        if result.get("accepted") is not False or result.get("dismissible") is not True:
            return
        current = await self.app._forecast(forecast_id)
        if current.state != LifecycleState.OPEN:
            return
        status = await self.app.participation_holds.status(forecast_id)
        hold = status["hold"]
        if hold is None:
            return
        event = await self.app.db.first("SELECT request_key FROM participation_hold_events WHERE id=?", (hold["holdId"],))
        if not event or not event["request_key"].startswith("source-watch:"):
            return
        observation = result["observation"]
        review_id = hashlib.sha256((observation["contentHash"]+current.specification_hash+'official-source-watch-v1').encode()).hexdigest()
        unresolved = await self.app.db.first(
            "SELECT 1 FROM official_source_reviews WHERE forecast_id=? AND specification_hash=? "
            "AND id!=? AND json_extract(result,'$.dismissible') IS NOT 1 LIMIT 1",
            (forecast_id, current.specification_hash, review_id))
        if unresolved:
            return
        await self.app.participation_holds.change(forecast_id, {
            "action": "release", "expectedRevision": status["revision"], "expectedHoldId": hold["holdId"],
            "specificationHash": current.specification_hash, "reason": "known_outcome_review",
            "evidenceUrl": observation["url"], "idempotencyKey": 'source-dismiss:'+review_id,
        }, dismissal_review_id=review_id)

    async def check_creation(self, spec: ForecastSpecification) -> None:
        if self.watch is None or not families(spec):
            return
        known = await self.watch.check_known({'officialSourceUrls': [source.url for source in spec.source_policy.primary_sources if source.is_official],
                                             'families': families(spec)})
        if known:
            artifacts = await self.app.ai.check_question_freshness(spec, known[:3], self.app.now_ms())
            if artifacts:
                await self.app.db.batch(self.app._artifact_sql(artifacts))

    async def run(self, *, limit: int = 2) -> dict[str, Any]:
        if self.watch is None:
            return {'enabled': False, 'polled': 0, 'reviewed': 0, 'failed': 0}
        await self.bootstrap()
        return {'enabled': True, **await self.watch.run(limit=max(1, min(6, limit)))}

    async def status(self) -> dict[str, Any]:
        counts = await self.app.db.first("SELECT COUNT(*) AS sources, SUM(enabled) AS enabled FROM official_watch_sources")
        reviews = await self.app.db.all('SELECT state,COUNT(*) AS count FROM official_source_reviews GROUP BY state')
        return {'enabled': self.enabled, 'sources': counts['sources'] if counts else 0,
                'reviews': {row['state']: row['count'] for row in reviews},
                'earlyResolution': 'official_monotonic_positive_only', 'policy': 'official-source-watch-v1'}
