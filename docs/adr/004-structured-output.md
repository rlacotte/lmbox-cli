# ADR-004 — Structured Output (Layer C "à fond")

| | |
|---|---|
| Status | Accepted |
| Date | 2026-05-13 |
| Decider | LMbox core team |

## Context

Layers A + B catch hallucinated *citations* (strings that look like
arrêts/articles but aren't). They don't catch the broader class of
"the agent gave me freeform prose where I asked for structured
data". Examples :

- JurisRecall promises a JSON with `precedents`, `references`,
  `pertinence`. The 7B model sometimes drops `pertinence` or
  returns a string where an array is expected.
- KYB Monitor promises a list of `flag` objects with `category`,
  `severity`, `evidence`. The model occasionally fills `severity`
  with "élevée" instead of one of the schema's enum values.

Downstream pipelines (cabinet's case management, RSSI's compliance
dashboard, partner's BI) require strict shape. A drift means an
ETL break, a missed alert, a stale report.

The LMbox Agent Manifest (ADR-001) already declares an optional
`spec.output_format.kind: json_schema`. This ADR is the production-
grade enforcement that backs it.

## Decision

We ship `lmbox_cli._outputs` with three primitives :

### 1. Validator (`validator.py`)

`validate_output(text_or_obj, schema) → list[ValidationFailure]`
based on `jsonschema` (already a project dep) with the Draft 2020-12
validator (matches OpenAI's `response_format=json_schema`).

Notable design choices :

- We call `Draft202012Validator.check_schema()` first so a broken
  schema (e.g. `required: "string"` instead of a list) surfaces
  as ONE clear `schema_error`, not 10 confusing per-character errors.
- Failures carry `path` (JSON Pointer-ish), `reason`, `offending_value`
  truncated to 200 chars, and `schema_keyword` — enough for the
  repair loop to construct a targeted re-prompt and for the audit
  log to be useful.
- `max_errors` cap (default 20) so a totally-broken response
  doesn't blow up the audit log.

### 2. Linter (`linter.py`)

Schema design foot-guns are why small models silently miscomply.
`lint_schema(schema) → list[LintIssue]` runs 7 rules :

| Level   | Rule                              | Catches                                 |
|---------|-----------------------------------|-----------------------------------------|
| ERROR   | `root_must_be_object`             | Schema not type=object at root          |
| ERROR   | `required_missing_in_properties`  | `required` lists a non-declared field   |
| WARNING | `missing_additional_properties`   | Object node without `additionalProperties`|
| WARNING | `permissive_additional_properties`| `additionalProperties: true`            |
| WARNING | `missing_description`             | Property without `description`          |
| WARNING | `oversized_enum`                  | Enum > 20 values (small models lose it) |
| WARNING | `unspecified_array_items`         | `array` without `items`                 |
| INFO    | `unbounded_string`                | `string` without `maxLength`            |

Exposed as `lmbox agent lint-schema [--strict] [--schema-file]`.
`--strict` fails on WARNINGs (default: only ERRORs fail). Useful
in CI for partner agent factories.

### 3. Repair loop (`repair.py`)

`enforce_structured_output(client, request, schema, max_attempts=3)`
calls the LLM, validates, and re-prompts up to N-1 times with the
structured failures attached to the user turn. The system prompt is
kept verbatim across attempts (KV-cache friendliness).

The repair preamble is intentionally in French (target audience),
explicit ("Ne renvoie QUE le JSON, sans préambule, sans ``` markdown"),
and bounded (top 10 failures listed). Exponential backoff between
attempts (0.5 s → 1 s → 2 s, cap 4 s) keeps us nice on misbehaving
backends.

`RepairResult` carries the **full** attempt trail — failed attempts
included. Operators see exactly what the model emitted, paragraph
by paragraph, attempt by attempt. This matters more than just
"final pass/fail" for debugging prompt-engineering.

### Why not grammar-constrained decoding?

We deliberately don't depend on GBNF / Outlines / Guidance. Reasons :

- Portability across Ollama / LiteLLM / vLLM / OpenAI cloud.
- Observability : a repair attempt is visible; a grammar-constrained
  failure is silent.
- The schema is the contract, not the decoder. When the backend
  supports `response_format=json_schema`, we pass the schema as a
  hint — but we never trust it alone. The validator + repair loop
  ARE the enforcement.

## Consequences

### Positive

- Partner pipelines stop breaking on shape drift. The cabinet's
  ETL can rely on the manifest's schema as a hard contract.
- The linter catches schema design bugs at write-time (the only
  time they're cheap to fix). One `--strict` lint in CI saves a
  week of "why is the model not respecting my enum" debugging.
- Every repair attempt is logged. The audit trail for a partner
  pipeline shows "model fixed it in attempt 2 — here's the diff".
  That's a 10× better story than "we made it work somehow".

### Negative / accepted trade-offs

- `jsonschema` is a project dep. Acceptable on the integrator side
  (where this code runs) — not on the appliance side, where Layer A
  is pure-stdlib for the same reason.
- The repair loop is bounded. After `max_attempts`, we surface a
  `StructuredOutputError` and the operator has to choose : retry
  with a stronger model, fix the prompt, or relax the schema.
- We don't (yet) try grammar-constrained decoding when supported.
  Next ADR if a partner blocks on it.

## References

- `lmbox_cli/_outputs/validator.py` — Draft 2020-12 validation
- `lmbox_cli/_outputs/linter.py` — 7 schema-design checks
- `lmbox_cli/_outputs/repair.py` — bounded repair loop
- `lmbox_cli/commands/lint_schema.py` — `lmbox agent lint-schema`
- `tests/test_outputs.py` (24 cases) — validator + linter + loop
- `tests/test_lint_schema_command.py` (9 cases) — CLI integration
