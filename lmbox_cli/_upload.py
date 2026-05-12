"""HTTP client for the LMbox cloud control plane.

For now there's a single endpoint we hit — POST /api/agents/<serial>/upload.
Kept in its own module so `lmbox agent deploy` stays thin and the
upload logic can grow (retries, resumable uploads) without bloating
the command.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class UploadResult:
    ok: bool
    status_code: int
    agent_installation_id: int | None
    state: str | None
    error: str | None
    raw: dict[str, Any]


def upload_bundle(
    *,
    api_base: str,
    serial: str,
    token: str,
    bundle_path: Path,
    sidecar_path: Path,
    timeout: float = 60.0,
) -> UploadResult:
    """POST the bundle + sidecar as multipart to /api/agents/<serial>/upload.

    Args:
      api_base:    e.g. "https://api.lmbox.eu" (production) or
                   "http://localhost:3000/api" (dev). We append
                   "/agents/<serial>/upload".
      serial:      target box serial.
      token:       bearer token (the target box's API key).
      bundle_path: path to the .lmbox tarball.
      sidecar_path: path to the .lmbox.json metadata.
      timeout:     httpx total timeout in seconds.

    Returns:
      UploadResult with the parsed JSON payload + status code.
    """
    url = f"{api_base.rstrip('/')}/agents/{serial}/upload"
    headers = {"Authorization": f"Bearer {token}"}

    with bundle_path.open("rb") as bf, sidecar_path.open("rb") as sf:
        files = {
            "bundle": (bundle_path.name, bf, "application/x-lmbox"),
            "sidecar": (sidecar_path.name, sf, "application/json"),
        }
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, files=files, headers=headers)

    try:
        raw = resp.json() if resp.content else {}
    except ValueError:
        raw = {"_raw_body": resp.text[:500]}

    return UploadResult(
        ok=resp.is_success,
        status_code=resp.status_code,
        agent_installation_id=raw.get("agent_installation_id"),
        state=raw.get("state"),
        error=raw.get("error") or (None if resp.is_success else f"HTTP {resp.status_code}"),
        raw=raw,
    )
