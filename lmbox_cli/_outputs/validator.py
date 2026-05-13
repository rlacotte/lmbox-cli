"""JSON Schema validator for structured agent outputs.

A thin wrapper around the `jsonschema` library that :

  - Picks the right Draft validator (defaulting to 2020-12 since that's
    what OpenAI's response_format=json_schema also uses).
  - Returns *structured* failures (path + reason + offending value)
    instead of a string blob, so the repair-loop can construct a
    targeted re-prompt.
  - Bounds error reporting at 20 issues so a model that emits
    garbage doesn't blow up the audit log.

Why not stdlib?
───────────────
The verifier (Layer A) is pure stdlib because it ships in customer
appliances. The output validator runs only on the integrator's side
(during `lmbox agent run` / `lmbox agent test`), where `jsonschema`
is already an explicit dependency. No reason to reimplement the
2020-12 spec by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import jsonschema
from jsonschema import Draft202012Validator


@dataclass(frozen=True)
class ValidationFailure:
    """One concrete reason a piece of output is invalid.

    `path` is the JSON Pointer to the offending field
    (e.g. `precedents.0.source_id`); empty for top-level errors.
    """

    path: str
    reason: str
    offending_value: object = None
    schema_keyword: str = ""

    def as_prompt_line(self) -> str:
        """Render the failure as a single line for the repair prompt.

        We want the model to see exactly what it broke, in plain
        French (legal target audience), with the JSON path so it
        can fix the right field without rewriting everything.
        """
        loc = f"`{self.path}`" if self.path else "(racine du document)"
        return f"- {loc} : {self.reason}"


def validate_output(
    output: str | dict | list,
    schema: dict,
    *,
    max_errors: int = 20,
) -> list[ValidationFailure]:
    """Validate a JSON output against a JSON Schema.

    `output` can be a JSON string OR an already-parsed object.
    If a string fails to parse, a single ValidationFailure with
    path="" and reason="JSON invalide: …" is returned.

    Returns an empty list when the output is valid.
    """
    if isinstance(output, (str, bytes, bytearray)):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as e:
            return [
                ValidationFailure(
                    path="",
                    reason=f"JSON invalide à la ligne {e.lineno} col {e.colno}: {e.msg}",
                    schema_keyword="parse_error",
                )
            ]
    else:
        parsed = output

    # Validate the SCHEMA itself first — without this, jsonschema
    # silently misinterprets a malformed `required: "x"` as a per-
    # character required list, producing a flood of meaningless
    # errors. check_schema raises a SchemaError we can surface
    # distinctly to the operator.
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        return [
            ValidationFailure(
                path="",
                reason=f"Schéma invalide: {e.message}",
                schema_keyword="schema_error",
            )
        ]
    validator = Draft202012Validator(schema)

    failures: list[ValidationFailure] = []
    for err in validator.iter_errors(parsed):
        if len(failures) >= max_errors:
            failures.append(
                ValidationFailure(
                    path="",
                    reason=f"… ({max_errors}+ erreurs — réponse probablement non récupérable)",
                    schema_keyword="truncated",
                )
            )
            break
        failures.append(
            ValidationFailure(
                path=_pointer(err.absolute_path),
                reason=err.message,
                offending_value=_safe_offending(err.instance),
                schema_keyword=err.validator or "",
            )
        )
    return failures


# ─── Internals ───────────────────────────────────────────────────


def _pointer(path) -> str:
    """Convert a jsonschema path deque to a dotted JSON Pointer."""
    return ".".join(str(p) for p in path)


def _safe_offending(value: object) -> object:
    """Make sure the offending value is something we can JSON-serialize
    in the audit log. For long strings, truncate to 200 chars."""
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "…"
    if isinstance(value, (dict, list)):
        # Round-trip through json to drop non-serializable nodes
        try:
            return json.loads(json.dumps(value, default=str))
        except Exception:
            return str(value)[:200]
    return value
