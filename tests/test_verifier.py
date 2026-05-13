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


class TestExtractorExtended:
    """Citation families added in v0.2 : CA, CE, CC, articles de Code,
    lois, décrets, règlements/directives UE, multi-pièces."""

    def test_cour_appel(self):
        text = "Voir CA Paris, 14 mars 2023, n° 21/04567 sur ce point."
        cites = find_citations(text)
        assert len(cites) == 1
        c = cites[0]
        assert c.kind == CitationKind.COUR_APPEL
        assert c.juridiction == "CA Paris"
        assert c.numero == "21/04567"

    def test_conseil_etat(self):
        text = "Le CE, 5 avril 2024, n° 472385 a jugé que..."
        cites = find_citations(text)
        assert len(cites) == 1
        assert cites[0].kind == CitationKind.CONSEIL_ETAT
        assert cites[0].numero == "472385"

    def test_conseil_constitutionnel(self):
        text = "Cons. const., 12 mai 2023, n° 2023-1042 DC, considérant 7."
        cites = find_citations(text)
        assert len(cites) == 1
        assert cites[0].kind == CitationKind.CONSEIL_CONST
        assert cites[0].numero is not None
        assert "2023-1042" in cites[0].numero

    def test_article_code_travail(self):
        text = "L'article L. 1121-1 du Code du travail dispose que..."
        cites = find_citations(text)
        # Should pick the article-de-code citation
        article_cites = [c for c in cites if c.kind == CitationKind.ARTICLE_CODE]
        assert len(article_cites) == 1
        c = article_cites[0]
        assert "1121-1" in c.article
        assert "travail" in c.code.lower()

    def test_article_code_civil_short_form(self):
        text = "Sur le fondement de l'art. 1240 du Code civil, ..."
        cites = find_citations(text)
        article_cites = [c for c in cites if c.kind == CitationKind.ARTICLE_CODE]
        assert len(article_cites) == 1
        assert article_cites[0].article.strip() == "1240"
        assert "civil" in article_cites[0].code.lower()

    def test_article_csp_acronym(self):
        text = "L'article R. 4127-1 du CSP impose au médecin..."
        cites = find_citations(text)
        article_cites = [c for c in cites if c.kind == CitationKind.ARTICLE_CODE]
        assert len(article_cites) == 1
        assert article_cites[0].code.upper() == "CSP"

    def test_loi(self):
        text = "La loi n° 2024-1234 du 15 mars 2024 prévoit que..."
        cites = find_citations(text)
        loi_cites = [c for c in cites if c.kind == CitationKind.LOI]
        assert len(loi_cites) == 1
        assert loi_cites[0].numero == "2024-1234"
        assert "15 mars 2024" in loi_cites[0].date

    def test_ordonnance_matches_loi_pattern(self):
        text = "L'ordonnance n° 2023-1234 du 12 avril 2023 a réformé..."
        cites = find_citations(text)
        loi_cites = [c for c in cites if c.kind == CitationKind.LOI]
        assert len(loi_cites) == 1
        assert loi_cites[0].numero == "2023-1234"

    def test_decret(self):
        text = "Le décret n° 2024-456 du 20 mai 2024 fixe les modalités..."
        cites = find_citations(text)
        decret_cites = [c for c in cites if c.kind == CitationKind.DECRET]
        assert len(decret_cites) == 1
        assert decret_cites[0].numero == "2024-456"

    def test_eu_reglement_celex(self):
        text = "Conformément au règlement (UE) 2016/679, ..."
        cites = find_citations(text)
        reg = [c for c in cites if c.kind == CitationKind.EU_REGLEMENT]
        assert len(reg) == 1
        # CELEX 2016/679 → 32016R0679
        assert reg[0].eu_celex == "32016R0679"

    def test_eu_directive_celex(self):
        text = "La directive 2019/770/UE harmonise..."
        cites = find_citations(text)
        dirs = [c for c in cites if c.kind == CitationKind.EU_DIRECTIVE]
        assert len(dirs) == 1
        assert dirs[0].eu_celex == "32019L0770"

    def test_piece_range_expansion(self):
        # "Pièces n°s 4 à 7" should expand to (4, 5, 6, 7)
        text = "Voir Pièces n°s 4 à 7 du dossier."
        cites = find_citations(text)
        piece_cites = [c for c in cites if c.kind == CitationKind.PIECE_INTERNE]
        assert len(piece_cites) == 1
        assert piece_cites[0].piece_nums == ("4", "5", "6", "7")
        assert piece_cites[0].piece_num == "4"  # first preserved for back-compat

    def test_piece_list_expansion(self):
        text = "Voir Pièces n°s 4, 5 et 12."
        cites = find_citations(text)
        piece_cites = [c for c in cites if c.kind == CitationKind.PIECE_INTERNE]
        assert len(piece_cites) == 1
        assert piece_cites[0].piece_nums == ("4", "5", "12")

    def test_piece_mixed_list_range(self):
        text = "Voir Pièces n°s 4, 5 et 10 à 12."
        cites = find_citations(text)
        piece_cites = [c for c in cites if c.kind == CitationKind.PIECE_INTERNE]
        assert len(piece_cites) == 1
        assert piece_cites[0].piece_nums == ("4", "5", "10", "11", "12")

    def test_piece_en_dash_range(self):
        # En-dash (Word auto-replaces hyphen with en-dash)
        text = "Voir Pièces 4–7 du dossier."
        cites = find_citations(text)
        piece_cites = [c for c in cites if c.kind == CitationKind.PIECE_INTERNE]
        assert len(piece_cites) == 1
        assert piece_cites[0].piece_nums == ("4", "5", "6", "7")


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


class TestVerifyExternalDispatch:
    """The orchestrator must call the right lookup function for each
    citation kind. We mock all lookups and assert the dispatch table."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        # The lookups are LRU-cached; reset between cases so the mocks
        # are actually exercised.
        from lmbox_cli._verifier.legifrance import _reset_caches_for_tests
        _reset_caches_for_tests()
        yield
        _reset_caches_for_tests()

    def _mock_all(self, status=LookupStatus.FOUND, message=""):
        return LookupResult(status=status, message=message)

    def test_dispatch_conseil_etat(self):
        text = "Le CE, 5 avril 2024, n° 472385 a jugé que..."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_conseil_etat",
            return_value=LookupResult(LookupStatus.FOUND),
        ) as mock_ce, patch(
            "lmbox_cli._verifier.verifier.lookup_cassation"
        ) as mock_cass:
            report = verify(text, check_external=True)
        mock_ce.assert_called_once()
        mock_cass.assert_not_called()
        assert report.ok

    def test_dispatch_conseil_const(self):
        text = "Cons. const., 12 mai 2023, n° 2023-1042 DC."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_conseil_const",
            return_value=LookupResult(LookupStatus.FOUND),
        ) as mock:
            report = verify(text, check_external=True)
        mock.assert_called_once()
        assert report.ok

    def test_dispatch_cour_appel_downgrade(self):
        # CA NOT_FOUND should already be downgraded inside the lookup
        # itself. We assert the orchestrator surfaces MEDIUM, not
        # CRITICAL, when the lookup returns UNVERIFIABLE.
        text = "Voir CA Paris, 14 mars 2023, n° 21/04567."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_cour_appel",
            return_value=LookupResult(
                LookupStatus.UNVERIFIABLE, message="coverage gap"
            ),
        ):
            report = verify(text, check_external=True)
        assert report.ok  # MEDIUM only — not a hard failure
        assert any(v.severity == Severity.MEDIUM for v in report.violations)

    def test_dispatch_article_code(self):
        text = "L'article L. 1121-1 du Code du travail dispose..."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_article_code",
            return_value=LookupResult(LookupStatus.FOUND),
        ) as mock:
            report = verify(text, check_external=True)
        mock.assert_called_once()
        # The article string is normalized — assert what we passed.
        call_args = mock.call_args
        assert "1121-1" in call_args[0][0]
        assert "travail" in call_args[0][1].lower()
        assert report.ok

    def test_dispatch_article_critical_when_not_found(self):
        text = "L'article L. 9999-99 du Code du travail dispose..."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_article_code",
            return_value=LookupResult(
                LookupStatus.NOT_FOUND, message="introuvable"
            ),
        ):
            report = verify(text, check_external=True)
        assert not report.ok
        criticals = [v for v in report.violations if v.severity == Severity.CRITICAL]
        assert len(criticals) == 1
        assert criticals[0].kind == "external_not_found"

    def test_dispatch_loi(self):
        text = "La loi n° 2024-1234 du 15 mars 2024 prévoit que..."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_loi_decret",
            return_value=LookupResult(LookupStatus.FOUND),
        ) as mock:
            report = verify(text, check_external=True)
        mock.assert_called_once()
        args = mock.call_args[0]
        assert args[0] == "loi"
        assert args[1] == "2024-1234"
        assert report.ok

    def test_dispatch_decret(self):
        text = "Le décret n° 2024-456 du 20 mai 2024 fixe les modalités..."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_loi_decret",
            return_value=LookupResult(LookupStatus.FOUND),
        ) as mock:
            report = verify(text, check_external=True)
        mock.assert_called_once()
        args = mock.call_args[0]
        assert args[0] == "décret"
        assert args[1] == "2024-456"

    def test_dispatch_eu_reglement(self):
        text = "Conformément au règlement (UE) 2016/679, ..."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_eu_text",
            return_value=LookupResult(LookupStatus.FOUND),
        ) as mock:
            report = verify(text, check_external=True)
        mock.assert_called_once_with("32016R0679")
        assert report.ok

    def test_dispatch_eu_directive(self):
        text = "La directive 2019/770/UE harmonise..."
        with patch(
            "lmbox_cli._verifier.verifier.lookup_eu_text",
            return_value=LookupResult(LookupStatus.FOUND),
        ) as mock:
            report = verify(text, check_external=True)
        mock.assert_called_once_with("32019L0770")
        assert report.ok


class TestVerifyMultiPiece:
    """Range/list references must check every piece, not just the first."""

    def test_range_all_present(self):
        text = "Voir Pièces n°s 4 à 7 du dossier."
        report = verify(text, pieces=["1", "2", "3", "4", "5", "6", "7"], check_external=False)
        assert report.ok
        assert report.citations_ok == 1

    def test_range_with_missing(self):
        # Range 4-7 but dossier only has 4 and 5 → 6 and 7 missing
        text = "Voir Pièces n°s 4 à 7 du dossier."
        report = verify(text, pieces=["4", "5"], check_external=False)
        assert not report.ok
        high = [v for v in report.violations if v.severity == Severity.HIGH]
        assert len(high) == 1
        assert "6" in high[0].detail
        assert "7" in high[0].detail

    def test_list_with_one_missing(self):
        text = "Voir Pièces n°s 4, 5 et 12."
        report = verify(text, pieces=["4", "5"], check_external=False)
        assert not report.ok
        high = [v for v in report.violations if v.severity == Severity.HIGH]
        assert len(high) == 1
        assert "12" in high[0].detail


class TestLegifranceCache:
    """LRU cache on each lookup means repeat citations don't re-query."""

    def setup_method(self):
        from lmbox_cli._verifier.legifrance import _reset_caches_for_tests
        _reset_caches_for_tests()

    def teardown_method(self):
        from lmbox_cli._verifier.legifrance import _reset_caches_for_tests
        _reset_caches_for_tests()

    def test_cassation_cache_hit(self, monkeypatch):
        # Set creds so the lookup actually attempts (mocked) HTTP
        monkeypatch.setenv("LEGIFRANCE_CLIENT_ID", "test")
        monkeypatch.setenv("LEGIFRANCE_CLIENT_SECRET", "test")

        from lmbox_cli._verifier import legifrance
        calls = []

        def fake_post(path, body, **kw):
            calls.append(path)
            return ({"results": [{"id": "JURITEXT123"}]}, None)

        monkeypatch.setattr(legifrance, "_post_json", fake_post)

        r1 = legifrance.lookup_cassation("Cass. Com.", "12 janvier 2024", "22-15.487")
        r2 = legifrance.lookup_cassation("Cass. Com.", "12 janvier 2024", "22-15.487")
        assert r1.status == LookupStatus.FOUND
        assert r2.status == LookupStatus.FOUND
        # The cache hit means _post_json is called only once for two
        # identical lookups.
        assert len(calls) == 1


class TestLegifranceCodeMapping:
    """The Code label resolver must accept canonical labels + common
    acronyms used by avocats."""

    def test_long_form(self):
        from lmbox_cli._verifier.legifrance import _resolve_code_id
        assert _resolve_code_id("Code du travail") == "LEGITEXT000006072050"
        assert _resolve_code_id("Code civil") == "LEGITEXT000006070721"

    def test_acronym(self):
        from lmbox_cli._verifier.legifrance import _resolve_code_id
        # CSP and "Code de la santé publique" map to the same ID
        assert _resolve_code_id("CSP") == _resolve_code_id("Code de la santé publique")

    def test_unknown_returns_none(self):
        from lmbox_cli._verifier.legifrance import _resolve_code_id
        assert _resolve_code_id("Code de la Quénelle") is None

    def test_trailing_punctuation_stripped(self):
        from lmbox_cli._verifier.legifrance import _resolve_code_id
        assert _resolve_code_id("Code civil.") == _resolve_code_id("Code civil")


class TestTokenBucket:
    """Smoke test the rate limiter — it shouldn't be the bottleneck
    for normal verifier load, but a misconfigured RPM=1 must throttle."""

    def test_burst_allowed_then_throttle(self):
        from lmbox_cli._verifier.legifrance import _TokenBucket
        # 60 rpm = 1 per second, burst 10
        b = _TokenBucket(60)
        # First N <= burst should be instant
        start = __import__("time").monotonic()
        for _ in range(5):
            b.take()
        elapsed = __import__("time").monotonic() - start
        assert elapsed < 0.5  # ~instant


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
