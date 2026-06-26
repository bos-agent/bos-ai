"""Default ``StructuredValidator`` adapter (BEP 12) — jsonschema-backed.

Lives in the assembly ring because it depends on ``jsonschema`` (third-party),
which the stdlib-pure agent ring may not import. Injected into agents so the
agent's structured-output path can enforce the caller's schema.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema

from bos.core.agent import StructuredOutputError


class JsonSchemaValidator:
    """Parse + JSON-Schema-validate a model response (implements StructuredValidator)."""

    def validate(self, content: str, schema: dict[str, Any]) -> Any:
        try:
            parsed = json.loads(content)
            jsonschema.validate(parsed, schema)
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            raise StructuredOutputError(str(e)) from e
        return parsed
