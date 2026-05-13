"""Tests for the streaming verifier.

Streaming behavior matters for live UX : we want violations to be
flagged as soon as a citation is complete, NOT at the end of the
brief. The tests below cover :

  - Stability detection (a citation isn't emitted mid-stream until
    enough trailing context is seen)
  - Dedup across chunks (the same citation seen twice across two
    feed() calls doesn't fire twice)
  - Final flush (citations at the very end of the buffer are emitted
    only at finalize())
  - Parity with the non-streaming verifier (same input → same final
    report)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lmbox_cli._verifier import (
    Severity,
    StreamingVerifier,
    verify,
    verify_stream,
)
from lmbox_cli._verifier.legifrance import LookupResult, LookupStatus


# ─── Streaming behavior ──────────────────────────────────────────


class TestStreamingPush:
    def test_piece_violation_emitted_when_stable(self):
        sv = StreamingVerifier(pieces=["1", "2"], check_external=False)
        # The citation is at the very end → not stable yet
        new = sv.feed("Le contrat (Pièce n° 12)")
        assert new == []
        # Enough trailing context → stable
        new = sv.feed(" prévoit que le concluant doit s'exécuter sans délai.")
        assert len(new) == 1
        assert new[0].severity == Severity.HIGH
        assert new[0].kind == "piece_not_in_dossier"

    def test_no_double_emit_across_chunks(self):
        sv = StreamingVerifier(pieces=["1", "2"], check_external=False)
        sv.feed("Le contrat (Pièce n° 12) prévoit que")
        sv.feed(" le concluant doit s'exécuter sans délai supplémentaire.")
        # The same buffer-state shouldn't re-emit
        further = sv.feed(" Encore une phrase ajoutée.")
        assert further == []  # no NEW violation, the piece was already seen
        report = sv.finalize()
        # Total violations is still 1
        assert len(report.violations) == 1

    def test_pieces_in_two_separate_chunks(self):
        sv = StreamingVerifier(pieces=["1", "2"], check_external=False)
        sv.feed("Voir Pièce n° 7 sur ce point ainsi que Pièce n° 9 ")
        # Both should already be settled (trailing text in same chunk)
        report = sv.finalize()
        # Both pieces are missing → 2 violations
        assert len(report.violations) == 2

    def test_final_flush_holds_last_citation(self):
        sv = StreamingVerifier(pieces=["1", "2"], check_external=False)
        new = sv.feed("Cette clause se trouve dans la Pièce n° 99")
        # Last citation, no trailing context → not yet stable
        assert new == []
        report = sv.finalize()
        # Flushed at finalize()
        assert len(report.violations) == 1

    def test_malformed_only_caught_at_finalize(self):
        sv = StreamingVerifier(pieces=None, check_external=False)
        sv.feed("Voir Cass. Com., 12 jav 2024 sur la matière.")
        # The streaming scan doesn't run malformed; only finalize() does.
        report = sv.finalize()
        criticals = [v for v in report.violations if v.severity == Severity.CRITICAL]
        assert any(v.kind == "malformed_citation" for v in criticals)


class TestStreamingParity:
    """The streaming + finalize report should match the non-streaming
    verify() exactly for the same input."""

    @pytest.mark.parametrize(
        "text,pieces",
        [
            ("Voir Pièce n° 12 sur ce point.", ["1", "2"]),
            (
                "Voir Pièces n°s 4 à 7 du dossier. La Pièce n° 99 corrobore.",
                ["1", "2", "3", "4", "5", "6", "7"],
            ),
            ("Cass. Com., 12 jav 2024 dispose que rien.", None),
            (
                "Voir Pièce n° 5 et Cass. Com., 12 jav 2024 ensemble dans le rapport.",
                ["1", "2", "3", "4", "5"],
            ),
        ],
    )
    def test_streaming_matches_batch(self, text: str, pieces):
        batch_report = verify(text, pieces=pieces, check_external=False)
        sv = StreamingVerifier(pieces=pieces, check_external=False)
        # Feed in tiny chunks to stress dedup / stability
        for i in range(0, len(text), 7):
            sv.feed(text[i : i + 7])
        streamed_report = sv.finalize()

        assert streamed_report.citations_total == batch_report.citations_total
        # Same number of violations, same severities, same kinds
        assert len(streamed_report.violations) == len(batch_report.violations)
        for sv_v, b_v in zip(
            sorted(streamed_report.violations, key=lambda v: (v.severity, v.citation.position)),
            sorted(batch_report.violations, key=lambda v: (v.severity, v.citation.position)),
        ):
            assert sv_v.severity == b_v.severity
            assert sv_v.kind == b_v.kind


class TestStreamingGenerator:
    def test_verify_stream_yields_violations(self):
        chunks = [
            "Voir Pièce n° 99 (ce qui est faux) ",
            "et Cass. Com., 12 jav 2024 (mois invalide). ",
            "Fin du document.",
        ]
        violations = list(
            verify_stream(chunks, pieces=["1", "2"], check_external=False)
        )
        assert len(violations) == 2
        kinds = {v.kind for v in violations}
        assert "piece_not_in_dossier" in kinds
        assert "malformed_citation" in kinds


class TestStreamingExternal:
    """External lookups during streaming should be cached AND only
    called once per unique citation, even if the same citation appears
    multiple times in the stream."""

    def setup_method(self):
        from lmbox_cli._verifier.legifrance import _reset_caches_for_tests
        _reset_caches_for_tests()

    def teardown_method(self):
        from lmbox_cli._verifier.legifrance import _reset_caches_for_tests
        _reset_caches_for_tests()

    def test_external_lookup_called_once_per_unique(self, monkeypatch):
        # Set creds so the lookup attempts an API call (mocked below)
        monkeypatch.setenv("LEGIFRANCE_CLIENT_ID", "test")
        monkeypatch.setenv("LEGIFRANCE_CLIENT_SECRET", "test")

        from lmbox_cli._verifier import legifrance
        api_calls = []

        def fake_post(path, body, **kw):
            api_calls.append(path)
            return ({"results": [{"id": "JURITEXT-xyz"}]}, None)

        monkeypatch.setattr(legifrance, "_post_json", fake_post)

        sv = StreamingVerifier(check_external=True)
        sv.feed("Cass. Com., 12 janvier 2024, n° 22-15.487 et plus tard ")
        sv.feed("encore Cass. Com., 12 janvier 2024, n° 22-15.487 confirme. ")
        sv.feed("Fin.")
        sv.finalize()
        # The same citation appears twice in the stream; LRU cache
        # collapses to a single Légifrance API hit.
        assert len(api_calls) == 1
