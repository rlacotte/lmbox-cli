"""OpenClawAdapter — compile a LMbox manifest into an OpenClaw SKILL.md bundle.

OpenClaw skills live in `~/.openclaw/workspace/skills/<slug>/SKILL.md`.
The format is YAML frontmatter + Markdown body (instructions for the
LLM at invocation time). We produce:

    output_dir/<slug>/
      SKILL.md          # frontmatter + body
      prompts/
        system.md       # copy of the agent's system prompt
      tools/            # if any
        ...

The body of SKILL.md is the agent's system prompt verbatim, plus
a short footer that documents the LMbox extensions (audit, RGPD
redact) for the kernel to honour at runtime via the LMbox helper
skills (`lmbox-audit-log`, `lmbox-rgpd-redact`, etc.) — see ADR-003
when it lands.

Mapping reference
─────────────────
LMbox manifest                       │ OpenClaw SKILL.md frontmatter
─────────────────────────────────────│──────────────────────────────────
metadata.slug                        │ name
metadata.description                 │ description
metadata.version                     │ metadata.lmbox.version
metadata.vendor                      │ metadata.lmbox.vendor
metadata.vertical                    │ metadata.lmbox.vertical
spec.model.primary                   │ metadata.lmbox.model
spec.connectors.required             │ metadata.openclaw.requires.config
spec.deployment.owui_role            │ metadata.lmbox.owui_role
spec.deployment.audit                │ metadata.lmbox.audit
spec.deployment.rgpd_redact          │ metadata.lmbox.rgpd_redact
spec.runtime_hints.openclaw.*        │ merged into frontmatter (overrides)
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from lmbox_cli._adapters.base import AdapterError, BuildResult

# OpenClaw v2026415+ exposes the SKILL.md schema we target here.
# When OpenClaw bumps to v2026512+ with the new agent-loop frontmatter,
# we'll branch on the `kernel_min_version` we declare.
OPENCLAW_MIN_VERSION = "v2026.4.15"


class OpenClawAdapter:
    """Adapter for the OpenClaw kernel.

    Stateless — instantiate with no args, call `compile()`.
    """

    name = "openclaw"

    def compile(
        self,
        manifest: dict[str, Any],
        *,
        agent_dir: Path,
        output_dir: Path,
    ) -> BuildResult:
        slug = manifest["metadata"]["slug"]
        target = output_dir / slug
        target.mkdir(parents=True, exist_ok=True)

        warnings: list[str] = []
        produced: list[Path] = []

        # ─── Build the SKILL.md frontmatter ──────────────────
        frontmatter = self._build_frontmatter(manifest, warnings)

        # ─── Compose the SKILL.md body ───────────────────────
        body = self._build_body(manifest, agent_dir)

        skill_md = target / "SKILL.md"
        skill_md.write_text(self._render_markdown(frontmatter, body), encoding="utf-8")
        produced.append(skill_md)

        # ─── Mirror prompts/system.md for kernels that load it ──
        prompt_src = agent_dir / manifest["spec"]["prompts"]["system"]
        if prompt_src.exists():
            (target / "prompts").mkdir(exist_ok=True)
            prompt_dst = target / "prompts" / "system.md"
            shutil.copyfile(prompt_src, prompt_dst)
            produced.append(prompt_dst)

        # ─── Copy tools/ if present ──────────────────────────
        tools_src = agent_dir / "tools"
        if tools_src.exists() and any(tools_src.iterdir()):
            tools_dst = target / "tools"
            tools_dst.mkdir(exist_ok=True)
            for entry in tools_src.rglob("*"):
                if entry.is_file() and entry.name != ".gitkeep":
                    rel = entry.relative_to(tools_src)
                    dst = tools_dst / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(entry, dst)
                    produced.append(dst)

        return BuildResult(
            kernel=self.name,
            kernel_min_version=OPENCLAW_MIN_VERSION,
            artefact_dir=target,
            files=produced,
            warnings=warnings,
        )

    # ─── Helpers ──────────────────────────────────────────────

    def _build_frontmatter(self, manifest: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        """Map LMbox fields to OpenClaw frontmatter keys."""
        md = manifest["metadata"]
        sp = manifest["spec"]
        depl = sp.get("deployment") or {}
        connectors = sp.get("connectors") or {}

        # Default OpenClaw frontmatter
        fm: dict[str, Any] = {
            "name": md["slug"],
            # display-name surfaces the partner-facing label in OWUI
            # dropdowns and the LMbox portal. Top-level (not under
            # metadata.lmbox) so unrelated tooling (openclaw control
            # ui, partner CLI listing) finds it without a deep walk.
            "display-name": md.get("display_name", md["slug"]),
            "description": md["description"],
            "user-invocable": True,
            "disable-model-invocation": False,
            "metadata": {
                "lmbox": {
                    # display_name is the partner-facing label shown in
                    # OWUI / portal dropdowns. The OWUI persona
                    # registration script reads this; without it,
                    # users see the slug instead of "NDA Reviewer".
                    "display_name": md.get("display_name", md["slug"]),
                    "version": md["version"],
                    "vendor": md["vendor"],
                    "vertical": md.get("vertical", "generic"),
                    "model": sp["model"]["primary"],
                    "audit": bool(depl.get("audit", True)),
                    "rgpd_redact": list(depl.get("rgpd_redact", [])),
                },
                "openclaw": {
                    "requires": {
                        # We surface required LMbox connectors as a
                        # `config:` key so the OpenClaw skill-loader
                        # gates this skill on those connectors being
                        # active on the box.
                        "config": [
                            f"connectors.{c}.enabled" for c in connectors.get("required", [])
                        ],
                    },
                },
            },
        }

        # Add owui_role if declared
        if "owui_role" in depl:
            fm["metadata"]["lmbox"]["owui_role"] = depl["owui_role"]

        # Drop empty `requires.config` to keep the frontmatter clean
        if not fm["metadata"]["openclaw"]["requires"]["config"]:
            del fm["metadata"]["openclaw"]["requires"]
            if not fm["metadata"]["openclaw"]:
                del fm["metadata"]["openclaw"]

        # ─── Apply runtime_hints.openclaw overrides ──────────
        # Partners can override any frontmatter key via
        # `spec.runtime_hints.openclaw.*`. We deep-merge so they
        # can target nested keys without rewriting the whole tree.
        hints = sp.get("runtime_hints", {}).get("openclaw", {})
        if isinstance(hints, dict):
            self._deep_merge(fm, hints)

        # ─── Sanity check: rgpd_redact tools we don't know ──
        unknown_redact = set(fm["metadata"]["lmbox"]["rgpd_redact"]) - {
            "email",
            "phone",
            "iban",
            "nir",
            "siren",
            "address",
            "fullname",
            "all",
        }
        if unknown_redact:
            warnings.append(
                f"Unknown rgpd_redact tags ignored by runtime: {sorted(unknown_redact)}"
            )

        return fm

    def _build_body(self, manifest: dict[str, Any], agent_dir: Path) -> str:
        """Compose the Markdown body — system prompt + LMbox footer."""
        sp = manifest["spec"]
        prompt_path = agent_dir / sp["prompts"]["system"]
        if not prompt_path.exists():
            raise AdapterError(f"System prompt not found: {prompt_path}")

        system_prompt = prompt_path.read_text(encoding="utf-8").rstrip()

        # Footer that documents LMbox-specific behaviour at runtime.
        # The LMbox helper skills installed on the box (lmbox-audit-log,
        # lmbox-rgpd-redact, lmbox-connector-bridge) read these hints
        # to inject the right behaviour. The LLM ignores it harmlessly.
        depl = sp.get("deployment") or {}
        footer_parts = ["", "<!-- LMbox runtime hints — do not edit by hand -->"]
        if depl.get("audit", True):
            footer_parts.append("<!-- lmbox:audit=true — every invocation logged to AuditLog -->")
        if depl.get("rgpd_redact"):
            tags = ", ".join(depl["rgpd_redact"])
            footer_parts.append(
                f"<!-- lmbox:rgpd_redact={tags} — stripped before any cloud LLM call -->"
            )
        for tool in sp.get("tools", []):
            footer_parts.append(f"<!-- lmbox:tool name={tool['name']} type={tool['type']} -->")

        return system_prompt + "\n" + "\n".join(footer_parts) + "\n"

    @staticmethod
    def _render_markdown(frontmatter: dict[str, Any], body: str) -> str:
        """Serialise frontmatter as YAML + concatenate body.

        We use `default_flow_style=False` and `sort_keys=False` so
        the output is predictable and reviewable.
        """
        yaml_block = yaml.safe_dump(
            frontmatter,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ).rstrip()
        return f"---\n{yaml_block}\n---\n\n{body}"

    @staticmethod
    def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
        """In-place recursive merge. `src` wins on conflicts."""
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                OpenClawAdapter._deep_merge(dst[k], v)
            else:
                dst[k] = v
