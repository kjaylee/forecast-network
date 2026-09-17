"""Shared field contracts for immutable records and generated wire schemas.

This checks Python record types and a small, explicit set of field constraints.
It is deliberately not an interpreter for externally supplied JSON Schemas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from enum import Enum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from .errors import ValidationError

MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_TEXT_LENGTH = 1_000_000
CONSTRAINTS = frozenset({
    "minimum", "maximum", "minLength", "maxLength", "pattern", "minItems",
    "maxItems", "uniqueItems", "const", "description", "title",
})


def scalar_text(value: str, *, path: str, nonblank: bool = True) -> None:
    if nonblank and not value.strip():
        raise ValidationError(f"{path}: text must not be blank")
    if len(value) > MAX_TEXT_LENGTH:
        raise ValidationError(f"{path}: text exceeds {MAX_TEXT_LENGTH} characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValidationError(f"{path}: unpaired Unicode surrogate") from exc


_HINTS: dict[type[Record], dict[str, Any]] = {}


def record_hints(cls: type[Record]) -> dict[str, Any]:
    if cls not in _HINTS:
        _HINTS[cls] = get_type_hints(cls)
    return _HINTS[cls]


def validate_type(value: Any, annotation: Any, path: str) -> None:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        for option in args:
            try:
                validate_type(value, option, path)
                return
            except ValidationError:
                continue
        raise ValidationError(f"{path}: value does not match any allowed type")
    if origin is Literal:
        if not any(type(value) is type(option) and value == option for option in args):
            raise ValidationError(f"{path}: expected one of {args!r}")
        return
    if origin is tuple:
        if type(value) is not tuple:
            raise ValidationError(f"{path}: expected immutable tuple")
        if len(args) == 2 and args[1] is Ellipsis:
            for index, item in enumerate(value):
                validate_type(item, args[0], f"{path}[{index}]")
        else:
            if len(value) != len(args):
                raise ValidationError(f"{path}: tuple length mismatch")
            for index, (item, expected) in enumerate(zip(value, args, strict=True)):
                validate_type(item, expected, f"{path}[{index}]")
        return
    if annotation is type(None):
        if value is not None:
            raise ValidationError(f"{path}: expected null")
        return
    if annotation is int:
        if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
            raise ValidationError(f"{path}: expected nonnegative safe integer")
        return
    if annotation is bool:
        if type(value) is not bool:
            raise ValidationError(f"{path}: expected boolean")
        return
    if annotation is str:
        if type(value) is not str:
            raise ValidationError(f"{path}: expected string")
        scalar_text(value, path=path)
        return
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is not annotation:
            raise ValidationError(f"{path}: expected {annotation.__name__}")
        return
    if isinstance(annotation, type) and issubclass(annotation, Record):
        if type(value) is not annotation:
            raise ValidationError(f"{path}: expected {annotation.__name__}")
        value.__post_init__()
        return
    raise TypeError(f"Unsupported domain field annotation: {annotation!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    schema_version: int = field(default=1, metadata={"const": 1})

    def __post_init__(self) -> None:
        hints = record_hints(type(self))
        for item in fields(self):
            value = getattr(self, item.name)
            path = f"{type(self).__name__}.{item.name}"
            validate_type(value, hints[item.name], path)
            unknown = set(item.metadata) - CONSTRAINTS
            if unknown:
                raise TypeError(f"{path}: unsupported field metadata {unknown}")
            for key, limit in item.metadata.items():
                if key == "const" and (type(value) is not type(limit) or value != limit):
                    raise ValidationError(f"{path}: expected constant {limit!r}")
                if value is None:
                    continue
                if key == "minimum" and value < limit:
                    raise ValidationError(f"{path}: below minimum {limit}")
                if key == "maximum" and value > limit:
                    raise ValidationError(f"{path}: above maximum {limit}")
                if key in ("minLength", "minItems") and len(value) < limit:
                    raise ValidationError(f"{path}: requires at least {limit} entries")
                if key in ("maxLength", "maxItems") and len(value) > limit:
                    raise ValidationError(f"{path}: allows at most {limit} entries")
                if key == "pattern" and re.fullmatch(limit, value) is None:
                    raise ValidationError(f"{path}: malformed value")
                if key == "uniqueItems" and limit:
                    if any(value[index] in value[:index] for index in range(len(value))):
                        raise ValidationError(f"{path}: duplicate entries")
        self.validate()

    def validate(self) -> None:
        """Override to enforce semantic relationships after field validation."""
