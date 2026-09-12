"""Stable domain failures; adapters may map these to transport-specific errors."""


class DomainError(ValueError):
    """A caller-supplied value or command violates the domain contract."""


class ValidationError(DomainError):
    """A record, wire representation, or commitment is invalid."""


class TransitionError(DomainError):
    """A command cannot be applied in the current lifecycle context."""


class ConcurrencyError(DomainError):
    """The caller's expected revision is stale."""


class IdempotencyConflict(DomainError):
    """An idempotency key was reused for a different command."""
