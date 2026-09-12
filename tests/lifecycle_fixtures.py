"""Lifecycle fixtures built exclusively through the public decision engine."""

from forecast_domain.lifecycle import (
    AdjudicateResolution,
    Archive,
    BeginChallenge,
    BeginResolution,
    BeginValidation,
    Command,
    Escalate,
    Finalize,
    LifecycleState,
    Lock,
    PauseForProviderOutage,
    ProposeResolution,
    Publish,
    ReviewDispute,
    SubmitDispute,
    adjudication_input_hash,
    apply_command,
    create_forecast,
)
from forecast_domain.models import AITask, Outcome

from tests import model_fixtures as model


def step(forecast, payload, now_ms, *, key=None):
    command = Command(idempotency_key=key or f"command-{forecast.revision + 1}-{payload.kind}",
                      expected_revision=forecast.revision, payload=payload)
    return apply_command(forecast, command, now_ms=now_ms)


def snapshots():
    spec = model.specification()
    draft = create_forecast(forecast_id="forecast-1", creator_id="creator-1", specification=spec, now_ms=0)
    validating = step(draft, BeginValidation(), 10).forecast
    opened = step(validating, Publish(assessment=model.validation(spec)), 60).forecast
    locked = step(opened, Lock(), 1000).forecast
    resolving = step(locked, BeginResolution(), 1001).forecast
    proposed = step(resolving, ProposeResolution(resolution=model.resolution(spec)), 2000).forecast
    challenge = step(proposed, BeginChallenge(duration_ms=1000), 2100).forecast
    disputed = step(challenge, SubmitDispute(dispute=model.dispute(spec, proposed.resolution)), 2200).forecast
    reviewed = step(disputed, ReviewDispute(review=model.review(
        disputed.disputes[0], proposed.resolution, spec, material_conflict=True)), 2300).forecast
    escalated = step(reviewed, Escalate(), 2400).forecast
    finalized = step(challenge, Finalize(), 3100).forecast
    archived = step(finalized, Archive(), 3200).forecast
    paused = step(challenge, PauseForProviderOutage(
        configured_providers=("provider-a", "provider-b"),
        unavailable_providers=("provider-a", "provider-b"), reason="All configured providers timed out."), 2200).forecast
    return {value.state: value for value in (draft, validating, opened, locked, resolving,
                                             proposed, challenge, disputed, escalated,
                                             paused, finalized, archived)}


def adjudication(forecast, now_ms=4000, *, provider="independent-adjudicator"):
    replacement = model.resolution(forecast.specification, outcome=Outcome.NO, proposed_at_ms=now_ms)
    adjudicator = model.provenance(
        AITask.ADJUDICATION, provider=provider, created_at_ms=now_ms,
        input_hash=adjudication_input_hash(forecast, replacement), output_hash=replacement.resolution_hash)
    return AdjudicateResolution(resolution=replacement, adjudicator=adjudicator)


def all_lifecycle_records():
    states = snapshots()
    challenge = states[LifecycleState.CHALLENGE]
    result = step(challenge, Finalize(), 3100)
    command = Command(idempotency_key="example-command", expected_revision=challenge.revision,
                      payload=Finalize())
    return (*states.values(), states[LifecycleState.PAUSED].pause,
            result.events[0], command, command.payload, result.receipt, result)
