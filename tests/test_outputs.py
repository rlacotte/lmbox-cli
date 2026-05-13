"""Tests for the structured-output layer (Layer C).

Covers : the JSON Schema validator, the schema linter, and the
repair loop. The repair loop is tested with a scripted FakeLLMClient
that returns a configurable sequence of strings — first invalid JSON,
then a valid one — and we assert the loop converges.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from lmbox_cli._llm import CompletionRequest, CompletionResponse
from lmbox_cli._outputs import (
    LintIssue,
    LintLevel,
    RepairOutcome,
    StructuredOutputError,
    ValidationFailure,
    enforce_structured_output,
    lint_schema,
    validate_output,
)


# ─── Validator ────────────────────────────────────────────────────


class TestValidator:
    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "items"],
        "properties": {
            "title": {"type": "string", "maxLength": 80},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_id", "snippet"],
                    "properties": {
                        "source_id": {"type": "string"},
                        "snippet": {"type": "string", "maxLength": 500},
                    },
                },
            },
        },
    }

    def test_valid_output_returns_empty(self):
        out = {
            "title": "Demo",
            "items": [{"source_id": "doc-1", "snippet": "Hello"}],
        }
        assert validate_output(out, self.SCHEMA) == []

    def test_valid_json_string_returns_empty(self):
        out = '{"title":"Demo","items":[{"source_id":"d","snippet":"s"}]}'
        assert validate_output(out, self.SCHEMA) == []

    def test_missing_required_field(self):
        out = {"title": "Demo"}  # `items` missing
        failures = validate_output(out, self.SCHEMA)
        assert any("items" in f.reason for f in failures)
        assert any(f.schema_keyword == "required" for f in failures)

    def test_wrong_type_returns_path(self):
        out = {"title": 123, "items": []}
        failures = validate_output(out, self.SCHEMA)
        assert any(f.path == "title" for f in failures)

    def test_invalid_json_string(self):
        failures = validate_output("not json {{", self.SCHEMA)
        assert len(failures) == 1
        assert failures[0].schema_keyword == "parse_error"

    def test_nested_path_pointer(self):
        out = {
            "title": "Demo",
            "items": [{"source_id": "doc-1"}],  # snippet missing
        }
        failures = validate_output(out, self.SCHEMA)
        # The nested failure should reference items.0
        paths = [f.path for f in failures]
        assert any("items.0" in p for p in paths)

    def test_max_errors_caps_reporting(self):
        # Schema requires 30 fields; an empty object → 30 failures
        big_schema = {
            "type": "object",
            "required": [f"f{i}" for i in range(30)],
            "properties": {f"f{i}": {"type": "string"} for i in range(30)},
        }
        failures = validate_output({}, big_schema, max_errors=5)
        assert len(failures) <= 6  # 5 + the "truncated" sentinel

    def test_broken_schema_surfaces_clearly(self):
        bad_schema = {"type": "object", "required": "not-a-list"}
        failures = validate_output({}, bad_schema)
        assert len(failures) == 1
        assert failures[0].schema_keyword == "schema_error"

    def test_prompt_line_for_repair(self):
        out = {"title": 123, "items": []}
        failures = validate_output(out, self.SCHEMA)
        # The first failure should produce a readable French line
        line = failures[0].as_prompt_line()
        assert line.startswith("- ")
        assert "title" in line


# ─── Linter ───────────────────────────────────────────────────────


class TestLinter:
    def test_clean_schema_no_issues(self):
        clean = {
            "type": "object",
            "additionalProperties": False,
            "required": ["a"],
            "properties": {
                "a": {"type": "string", "maxLength": 80, "description": "Foo."},
            },
        }
        issues = lint_schema(clean)
        # Clean schemas may have INFO issues but no ERROR/WARNING
        bad = [i for i in issues if i.level in (LintLevel.ERROR, LintLevel.WARNING)]
        assert bad == []

    def test_root_must_be_object(self):
        issues = lint_schema({"type": "string"})
        assert any(i.rule == "root_must_be_object" for i in issues)
        assert all(i.level == LintLevel.ERROR for i in issues if i.rule == "root_must_be_object")

    def test_missing_description_warns(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "string", "maxLength": 80}},  # no description
        }
        issues = lint_schema(schema)
        descs = [i for i in issues if i.rule == "missing_description"]
        assert len(descs) == 1
        assert descs[0].level == LintLevel.WARNING

    def test_missing_additional_properties_warns(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string", "maxLength": 80, "description": "x"}},
        }
        issues = lint_schema(schema)
        assert any(i.rule == "missing_additional_properties" for i in issues)

    def test_unbounded_string_info(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "description": "Name."}  # no maxLength
            },
        }
        issues = lint_schema(schema)
        unbound = [i for i in issues if i.rule == "unbounded_string"]
        assert len(unbound) == 1
        assert unbound[0].level == LintLevel.INFO

    def test_oversized_enum_warns(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {
                    "type": "string",
                    "description": "x",
                    "enum": [f"v{i}" for i in range(30)],
                }
            },
        }
        issues = lint_schema(schema)
        assert any(i.rule == "oversized_enum" for i in issues)

    def test_required_missing_in_properties_error(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "ghost"],
            "properties": {"title": {"type": "string", "maxLength": 80, "description": "x"}},
        }
        issues = lint_schema(schema)
        errs = [i for i in issues if i.rule == "required_missing_in_properties"]
        assert len(errs) == 1
        assert errs[0].level == LintLevel.ERROR

    def test_unspecified_array_items_warns(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tags": {"type": "array", "description": "tags"},  # no items
            },
        }
        issues = lint_schema(schema)
        assert any(i.rule == "unspecified_array_items" for i in issues)

    def test_issues_sorted_errors_first(self):
        schema = {
            "type": "object",
            "required": ["ghost"],
            "properties": {"a": {"type": "string", "maxLength": 80}},
        }
        issues = lint_schema(schema)
        levels = [i.level for i in issues]
        # All ERRORs come before any WARNING/INFO
        first_warn = next(
            (i for i, lv in enumerate(levels) if lv != LintLevel.ERROR),
            len(levels),
        )
        assert all(lv == LintLevel.ERROR for lv in levels[:first_warn])


# ─── Repair loop ──────────────────────────────────────────────────


class FakeScripted:
    """LLM stub returning a scripted sequence of responses."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        if not self.responses:
            raise RuntimeError("script exhausted")
        return CompletionResponse(content=self.responses.pop(0), model=req.model)

    def stream(self, req: CompletionRequest) -> Iterator[str]:
        yield self.complete(req).content


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["greeting"],
    "properties": {"greeting": {"type": "string", "maxLength": 80}},
}


class TestRepairLoop:
    def test_succeeds_on_first_try(self):
        client = FakeScripted(['{"greeting": "bonjour"}'])
        req = CompletionRequest(model="m", system="sys", user="say hello")
        result = enforce_structured_output(client, req, SCHEMA)
        assert result.succeeded
        assert result.output == {"greeting": "bonjour"}
        assert len(result.attempts) == 1
        assert len(client.calls) == 1

    def test_repairs_after_one_failure(self):
        client = FakeScripted([
            '{"hello": "world"}',          # missing required `greeting`
            '{"greeting": "bonjour"}',     # valid
        ])
        req = CompletionRequest(model="m", system="sys", user="say hi")
        result = enforce_structured_output(
            client, req, SCHEMA, max_attempts=3, backoff_base=0
        )
        assert result.succeeded
        assert len(result.attempts) == 2
        # Second attempt's user prompt should mention the failure
        second_call = client.calls[1].user
        assert "greeting" in second_call.lower() or "required" in second_call.lower()

    def test_exhausts_after_max_attempts(self):
        client = FakeScripted(['{"wrong": 1}'] * 5)  # never valid
        req = CompletionRequest(model="m", system="sys", user="say hi")
        result = enforce_structured_output(
            client, req, SCHEMA, max_attempts=2, backoff_base=0
        )
        assert not result.succeeded
        assert result.outcome is RepairOutcome.EXHAUSTED
        assert len(result.attempts) == 2
        assert len(client.calls) == 2

    def test_on_attempt_hook_fires_each_round(self):
        client = FakeScripted([
            '{"wrong": 1}',
            '{"greeting": "ok"}',
        ])
        seen = []
        req = CompletionRequest(model="m", system="sys", user="say hi")
        enforce_structured_output(
            client, req, SCHEMA,
            max_attempts=3,
            backoff_base=0,
            on_attempt=seen.append,
        )
        assert len(seen) == 2
        assert seen[0].is_valid is False
        assert seen[1].is_valid is True

    def test_max_attempts_must_be_positive(self):
        client = FakeScripted(['{}'])
        req = CompletionRequest(model="m", system="sys", user="x")
        with pytest.raises(ValueError):
            enforce_structured_output(client, req, SCHEMA, max_attempts=0)

    def test_invalid_json_counts_as_failure(self):
        client = FakeScripted([
            "ceci n'est pas du json {{{",
            '{"greeting": "ok"}',
        ])
        req = CompletionRequest(model="m", system="sys", user="x")
        result = enforce_structured_output(
            client, req, SCHEMA, max_attempts=3, backoff_base=0
        )
        assert result.succeeded
        # First attempt's failures should include a parse_error
        first_failures = result.attempts[0].failures
        assert any(f.schema_keyword == "parse_error" for f in first_failures)
