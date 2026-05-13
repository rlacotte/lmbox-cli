"""Verifier orchestrator — runs every check and produces a structured report.

Public entrypoint is `verify(text, ...)`. Callers get back a
`VerificationReport` with three counters (citations checked, OK,
flagged) and a list of `Violation`s ordered by severity.

The verifier is intentionally non-destructive — it never modifies
the input text. Callers decide what to do with the report :

  - Display + ask the human (CLI default, partner-friendly)
  - Strip flagged citations from the text (server-side post-process)
  - Re-prompt the agent with the report attached (full strict mode)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from lmbox_cli._verifier.extractor import (
    Citation,
    CitationKind,
    find_citations,
    find_malformed,
)
from lmbox_cli._verifier.legifrance import (
    LookupResult,
    LookupStatus,
    lookup_article_code,
    lookup_cassation,
    lookup_conseil_const,
    lookup_conseil_etat,
    lookup_cour_appel,
    lookup_eu_text,
    lookup_generic,
    lookup_loi_decret,
)


class Severity(str, Enum):
    """Severity tiers reported to the operator.

    LOW       — informational. Citation is well-formed and verified.
    MEDIUM    — citation is well-formed but we couldn't verify it
                (Légifrance creds missing, API down, juridiction not
                yet supported). Operator should spot-check manually.
    HIGH      — citation refers to a piece n° X that's not in the
                provided pieces list. Probable hallucination.
    CRITICAL  — citation does NOT exist in Légifrance, OR is
                malformed. Almost certainly a hallucination.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Violation:
    severity: Severity
    kind: str           # short tag : "external_not_found", "piece_not_in_dossier", ...
    citation: Citation  # the raw extraction
    detail: str = ""    # human-readable explanation


@dataclass
class VerificationReport:
    citations_total: int = 0
    citations_ok: int = 0
    violations: list[Violation] = field(default_factory=list)
    legifrance_configured: bool = False

    @property
    def ok(self) -> bool:
        """True when no HIGH or CRITICAL violations were detected.

        MEDIUM (unverifiable) is NOT a failure — it just means we
        couldn't check, the operator should. CRITICAL is a real
        hallucination signal, HIGH is a likely one.
        """
        return not any(
            v.severity in (Severity.HIGH, Severity.CRITICAL) for v in self.violations
        )

    def by_severity(self, severity: Severity) -> list[Violation]:
        return [v for v in self.violations if v.severity == severity]


def verify(
    text: str,
    *,
    pieces: list[str] | None = None,
    check_external: bool = True,
) -> VerificationReport:
    """Run every guardrail on a generated text. Pure function.

    text           : the agent's raw output to inspect
    pieces         : list of piece numbers actually present in the
                     dossier (e.g. ["1", "2", "3", "7"]). When None,
                     internal piece checks are skipped (no list to
                     compare against).
    check_external : when False, skip the Légifrance API calls
                     entirely (useful for unit tests + offline mode).
    """
    report = VerificationReport()

    # ─── Internal pieces ──────────────────────────────────────
    citations = find_citations(text)
    pieces_set = {str(p).strip() for p in pieces} if pieces is not None else None

    for c in citations:
        report.citations_total += 1
        if c.kind == CitationKind.PIECE_INTERNE:
            _check_piece(c, pieces_set, report)
        elif check_external and c.kind in CitationKind.__members__.values():
            _check_external(c, report)
        else:
            # External check skipped — count as OK (no actionable signal).
            report.citations_ok += 1

    # ─── Malformed citations ─────────────────────────────────
    for m in find_malformed(text):
        report.violations.append(
            Violation(
                severity=Severity.CRITICAL,
                kind="malformed_citation",
                citation=m,
                detail="Citation au format quasi-canonique mais avec une faute "
                "(mois invalide, séparateur incorrect, etc.) — l'agent a inventé "
                "une référence en se rappelant approximativement le format.",
            )
        )

    # Légifrance creds detection (informational, surfaced once at the top)
    report.legifrance_configured = check_external and bool(
        # cheap probe : lookup_cassation returns UNVERIFIABLE w/ a specific
        # message when creds are missing; we just check the env directly.
        __import__("os").environ.get("LEGIFRANCE_CLIENT_ID")
    )

    # Sort violations CRITICAL → HIGH → MEDIUM → LOW, then by source position
    severity_order = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
    }
    report.violations.sort(
        key=lambda v: (severity_order[v.severity], v.citation.position)
    )

    return report


# ─── Per-citation handlers ───────────────────────────────────────


def _check_piece(
    c: Citation, pieces_set: set[str] | None, report: VerificationReport
) -> None:
    """Verify that every piece referenced is in the dossier inventory.

    Handles single (`Pièce n° 7`), list (`Pièces n°s 4 et 5`) and
    range (`Pièces n°s 4 à 7`) references uniformly via the
    `piece_nums` tuple populated by the extractor.
    """
    if pieces_set is None:
        report.violations.append(
            Violation(
                severity=Severity.MEDIUM,
                kind="piece_unverifiable",
                citation=c,
                detail="Liste des pièces du dossier non fournie au verifier — "
                "vérifier manuellement.",
            )
        )
        return

    nums = c.piece_nums or ((c.piece_num,) if c.piece_num else ())
    missing = [n for n in nums if n and n not in pieces_set]
    if not missing:
        report.citations_ok += 1
        return

    available = sorted(
        pieces_set, key=lambda x: int(x) if x.isdigit() else 9999
    )
    if len(missing) == 1:
        detail = (
            f"Pièce n° {missing[0]} référencée par l'agent mais absente du "
            f"dossier (pièces disponibles : {available})."
        )
    else:
        detail = (
            f"Pièces n° {missing} référencées par l'agent mais absentes du "
            f"dossier (pièces disponibles : {available})."
        )
    report.violations.append(
        Violation(
            severity=Severity.HIGH,
            kind="piece_not_in_dossier",
            citation=c,
            detail=detail,
        )
    )


def _check_external(c: Citation, report: VerificationReport) -> None:
    """Route a citation to the right Légifrance/EUR-Lex lookup and
    translate the result into a Violation severity."""
    result = _dispatch_lookup(c)
    if result is None:
        # Out-of-scope citation kind (e.g. PIECE_INTERNE got here by
        # mistake, or a new kind without a lookup). Count as OK.
        report.citations_ok += 1
        return

    if result.status == LookupStatus.FOUND:
        report.citations_ok += 1
    elif result.status == LookupStatus.NOT_FOUND:
        report.violations.append(
            Violation(
                severity=Severity.CRITICAL,
                kind="external_not_found",
                citation=c,
                detail=f"Référence non trouvée — probable hallucination. "
                f"({result.message})",
            )
        )
    else:  # UNVERIFIABLE
        report.violations.append(
            Violation(
                severity=Severity.MEDIUM,
                kind="external_unverifiable",
                citation=c,
                detail=result.message,
            )
        )


def _dispatch_lookup(c: Citation) -> LookupResult | None:
    """Route a Citation to its lookup function. Returns None when the
    kind has no external check (e.g. PIECE_INTERNE, MALFORMED — those
    are handled elsewhere)."""
    if c.kind == CitationKind.CASSATION:
        return lookup_cassation(c.juridiction or "", c.date or "", c.numero)
    if c.kind == CitationKind.CONSEIL_ETAT:
        return lookup_conseil_etat(c.juridiction or "", c.date or "", c.numero)
    if c.kind == CitationKind.CONSEIL_CONST:
        return lookup_conseil_const(c.juridiction or "", c.date or "", c.numero)
    if c.kind == CitationKind.COUR_APPEL:
        return lookup_cour_appel(c.juridiction or "", c.date or "", c.numero)
    if c.kind == CitationKind.ARTICLE_CODE:
        return lookup_article_code(c.article or "", c.code or "")
    if c.kind == CitationKind.LOI:
        return lookup_loi_decret("loi", c.numero or "", c.date or "")
    if c.kind == CitationKind.DECRET:
        return lookup_loi_decret("décret", c.numero or "", c.date or "")
    if c.kind in (CitationKind.EU_REGLEMENT, CitationKind.EU_DIRECTIVE):
        return lookup_eu_text(c.eu_celex or "")
    return None
