"""Strict record decoding and versioned, portable content commitments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from enum import Enum
from types import UnionType
from typing import Any, Literal, TypeVar, Union, get_args, get_origin

from .errors import ValidationError
from .records import MAX_SAFE_INTEGER, Record, record_hints, scalar_text, validate_type

T = TypeVar("T", bound=Record)
COMMITMENT_PREFIX = b"forecast-network:sha256:canonical-json:v1\n"
MAX_JSON_BYTES = 8 * 1024 * 1024


def to_dict(record: Record) -> dict[str, Any]:
    if not isinstance(record, Record):
        raise ValidationError("Expected a domain record")
    return {item.name: _wire(getattr(record, item.name)) for item in fields(record)}


def _wire(value: Any) -> Any:
    if isinstance(value, Record):
        return to_dict(value)
    if isinstance(value, Enum):
        return _wire(value.value)
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValidationError("Canonical integer exceeds portable range")
        return value
    if type(value) is str:
        scalar_text(value, path="canonical string", nonblank=False)
        return value
    if type(value) in (tuple, list):
        return [_wire(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValidationError("Canonical object keys must be strings")
        for key in value:
            scalar_text(key, path="canonical key", nonblank=False)
        return {key: _wire(item) for key, item in value.items()}
    raise ValidationError(f"Unsupported canonical value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _wire(value), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, UnicodeError) as exc:
        raise ValidationError("Invalid or excessively nested canonical value") from exc


def content_hash(value: Any) -> str:
    return hashlib.sha256(COMMITMENT_PREFIX + canonical_bytes(value)).hexdigest()


def dumps(record: Record) -> str:
    return canonical_bytes(record).decode("utf-8")


def _decode_value(annotation: Any, value: Any, path: str) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        failures: list[str] = []
        for option in args:
            try:
                return _decode_value(option, value, path)
            except ValidationError as exc:
                failures.append(str(exc))
        raise ValidationError(f"{path}: no matching contract ({'; '.join(failures)})")
    if origin is tuple:
        if type(value) is not list:
            raise ValidationError(f"{path}: expected JSON array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_value(args[0], item, f"{path}[{i}]")
                         for i, item in enumerate(value))
        if len(value) != len(args):
            raise ValidationError(f"{path}: array length mismatch")
        return tuple(_decode_value(kind, item, f"{path}[{i}]")
                     for i, (kind, item) in enumerate(zip(args, value, strict=True)))
    if isinstance(annotation, type) and issubclass(annotation, Record):
        return from_dict(annotation, value)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is not str:
            raise ValidationError(f"{path}: expected enum string")
        try:
            return annotation(value)
        except ValueError as exc:
            raise ValidationError(f"{path}: unknown {annotation.__name__} value") from exc
    if origin is Literal or annotation in (str, int, bool, type(None)):
        validate_type(value, annotation, path)
        return value
    raise TypeError(f"Unsupported domain field annotation: {annotation!r}")


def from_dict(cls: type[T], value: Any) -> T:
    if not isinstance(cls, type) or not issubclass(cls, Record):
        raise TypeError("Decoder target must be a Record type")
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValidationError(f"{cls.__name__}: expected JSON object")
    names = {item.name for item in fields(cls)}
    if set(value) != names:
        missing, extra = names - set(value), set(value) - names
        raise ValidationError(f"{cls.__name__}: missing={sorted(missing)}, unknown={sorted(extra)}")
    hints = record_hints(cls)
    decoded = {name: _decode_value(hints[name], item, f"{cls.__name__}.{name}")
               for name, item in value.items()}
    return cls(**decoded)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_number(value: str) -> Any:
    raise ValidationError(f"Non-integer JSON number is forbidden: {value}")


def loads(cls: type[T], raw: str | bytes) -> T:
    if type(raw) not in (str, bytes):
        raise ValidationError("Expected JSON text or UTF-8 bytes")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if len(text.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValidationError("JSON exceeds the 8 MiB record boundary")
        value = json.loads(
            text, object_pairs_hook=_object_pairs,
            parse_float=_reject_number, parse_constant=_reject_number,
        )
        return from_dict(cls, value)
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("Malformed, non-UTF-8, or excessively nested JSON") from exc
