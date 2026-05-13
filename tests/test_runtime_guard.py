"""Tests for the runtime guard (Layer B).

The GuardedStream wraps any iterator of str chunks. We exercise the
three modes (strict / annotate / warn), the cancellation semantics,
the event hook contract, and the partial-text preservation guarantee.

We don't need the LLM backend here — a plain list of strings is
enough to drive the guard deterministically.
"""

from __future__ import annotations

import pytest

from lmbox_cli._runtime import (
    GuardEvent,
    GuardEventType,
    GuardMode,
    GuardedStream,
    GuardedStreamViolation,
)
from lmbox_cli._verifier import Severity


def _chunks_with_bad_piece() -> list[str]:
    return [
        "Le contrat ",
        "(Pièce n° 99) ",
        "prévoit que le concluant doit s'exécuter. ",
        "Fin du document.",
    ]


def _chunks_with_malformed_cass() -> list[str]:
    return [
        "Voir ",
        "Cass. Com., 12 jav 2024 ",
        "sur ce point. ",
        "Fin.",
    ]


# ─── STRICT mode ──────────────────────────────────────────────────


class TestStrictMode:
    def test_cancels_on_piece_not_in_dossier(self):
        gs = GuardedStream(
            producer=_chunks_with_bad_piece(),
            pieces=["1", "2", "3"],
            check_external=False,
            mode=GuardMode.STRICT,
        )
        emitted: list[str] = []
        with pytest.raises(GuardedStreamViolation) as exc_info:
            for chunk in gs:
                emitted.append(chunk)

        # Some chunks were yielded before cancellation
        assert "".join(emitted).startswith("Le contrat ")
        # Cancellation flag + violations attached
        err = exc_info.value
        assert err.report.ok is False
        assert any(v.kind == "piece_not_in_dossier" for v in err.violations)
        # Partial text preserved for audit
        assert "Pièce n° 99" in err.partial_text
        # Result mirror state on the guard
        assert gs.result.cancelled is True

    def test_strict_passes_when_no_violation(self):
        chunks = ["Texte propre, ", "aucune citation. ", "Fin."]
        gs = GuardedStream(
            producer=chunks,
            pieces=["1", "2"],
            check_external=False,
            mode=GuardMode.STRICT,
        )
        out = "".join(gs)
        assert out == "Texte propre, aucune citation. Fin."
        assert gs.result.cancelled is False
        assert gs.result.report.ok is True

    def test_block_severity_critical_lets_high_through(self):
        """With block_severity=CRITICAL, a HIGH (piece) violation
        should NOT cancel — only CRITICAL (malformed/not_found) does."""
        gs = GuardedStream(
            producer=_chunks_with_bad_piece(),
            pieces=["1", "2", "3"],
            check_external=False,
            mode=GuardMode.STRICT,
            block_severity=Severity.CRITICAL,
        )
        # Should NOT raise — HIGH < CRITICAL → not blocking
        out = "".join(gs)
        assert "Pièce n° 99" in out
        assert gs.result.cancelled is False


# ─── ANNOTATE mode ────────────────────────────────────────────────


class TestAnnotateMode:
    def test_inserts_marker_after_offending_citation(self):
        gs = GuardedStream(
            producer=_chunks_with_bad_piece(),
            pieces=["1", "2", "3"],
            check_external=False,
            mode=GuardMode.ANNOTATE,
        )
        out = "".join(gs)
        # The brief continues past the violation
        assert "Fin du document." in out
        # Marker inserted, brief still complete
        assert "HALLUCINATION SUSPECTÉE" in out
        assert gs.result.annotations_inserted == 1
        assert gs.result.cancelled is False

    def test_annotation_template_is_tunable(self):
        gs = GuardedStream(
            producer=_chunks_with_bad_piece(),
            pieces=["1", "2", "3"],
            check_external=False,
            mode=GuardMode.ANNOTATE,
            annotation_template=" [CHECK: {kind}]",
        )
        out = "".join(gs)
        assert "[CHECK: piece_not_in_dossier]" in out

    def test_annotate_handles_malformed_at_finalize(self):
        # Malformed citations are caught at finalize() — they should
        # also be annotated (as a tail annotation).
        gs = GuardedStream(
            producer=_chunks_with_malformed_cass(),
            check_external=False,
            mode=GuardMode.ANNOTATE,
        )
        out = "".join(gs)
        assert "Fin." in out
        assert gs.result.annotations_inserted >= 1
        assert "malformed_citation" in out or "HALLUCINATION" in out


# ─── WARN mode ────────────────────────────────────────────────────


class TestWarnMode:
    def test_stream_passes_through_unchanged(self):
        original = "".join(_chunks_with_bad_piece())
        gs = GuardedStream(
            producer=_chunks_with_bad_piece(),
            pieces=["1", "2", "3"],
            check_external=False,
            mode=GuardMode.WARN,
        )
        out = "".join(gs)
        assert out == original  # NOT modified
        # But violations are reported via the final report
        assert gs.result.report.ok is False
        assert any(
            v.kind == "piece_not_in_dossier"
            for v in gs.result.report.violations
        )

    def test_event_hook_fires_on_violation(self):
        events: list[GuardEvent] = []
        gs = GuardedStream(
            producer=_chunks_with_bad_piece(),
            pieces=["1", "2", "3"],
            check_external=False,
            mode=GuardMode.WARN,
            on_event=events.append,
        )
        list(gs)
        types = [e.type for e in events]
        assert GuardEventType.CHUNK in types
        assert GuardEventType.VIOLATION in types
        assert GuardEventType.FINALIZED in types
        # Cancellation not fired in warn mode
        assert GuardEventType.CANCELLED not in types


# ─── Event-hook isolation ─────────────────────────────────────────


class TestEventHookIsolation:
    def test_failing_hook_does_not_break_stream(self):
        def bad_hook(_e: GuardEvent) -> None:
            raise RuntimeError("observability sink down")

        chunks = ["Texte propre. ", "Fin."]
        gs = GuardedStream(
            producer=chunks, check_external=False,
            mode=GuardMode.WARN, on_event=bad_hook,
        )
        out = "".join(gs)
        assert out == "Texte propre. Fin."  # Stream completed despite the hook


# ─── Edge cases ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_chunks_are_dropped(self):
        gs = GuardedStream(
            producer=["", "Hello ", "", "world.", ""],
            check_external=False,
            mode=GuardMode.WARN,
        )
        assert "".join(gs) == "Hello world."

    def test_finalize_runs_even_on_empty_stream(self):
        gs = GuardedStream(producer=[], check_external=False, mode=GuardMode.WARN)
        list(gs)
        assert gs.result.report is not None
        assert gs.result.report.citations_total == 0

    def test_strict_with_no_creds_still_works_on_format_only(self, monkeypatch):
        """STRICT mode without LEGIFRANCE_CLIENT_ID — malformed
        citations are CRITICAL → still cancels."""
        monkeypatch.delenv("LEGIFRANCE_CLIENT_ID", raising=False)
        monkeypatch.delenv("LEGIFRANCE_CLIENT_SECRET", raising=False)
        gs = GuardedStream(
            producer=_chunks_with_malformed_cass(),
            check_external=False,
            mode=GuardMode.STRICT,
        )
        # Malformed is only detected at finalize(); strict mode treats
        # the post-stream violation as non-cancellable (the stream
        # already ended). We surface it via the final report instead.
        out = "".join(gs)
        # No cancellation, but the report flags it
        assert gs.result.cancelled is False
        assert gs.result.report.ok is False

    def test_producer_close_is_called_on_cancel(self):
        """Strict cancellation should call .close() on a generator producer."""
        closed = {"flag": False}

        def gen():
            try:
                yield "Le contrat "
                yield "(Pièce n° 99) "
                yield "prévoit que le concluant doit s'exécuter. "
                yield "Fin."
            except GeneratorExit:
                closed["flag"] = True
                raise

        gs = GuardedStream(
            producer=gen(),
            pieces=["1", "2"],
            check_external=False,
            mode=GuardMode.STRICT,
        )
        with pytest.raises(GuardedStreamViolation):
            list(gs)
        assert closed["flag"] is True


# ─── Block severity = MEDIUM ──────────────────────────────────────


class TestConfigurableBlockSeverity:
    def test_block_on_medium_in_warn_mode(self):
        """When pieces is None, every Pièce ref is MEDIUM unverifiable.
        With block_severity=MEDIUM in WARN, the event still fires."""
        events: list[GuardEvent] = []
        gs = GuardedStream(
            producer=["Voir Pièce n° 5 sur ce point. ", "Fin."],
            pieces=None,
            check_external=False,
            mode=GuardMode.WARN,
            block_severity=Severity.MEDIUM,
            on_event=events.append,
        )
        list(gs)
        violations = [
            e for e in events if e.type == GuardEventType.VIOLATION
        ]
        assert len(violations) >= 1
        assert violations[0].violation.severity == Severity.MEDIUM
