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
    LookupStatus,
    lookup_cassation,
    lookup_generic,
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
            if pieces_set is None:
                # Caller didn't pass a pieces list. We can't verify;
                # surface as MEDIUM so the operator knows.
                report.violations.append(
                    Violation(
                        severity=Severity.MEDIUM,
                        kind="piece_unverifiable",
                        citation=c,
                        detail="Liste des pièces du dossier non fournie au verifier — "
                        "vérifier manuellement.",
                    )
                )
            elif c.piece_num and c.piece_num not in pieces_set:
                report.violations.append(
                    Violation(
                        severity=Severity.HIGH,
                        kind="piece_not_in_dossier",
                        citation=c,
                        detail=f"Pièce n° {c.piece_num} référencée par l'agent mais "
                        f"absente du dossier (pièces disponibles : "
                        f"{sorted(pieces_set, key=lambda x: int(x) if x.isdigit() else 9999)}).",
                    )
                )
            else:
                report.citations_ok += 1

        elif c.kind == CitationKind.CASSATION and check_external:
            result = lookup_cassation(c.juridiction or "", c.date or "", c.numero)
            if result.status == LookupStatus.FOUND:
                report.citations_ok += 1
            elif result.status == LookupStatus.NOT_FOUND:
                report.violations.append(
                    Violation(
                        severity=Severity.CRITICAL,
                        kind="external_not_found",
                        citation=c,
                        detail=f"Arrêt non trouvé dans Légifrance — probable hallucination. "
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

        elif c.kind in (CitationKind.CONSEIL_ETAT, CitationKind.CONSEIL_CONST,
                        CitationKind.COUR_APPEL) and check_external:
            result = lookup_generic(c.juridiction or "", c.date or "", c.numero)
            report.violations.append(
                Violation(
                    severity=Severity.MEDIUM,
                    kind="external_unverifiable",
                    citation=c,
                    detail=result.message,
                )
            )

        else:
            # External check skipped or not applicable — count as OK
            # (we have nothing actionable to report).
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
