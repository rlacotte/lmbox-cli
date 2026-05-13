"""Regex extraction of French legal citations from agent outputs.

We target three families of references that legal agents are most
prone to hallucinate :

  1. Cour de cassation arrêts : `Cass. Com., 12 janvier 2024, n° 22-15.487`
  2. Cour d'appel arrêts      : `CA Paris, 14 mars 2023, n° 21/04567`
  3. Conseil d'État           : `CE, 5 avril 2024, n° 472385`
  4. Conseil constitutionnel  : `Cons. const., 12 mai 2023, n° 2023-1042 DC`
  5. Pièces internes          : `Pièce n° 12`, `pièce 7`, `(Pièces n°s 4 et 5)`

For each match, we capture the canonical form + the surrounding
context (~80 chars) so the caller can highlight the offending snippet
to the operator.

The regexes are intentionally restrictive — better to miss a weird
edge case than to claim a non-citation is a citation. False positives
break trust in the tool faster than false negatives.

References that look almost-right but fail canonical format (wrong
month, missing pourvoi number, etc.) are caught by `find_malformed`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class CitationKind(str, Enum):
    CASSATION = "cassation"
    COUR_APPEL = "cour_appel"
    CONSEIL_ETAT = "conseil_etat"
    CONSEIL_CONST = "conseil_const"
    PIECE_INTERNE = "piece_interne"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class Citation:
    kind: CitationKind
    raw: str         # the matched substring as it appears in the text
    context: str     # ~80 chars surrounding the match
    position: int    # start index in the source text
    # Parsed fields (best-effort). None when unparseable.
    juridiction: str | None = None
    date: str | None = None
    numero: str | None = None
    piece_num: str | None = None


# ─── Canonical French legal citation patterns ────────────────────

# Months in lowercase, French. Used as a constraint so "12 jav 2024"
# (truncated) doesn't match — caught by find_malformed instead.
_MONTH = r"(?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre)"

# Cour de cassation : `Cass. Com.`, `Cass. Civ. 2e`, `Cass. Soc.`, `Cass. Crim.`
_CASS_CHAMBER = r"(?:Civ\.\s*\d?(?:re|e|ère|ème)?|Com\.|Soc\.|Crim\.|AP|Mixte|R[ée]un\.)"
_CASS_PATTERN = re.compile(
    rf"""
    \bCass\.\s*                              # Cass.
    (?P<chambre>{_CASS_CHAMBER})\.?,?\s*     # Com., Civ. 1re, etc.
    (?P<jour>\d{{1,2}})\s+
    (?P<mois>{_MONTH})\s+
    (?P<annee>\d{{4}})
    (?:,\s*n°?\s*(?P<numero>\d{{2}}-\d{{2}}\.?\d{{3,4}}))?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Cour d'appel : `CA Paris, 14 mars 2023, n° 21/04567`
_CA_PATTERN = re.compile(
    rf"""
    \bCA\s+
    (?P<ville>[A-ZÉÈÊÀÂÎÔÛ][a-zéèêàâîôûç-]+(?:\s+[A-ZÉÈÊÀÂÎÔÛ][a-zéèêàâîôûç-]+)?)\s*,?\s*
    (?P<jour>\d{{1,2}})\s+
    (?P<mois>{_MONTH})\s+
    (?P<annee>\d{{4}})
    (?:,\s*n°?\s*(?P<numero>\d{{2}}/\d{{4,6}}))?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Conseil d'État : `CE, 5 avril 2024, n° 472385`
_CE_PATTERN = re.compile(
    rf"""
    \bCE,?\s+
    (?P<jour>\d{{1,2}})\s+
    (?P<mois>{_MONTH})\s+
    (?P<annee>\d{{4}})
    (?:,\s*n°?\s*(?P<numero>\d{{5,7}}))?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Conseil constitutionnel : `Cons. const., 12 mai 2023, n° 2023-1042 DC`
_CC_PATTERN = re.compile(
    rf"""
    \bCons\.\s*const\.,?\s*
    (?P<jour>\d{{1,2}})\s+
    (?P<mois>{_MONTH})\s+
    (?P<annee>\d{{4}})
    (?:,\s*n°?\s*(?P<numero>\d{{4}}-\d{{3,4}}\s*[A-Z]{{2,3}}))?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Pièces internes : `Pièce n° 12`, `Pièces n°s 4 et 5`, `(Pièce 7)`
_PIECE_PATTERN = re.compile(
    r"""
    \bPi[èe]ces?\s*
    (?:n°?s?\s*)?
    (?P<numeros>\d+(?:\s*(?:[à,-]|et)\s*\d+)*)
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _context(text: str, start: int, end: int, radius: int = 60) -> str:
    """Return ~radius chars of context around the match for the report."""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{text[lo:hi].strip()}{suffix}"


def find_citations(text: str) -> list[Citation]:
    """Extract every recognised citation from the text.

    Returns them in source order. Duplicates are kept (e.g. an agent
    citing the same arrêt twice should be checked twice — both
    occurrences are visible in the report).
    """
    results: list[Citation] = []

    for m in _CASS_PATTERN.finditer(text):
        chambre = (m.group("chambre") or "").strip().rstrip(".")
        date = f"{m.group('jour')} {m.group('mois')} {m.group('annee')}"
        results.append(
            Citation(
                kind=CitationKind.CASSATION,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                juridiction=f"Cass. {chambre}",
                date=date,
                numero=m.group("numero"),
            )
        )

    for m in _CA_PATTERN.finditer(text):
        ville = (m.group("ville") or "").strip()
        date = f"{m.group('jour')} {m.group('mois')} {m.group('annee')}"
        results.append(
            Citation(
                kind=CitationKind.COUR_APPEL,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                juridiction=f"CA {ville}",
                date=date,
                numero=m.group("numero"),
            )
        )

    for m in _CE_PATTERN.finditer(text):
        date = f"{m.group('jour')} {m.group('mois')} {m.group('annee')}"
        results.append(
            Citation(
                kind=CitationKind.CONSEIL_ETAT,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                juridiction="CE",
                date=date,
                numero=m.group("numero"),
            )
        )

    for m in _CC_PATTERN.finditer(text):
        date = f"{m.group('jour')} {m.group('mois')} {m.group('annee')}"
        results.append(
            Citation(
                kind=CitationKind.CONSEIL_CONST,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                juridiction="Cons. const.",
                date=date,
                numero=m.group("numero"),
            )
        )

    for m in _PIECE_PATTERN.finditer(text):
        nums_raw = m.group("numeros")
        # Take the first number as the canonical piece reference; the rest
        # (when "Pièces n°s 4 et 5") get their own Citation entries via
        # the regex re-running on the trailing portion. For now, we keep
        # the first.
        first_num = re.match(r"\d+", nums_raw)
        if first_num:
            results.append(
                Citation(
                    kind=CitationKind.PIECE_INTERNE,
                    raw=m.group(0).strip(),
                    context=_context(text, m.start(), m.end()),
                    position=m.start(),
                    piece_num=first_num.group(0),
                )
            )

    # Stable order by position so the report reads top-to-bottom of
    # the source text.
    results.sort(key=lambda c: c.position)
    return results


def find_malformed(text: str) -> list[Citation]:
    """Detect citations that look almost-right but fail canonical format.

    Common malformations :
      - Month truncated or wrong : "Cass. Com., 12 jav 2024"
      - Wrong separator in pourvoi : "n° 22-15487" (missing dot)
      - Mixed FR/EN : "Cass. Comm., Jan 12 2024"

    These are HIGH-RISK signals — the model knew the format roughly
    but made it up. Flag them harder than missing-from-DB.
    """
    results: list[Citation] = []

    # Pattern that matches a Cassation-shaped citation REGARDLESS of
    # month validity (any 2-15 letter word in the month slot).
    permissive = re.compile(
        r"""
        \bCass\.\s*\w+\.?,?\s+
        \d{1,2}\s+
        (?P<month>[a-zàâéèêîïôûùçœ]{2,15})\s+
        \d{4}
        """,
        re.VERBOSE | re.IGNORECASE,
    )
    valid_months = {
        "janvier", "février", "fevrier", "mars", "avril", "mai", "juin",
        "juillet", "août", "aout", "septembre", "octobre", "novembre",
        "décembre", "decembre",
    }
    for m in permissive.finditer(text):
        month = m.group("month").lower()
        if month not in valid_months:
            results.append(
                Citation(
                    kind=CitationKind.MALFORMED,
                    raw=m.group(0).strip(),
                    context=_context(text, m.start(), m.end()),
                    position=m.start(),
                )
            )

    results.sort(key=lambda c: c.position)
    return results
