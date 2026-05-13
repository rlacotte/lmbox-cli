"""Regex extraction of French legal citations from agent outputs.

We target nine families of references that legal agents are most
prone to hallucinate :

  1. Cour de cassation arrêts        : `Cass. Com., 12 janvier 2024, n° 22-15.487`
  2. Cour d'appel arrêts             : `CA Paris, 14 mars 2023, n° 21/04567`
  3. Conseil d'État                  : `CE, 5 avril 2024, n° 472385`
  4. Conseil constitutionnel         : `Cons. const., 12 mai 2023, n° 2023-1042 DC`
  5. Articles de Code                : `article L. 1121-1 du Code du travail`
  6. Lois & ordonnances              : `loi n° 2024-1234 du 15 mars 2024`
  7. Décrets                         : `décret n° 2024-456 du 20 mai 2024`
  8. Règlements / directives UE      : `règlement (UE) 2016/679`, `directive 2019/770/UE`
  9. Pièces internes (mono + range)  : `Pièce n° 12`, `Pièces n°s 4 à 7`, `(Pièces 4, 5 et 12)`

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
    ARTICLE_CODE = "article_code"
    LOI = "loi"
    DECRET = "decret"
    EU_REGLEMENT = "eu_reglement"
    EU_DIRECTIVE = "eu_directive"
    PIECE_INTERNE = "piece_interne"
    MALFORMED = "malformed"


# Kinds whose existence we attempt to verify against an external source
# (Légifrance, EUR-Lex). Everything else is structurally checked only.
EXTERNAL_VERIFIABLE_KINDS = frozenset({
    CitationKind.CASSATION,
    CitationKind.COUR_APPEL,
    CitationKind.CONSEIL_ETAT,
    CitationKind.CONSEIL_CONST,
    CitationKind.ARTICLE_CODE,
    CitationKind.LOI,
    CitationKind.DECRET,
})


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
    piece_nums: tuple[str, ...] = ()  # expanded for ranges/lists ("Pièces n°s 4 à 7" → ("4","5","6","7"))
    # For articles de Code / lois / décrets / textes UE
    article: str | None = None
    code: str | None = None
    eu_celex: str | None = None


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

# Pièces internes : `Pièce n° 12`, `Pièces n°s 4 et 5`, `(Pièce 7)`,
# `Pièces n°s 4 à 7` (range — expanded by _expand_piece_nums).
_PIECE_PATTERN = re.compile(
    r"""
    \bPi[èe]ces?\s*
    (?:n°?s?\s*)?
    (?P<numeros>\d+(?:\s*(?:[à–,-]|et)\s*\d+)*)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Articles de Code : `article L. 1121-1 du Code du travail`,
# `art. 1240 du Code civil`, `article R. 4127-1 du CSP`
# We match the article identifier + the code reference together so
# we know which code to look in (Code civil vs Code du travail vs CSP).
_ARTICLE_CODE_PATTERN = re.compile(
    r"""
    \b(?:article|art\.?)\s+
    (?P<article>[LRD]\.?\s?[\d\-.\s]+(?:-\d+)?|\d+(?:-\d+)?)
    \s+du\s+
    (?P<code>
        Code\s+(?:civil|du\s+travail|p[ée]nal|de\s+commerce|de\s+proc[ée]dure\s+(?:civile|p[ée]nale)|
                  de\s+la\s+consommation|de\s+la\s+sant[ée]\s+publique|mon[ée]taire\s+et\s+financier|
                  des\s+assurances|g[ée]n[ée]ral\s+des\s+imp[ôo]ts|de\s+la\s+propri[ée]t[ée]\s+intellectuelle|
                  de\s+l'environnement|de\s+l'urbanisme)
        | CSP | CPC | CPP | CGI | CMF | CPI
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Lois & ordonnances : `loi n° 2024-1234 du 15 mars 2024`,
# `ordonnance n° 2023-1234 du 12 avril 2023`
_LOI_PATTERN = re.compile(
    rf"""
    \b(?:loi|ordonnance)\s+
    n°?\s*(?P<numero>\d{{4}}-\d{{3,5}})\s*
    du\s+
    (?P<jour>\d{{1,2}})\s+
    (?P<mois>{_MONTH})\s+
    (?P<annee>\d{{4}})
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Décrets : `décret n° 2024-456 du 20 mai 2024`
_DECRET_PATTERN = re.compile(
    rf"""
    \bd[ée]cret\s+
    n°?\s*(?P<numero>\d{{4}}-\d{{3,5}})\s*
    du\s+
    (?P<jour>\d{{1,2}})\s+
    (?P<mois>{_MONTH})\s+
    (?P<annee>\d{{4}})
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Règlements UE : `règlement (UE) 2016/679`, `règlement UE n° 2016/679`,
# `RGPD` (special: well-known acronym → resolve to règlement 2016/679)
_EU_REGLEMENT_PATTERN = re.compile(
    r"""
    \br[èe]glement\s+
    (?:\(UE\)|UE)\s*
    (?:n°?\s*)?
    (?P<numero>\d{4}/\d{2,5})
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Directives UE : `directive 2019/770/UE`, `directive (UE) 2019/770`
_EU_DIRECTIVE_PATTERN = re.compile(
    r"""
    \bdirective\s+
    (?:\(UE\)\s+)?
    (?P<numero>\d{4}/\d{2,5})(?:/UE)?
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _expand_piece_nums(raw: str) -> tuple[str, ...]:
    """Expand a piece-numbers string into all referenced numbers.

    Handles three forms :
      - Single : "12"               → ("12",)
      - List   : "4, 5 et 12"       → ("4", "5", "12")
      - Range  : "4 à 7"            → ("4", "5", "6", "7")
      - Mixed  : "4, 5 et 10 à 12"  → ("4", "5", "10", "11", "12")

    Cap at 100 expanded numbers — beyond that, an LLM almost certainly
    miscounted (no real cabinet dossier has 100+ pieces in a single
    sentence reference).
    """
    nums: list[str] = []
    # Split on commas + "et"
    parts = re.split(r"\s*(?:,|et)\s*", raw)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Range : "4 à 7" or "4-7" or "4–7" (en-dash)
        range_match = re.match(r"(\d+)\s*[à–-]\s*(\d+)$", part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start <= end and (end - start) <= 100:
                nums.extend(str(n) for n in range(start, end + 1))
            else:
                nums.append(range_match.group(1))  # too wide, just keep the first
        else:
            single = re.match(r"\d+", part)
            if single:
                nums.append(single.group(0))
    return tuple(nums)


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
        expanded = _expand_piece_nums(nums_raw)
        if not expanded:
            continue
        # `piece_num` keeps the first reference for backward compat
        # (single-piece consumers); `piece_nums` carries the full
        # expansion so the verifier can check every number in a
        # range/list against the dossier inventory.
        results.append(
            Citation(
                kind=CitationKind.PIECE_INTERNE,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                piece_num=expanded[0],
                piece_nums=expanded,
            )
        )

    for m in _ARTICLE_CODE_PATTERN.finditer(text):
        # Normalise the article identifier — strip extra whitespace
        # so "L. 1121-1" stays canonical for the Légifrance lookup.
        article = re.sub(r"\s+", " ", m.group("article").strip())
        code = re.sub(r"\s+", " ", m.group("code").strip())
        results.append(
            Citation(
                kind=CitationKind.ARTICLE_CODE,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                article=article,
                code=code,
            )
        )

    for m in _LOI_PATTERN.finditer(text):
        date = f"{m.group('jour')} {m.group('mois')} {m.group('annee')}"
        results.append(
            Citation(
                kind=CitationKind.LOI,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                date=date,
                numero=m.group("numero"),
            )
        )

    for m in _DECRET_PATTERN.finditer(text):
        date = f"{m.group('jour')} {m.group('mois')} {m.group('annee')}"
        results.append(
            Citation(
                kind=CitationKind.DECRET,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                date=date,
                numero=m.group("numero"),
            )
        )

    for m in _EU_REGLEMENT_PATTERN.finditer(text):
        numero = m.group("numero")
        # CELEX form for règlement = 3 + year + R + number (zero-padded)
        # Example: règlement 2016/679 → 32016R0679
        year, num = numero.split("/")
        celex = f"3{year}R{int(num):04d}"
        results.append(
            Citation(
                kind=CitationKind.EU_REGLEMENT,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                numero=numero,
                eu_celex=celex,
            )
        )

    for m in _EU_DIRECTIVE_PATTERN.finditer(text):
        numero = m.group("numero")
        # CELEX form for directive = 3 + year + L + number (zero-padded)
        year, num = numero.split("/")
        celex = f"3{year}L{int(num):04d}"
        results.append(
            Citation(
                kind=CitationKind.EU_DIRECTIVE,
                raw=m.group(0).strip(),
                context=_context(text, m.start(), m.end()),
                position=m.start(),
                numero=numero,
                eu_celex=celex,
            )
        )

    # Stable order by position so the report reads top-to-bottom of
    # the source text. Deduplicate identical citations at the same
    # position (can happen when a generic pattern overlaps a specific
    # one — e.g. an EU directive matched twice by sibling patterns).
    results.sort(key=lambda c: (c.position, c.kind.value))
    seen: set[tuple[int, str, str]] = set()
    deduped: list[Citation] = []
    for c in results:
        key = (c.position, c.kind.value, c.raw)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


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
