# ADR-001 — LMbox Agent Manifest

| | |
|---|---|
| Status | Accepted |
| Date | 2026-05-12 |
| Decider | LMbox core team |

## Context

LMbox is shipping an Agent SDK so integrator partners (Sopra, Inetum,
Magellan, regional ESN) can build verticalised AI agents on the LMbox
appliance in a few days rather than a few weeks. The SDK must:

1. Run on top of an open-source agent runtime kernel (OpenClaw — see
   technical audit). We do **not** intend to write a kernel from
   scratch.
2. Preserve our ability to swap the kernel later — for another OSS
   runtime (Hermes) or for a proprietary LMbox-native kernel if /
   when the strategic case warrants it.
3. Stay compatible with the AgentSkills external spec OpenClaw
   follows, so a properly-built LMbox agent can run on any
   AgentSkills-compatible runtime with minimal translation.

This ADR fixes the on-disk format of a "LMbox Agent" — the source
artifact a partner writes and `lmbox agent new` scaffolds.

## Decision

A LMbox Agent is a directory with the following layout:

```
my-agent/
├── manifest.yaml            (required — the LMbox Agent Manifest)
├── prompts/
│   └── system.md            (required — the agent's primary prompt)
├── tools/                   (optional — function-call tool definitions)
│   └── *.py
├── evals/
│   └── golden.jsonl         (required — at least one golden test case)
└── README.md                (recommended — human-facing description)
```

The `manifest.yaml` is the single source of truth. Every other file
is referenced by relative path from it.

### Manifest schema (v1)

```yaml
apiVersion: lmbox.eu/v1
kind: Agent

metadata:
  slug: contract-review        # kebab-case, [a-z0-9-], 3..64 chars, unique per vendor
  version: 1.0.0               # semver
  vendor: lmbox                # or partner name, free-form, kebab-case
  vertical: legal              # see Vertical enum below
  display_name: Contract Review
  description: One-paragraph description of what this agent does.

spec:
  model:
    primary: mistral-large-2   # name of the model registered in LiteLLM
    fallback: gemma3-27b       # optional
    temperature: 0.2
    max_tokens: 4096

  prompts:
    system: prompts/system.md  # path, relative to manifest.yaml

  tools:                       # optional, may be empty
    - name: search_internal_docs
      type: rag                # rag | action | http | shell
      source: connectors.sharepoint.contracts
      description: Searches the customer's SharePoint document store.

  connectors:                  # which LMbox connectors are required
    required: [sharepoint]
    optional: [outlook, jira]

  evals:
    pass_threshold: 0.85       # fraction of golden cases that must pass
    golden: evals/golden.jsonl

  deployment:
    owui_role: legal           # which OWUI role group can invoke it
    audit: true                # log every invocation to LMbox AuditLog
    rgpd_redact: []            # list of PII tags to redact before LLM call

  runtime_hints:               # optional, kernel-specific overrides
    openclaw:
      user_invocable: true
      disable_model_invocation: false
```

#### Vertical enum

`generic | legal | finance | health | hr | sales | dev | ops |
compliance | public | industry`

Used by the marketplace and partner programme to filter; also
informs default prompt templates.

### Why a neutral manifest (not SKILL.md directly)?

OpenClaw's native skill format is `SKILL.md` (YAML frontmatter +
Markdown body). It is **not** a stable contract across kernels. If
we coupled directly to it, switching kernel later would mean
rewriting every skill.

The LMbox `manifest.yaml` is a kernel-agnostic format. The CLI's
`build` command compiles it to the kernel's native format at deploy
time:

- `OpenClawAdapter.compile(manifest)` → produces `SKILL.md` + workspace files
- `HermesAdapter.compile(manifest)` → produces Hermes-shaped files
  (added when/if needed)
- `LmboxNativeAdapter.compile(manifest)` → produces our future
  proprietary format (added year 2-3 if at all)

Partners write **once**, the SDK retargets.

### Why YAML and not JSON / TOML?

- YAML supports inline comments, which we want in user-facing
  configuration files (each block has a meaning, partners read this
  by hand).
- The Rails ecosystem (where the cloud control plane lives) already
  uses YAML for sister concerns (i18n, schema seeds).
- The JSON Schema we publish (see `lmbox_cli/schema/agent_v1.schema.json`)
  validates YAML inputs once parsed.

### Versioning

The `apiVersion: lmbox.eu/v1` field is the contract version, separate
from the agent's own `metadata.version`. Breaking changes to the
manifest format will:

1. Introduce `lmbox.eu/v2` alongside `lmbox.eu/v1`.
2. Keep the v1 schema validating for at least 18 months.
3. Ship a `lmbox agent migrate` command that translates v1 to v2.

This is the standard k8s pattern. Our reference is intentional —
ESN ingénieurs already know it.

## Consequences

### Positive

- Partner code is portable across runtimes (OpenClaw today,
  potentially Hermes or a proprietary kernel later) with zero
  changes to their manifest or skill content.
- The manifest documents the agent's intent in human-readable form;
  it doubles as deployment artefact AND documentation.
- The contract is small enough (≈ 30 lines per agent) that partners
  can author by hand if they want.

### Negative

- We add a translation layer (`Adapter.compile()`) that costs ~1-2
  weeks of dev per supported kernel. Acceptable cost for the
  optionality.
- The YAML format will need migration tooling at v2. Plan it now,
  not later.
- We diverge slightly from the AgentSkills spec on file structure
  (we use `manifest.yaml`, not `SKILL.md`). The adapter bridges that
  gap. We accept the divergence because the AgentSkills spec is
  still evolving and not yet 1.0.

### Out of scope (for this ADR)

- The runtime-side execution model (handled by OpenClaw + our
  `lmbox-agent-gateway` translation service — separate ADR-002).
- The `lmbox-skills/*` proprietary helper skills (audit, RGPD,
  connector-bridge — separate ADR-003).
- Deployment pipeline (heartbeat command handler `install_skill` —
  separate ADR-004).
