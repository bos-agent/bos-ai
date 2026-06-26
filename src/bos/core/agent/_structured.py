"""Structured-output helpers for the agent ring (BEP 12).

The agent ring is stdlib-pure (no third-party imports — enforced by the ring
isolation guard), so JSON-Schema *validation* (which needs ``jsonschema``) is a
**port** here, implemented by an outer ring and injected. What lives here is
only what the loop needs without third-party deps: the provider-hint sanitizer,
the "unusable completion" set, the error type, the validator protocol, and a
stdlib parse-only fallback.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

# Provider finish_reason values that mean the model did not return a usable,
# complete answer. NOT schema problems: a "reply ONLY with JSON" retry hint
# cannot repair a truncation, content filter, or provider error.
UNUSABLE_FINISH_REASONS = frozenset({"length", "content_filter", "error"})


class StructuredOutputError(ValueError):
    """Raised when a usable, schema-valid structured result cannot be obtained.

    Subclasses ValueError so callers catching ValueError keep working, while
    giving an accurate, catchable type for parse/validation failures.
    """


def provider_hint_schema(schema: Any) -> Any:
    """Return a deep copy of *schema* safe to send as a provider hint.

    ``additionalProperties`` is the OpenAI strict-schema idiom; Gemini's
    ``response_schema`` rejects it. Local validation stays authoritative on the
    original schema, so dropping it from the hint costs nothing.
    """
    if isinstance(schema, dict):
        return {k: provider_hint_schema(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [provider_hint_schema(v) for v in schema]
    return schema


def parse_json(content: str) -> Any:
    """Stdlib parse-only fallback used when no schema validator is injected.

    Guarantees the output is valid JSON (raising ``StructuredOutputError`` if
    not) but does *not* enforce the schema — that requires an injected
    ``StructuredValidator``.
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise StructuredOutputError(str(e)) from e


@runtime_checkable
class StructuredValidator(Protocol):
    """Port: parse + validate a model response against a JSON Schema.

    Returns the parsed object, or raises ``StructuredOutputError`` on a parse or
    schema-validation failure (a repairable error — the agent may re-prompt).
    The concrete (``jsonschema``-based) adapter lives in an outer ring.
    """

    def validate(self, content: str, schema: dict[str, Any]) -> Any: ...
