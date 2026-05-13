"""Unit tests for the citation verifier.

Focus is on the regex extractor + the orchestrator's severity
logic. The Légifrance client is mocked — we don't want unit tests
hitting the production API on every CI run.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from lmbox_cli._verifier import Severity, verify
from lmbox_cli._verifier.extractor import (
    CitationKind,
    find_citations,
    find_malformed,
)
from lmbox_cli._verifier.legifrance import LookupResult, LookupStatus


# ─── Extractor unit tests ────────────────────────────────────────


class TestExtractor:
    def test_cassation_full_citation(self):
        text = "Voir Cass. Com., 12 janvier 2024, n° 22-15.487 sur la matière."
        cites = find_citations(text)
        assert len(cites) == 1
        c = cites[0]
        assert c.kind == CitationKind.CASSATION
        assert c.juridiction == "Cass. Com"
        assert c.numero == "22-15.487"
        assert "12 janvier 2024" in c.date

    def test_cassation_civ_with_chamber(self):
        text = "L'arrêt Cass. Civ. 1re, 3 avril 2019, n° 17-15.234 ..."
        cites = find_citations(text)
        assert len(cites) == 1
        assert cites[0].kind == CitationKind.CASSATION

    def test_cassation_without_pourvoi(self):
        # Pourvoi number missing — still extracted, but numero=None
        text = "Cass. Soc., 15 mars 2022 a précisé que..."
        cites = find_citations(text)
        assert len(cites) == 1
        assert cites[0].numero is None

    def test_piece_simple(self):
        text = "Le contrat (Pièce n° 3) prévoit que..."
        cites = find_citations(text)
        assert len(cites) == 1
        assert cites[0].kind == CitationKind.PIECE_INTERNE
        assert cites[0].piece_num == "3"

    def test_piece_lowercase_no_circle(self):
        text = "Comme indiqué dans la pièce 7, l'entreprise..."
        cites = find_citations(text)
        assert len(cites) == 1
        assert cites[0].piece_num == "7"

    def test_no_citation_in_plain_text(self):
        text = "Cette clause est manifestement excessive et doit être annulée."
        assert find_citations(text) == []

    def test_multiple_citations_in_order(self):
        text = (
            "Comme Cass. Com., 5 mai 2023, n° 21-15.487 puis Pièce n° 4 et enfin "
            "Cass. Soc., 12 juin 2024, n° 22-18.111 confirment..."
        )
        cites = find_citations(text)
        assert len(cites) == 3
        # Stable order by source position
        assert cites[0].kind == CitationKind.CASSATION
        assert cites[1].kind == CitationKind.PIECE_INTERNE
        assert cites[2].kind == CitationKind.CASSATION
        assert cites[0].position < cites[1].position < cites[2].position


class TestMalformedDetector:
    def test_invalid_month_caught(self):
        # Truncated month — model "remembered" the format approximately
        text = "Voir Cass. Com., 12 jav 2024 sur la matière."
        malformed = find_malformed(text)
        assert len(malformed) == 1
        assert malformed[0].kind == CitationKind.MALFORMED

    def test_english_month_caught(self):
        text = "Cass. Comm., 12 January 2024 prévoit..."
        malformed = find_malformed(text)
        assert len(malformed) == 1

    def test_valid_month_not_flagged(self):
        text = "Voir Cass. Com., 12 janvier 2024 sur la matière."
        malformed = find_malformed(text)
        assert malformed == []

    def test_accented_february_accepted(self):
        text = "Cass. Soc., 8 février 2023 sur la matière."
        malformed = find_malformed(text)
        assert malformed == []


# ─── Verifier integration tests ──────────────────────────────────


class TestVerifyPieces:
    def test_piece_in_dossier_passes(self):
        text = "Le contrat (Pièce n° 3) prévoit..."
        report = verify(text, pieces=["1", "2", "3", "4"], check_external=False)
        assert report.ok
        assert report.citations_total == 1
        assert report.citations_ok == 1
        assert report.violations == []

    def test_piece_not_in_dossier_flagged_HIGH(self):
        # Model invented Pièce n° 12 — only 1..4 actually exist
        text = "L'article 5 (Pièce n° 12) prévoit..."
        report = verify(text, pieces=["1", "2", "3", "4"], check_external=False)
        assert not report.ok
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.severity == Severity.HIGH
        assert v.kind == "piece_not_in_dossier"
        assert "12" in v.detail

    def test_no_pieces_list_is_unverifiable(self):
        text = "Le contrat (Pièce n° 3) prévoit..."
        report = verify(text, pieces=None, check_external=False)
        # Single MEDIUM, not a failure
        assert report.ok
        assert len(report.violations) == 1
        assert report.violations[0].severity == Severity.MEDIUM


class TestVerifyExternal:
    def test_unverifiable_when_no_creds(self, monkeypatch):
        # Ensure no real API call: env vars stripped
        monkeypatch.delenv("LEGIFRANCE_CLIENT_ID", raising=False)
        monkeypatch.delenv("LEGIFRANCE_CLIENT_SECRET", raising=False)
        text = "Voir Cass. Com., 12 janvier 2024, n° 22-15.487 sur..."
        report = verify(text, check_external=True)
        # Cassation citation flagged MEDIUM (unverifiable, not a fail)
        assert report.ok
        assert any(v.severity == Severity.MEDIUM for v in report.violations)

    def test_critical_when_api_says_not_found(self):
        text = "Voir Cass. Com., 12 janvier 2024, n° 22-15.487 sur..."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_cassation",
            return_value=LookupResult(
                status=LookupStatus.NOT_FOUND,
                message="Aucun arrêt trouvé pour cette référence dans Légifrance.",
            ),
        ):
            report = verify(text, check_external=True)
        assert not report.ok
        criticals = [v for v in report.violations if v.severity == Severity.CRITICAL]
        assert len(criticals) >= 1
        assert criticals[0].kind == "external_not_found"

    def test_skip_external_skips_legifrance(self):
        text = "Voir Cass. Com., 12 janvier 2024, n° 22-15.487 sur..."
        with patch("lmbox_cli._verifier.verifier.lookup_cassation") as mock:
            verify(text, check_external=False)
        mock.assert_not_called()


class TestVerifyMalformed:
    def test_malformed_caught_as_critical(self):
        # Invalid month → hallucination signal
        text = "Cass. Com., 12 jav 2024 dispose que..."
        report = verify(text, check_external=False)
        assert not report.ok
        criticals = [v for v in report.violations if v.severity == Severity.CRITICAL]
        assert len(criticals) >= 1
        assert any(v.kind == "malformed_citation" for v in criticals)


class TestVerifyOrdering:
    def test_violations_sorted_critical_first(self):
        text = (
            "Le contrat (Pièce n° 99) prévoit... "
            "Voir Cass. Com., 12 jav 2024 sur..."
        )
        report = verify(text, pieces=["1", "2"], check_external=False)
        assert len(report.violations) >= 2
        # CRITICAL (malformed) before HIGH (piece_not_in_dossier)
        severities = [v.severity for v in report.violations]
        assert severities.index(Severity.CRITICAL) < severities.index(Severity.HIGH)
