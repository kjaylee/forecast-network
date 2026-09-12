"""Generate Draft 2020-12 contracts from the actual immutable record fields."""

from __future__ import annotations

from dataclasses import fields
from enum import Enum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from .records import MAX_SAFE_INTEGER, MAX_TEXT_LENGTH, Record, record_hints

SCHEMA_BASE = "https://schemas.forecast.network/v1/"


def _annotation_schema(annotation: Any, definitions: dict[str, Any]) -> dict[str, Any]:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        return {"anyOf": [_annotation_schema(option, definitions) for option in args]}
    if origin is Literal:
        return {"const": args[0]} if len(args) == 1 else {"enum": list(args)}
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return {"type": "array", "items": _annotation_schema(args[0], definitions)}
        return {"type": "array", "prefixItems": [_annotation_schema(a, definitions) for a in args],
                "minItems": len(args), "maxItems": len(args)}
    if annotation is type(None):
        return {"type": "null"}
    if annotation is int:
        return {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is str:
        return {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_LENGTH, "pattern": r"\S"}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"type": "string", "enum": [item.value for item in annotation]}
    if isinstance(annotation, type) and issubclass(annotation, Record):
        _define(annotation, definitions)
        return {"$ref": f"#/$defs/{annotation.__name__}"}
    raise TypeError(f"Unsupported schema annotation: {annotation!r}")


def _define(cls: type[Record], definitions: dict[str, Any]) -> None:
    if cls.__name__ in definitions:
        return
    definitions[cls.__name__] = {}
    hints = record_hints(cls)
    properties = {}
    for item in fields(cls):
        properties[item.name] = {
            **_annotation_schema(hints[item.name], definitions), **dict(item.metadata),
        }
    definitions[cls.__name__] = {
        "type": "object", "additionalProperties": False,
        "properties": properties, "required": list(properties),
    }


def schema_for(cls: type[Record]) -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    _define(cls, definitions)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://schemas.forecast.network/v{schema_version(cls)}/{cls.__name__}.schema.json",
        "title": cls.__name__, "$ref": f"#/$defs/{cls.__name__}",
        "$comment": "Structural contract. Decode through the domain boundary to enforce semantic invariants.",
        "$defs": definitions,
    }


def schema_version(cls: type[Record]) -> int:
    return int(next(item.metadata["const"] for item in fields(cls) if item.name == "schema_version"))


def all_record_types() -> tuple[type[Record], ...]:
    from . import early_resolution, lifecycle, models, pricing

    result: dict[str, type[Record]] = {}
    for module in (models, lifecycle, early_resolution, pricing):
        for value in vars(module).values():
            if (isinstance(value, type) and issubclass(value, Record)
                    and value.__module__ == module.__name__):
                if value.__name__ in result and result[value.__name__] is not value:
                    raise TypeError(f"Duplicate schema name: {value.__name__}")
                result[value.__name__] = value
    return tuple(result[name] for name in sorted(result))
