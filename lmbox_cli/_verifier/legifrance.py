"""Légifrance + EUR-Lex API clients — verify French/EU legal citations.

Architecture
────────────
A citation extracted from an agent's output is fed to the right
`lookup_*` function depending on its kind :

    Citation kind        →  Lookup function
    ─────────────────────────────────────────────────
    CASSATION            →  lookup_cassation
    CONSEIL_ETAT         →  lookup_conseil_etat
    CONSEIL_CONST        →  lookup_conseil_const
    COUR_APPEL           →  lookup_cour_appel
    ARTICLE_CODE         →  lookup_article_code
    LOI / DECRET         →  lookup_loi_decret
    EU_REGLEMENT/DIR     →  lookup_eu_text

Every lookup returns a `LookupResult` with one of three statuses :
FOUND / NOT_FOUND / UNVERIFIABLE. The verifier translates each
status into a severity level (FOUND = OK, NOT_FOUND = CRITICAL,
UNVERIFIABLE = MEDIUM).

Production reliability primitives
─────────────────────────────────
1. OAuth2 token cache (1h, refresh at 50 min, in-memory).
2. LRU cache on each lookup (keyed by (juridiction|kind, date, numero))
   so the same arrêt cited twice in a brief incurs one round-trip.
3. Token-bucket rate limiter (default 90 req/min — under Légifrance's
   100 req/min free tier, with headroom for the OAuth refresh).
4. Retry with exponential backoff on transient errors (429, 502, 503,
   504, network timeout) — 3 attempts, jitter, capped at 8 s total.
5. Hard timeout (15 s default) — a verifier MUST never block an
   agent run for minutes.

The whole module is pure-stdlib (urllib + json) — zero runtime
dependency beyond what lmbox-cli already ships. Easier to vendor in
a customer's appliance without dragging httpx/requests/pydantic in.

Caveats
───────
- Légifrance's coverage of old arrêts (pre-2000) is patchy. A
  NOT_FOUND on an old CA arrêt may be a real miss, not invention.
  The verifier marks the severity accordingly and surfaces the
  unverifiability clearly to the operator.
- The EUR-Lex API is queried by CELEX number (e.g. `32016R0679` for
  GDPR). We compute the CELEX in the extractor and pass it through.
"""

from __future__ import annotations

import functools
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum


# ─── Public types ────────────────────────────────────────────────


class LookupStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNVERIFIABLE = "unverifiable"  # API not configured / transient / out-of-scope


@dataclass(frozen=True)
class LookupResult:
    status: LookupStatus
    message: str = ""
    canonical_url: str | None = None


# ─── Module-level config (env-tunable for ops) ───────────────────


_PISTE_BASE = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
_OAUTH_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
_EURLEX_BASE = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/"

_DEFAULT_TIMEOUT = float(os.environ.get("LMBOX_VERIFIER_TIMEOUT", "15"))
_MAX_RETRIES = int(os.environ.get("LMBOX_VERIFIER_RETRIES", "3"))
_RATE_LIMIT_RPM = int(os.environ.get("LMBOX_VERIFIER_RPM", "90"))


# ─── OAuth2 token cache ──────────────────────────────────────────


_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


def _get_access_token() -> str | None:
    """Fetch (or reuse) an OAuth2 bearer token for the PISTE API.

    Returns None when no credentials are set — callers should fall
    back to LookupStatus.UNVERIFIABLE. Token-cache is thread-safe so
    parallel verifier runs (e.g. `lmbox agent test --strict` on a
    multi-case golden suite) don't race the refresh.
    """
    client_id = os.environ.get("LEGIFRANCE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LEGIFRANCE_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        return None

    cache_key = client_id
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        now = time.time()
        if cached and cached[1] > now + 60:
            return cached[0]

    payload = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "openid",
        }
    ).encode()
    req = urllib.request.Request(
        _OAUTH_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        with _TOKEN_LOCK:
            _TOKEN_CACHE[cache_key] = (token, time.time() + expires_in - 600)
        return token
    except (urllib.error.URLError, KeyError, json.JSONDecodeError):
        return None


# ─── Token-bucket rate limiter ───────────────────────────────────


class _TokenBucket:
    """Simple token-bucket. Refills `rate_per_min` tokens per minute,
    capacity = burst. Thread-safe.

    We pick burst = rate / 6 (~10 s of headroom) so a brief written
    by an agent citing 15 articles + 4 arrêts doesn't trip a 429 on
    the first batch, but a runaway loop is throttled within seconds.
    """

    def __init__(self, rate_per_min: int) -> None:
        self.rate = max(1, rate_per_min) / 60.0  # tokens / sec
        self.capacity = max(5, rate_per_min // 6)
        self.tokens = float(self.capacity)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.last) * self.rate
                )
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                # Compute how long until we'll have a token
                wait = (1 - self.tokens) / self.rate
            time.sleep(min(wait, 1.0))


_BUCKET = _TokenBucket(_RATE_LIMIT_RPM)


# ─── HTTP helper with retry + rate limit ─────────────────────────


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _post_json(
    path: str,
    body: dict,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[dict | None, str | None]:
    """POST `body` (JSON) to `_PISTE_BASE/path` with the OAuth token.

    Returns (parsed_response, None) on success, (None, error_message)
    on failure. Applies rate-limit + 3 retries with exponential
    backoff + jitter for transient errors.
    """
    token = _get_access_token()
    if not token:
        return None, "credentials_missing"

    url = f"{_PISTE_BASE}/{path.lstrip('/')}"
    encoded = json.dumps(body).encode()

    last_err = "unknown_error"
    for attempt in range(_MAX_RETRIES):
        _BUCKET.take()
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode()), None
        except urllib.error.HTTPError as e:
            if e.code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES - 1:
                backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                time.sleep(min(backoff, 8.0))
                last_err = f"http_{e.code}"
                continue
            return None, f"http_{e.code}: {e.reason}"
        except urllib.error.URLError as e:
            if attempt < _MAX_RETRIES - 1:
                backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                time.sleep(min(backoff, 8.0))
                last_err = f"network: {e.reason}"
                continue
            return None, f"network: {e.reason}"
        except json.JSONDecodeError as e:
            return None, f"bad_json: {e}"

    return None, last_err


def _first_hit_url(data: dict) -> str | None:
    """Best-effort: pull out the canonical Légifrance URL from a
    /consult/* response. The API returns several shapes depending on
    endpoint — we accept the union and degrade silently."""
    results = data.get("results") or data.get("hits") or data.get("items") or []
    if not results:
        return None
    first = results[0]
    url_id = (
        first.get("id")
        or first.get("titleId")
        or first.get("cid")
        or first.get("textId")
    )
    if not url_id:
        return None
    return f"https://www.legifrance.gouv.fr/juri/id/{url_id}"


# ─── Per-citation lookups (each one is LRU-cached) ───────────────


@functools.lru_cache(maxsize=512)
def lookup_cassation(
    juridiction: str, date: str, numero: str | None
) -> LookupResult:
    """Verify a Cour de cassation arrêt against Légifrance.

    juridiction : "Cass. Com.", "Cass. Soc.", "Cass. Civ. 1re"...
    date        : "12 janvier 2024"
    numero      : pourvoi (e.g. "22-15.487"), or None
    """
    query = numero.replace(".", "") if numero else f"{juridiction} {date}"
    return _lookup_juri(
        query=query,
        endpoint="consult/getJuriCass",
        facette_value="CASS",
        not_found_msg="Aucun arrêt de Cassation trouvé pour cette référence.",
    )


@functools.lru_cache(maxsize=512)
def lookup_conseil_etat(
    juridiction: str, date: str, numero: str | None
) -> LookupResult:
    """Verify a Conseil d'État decision. Légifrance endpoint:
    `consult/getJuriCetat` (or generic /search with JURI_ADMIN facet).

    Pre-2000 CE decisions are sparse on Légifrance — a NOT_FOUND on
    an old citation may be real, not a hallucination. The orchestrator
    surfaces the date so the operator can judge.
    """
    query = numero or f"Conseil d'Etat {date}"
    return _lookup_juri(
        query=query,
        endpoint="consult/getJuriCetat",
        facette_value="CETAT",
        not_found_msg="Aucune décision du Conseil d'État trouvée pour cette référence.",
    )


@functools.lru_cache(maxsize=512)
def lookup_conseil_const(
    juridiction: str, date: str, numero: str | None
) -> LookupResult:
    """Verify a Conseil constitutionnel decision (QPC, DC, LP, etc.).
    Endpoint: `consult/getCons` — numero format is `2023-1042 DC`.
    """
    query = numero or f"Conseil constitutionnel {date}"
    return _lookup_juri(
        query=query,
        endpoint="consult/getCons",
        facette_value="CONSTIT",
        not_found_msg="Aucune décision du Conseil constitutionnel trouvée.",
    )


@functools.lru_cache(maxsize=512)
def lookup_cour_appel(
    juridiction: str, date: str, numero: str | None
) -> LookupResult:
    """Verify a Cour d'appel arrêt (civil/commercial).

    NOTE : Légifrance's coverage of CA judicial decisions is the
    weakest of the four — for a CA Paris from 2019, expect a
    UNVERIFIABLE rate around 30 %. We still query; verifier marks
    UNVERIFIABLE on miss rather than NOT_FOUND when we know the
    endpoint coverage is incomplete.
    """
    query = numero or f"{juridiction} {date}"
    result = _lookup_juri(
        query=query,
        endpoint="consult/juriJudi",
        facette_value="JURI",
        not_found_msg="Aucun arrêt de Cour d'appel trouvé.",
    )
    # Downgrade NOT_FOUND → UNVERIFIABLE for CA, given known coverage gaps.
    if result.status is LookupStatus.NOT_FOUND:
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message=(
                "Aucun arrêt trouvé sur Légifrance, mais la couverture des "
                "Cours d'appel y est partielle. Vérification manuelle "
                "recommandée."
            ),
        )
    return result


@functools.lru_cache(maxsize=512)
def lookup_article_code(article: str, code: str) -> LookupResult:
    """Verify that an article of a French Code exists.

    `code` is the human label as written in the brief ("Code du
    travail", "CSP", "Code civil"…). We map it to Légifrance's
    internal code identifier and call `consult/code`.
    """
    code_id = _resolve_code_id(code)
    if code_id is None:
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message=f"Code inconnu du mapping : « {code} ». Vérification "
            "manuelle requise.",
        )

    body = {
        "recherche": {
            "champs": [
                {"typeChamp": "NUM_ARTICLE", "criteres": [{"valeur": article}]}
            ],
            "filtres": [{"facette": "CODE_DATE_VERSION", "singleDate": int(time.time() * 1000)}],
            "fond": "CODE_DATE",
            "pageSize": 1,
        },
        "fond": "CODE_DATE",
        "id": code_id,
    }
    data, err = _post_json("consult/code", body)
    if err == "credentials_missing":
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message="LEGIFRANCE_CLIENT_ID/SECRET non configurés — l'article "
            f"« {article} du {code} » n'a pas pu être vérifié.",
        )
    if err:
        return LookupResult(status=LookupStatus.UNVERIFIABLE, message=err)

    # /consult/code shape: {"articles": [{"id": "...", "num": "..."}, ...]}
    articles = (data or {}).get("articles") or (data or {}).get("results") or []
    if articles:
        url_id = articles[0].get("id") or articles[0].get("cid")
        canonical = (
            f"https://www.legifrance.gouv.fr/codes/article_lc/{url_id}"
            if url_id
            else None
        )
        return LookupResult(status=LookupStatus.FOUND, canonical_url=canonical)
    return LookupResult(
        status=LookupStatus.NOT_FOUND,
        message=f"Article « {article} » non trouvé dans {code}.",
    )


@functools.lru_cache(maxsize=512)
def lookup_loi_decret(kind: str, numero: str, date: str) -> LookupResult:
    """Verify a loi / ordonnance / décret by NOR or numero+date.

    Endpoint: `consult/jorf` (Journal officiel) for lois et décrets.
    """
    body = {
        "recherche": {
            "champs": [
                {
                    "typeChamp": "NUM_TEXTE",
                    "criteres": [{"valeur": numero}],
                }
            ],
            "fond": "JORF",
            "pageSize": 1,
        }
    }
    data, err = _post_json("consult/jorf", body)
    if err == "credentials_missing":
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message=f"LEGIFRANCE_CLIENT_ID/SECRET non configurés — {kind} "
            f"n° {numero} non vérifié.",
        )
    if err:
        return LookupResult(status=LookupStatus.UNVERIFIABLE, message=err)

    canonical = _first_hit_url(data or {})
    if canonical or ((data or {}).get("results") or (data or {}).get("items")):
        return LookupResult(status=LookupStatus.FOUND, canonical_url=canonical)
    return LookupResult(
        status=LookupStatus.NOT_FOUND,
        message=f"{kind.capitalize()} n° {numero} du {date} introuvable au JORF.",
    )


@functools.lru_cache(maxsize=512)
def lookup_eu_text(celex: str) -> LookupResult:
    """Verify that an EU regulation/directive exists by CELEX number.

    EUR-Lex exposes a stable URL per CELEX. We do a HEAD request to
    confirm the document exists. Cheap, no API key, no rate limit
    issue — but no body parsing.
    """
    if not celex or len(celex) < 8:
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE, message=f"CELEX invalide : {celex}"
        )

    url = f"{_EURLEX_BASE}?uri=CELEX:{celex}"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            if 200 <= resp.status < 400:
                return LookupResult(
                    status=LookupStatus.FOUND, canonical_url=url
                )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return LookupResult(
                status=LookupStatus.NOT_FOUND,
                message=f"CELEX {celex} introuvable sur EUR-Lex.",
            )
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message=f"EUR-Lex HTTP {e.code}",
        )
    except urllib.error.URLError as e:
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE, message=f"EUR-Lex unreachable: {e.reason}"
        )
    return LookupResult(
        status=LookupStatus.UNVERIFIABLE, message="EUR-Lex returned unexpected status"
    )


# Kept for backward compat: the old verifier dispatch table called
# `lookup_generic` for anything that wasn't Cassation. New callers
# should route to the specific function via the dispatch in
# verifier.py.
def lookup_generic(
    juridiction: str, date: str, numero: str | None
) -> LookupResult:
    return LookupResult(
        status=LookupStatus.UNVERIFIABLE,
        message=f"Lookup non implémenté pour cette juridiction ({juridiction}).",
    )


# ─── Internals ───────────────────────────────────────────────────


def _lookup_juri(
    *,
    query: str,
    endpoint: str,
    facette_value: str,
    not_found_msg: str,
) -> LookupResult:
    """Shared shape for the four jurisprudence endpoints."""
    body = {
        "recherche": {
            "champs": [{"typeChamp": "ALL", "criteres": [{"valeur": query}]}],
            "filtres": [{"facette": "JURIDICTION", "valeurs": [facette_value]}],
            "pageSize": 5,
        }
    }
    data, err = _post_json(endpoint, body)
    if err == "credentials_missing":
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message="LEGIFRANCE_CLIENT_ID/SECRET non configurés — format de la "
            "citation OK, mais existence non confirmée sur Légifrance.",
        )
    if err:
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message=f"Légifrance API: {err}",
        )

    canonical = _first_hit_url(data or {})
    if canonical or ((data or {}).get("results") or (data or {}).get("hits")):
        return LookupResult(status=LookupStatus.FOUND, canonical_url=canonical)
    return LookupResult(status=LookupStatus.NOT_FOUND, message=not_found_msg)


# ─── Code label → Légifrance code identifier mapping ─────────────


# These identifiers are stable Légifrance code IDs (`LEGITEXT…`).
# They identify a code as a versioned text base; the article lookup
# resolves an article number against that base.
_CODE_ID_BY_LABEL = {
    # Long forms (case-insensitive matching done in _resolve_code_id)
    "code civil": "LEGITEXT000006070721",
    "code du travail": "LEGITEXT000006072050",
    "code pénal": "LEGITEXT000006070719",
    "code de commerce": "LEGITEXT000005634379",
    "code de procédure civile": "LEGITEXT000006070716",
    "code de procédure pénale": "LEGITEXT000006071154",
    "code de la consommation": "LEGITEXT000006069565",
    "code de la santé publique": "LEGITEXT000006072665",
    "code monétaire et financier": "LEGITEXT000006072026",
    "code des assurances": "LEGITEXT000006073984",
    "code général des impôts": "LEGITEXT000006069577",
    "code de la propriété intellectuelle": "LEGITEXT000006069414",
    "code de l'environnement": "LEGITEXT000006074220",
    "code de l'urbanisme": "LEGITEXT000006074075",
    # Acronyms commonly used by avocats
    "csp": "LEGITEXT000006072665",   # Santé publique
    "cpc": "LEGITEXT000006070716",   # Procédure civile
    "cpp": "LEGITEXT000006071154",   # Procédure pénale
    "cgi": "LEGITEXT000006069577",   # Général des impôts
    "cmf": "LEGITEXT000006072026",   # Monétaire et financier
    "cpi": "LEGITEXT000006069414",   # Propriété intellectuelle
}


def _resolve_code_id(label: str) -> str | None:
    """Map a human label ("Code du travail", "CSP") to a Légifrance ID.

    Lowercased + stripped. Returns None when unknown — caller should
    surface as UNVERIFIABLE rather than NOT_FOUND.
    """
    if not label:
        return None
    key = label.strip().lower()
    # Normalise "code du travail." or "code du travail," tails
    key = key.rstrip(".,;: ")
    return _CODE_ID_BY_LABEL.get(key)


# ─── Test helper ─────────────────────────────────────────────────


def _reset_caches_for_tests() -> None:
    """Clear LRU + token cache. Called by the test suite between cases
    to keep behavior deterministic. Not exposed in __init__.py."""
    lookup_cassation.cache_clear()
    lookup_conseil_etat.cache_clear()
    lookup_conseil_const.cache_clear()
    lookup_cour_appel.cache_clear()
    lookup_article_code.cache_clear()
    lookup_loi_decret.cache_clear()
    lookup_eu_text.cache_clear()
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()
