"""Tests for `lmbox agent build` and the OpenClawAdapter.

Two layers:

1. Unit tests for the adapter on plain dicts (no filesystem fluff,
   pure compile logic verifiable in milliseconds).

2. End-to-end CLI tests that scaffold a real agent, run
   `lmbox agent build`, and inspect the produced SKILL.md to confirm
   the YAML frontmatter + body round-trip is correct.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from lmbox_cli._adapters import OpenClawAdapter
from lmbox_cli._adapters.base import AdapterError, BuildResult
from lmbox_cli.cli import app

runner = CliRunner()


def _minimal_manifest(**overrides) -> dict:
    """Tiny manifest fixture used by adapter tests."""
    base = {
        "apiVersion": "lmbox.eu/v1",
        "kind": "Agent",
        "metadata": {
            "slug": "demo",
            "version": "0.1.0",
            "vendor": "lmbox",
            "vertical": "generic",
            "display_name": "Demo",
            "description": "An agent for testing the adapter pipeline.",
        },
        "spec": {
            "model": {"primary": "mistral-large-2", "temperature": 0.2},
            "prompts": {"system": "prompts/system.md"},
            "tools": [],
            "connectors": {"required": [], "optional": []},
            "evals": {"pass_threshold": 0.8, "golden": "evals/golden.jsonl"},
            "deployment": {"audit": True, "rgpd_redact": []},
        },
    }
    base.update(overrides)
    return base


def _stage_agent(tmp_path: Path, system_prompt: str = "You are a helper.") -> Path:
    """Create the minimum on-disk files an adapter needs."""
    agent = tmp_path / "agent"
    (agent / "prompts").mkdir(parents=True)
    (agent / "prompts" / "system.md").write_text(system_prompt)
    return agent


# ─── Unit: OpenClawAdapter ────────────────────────────────────


def test_adapter_emits_skill_md(tmp_path: Path) -> None:
    agent = _stage_agent(tmp_path)
    out = tmp_path / "build"
    out.mkdir()

    result = OpenClawAdapter().compile(_minimal_manifest(), agent_dir=agent, output_dir=out)

    assert isinstance(result, BuildResult)
    assert result.kernel == "openclaw"
    skill = result.artefact_dir / "SKILL.md"
    assert skill.exists()
    content = skill.read_text()
    assert content.startswith("---\n")
    assert "name: demo" in content
    assert "You are a helper" in content  # system prompt body


def test_adapter_frontmatter_round_trips(tmp_path: Path) -> None:
    """Parse the YAML frontmatter back and check key fields."""
    agent = _stage_agent(tmp_path)
    out = tmp_path / "build"
    out.mkdir()

    OpenClawAdapter().compile(_minimal_manifest(), agent_dir=agent, output_dir=out)

    skill = (out / "demo" / "SKILL.md").read_text()
    fm_text = skill.split("---")[1]
    fm = yaml.safe_load(fm_text)

    assert fm["name"] == "demo"
    assert fm["user-invocable"] is True
    assert fm["metadata"]["lmbox"]["version"] == "0.1.0"
    assert fm["metadata"]["lmbox"]["model"] == "mistral-large-2"
    assert fm["metadata"]["lmbox"]["audit"] is True


def test_adapter_required_connectors_become_config_gates(tmp_path: Path) -> None:
    """Connectors in `spec.connectors.required` surface as openclaw `requires.config`."""
    agent = _stage_agent(tmp_path)
    out = tmp_path / "build"
    out.mkdir()

    m = _minimal_manifest()
    m["spec"]["connectors"]["required"] = ["sharepoint", "outlook"]

    OpenClawAdapter().compile(m, agent_dir=agent, output_dir=out)

    fm = yaml.safe_load((out / "demo" / "SKILL.md").read_text().split("---")[1])
    cfg = fm["metadata"]["openclaw"]["requires"]["config"]
    assert "connectors.sharepoint.enabled" in cfg
    assert "connectors.outlook.enabled" in cfg


def test_adapter_runtime_hints_override_frontmatter(tmp_path: Path) -> None:
    """Partner can override OpenClaw frontmatter via runtime_hints.openclaw."""
    agent = _stage_agent(tmp_path)
    out = tmp_path / "build"
    out.mkdir()

    m = _minimal_manifest()
    m["spec"]["runtime_hints"] = {
        "openclaw": {"user-invocable": False, "disable-model-invocation": True}
    }

    OpenClawAdapter().compile(m, agent_dir=agent, output_dir=out)
    fm = yaml.safe_load((out / "demo" / "SKILL.md").read_text().split("---")[1])
    assert fm["user-invocable"] is False
    assert fm["disable-model-invocation"] is True


def test_adapter_warns_on_unknown_rgpd_redact(tmp_path: Path) -> None:
    agent = _stage_agent(tmp_path)
    out = tmp_path / "build"
    out.mkdir()

    m = _minimal_manifest()
    m["spec"]["deployment"]["rgpd_redact"] = ["email", "fictional-tag"]

    result = OpenClawAdapter().compile(m, agent_dir=agent, output_dir=out)
    assert any("fictional-tag" in w for w in result.warnings)


def test_adapter_copies_prompts_dir(tmp_path: Path) -> None:
    """The mirrored prompts/system.md must land in the artefact dir."""
    agent = _stage_agent(tmp_path, system_prompt="Be brief.")
    out = tmp_path / "build"
    out.mkdir()

    result = OpenClawAdapter().compile(_minimal_manifest(), agent_dir=agent, output_dir=out)

    mirrored = result.artefact_dir / "prompts" / "system.md"
    assert mirrored.exists()
    assert mirrored.read_text() == "Be brief."


def test_adapter_raises_on_missing_prompt(tmp_path: Path) -> None:
    """Manifest points to a prompt that doesn't exist → AdapterError."""
    agent = tmp_path / "agent"
    agent.mkdir()  # NO prompts/ subdir
    out = tmp_path / "build"
    out.mkdir()

    import pytest

    with pytest.raises(AdapterError):
        OpenClawAdapter().compile(_minimal_manifest(), agent_dir=agent, output_dir=out)


def test_adapter_inlines_tools_metadata_in_body(tmp_path: Path) -> None:
    """Tools declared in the manifest must appear as runtime hints in the body."""
    agent = _stage_agent(tmp_path)
    out = tmp_path / "build"
    out.mkdir()

    m = _minimal_manifest()
    m["spec"]["tools"] = [
        {"name": "search_docs", "type": "rag", "description": "Searches docs."},
        {"name": "send_email", "type": "action", "description": "Sends an email."},
    ]

    OpenClawAdapter().compile(m, agent_dir=agent, output_dir=out)

    body = (out / "demo" / "SKILL.md").read_text()
    assert "lmbox:tool name=search_docs type=rag" in body
    assert "lmbox:tool name=send_email type=action" in body


# ─── End-to-end CLI ───────────────────────────────────────────


def _scaffold(tmp_path: Path, template: str = "_base") -> Path:
    res = runner.invoke(app, ["agent", "new", "my-agent", "-t", template, "-o", str(tmp_path)])
    assert res.exit_code == 0
    return tmp_path / "my-agent"


def test_build_command_default_kernel(tmp_path: Path) -> None:
    agent = _scaffold(tmp_path)
    res = runner.invoke(app, ["agent", "build", str(agent)])
    assert res.exit_code == 0, res.stdout
    assert "openclaw" in res.stdout.lower()
    assert (agent / ".build" / "openclaw" / "my-agent" / "SKILL.md").exists()


def test_build_legal_template_includes_tools(tmp_path: Path) -> None:
    """The legal-document template declares 3 tools; they must end up in the body."""
    agent = _scaffold(tmp_path, template="legal-document")
    res = runner.invoke(app, ["agent", "build", str(agent)])
    assert res.exit_code == 0, res.stdout
    body = (agent / ".build" / "openclaw" / "my-agent" / "SKILL.md").read_text()
    assert "search_clause_library" in body
    assert "search_jurisprudence" in body
    assert "send_review_email" in body


def test_build_with_unknown_kernel_exits_2(tmp_path: Path) -> None:
    agent = _scaffold(tmp_path)
    res = runner.invoke(app, ["agent", "build", str(agent), "--kernel", "bogus"])
    assert res.exit_code == 2
    assert "Unknown kernel" in res.stdout


def test_build_custom_output_dir(tmp_path: Path) -> None:
    agent = _scaffold(tmp_path)
    custom_out = tmp_path / "elsewhere"
    res = runner.invoke(app, ["agent", "build", str(agent), "--output-dir", str(custom_out)])
    assert res.exit_code == 0, res.stdout
    assert (custom_out / "my-agent" / "SKILL.md").exists()
    # Default location should NOT exist when --output-dir is overridden
    assert not (agent / ".build").exists()


def test_build_clean_wipes_previous(tmp_path: Path) -> None:
    agent = _scaffold(tmp_path)
    out = agent / ".build" / "openclaw"
    out.mkdir(parents=True)
    stale = out / "stale.txt"
    stale.write_text("leftover from previous build")

    res = runner.invoke(app, ["agent", "build", str(agent)])
    assert res.exit_code == 0, res.stdout
    assert not stale.exists()  # cleaned


def test_build_no_clean_keeps_previous(tmp_path: Path) -> None:
    agent = _scaffold(tmp_path)
    out = agent / ".build" / "openclaw"
    out.mkdir(parents=True)
    keep = out / "keep.txt"
    keep.write_text("should survive")

    res = runner.invoke(app, ["agent", "build", str(agent), "--no-clean"])
    assert res.exit_code == 0, res.stdout
    assert keep.exists()
