"""Structured output layer (Layer C) — enforce JSON Schema on LLM output.

When an agent's manifest declares ::

    spec:
      output_format:
        kind: json_schema
        schema:
          type: object
          required: [precedents, references]
          properties:
            precedents: { type: array, ... }
            references: { type: array, ... }

…the runtime guarantees that what comes back from the model :

  1. Parses as valid JSON.
  2. Validates against the declared schema (draft 2020-12).
  3. If either check fails, the model is re-prompted with the
     validation error message attached, up to N attempts.
  4. After N failed attempts, the call raises StructuredOutputError
     with the partial output + full error trail for the audit log.

In addition to runtime enforcement, this module provides a
**schema linter** (`lint`) that surfaces common foot-guns at agent
build time :

  - Missing field descriptions (model can't tell what to emit).
  - `additionalProperties` not set to false (model fills in extras).
  - Unbounded strings (no maxLength → token-bloat risk).
  - Enum with too many options.
  - Conflicting `required` and missing properties.

The linter is wired into `lmbox agent validate` and exposed as
`lmbox agent lint-schema` for partners writing schemas manually.

See `docs/adr/004-structured-output.md` for the design rationale.
"""

from __future__ import annotations

from lmbox_cli._outputs.linter import LintIssue, LintLevel, lint_schema
from lmbox_cli._outputs.repair import (
    RepairAttempt,
    RepairOutcome,
    RepairResult,
    StructuredOutputError,
    enforce_structured_output,
)
from lmbox_cli._outputs.validator import (
    ValidationFailure,
    validate_output,
)

__all__ = [
    "LintIssue",
    "LintLevel",
    "RepairAttempt",
    "RepairOutcome",
    "RepairResult",
    "StructuredOutputError",
    "ValidationFailure",
    "enforce_structured_output",
    "lint_schema",
    "validate_output",
]
