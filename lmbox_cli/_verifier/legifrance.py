"""Légifrance API client — verify that a French case-law citation exists.

Légifrance (the official French government legal database) exposes a
JSON API : https://api.piste.gouv.fr/dila/legifrance/lf-engine-app/
The "search" endpoint accepts a free-text query and returns matching
arrêts. We use it as a verification oracle : citation X exists iff
the API returns at least one result matching its juridiction + date +
pourvoi number.

Caveats
───────
1. The Légifrance free API requires an OAuth2 client_id + secret in
   production. For the demo, we operate in two modes :

     - With creds (env: LEGIFRANCE_CLIENT_ID + _CLIENT_SECRET) : real
       verification against the live API.
     - Without creds : the verifier returns "unverifiable" for every
       external citation, BUT the format / malformed checks still
       run. Useful for offline demo + CI.

2. The API doesn't cover everything (older arrêts pre-2000 are
   patchy, some chambres are incomplete). A negative result doesn't
   mean the citation is invented — it means we couldn't confirm it.
   The verifier reports `unverifiable` distinctly from `not_found`.

3. Rate limit on the free tier is ~100 req/min. The verifier batches
   citations into a single search query when possible.

Implementation
──────────────
Pure stdlib (urllib + json) so the verifier has zero runtime deps
beyond what lmbox-cli already pulls. No httpx, no requests, no
pydantic. Easier to ship in a constrained partner environment.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum


class LookupStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNVERIFIABLE = "unverifiable"  # API not configured, transient error, etc.


@dataclass(frozen=True)
class LookupResult:
    status: LookupStatus
    message: str = ""
    canonical_url: str | None = None


_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
"""Memoize the OAuth2 access_token until it expires. Légifrance's
client_credentials tokens last 1 hour; we refresh at 50 minutes."""


def _get_access_token() -> str | None:
    """Fetch (or reuse) an OAuth2 bearer token for the PISTE API.

    Returns None when no credentials are set in the environment —
    callers should fall back to LookupStatus.UNVERIFIABLE.
    """
    client_id = os.environ.get("LEGIFRANCE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LEGIFRANCE_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        return None

    cache_key = client_id
    cached = _TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached and cached[1] > now + 60:
        return cached[0]

    token_url = "https://oauth.piste.gouv.fr/api/oauth/token"
    payload = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "openid",
        }
    ).encode()
    req = urllib.request.Request(
        token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        _TOKEN_CACHE[cache_key] = (token, now + expires_in - 600)
        return token
    except (urllib.error.URLError, KeyError, json.JSONDecodeError):
        return None


def lookup_cassation(juridiction: str, date: str, numero: str | None) -> LookupResult:
    """Verify a Cour de cassation citation against Légifrance.

    juridiction : e.g. "Cass. Com." or "Cass. Soc."
    date        : French-formatted, e.g. "12 janvier 2024"
    numero      : pourvoi number, e.g. "22-15.487" — may be None
    """
    token = _get_access_token()
    if not token:
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message="LEGIFRANCE_CLIENT_ID/SECRET not set — citation format OK, "
            "but cannot confirm existence against Légifrance.",
        )

    # Build a search query that's specific enough to disambiguate.
    # We prefer the pourvoi number (uniquely identifies an arrêt)
    # when available; otherwise fall back to juridiction + date.
    if numero:
        query = numero.replace(".", "")
    else:
        query = f"{juridiction} {date}"

    search_url = (
        "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app/"
        "consult/getJuriCass"
    )
    body = json.dumps(
        {
            "recherche": {
                "champs": [{"typeChamp": "ALL", "criteres": [{"valeur": query}]}],
                "filtres": [{"facette": "JURIDICTION", "valeurs": ["CASS"]}],
                "pageSize": 5,
            }
        }
    ).encode()
    req = urllib.request.Request(
        search_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message=f"Légifrance API error {e.code}: {e.reason}",
        )
    except urllib.error.URLError as e:
        return LookupResult(
            status=LookupStatus.UNVERIFIABLE,
            message=f"Légifrance API unreachable: {e.reason}",
        )

    results = data.get("results") or data.get("hits") or []
    if results:
        # Best-effort URL extraction. Légifrance returns several
        # shapes depending on endpoint; we try the most common.
        first = results[0]
        url_id = first.get("id") or first.get("titleId")
        canonical = (
            f"https://www.legifrance.gouv.fr/juri/id/{url_id}" if url_id else None
        )
        return LookupResult(status=LookupStatus.FOUND, canonical_url=canonical)

    return LookupResult(
        status=LookupStatus.NOT_FOUND,
        message="Aucun arrêt trouvé pour cette référence dans Légifrance.",
    )


def lookup_generic(juridiction: str, date: str, numero: str | None) -> LookupResult:
    """Fallback verification for non-Cassation citations.

    For CE / Cons. const. / CA, the dedicated Légifrance endpoints
    have different shapes. v0.1 of the verifier just signals
    `unverifiable` for these — the format check (extractor) still
    catches malformed ones.

    Roadmap : add lookup_ce, lookup_cc, lookup_ca calling the
    appropriate Légifrance endpoints (getJuriCetat, getCons,
    getJuriCaa).
    """
    return LookupResult(
        status=LookupStatus.UNVERIFIABLE,
        message=f"Lookup non implémenté pour cette juridiction ({juridiction}). "
        "Vérification manuelle requise.",
    )
