"""Tests for `lmbox agent deploy` and the _upload module.

The upload itself talks to an HTTP endpoint; we use httpx.MockTransport
to short-circuit the real network — fast tests, no surprises in CI.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import lmbox_cli.commands.deploy as deploy_cmd
from lmbox_cli._upload import upload_bundle
from lmbox_cli.cli import app

runner = CliRunner()


# ─── Helpers ──────────────────────────────────────────────────


def _scaffold_built(tmp_path: Path) -> Path:
    """Scaffold + build → returns the agent dir (ready to pack/deploy)."""
    r1 = runner.invoke(app, ["agent", "new", "demo", "-o", str(tmp_path)])
    assert r1.exit_code == 0
    agent = tmp_path / "demo"
    r2 = runner.invoke(app, ["agent", "build", str(agent)])
    assert r2.exit_code == 0
    return agent


def _make_mock_client(
    *,
    status: int = 201,
    body: dict | None = None,
    fail_with: Exception | None = None,
):
    """Returns a fresh httpx.Client wired to a MockTransport.

    Records the captured request for later assertions.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_with:
            raise fail_with
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content_length"] = len(request.content)
        return httpx.Response(status, json=body or {"ok": True})

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport), captured


# ─── Unit: _upload ────────────────────────────────────────────


def test_upload_returns_ok_on_201(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "x.lmbox"
    sidecar = tmp_path / "x.lmbox.json"
    bundle.write_bytes(b"tarball-bytes")
    sidecar.write_text('{"sha256":"abc"}')

    client, captured = _make_mock_client(
        status=201,
        body={
            "ok": True,
            "agent_installation_id": 42,
            "state": "pending",
            "sha256": "abc",
        },
    )

    # Patch the Client constructor used inside upload_bundle.
    import lmbox_cli._upload as upload_mod

    monkeypatch.setattr(upload_mod.httpx, "Client", lambda timeout=60.0: client)

    result = upload_bundle(
        api_base="https://api.example.com",
        serial="BOX-S-001",
        token="secret",
        bundle_path=bundle,
        sidecar_path=sidecar,
    )

    assert result.ok
    assert result.status_code == 201
    assert result.agent_installation_id == 42
    assert result.state == "pending"
    assert "Bearer secret" in captured["headers"]["authorization"]
    assert "agents/BOX-S-001/upload" in captured["url"]


def test_upload_returns_error_on_401(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "x.lmbox"
    sidecar = tmp_path / "x.lmbox.json"
    bundle.write_bytes(b"x")
    sidecar.write_text("{}")

    client, _ = _make_mock_client(status=401, body={"ok": False, "error": "Invalid token"})
    import lmbox_cli._upload as upload_mod

    monkeypatch.setattr(upload_mod.httpx, "Client", lambda timeout=60.0: client)

    result = upload_bundle(
        api_base="https://api.example.com",
        serial="BOX-X",
        token="bad",
        bundle_path=bundle,
        sidecar_path=sidecar,
    )
    assert not result.ok
    assert result.status_code == 401
    assert "Invalid" in result.error


# ─── End-to-end CLI ───────────────────────────────────────────


@pytest.fixture
def patch_upload(monkeypatch):
    """Replaces upload_bundle in commands.deploy with a controllable stub."""
    captured = {}

    def fake_upload(*, api_base, serial, token, bundle_path, sidecar_path, timeout=60.0):
        captured["api_base"] = api_base
        captured["serial"] = serial
        captured["token"] = token
        captured["bundle_path"] = bundle_path
        captured["sidecar_path"] = sidecar_path
        # Read the files to prove they exist + are non-empty
        captured["bundle_size"] = bundle_path.stat().st_size
        captured["sidecar_size"] = sidecar_path.stat().st_size
        return captured.setdefault(
            "_result_override",
            _ok_result(),
        )

    monkeypatch.setattr(deploy_cmd, "upload_bundle", fake_upload)
    return captured


def _ok_result(state: str = "pending", id_: int = 99):
    from lmbox_cli._upload import UploadResult

    return UploadResult(
        ok=True,
        status_code=201,
        agent_installation_id=id_,
        state=state,
        error=None,
        raw={"ok": True},
    )


def _fail_result(http: int = 401, error: str = "Invalid token"):
    from lmbox_cli._upload import UploadResult

    return UploadResult(
        ok=False,
        status_code=http,
        agent_installation_id=None,
        state=None,
        error=error,
        raw={"ok": False, "error": error},
    )


def test_deploy_happy_path(tmp_path: Path, patch_upload) -> None:
    agent = _scaffold_built(tmp_path)
    res = runner.invoke(
        app,
        [
            "agent",
            "deploy",
            str(agent),
            "--box",
            "BOX-S-042",
            "--token",
            "secret-token",
            "--skip-build",
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert "Deployed" in res.stdout
    assert patch_upload["serial"] == "BOX-S-042"
    assert patch_upload["token"] == "secret-token"
    assert patch_upload["bundle_size"] > 0
    assert patch_upload["sidecar_size"] > 0


def test_deploy_uses_env_token(tmp_path: Path, monkeypatch, patch_upload) -> None:
    agent = _scaffold_built(tmp_path)
    monkeypatch.setenv("LMBOX_API_TOKEN", "env-token")

    res = runner.invoke(
        app,
        [
            "agent",
            "deploy",
            str(agent),
            "--box",
            "BOX-Y",
            "--skip-build",
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert patch_upload["token"] == "env-token"


def test_deploy_without_token_exits_2(tmp_path: Path, monkeypatch) -> None:
    agent = _scaffold_built(tmp_path)
    monkeypatch.delenv("LMBOX_API_TOKEN", raising=False)

    res = runner.invoke(
        app,
        [
            "agent",
            "deploy",
            str(agent),
            "--box",
            "BOX-Z",
            "--skip-build",
        ],
    )
    assert res.exit_code == 2
    assert "No API token" in res.stdout


def test_deploy_upload_failure_exits_1(tmp_path: Path, monkeypatch) -> None:
    """Server says 401 → exit 1 with the error message visible."""
    agent = _scaffold_built(tmp_path)

    def fake_upload(**kwargs):
        return _fail_result(http=401, error="Invalid box API key")

    monkeypatch.setattr(deploy_cmd, "upload_bundle", fake_upload)

    res = runner.invoke(
        app,
        [
            "agent",
            "deploy",
            str(agent),
            "--box",
            "BOX-X",
            "--token",
            "bad",
            "--skip-build",
        ],
    )
    assert res.exit_code == 1
    assert "Upload failed" in res.stdout
    assert "Invalid box API key" in res.stdout


def test_deploy_api_base_override(tmp_path: Path, patch_upload) -> None:
    agent = _scaffold_built(tmp_path)
    res = runner.invoke(
        app,
        [
            "agent",
            "deploy",
            str(agent),
            "--box",
            "BOX",
            "--token",
            "t",
            "--skip-build",
            "--api",
            "http://localhost:3000/api",
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert patch_upload["api_base"] == "http://localhost:3000/api"
