# lmbox-cli

[![Test](https://github.com/rlacotte/lmbox-cli/actions/workflows/test.yml/badge.svg)](https://github.com/rlacotte/lmbox-cli/actions/workflows/test.yml)
[![Pages](https://github.com/rlacotte/lmbox-cli/actions/workflows/pages.yml/badge.svg)](https://rlacotte.github.io/lmbox-cli/)
[![PyPI](https://img.shields.io/pypi/v/lmbox-cli.svg)](https://pypi.org/project/lmbox-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](pyproject.toml)

The partner-facing CLI to scaffold, validate, test, and deploy
**LMbox agents** — sovereign AI agents that run on the LMbox
appliance inside a customer's own infrastructure.

> **Status — Alpha (0.1.x)** · Schema is locked at `lmbox.eu/v1` ;
> the runtime adapter targets OpenClaw ; the deploy pipeline is in
> active development. See [docs/adr/](docs/adr/) for the design
> decisions that are stable.

## Install

```bash
pip install lmbox-cli
# or
pipx install lmbox-cli
```

Then verify :

```bash
$ lmbox --version
lmbox 0.1.0
```

## Quick start (5 minutes)

```bash
# 1. Scaffold a new agent from the base template
lmbox agent new my-first-agent

# 2. Or scaffold from a vertical template (legal-document is shipped)
lmbox agent new contract-review --template legal-document --vendor sopra

# 3. Edit, then validate
cd contract-review
$EDITOR manifest.yaml
$EDITOR prompts/system.md
lmbox agent validate

# 4. Coming next: `lmbox agent test` (local evals) + `lmbox agent deploy`
```

## What is an LMbox agent?

A directory with:

```
my-agent/
├── manifest.yaml         # the contract — see ADR-001
├── prompts/system.md     # the system prompt
├── tools/                # optional Python tool implementations
├── evals/golden.jsonl    # golden test cases
└── README.md             # human-facing description
```

The `manifest.yaml` is **kernel-agnostic** : it documents what the
agent does, what it needs, and how to evaluate it. The CLI compiles
it down to whatever runtime the LMbox appliance ships (OpenClaw
today, potentially other kernels later). Partners write once.

## Why this exists

LMbox appliances ship a runtime that runs locally inside the
customer's network — zero data leaves. Building useful agents on
top of that runtime used to take an integrator **15 days per agent**
(prompt engineering, RAG plumbing, connector wiring, eval harness,
deployment). The Agent SDK collapses that to **2-4 days** per agent
by giving partners:

- A neutral manifest format (this CLI's `manifest.yaml`)
- Templates pre-wired for common verticals (legal, finance, health, ...)
- A local eval harness (`lmbox agent test`)
- A signed-deployment path via the LMbox heartbeat command queue

The full story is in [docs.lmbox.eu/agent-sdk](https://docs.lmbox.eu/agent-sdk).

## Commands

| Command | Status | Purpose |
|---|---|---|
| `lmbox agent new <slug>` | ✅ shipped (0.1) | Scaffold from a template. |
| `lmbox agent validate` | ✅ shipped (0.1) | Schema + cross-reference checks. |
| `lmbox agent test` | ✅ shipped (0.2) | Run golden evals against a local LLM (OpenAI-compatible: Ollama, LiteLLM, vLLM). |
| `lmbox agent build` | ✅ shipped (0.3) | Compile manifest → kernel-native bundle (SKILL.md for OpenClaw). Pluggable adapter — future kernels swap in without touching agent sources. |
| `lmbox agent pack` | ✅ shipped (0.4) | Bundle a built agent into a reproducible signed .lmbox tarball (HMAC-SHA256). Air-gap-friendly. |
| `lmbox agent deploy --box <serial>` | ✅ shipped (0.4) | Full pipeline: build → pack → upload to cloud → queue install via heartbeat. |
| `lmbox agent logs <slug>` | 🚧 0.5 | Tail execution logs from a box. |

## Available templates

| Template | Vertical | What it scaffolds |
|---|---|---|
| `_base` | generic | Minimal agent that passes `validate` immediately. |
| `legal-document` | legal | Contract / NDA / SOW review with SharePoint RAG + Outlook reply. |

More templates land each release. Partners can ship their own
template by dropping it under `lmbox_cli/templates/` and submitting
a PR.

## Develop on the CLI itself

```bash
git clone https://github.com/rlacotte/lmbox-cli
cd lmbox-cli
pip install -e ".[dev]"
pytest
```

## Licence

MIT — see [LICENSE](LICENSE).

## Links

- [docs.lmbox.eu/agent-sdk](https://docs.lmbox.eu/agent-sdk) — full guide
- [lmbox.eu/partenaires](https://lmbox.eu/partenaires) — partner program
- [docs/adr/](docs/adr/) — architecture decision records
