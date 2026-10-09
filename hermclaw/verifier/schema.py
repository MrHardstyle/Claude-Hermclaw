"""Schema evidence: parse a JSON/YAML/TOML file and validate it against a JSON Schema.

``jsonschema`` is used when it is importable (it is not a declared dependency); otherwise a small built-in validator
supports ``type``, ``required``, ``properties``, ``additionalProperties``, ``enum``, ``const``, ``items``
(schema or tuple form), ``minItems``/``maxItems``, ``minLength``/``maxLength``, ``pattern``,
``minimum``/``maximum``/``exclusiveMinimum``/``exclusiveMaximum``, ``allOf``/``anyOf``/``oneOf``/``not``.
Unsupported keywords make the built-in validation fail loudly instead of passing silently. Remote ``$ref``
resolution is never attempted (no network access from the verifier).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import tomllib
from typing import Any

from hermclaw.tools.workspace import regex_is_risky
from hermclaw.verifier.syntax import load_yaml_documents

MAX_ERRORS = 20
SUPPORTED = frozenset(
    {
        "type",
        "required",
        "properties",
        "additionalProperties",
        "enum",
        "const",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        # annotations without validation semantics
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
        "definitions",
        "$defs",
    }
)


class SchemaParseError(ValueError):
    pass


def parse_document(text: str, fmt: str) -> Any:
    try:
        if fmt == "json":
            return json.loads(text)
        if fmt == "toml":
            return tomllib.loads(text)
        if fmt == "yaml":
            docs = load_yaml_documents(text)
            return docs[0] if len(docs) == 1 else docs
    except Exception as exc:
        raise SchemaParseError(f"invalid {fmt.upper()}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}") from exc
    raise SchemaParseError(f"unsupported format {fmt!r}")


def jsonify(value: Any) -> Any:
    """Map YAML/TOML-only types (dates, tuples, sets) onto JSON types before validation."""
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [jsonify(v) for v in value]
    if isinstance(value, _dt.date | _dt.time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def validate(instance: Any, schema: dict[str, Any]) -> tuple[list[str], str]:
    """``(errors, engine)``; raises ``ValueError`` for an invalid schema."""
    data = jsonify(instance)
    try:
        from jsonschema import validators
    except ImportError:  # pragma: no cover - depends on the environment
        return validate_builtin(data, schema), "builtin"
    cls = validators.validator_for(schema)
    try:
        cls.check_schema(schema)
    except Exception as exc:
        raise ValueError(f"invalid JSON schema: {getattr(exc, 'message', exc)}") from exc
    errors: list[str] = []
    for err in sorted(cls(schema).iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path]):
        where = "$" + "".join(f"[{p!r}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        errors.append(f"{where}: {err.message}"[:500])
        if len(errors) >= MAX_ERRORS:
            break
    return errors, "jsonschema"


# ------------------------------------------------------------------------------------------- built-in validator
_TYPES: dict[str, Any] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def _json_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def validate_builtin(instance: Any, schema: Any, where: str = "$") -> list[str]:
    errors: list[str] = []
    _validate(instance, schema, where, errors)
    return errors[:MAX_ERRORS]


def _validate(v: Any, schema: Any, where: str, errors: list[str]) -> None:
    if len(errors) >= MAX_ERRORS:
        return
    if schema is True:
        return
    if schema is False:
        errors.append(f"{where}: no value is allowed here")
        return
    if not isinstance(schema, dict):
        raise ValueError(f"invalid JSON schema at {where}: expected an object")
    unknown = sorted(set(schema) - SUPPORTED)
    if unknown:
        errors.append(f"{where}: schema keyword(s) {unknown} are not supported by the built-in validator")
        return
    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        for name in names:
            if name not in _TYPES:
                raise ValueError(f"invalid JSON schema at {where}: unknown type {name!r}")
        if not any(_TYPES[name](v) for name in names):
            errors.append(f"{where}: expected type {' or '.join(names)}, got {type(v).__name__}")
            return
    if "enum" in schema and not any(_json_equal(v, e) for e in schema["enum"]):
        errors.append(f"{where}: {v!r} is not one of {schema['enum']!r}"[:500])
    if "const" in schema and not _json_equal(v, schema["const"]):
        errors.append(f"{where}: expected constant {schema['const']!r}"[:500])
    if isinstance(v, dict):
        _validate_object(v, schema, where, errors)
    if isinstance(v, list):
        _validate_array(v, schema, where, errors)
    if isinstance(v, str):
        _validate_string(v, schema, where, errors)
    if isinstance(v, int | float) and not isinstance(v, bool):
        _validate_number(v, schema, where, errors)
    for sub in schema.get("allOf", []):
        _validate(v, sub, where, errors)
    if "anyOf" in schema and not any(not validate_builtin(v, s, where) for s in schema["anyOf"]):
        errors.append(f"{where}: does not match any schema of anyOf")
    if "oneOf" in schema:
        hits = sum(1 for s in schema["oneOf"] if not validate_builtin(v, s, where))
        if hits != 1:
            errors.append(f"{where}: must match exactly one schema of oneOf (matched {hits})")
    if "not" in schema and not validate_builtin(v, schema["not"], where):
        errors.append(f"{where}: must not match the 'not' schema")


def _validate_object(v: dict[str, Any], schema: dict[str, Any], where: str, errors: list[str]) -> None:
    for key in schema.get("required", []):
        if key not in v:
            errors.append(f"{where}: missing required property {key!r}")
    props: dict[str, Any] = schema.get("properties", {})
    for key, sub in props.items():
        if key in v:
            _validate(v[key], sub, f"{where}.{key}", errors)
    extra = schema.get("additionalProperties", True)
    for key, value in v.items():
        if key in props:
            continue
        if extra is False:
            errors.append(f"{where}: additional property {key!r} is not allowed")
        elif isinstance(extra, dict):
            _validate(value, extra, f"{where}.{key}", errors)
    if "minProperties" in schema and len(v) < int(schema["minProperties"]):
        errors.append(f"{where}: expected at least {schema['minProperties']} properties")
    if "maxProperties" in schema and len(v) > int(schema["maxProperties"]):
        errors.append(f"{where}: expected at most {schema['maxProperties']} properties")


def _validate_array(v: list[Any], schema: dict[str, Any], where: str, errors: list[str]) -> None:
    items = schema.get("items")
    if isinstance(items, list):
        for i, (item, sub) in enumerate(zip(v, items, strict=False)):
            _validate(item, sub, f"{where}[{i}]", errors)
    elif items is not None:
        for i, item in enumerate(v):
            _validate(item, items, f"{where}[{i}]", errors)
    if "minItems" in schema and len(v) < int(schema["minItems"]):
        errors.append(f"{where}: expected at least {schema['minItems']} items, got {len(v)}")
    if "maxItems" in schema and len(v) > int(schema["maxItems"]):
        errors.append(f"{where}: expected at most {schema['maxItems']} items, got {len(v)}")
    if schema.get("uniqueItems"):
        for i, a in enumerate(v):
            if any(_json_equal(a, b) for b in v[i + 1 :]):
                errors.append(f"{where}: items are not unique")
                break


def _validate_string(v: str, schema: dict[str, Any], where: str, errors: list[str]) -> None:
    if "minLength" in schema and len(v) < int(schema["minLength"]):
        errors.append(f"{where}: shorter than {schema['minLength']} characters")
    if "maxLength" in schema and len(v) > int(schema["maxLength"]):
        errors.append(f"{where}: longer than {schema['maxLength']} characters")
    if "pattern" in schema:
        pattern = str(schema["pattern"])
        if regex_is_risky(pattern):
            raise ValueError(f"invalid JSON schema at {where}: pattern {pattern!r} risks catastrophic backtracking")
        if not re.search(pattern, v):
            errors.append(f"{where}: does not match pattern {pattern!r}")


def _validate_number(v: float, schema: dict[str, Any], where: str, errors: list[str]) -> None:
    if "minimum" in schema and v < schema["minimum"]:
        errors.append(f"{where}: {v} is less than the minimum {schema['minimum']}")
    if "maximum" in schema and v > schema["maximum"]:
        errors.append(f"{where}: {v} is greater than the maximum {schema['maximum']}")
    if "exclusiveMinimum" in schema and not isinstance(schema["exclusiveMinimum"], bool) and v <= schema["exclusiveMinimum"]:
        errors.append(f"{where}: {v} must be greater than {schema['exclusiveMinimum']}")
    if "exclusiveMaximum" in schema and not isinstance(schema["exclusiveMaximum"], bool) and v >= schema["exclusiveMaximum"]:
        errors.append(f"{where}: {v} must be less than {schema['exclusiveMaximum']}")
