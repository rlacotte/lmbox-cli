"""Bundle assembly + integrity signing.

Takes the output of `lmbox agent build` and produces a single .lmbox
tarball (gzip-compressed tar) ready to be uploaded to the cloud
control plane. A `.lmbox.json` sidecar carries the integrity
metadata: SHA-256 of the tarball + optional HMAC.

Format on disk
──────────────
    my-agent-0.1.0.lmbox       # the gzipped tarball
    my-agent-0.1.0.lmbox.json  # { sha256, hmac (optional), size, manifest }

The HMAC is computed with HMAC-SHA256 over the tarball bytes using
a partner key (LMBOX_PACK_KEY env var). It is OPTIONAL at pack time
— if no key is set, the sidecar carries only sha256. The cloud may
require a valid HMAC for accepted partner uploads (verifiable
provenance).

Why HMAC not X.509?
───────────────────
HMAC is enough for the cloud→box trust path (the cloud holds the
HMAC key per customer). X.509 code signing is what we want for
partner-published agents in 0.5+ when the marketplace opens.
Trade-offs documented in ADR-004 (when it lands).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Bundle:
    """The artefact set produced by `pack()` — both files."""

    tarball: Path  # the .lmbox file
    sidecar: Path  # the .lmbox.json metadata file
    sha256: str  # hex digest of the tarball
    hmac_sha256: str | None  # hex digest if a key was provided
    size_bytes: int


def pack(
    *,
    build_dir: Path,
    manifest: dict[str, Any],
    output_dir: Path,
    hmac_key: bytes | None = None,
    distributor_partner_slug: str | None = None,
    lmbox_signature: str | None = None,
) -> Bundle:
    """Pack a build directory into a signed .lmbox bundle.

    Args:
      build_dir:  The directory produced by `lmbox agent build`
                  (typically <agent>/.build/<kernel>/<slug>/).
      manifest:   The parsed manifest.yaml (for naming + metadata).
      output_dir: Where to drop the .lmbox + .lmbox.json files.
                  Created if it doesn't exist.
      hmac_key:   Optional bytes used as the HMAC key. None ⇒ no HMAC
                  computed (only sha256 in sidecar).

    Returns:
      A `Bundle` describing the produced files. Both files exist
      on disk when this returns.

    Raises:
      FileNotFoundError if `build_dir` is missing or empty.
    """
    if not build_dir.exists():
        raise FileNotFoundError(f"Build dir does not exist: {build_dir}")
    if not any(build_dir.rglob("*")):
        raise FileNotFoundError(f"Build dir is empty: {build_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    md = manifest["metadata"]
    slug = md["slug"]
    version = md["version"]
    base_name = f"{slug}-{version}.lmbox"

    tarball_path = output_dir / base_name
    sidecar_path = output_dir / f"{base_name}.json"

    # ─── Build the tarball ────────────────────────────────────
    # Reproducible: deterministic mtime (manifest version), no uid/gid,
    # entries sorted. So two `pack` runs from the same source emit
    # byte-identical artefacts → byte-identical sha256 / HMAC.
    epoch = _manifest_mtime(md)

    with tarfile.open(tarball_path, "w:gz", compresslevel=9) as tar:
        for path in sorted(build_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(build_dir).as_posix()
            info = tar.gettarinfo(str(path), arcname=arcname)
            info.mtime = epoch
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o644
            with path.open("rb") as f:
                tar.addfile(info, f)

    # ─── Hash + (optional) HMAC ──────────────────────────────
    data = tarball_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    hm: str | None = None
    if hmac_key is not None:
        hm = hmac.new(hmac_key, data, hashlib.sha256).hexdigest()

    # ─── Sidecar metadata ────────────────────────────────────
    sidecar = {
        "bundle_format": "lmbox.eu/bundle/v1",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "agent": {
            "slug": slug,
            "version": version,
            "vendor": md["vendor"],
            "vertical": md.get("vertical", "generic"),
        },
        "tarball": base_name,
        "size_bytes": len(data),
        "sha256": sha,
        "hmac_sha256": hm,
        # Slug du partner distributor (Sopra, Magellan, …) qui installe
        # cet agent. Le cloud LMbox attribue le revenue share marketplace
        # à ce partner. Optionnel : si None, fallback côté cloud sur
        # box.customer.partner.
        "distributor_partner_slug": distributor_partner_slug,
        # Signature HMAC LMbox du bundle, requise pour les agents
        # marketplace publiés. Le cloud refuse l'install si le slug+version
        # match un MarketplaceAgent published mais que la signature
        # manque ou ne match pas.
        "lmbox_signature": lmbox_signature,
        "manifest_snapshot": manifest,
    }
    sidecar_path.write_text(
        json.dumps(sidecar, indent=2, ensure_ascii=False, sort_keys=False),
        encoding="utf-8",
    )

    return Bundle(
        tarball=tarball_path,
        sidecar=sidecar_path,
        sha256=sha,
        hmac_sha256=hm,
        size_bytes=len(data),
    )


def verify(bundle_path: Path, *, hmac_key: bytes | None = None) -> tuple[bool, str]:
    """Verify a .lmbox tarball against its sidecar. Used by the box at install time.

    Returns (ok, reason). If `hmac_key` is None we only check sha256.
    """
    sidecar_path = bundle_path.with_suffix(bundle_path.suffix + ".json")
    if not sidecar_path.exists():
        return False, f"Sidecar missing: {sidecar_path.name}"

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    data = bundle_path.read_bytes()

    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha != sidecar.get("sha256"):
        return False, f"sha256 mismatch (got {actual_sha[:12]}…)"

    if hmac_key is not None:
        expected_hmac = sidecar.get("hmac_sha256")
        if expected_hmac is None:
            return False, "sidecar carries no HMAC but a key was provided"
        actual_hmac = hmac.new(hmac_key, data, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual_hmac, expected_hmac):
            return False, "HMAC mismatch"

    return True, "ok"


def _manifest_mtime(metadata: dict[str, Any]) -> int:
    """Deterministic mtime from the manifest version.

    Hashing the version string + slug gives a stable but distinct
    timestamp per release. Avoids the "all files have today's date"
    behaviour that breaks tarball reproducibility.
    """
    seed = f"{metadata['vendor']}|{metadata['slug']}|{metadata['version']}".encode()
    # Map the digest to a sane epoch (post-2020, pre-2038 to stay safe
    # on 32-bit tar consumers that still exist on some old boxes).
    digest = int.from_bytes(hashlib.sha256(seed).digest()[:4], "big")
    return 1_577_836_800 + (digest % (15 * 365 * 24 * 3600))  # 2020-01-01 + up to 15y
