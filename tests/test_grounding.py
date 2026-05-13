"""Tests for the grounding layer (Layer D).

We test :
  - The path walker (dot syntax + wildcards)
  - The tracer (record + dedupe of source_ids)
  - The enforcer (strict / warn / off, claimed vs retrieved match)
"""

from __future__ import annotations

import pytest

from lmbox_cli._grounding import (
    GroundingEnforcer,
    GroundingMode,
    GroundingTracer,
    enforce_grounding,
)
from lmbox_cli._grounding.enforcer import _walk_path


# ─── Path walker ──────────────────────────────────────────────────


class TestPathWalker:
    def test_top_level_scalar(self):
        out = {"source_id": "doc-1"}
        results = list(_walk_path(out, "source_id"))
        assert results == [("doc-1", "source_id")]

    def test_wildcard_over_array(self):
        out = {
            "precedents": [
                {"source_id": "doc-1"},
                {"source_id": "doc-2"},
            ]
        }
        results = list(_walk_path(out, "precedents.*.source_id"))
        assert [v for v, _ in results] == ["doc-1", "doc-2"]
        # Resolved paths include the array indices
        assert [p for _, p in results] == [
            "precedents.0.source_id",
            "precedents.1.source_id",
        ]

    def test_nested_wildcards(self):
        out = {
            "sections": [
                {"cited": [{"source_id": "A"}, {"source_id": "B"}]},
                {"cited": [{"source_id": "C"}]},
            ]
        }
        results = list(_walk_path(out, "sections.*.cited.*.source_id"))
        assert [v for v, _ in results] == ["A", "B", "C"]

    def test_missing_key_yields_nothing(self):
        out = {"foo": "bar"}
        assert list(_walk_path(out, "ghost.source_id")) == []

    def test_wildcard_on_object_walks_values(self):
        out = {"by_id": {"a": {"source_id": "X"}, "b": {"source_id": "Y"}}}
        results = list(_walk_path(out, "by_id.*.source_id"))
        # Order not guaranteed (dict iteration); check membership
        assert {v for v, _ in results} == {"X", "Y"}


# ─── Tracer ───────────────────────────────────────────────────────


class TestTracer:
    def test_record_and_read(self):
        t = GroundingTracer()
        t.record_tool_call(
            "search_dossiers_internes",
            args={"query": "non-concurrence"},
            returned_source_ids=["interne-2019-453", "interne-2021-712"],
        )
        assert t.source_ids == {"interne-2019-453", "interne-2021-712"}
        assert t.has_source_id("interne-2019-453")
        assert not t.has_source_id("interne-2099-999")

    def test_multiple_calls_dedupe(self):
        t = GroundingTracer()
        t.record_tool_call("a", returned_source_ids=["x", "y"])
        t.record_tool_call("a", returned_source_ids=["y", "z"])
        assert t.source_ids == {"x", "y", "z"}
        # Audit trail keeps both calls
        assert len(t.calls) == 2

    def test_empty_source_ids_ignored(self):
        t = GroundingTracer()
        t.record_tool_call("a", returned_source_ids=["x", "", None])  # type: ignore[list-item]
        assert t.source_ids == {"x"}

    def test_reset(self):
        t = GroundingTracer()
        t.record_tool_call("a", returned_source_ids=["x"])
        t.reset()
        assert t.source_ids == set()
        assert t.calls == []


# ─── Enforcer ─────────────────────────────────────────────────────


class TestEnforcer:
    def _make(self, retrieved: list[str]) -> GroundingTracer:
        t = GroundingTracer()
        t.record_tool_call("search", returned_source_ids=retrieved)
        return t

    def test_strict_passes_when_all_match(self):
        tracer = self._make(["doc-1", "doc-2"])
        output = {
            "precedents": [
                {"source_id": "doc-1"},
                {"source_id": "doc-2"},
            ]
        }
        report = enforce_grounding(
            output, tracer,
            mode=GroundingMode.STRICT,
            source_id_paths=["precedents.*.source_id"],
        )
        assert report.ok
        assert report.claimed_source_ids == ["doc-1", "doc-2"]
        assert not report.should_block()

    def test_strict_flags_invented_source(self):
        tracer = self._make(["doc-1"])
        output = {
            "precedents": [
                {"source_id": "doc-1"},
                {"source_id": "doc-INVENTED"},
            ]
        }
        report = enforce_grounding(
            output, tracer,
            mode=GroundingMode.STRICT,
            source_id_paths=["precedents.*.source_id"],
        )
        assert not report.ok
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.claimed_source_id == "doc-INVENTED"
        assert v.path == "precedents.1.source_id"
        assert v.severity == "critical"
        assert report.should_block()

    def test_warn_does_not_block(self):
        tracer = self._make(["doc-1"])
        output = {"precedents": [{"source_id": "doc-INVENTED"}]}
        report = enforce_grounding(
            output, tracer,
            mode=GroundingMode.WARN,
            source_id_paths=["precedents.*.source_id"],
        )
        assert not report.ok
        assert not report.should_block()  # WARN mode

    def test_off_skips_everything(self):
        tracer = self._make([])
        output = {"precedents": [{"source_id": "anything"}]}
        report = enforce_grounding(
            output, tracer,
            mode=GroundingMode.OFF,
            source_id_paths=["precedents.*.source_id"],
        )
        assert report.ok
        assert report.violations == []

    def test_empty_paths_records_skip(self):
        tracer = self._make(["doc-1"])
        output = {"precedents": [{"source_id": "doc-INVENTED"}]}
        report = enforce_grounding(
            output, tracer,
            mode=GroundingMode.STRICT,
            source_id_paths=[],
        )
        # No paths declared → no violations but warning surfaced
        assert report.ok
        assert "aucun chemin" in report.skipped_paths[0]

    def test_path_resolving_to_nothing_is_skipped(self):
        tracer = self._make(["doc-1"])
        output = {"items": [{"source_id": "doc-1"}]}
        report = enforce_grounding(
            output, tracer,
            mode=GroundingMode.STRICT,
            source_id_paths=["precedents.*.source_id"],  # path doesn't exist
        )
        assert report.ok
        assert "precedents.*.source_id" in report.skipped_paths

    def test_multiple_paths(self):
        tracer = self._make(["A", "B", "C"])
        output = {
            "precedents": [{"source_id": "A"}, {"source_id": "INVENTED-1"}],
            "references": [{"source_id": "C"}, {"source_id": "INVENTED-2"}],
        }
        report = enforce_grounding(
            output, tracer,
            mode=GroundingMode.STRICT,
            source_id_paths=[
                "precedents.*.source_id",
                "references.*.source_id",
            ],
        )
        assert len(report.violations) == 2
        claimed = {v.claimed_source_id for v in report.violations}
        assert claimed == {"INVENTED-1", "INVENTED-2"}

    def test_enforcer_object_api(self):
        tracer = self._make(["doc-1"])
        e = GroundingEnforcer(
            tracer,
            mode=GroundingMode.STRICT,
            source_id_paths=["precedents.*.source_id"],
        )
        report = e.check(
            {"precedents": [{"source_id": "doc-1"}, {"source_id": "ghost"}]}
        )
        assert len(report.violations) == 1

    def test_non_string_source_id_ignored(self):
        tracer = self._make(["doc-1"])
        # Bad schema: source_id is an int. Enforcer skips quietly —
        # this is a Layer C problem, not Layer D.
        output = {"precedents": [{"source_id": 42}]}
        report = enforce_grounding(
            output, tracer,
            mode=GroundingMode.STRICT,
            source_id_paths=["precedents.*.source_id"],
        )
        assert report.ok  # no violation emitted
        assert report.claimed_source_ids == []


# ─── Manifest schema acceptance ───────────────────────────────────


class TestManifestExtension:
    def test_manifest_with_grounding_validates(self, tmp_path):
        """A manifest with spec.grounding should pass our schema validator."""
        from typer.testing import CliRunner
        from lmbox_cli.cli import app
        import yaml

        runner = CliRunner()
        result = runner.invoke(app, ["agent", "new", "g-agent", "-o", str(tmp_path)])
        assert result.exit_code == 0
        manifest_path = tmp_path / "g-agent" / "manifest.yaml"
        data = yaml.safe_load(manifest_path.read_text())
        data["spec"]["grounding"] = {
            "mode": "strict",
            "source_id_paths": ["precedents.*.source_id"],
        }
        manifest_path.write_text(yaml.safe_dump(data))

        # Validate via the lmbox CLI
        v = runner.invoke(app, ["agent", "validate", str(tmp_path / "g-agent")])
        assert v.exit_code == 0, v.stdout
