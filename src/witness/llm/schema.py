"""Turn pydantic models into JSON schemas that structured-output APIs accept."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Constrained-decoding APIs reject or ignore these; pydantic still enforces them on the reply.
_UNSUPPORTED = {
    "default",
    "title",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Inline ``$ref``s, require every property, and forbid extra keys at every level."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    result = _rewrite(raw, defs)
    assert isinstance(result, dict)
    return result


def _rewrite(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_rewrite(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _rewrite(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    out = {k: _rewrite(v, defs) for k, v in node.items() if k not in _UNSUPPORTED}
    if out.get("type") == "object" and "properties" in out:
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out
