"""Tests for `lmbox agent pack` and the _bundle module.

Two layers:
- Unit tests on the pack/verify helpers (no CLI fluff).
- End-to-end CLI tests that go new → build → pack and inspect
  the produced tarball.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from typer.testing import CliRunner

from lmbox_cli._bundle import pack, verify
from lmbox_cli.cli import app

runner = CliRunner()


def _stage_build_dir(tmp_path: Path) -> Path:
    """Mock a `lmbox agent build` output: a few files in a build dir."""
    build = tmp_path / "build" / "my-agent"
    (build / "prompts").mkdir(parents=True)
    (build / "SKILL.md").write_text("---\nname: my-agent\n---\n\nbody")
    (build / "prompts" / "system.md").write_text("You are X.")
    return build


def _minimal_manifest() -> dict:
    return {
        "apiVersion": "lmbox.eu/v1",
        "kind": "Agent",
        "metadata": {
            "slug": "my-agent",
            "version": "1.2.3",
            "vendor": "lmbox",
            "vertical": "generic",
            "display_name": "My Agent",
            "description": "Just a fixture for pack tests.",
        },
        "spec": {
            "model": {"primary": "mistral-large-2"},
            "prompts": {"system": "prompts/system.md"},
            "evals": {"golden": "evals/golden.jsonl"},
        },
    }


# ─── Unit: pack / verify ──────────────────────────────────────


def test_pack_produces_tarball_and_sidecar(tmp_path: Path) -> None:
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "dist"

    bundle = pack(build_dir=build, manifest=_minimal_manifest(), output_dir=out)

    assert bundle.tarball.exists()
    assert bundle.sidecar.exists()
    assert bundle.tarball.name == "my-agent-1.2.3.lmbox"
    assert bundle.sidecar.name == "my-agent-1.2.3.lmbox.json"
    assert bundle.size_bytes > 0
    assert len(bundle.sha256) == 64
    assert bundle.hmac_sha256 is None  # no key provided


def test_pack_tarball_contains_all_files(tmp_path: Path) -> None:
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "dist"
    bundle = pack(build_dir=build, manifest=_minimal_manifest(), output_dir=out)

    with tarfile.open(bundle.tarball, "r:gz") as tar:
        names = sorted(tar.getnames())
    assert "SKILL.md" in names
    assert "prompts/system.md" in names


def test_pack_with_hmac_signs(tmp_path: Path) -> None:
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "dist"
    key = b"secret-key-bytes"

    bundle = pack(
        build_dir=build,
        manifest=_minimal_manifest(),
        output_dir=out,
        hmac_key=key,
    )
    assert bundle.hmac_sha256 is not None
    assert len(bundle.hmac_sha256) == 64


def test_pack_is_reproducible(tmp_path: Path) -> None:
    """Two pack runs from identical sources must produce byte-identical tarballs."""
    build_a = _stage_build_dir(tmp_path / "a")
    out_a = tmp_path / "dist-a"
    bundle_a = pack(build_dir=build_a, manifest=_minimal_manifest(), output_dir=out_a)

    build_b = _stage_build_dir(tmp_path / "b")
    out_b = tmp_path / "dist-b"
    bundle_b = pack(build_dir=build_b, manifest=_minimal_manifest(), output_dir=out_b)

    sha_a = hashlib.sha256(bundle_a.tarball.read_bytes()).hexdigest()
    sha_b = hashlib.sha256(bundle_b.tarball.read_bytes()).hexdigest()
    assert sha_a == sha_b
    assert bundle_a.sha256 == bundle_b.sha256


def test_pack_raises_on_empty_build_dir(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    import pytest

    with pytest.raises(FileNotFoundError, match="empty"):
        pack(build_dir=empty, manifest=_minimal_manifest(), output_dir=tmp_path / "dist")


def test_verify_round_trip(tmp_path: Path) -> None:
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "dist"
    key = b"another-key"

    bundle = pack(
        build_dir=build,
        manifest=_minimal_manifest(),
        output_dir=out,
        hmac_key=key,
    )

    ok, reason = verify(bundle.tarball, hmac_key=key)
    assert ok, reason


def test_verify_detects_hmac_mismatch(tmp_path: Path) -> None:
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "dist"
    bundle = pack(
        build_dir=build,
        manifest=_minimal_manifest(),
        output_dir=out,
        hmac_key=b"original",
    )

    ok, reason = verify(bundle.tarball, hmac_key=b"WRONG")
    assert not ok
    assert "HMAC" in reason


def test_verify_detects_tarball_tampering(tmp_path: Path) -> None:
    """If the tarball is modified after pack, sha256 verify must fail."""
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "dist"
    bundle = pack(build_dir=build, manifest=_minimal_manifest(), output_dir=out)

    bundle.tarball.write_bytes(bundle.tarball.read_bytes() + b"corrupted")

    ok, reason = verify(bundle.tarball)
    assert not ok
    assert "sha256" in reason


def test_verify_no_hmac_in_sidecar(tmp_path: Path) -> None:
    """Verifying with a key when the sidecar has no hmac must fail with a clear message."""
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "dist"
    bundle = pack(build_dir=build, manifest=_minimal_manifest(), output_dir=out)
    # bundle has no hmac (no key passed)

    ok, reason = verify(bundle.tarball, hmac_key=b"anything")
    assert not ok
    assert "no HMAC" in reason


# ─── End-to-end CLI ───────────────────────────────────────────


def _scaffold_and_build(tmp_path: Path) -> Path:
    """Scaffold + build → returns the agent dir."""
    r1 = runner.invoke(app, ["agent", "new", "demo", "-o", str(tmp_path)])
    assert r1.exit_code == 0
    agent = tmp_path / "demo"
    r2 = runner.invoke(app, ["agent", "build", str(agent)])
    assert r2.exit_code == 0
    return agent


def test_cli_pack_creates_default_dist(tmp_path: Path) -> None:
    agent = _scaffold_and_build(tmp_path)
    res = runner.invoke(app, ["agent", "pack", str(agent)])
    assert res.exit_code == 0, res.stdout
    assert "Packed" in res.stdout
    dist = agent / ".dist"
    assert any(p.name.endswith(".lmbox") for p in dist.iterdir())
    assert any(p.name.endswith(".lmbox.json") for p in dist.iterdir())


def test_cli_pack_without_build_exits_1(tmp_path: Path) -> None:
    """If user skips `build`, pack must fail clearly."""
    res = runner.invoke(app, ["agent", "new", "demo", "-o", str(tmp_path)])
    assert res.exit_code == 0
    res = runner.invoke(app, ["agent", "pack", str(tmp_path / "demo")])
    assert res.exit_code == 1
    assert "No build" in res.stdout


def test_cli_pack_with_hmac_env(tmp_path: Path, monkeypatch) -> None:
    agent = _scaffold_and_build(tmp_path)
    monkeypatch.setenv("LMBOX_PACK_KEY", "hello-world-key")

    res = runner.invoke(app, ["agent", "pack", str(agent)])
    assert res.exit_code == 0, res.stdout

    # Verify the sidecar carries an HMAC
    sidecar = next((agent / ".dist").glob("*.lmbox.json"))
    data = json.loads(sidecar.read_text())
    assert data["hmac_sha256"] is not None
    assert len(data["hmac_sha256"]) == 64


def test_cli_pack_no_hmac_flag_overrides_env(tmp_path: Path, monkeypatch) -> None:
    """--no-hmac suppresses HMAC even if the env var is set."""
    agent = _scaffold_and_build(tmp_path)
    monkeypatch.setenv("LMBOX_PACK_KEY", "irrelevant")

    res = runner.invoke(app, ["agent", "pack", str(agent), "--no-hmac"])
    assert res.exit_code == 0, res.stdout

    sidecar = next((agent / ".dist").glob("*.lmbox.json"))
    data = json.loads(sidecar.read_text())
    assert data["hmac_sha256"] is None


def test_pack_bundle_distributor_partner_slug(tmp_path: Path) -> None:
    """`distributor_partner_slug` is inscribed in the sidecar."""
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "out"
    bundle = pack(
        build_dir=build,
        manifest=_minimal_manifest(),
        output_dir=out,
        distributor_partner_slug="magellan",
    )
    sidecar = json.loads(bundle.sidecar.read_text(encoding="utf-8"))
    assert sidecar["distributor_partner_slug"] == "magellan"


def test_pack_bundle_lmbox_signature(tmp_path: Path) -> None:
    """`lmbox_signature` is inscribed in the sidecar."""
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "out"
    bundle = pack(
        build_dir=build,
        manifest=_minimal_manifest(),
        output_dir=out,
        lmbox_signature="lmbox-mp-1.deadbeef" + "0" * 56,
    )
    sidecar = json.loads(bundle.sidecar.read_text(encoding="utf-8"))
    assert sidecar["lmbox_signature"].startswith("lmbox-mp-1.")


def test_pack_bundle_no_attribution(tmp_path: Path) -> None:
    """Without the new flags, sidecar carries None for both fields."""
    build = _stage_build_dir(tmp_path)
    out = tmp_path / "out"
    bundle = pack(build_dir=build, manifest=_minimal_manifest(), output_dir=out)
    sidecar = json.loads(bundle.sidecar.read_text(encoding="utf-8"))
    assert sidecar["distributor_partner_slug"] is None
    assert sidecar["lmbox_signature"] is None


def test_cli_pack_with_as_distributor(tmp_path: Path) -> None:
    """`lmbox agent pack --as-distributor sopra` writes the slug to sidecar."""
    agent = _scaffold_and_build(tmp_path)
    res = runner.invoke(app, ["agent", "pack", str(agent), "--as-distributor", "sopra"])
    assert res.exit_code == 0, res.stdout
    sidecar = next((agent / ".dist").glob("*.lmbox.json"))
    data = json.loads(sidecar.read_text())
    assert data["distributor_partner_slug"] == "sopra"


def test_cli_pack_with_lmbox_signature_env(tmp_path: Path, monkeypatch) -> None:
    """LMBOX_AGENT_SIGNATURE env is read by --lmbox-signature."""
    agent = _scaffold_and_build(tmp_path)
    monkeypatch.setenv("LMBOX_AGENT_SIGNATURE", "lmbox-mp-1.test-sig")
    res = runner.invoke(app, ["agent", "pack", str(agent)])
    assert res.exit_code == 0, res.stdout
    sidecar = next((agent / ".dist").glob("*.lmbox.json"))
    data = json.loads(sidecar.read_text())
    assert data["lmbox_signature"] == "lmbox-mp-1.test-sig"
