---
title: LMbox Agent SDK — Documentation
description: Architecture decisions, schemas, and reference for partners building agents on the LMbox appliance.
---

# LMbox Agent SDK

This site hosts the **stable design documents** for the LMbox Agent
SDK — the kernel-agnostic toolkit partners use to write sovereign AI
agents that run on LMbox appliances.

Source code: <https://github.com/rlacotte/lmbox-cli>
Partner program: <https://lmbox.eu/partenaires>

## Architecture Decision Records

ADRs are the source of truth for design choices. They are stable —
changes here require a new ADR, never an in-place rewrite.

| # | Title | Status |
|---|---|---|
| [001](adr/001-agent-manifest.html) | Agent Manifest format (`lmbox.eu/v1`) | Accepted |

More ADRs land as the SDK matures (kernel abstraction, deployment
pipeline, eval harness…). They will appear in the table above.

## Quick start

```bash
pip install lmbox-cli

lmbox agent new my-first-agent
lmbox agent new contract-review --template legal-document --vendor sopra
cd contract-review
$EDITOR manifest.yaml
$EDITOR prompts/system.md
lmbox agent validate
```

## Schema

The manifest format is published as a JSON Schema:

- [`agent_v1.schema.json`](https://github.com/rlacotte/lmbox-cli/blob/main/lmbox_cli/schema/agent_v1.schema.json)

Validate any LMbox manifest against it with `lmbox agent validate`
or any standard JSON Schema validator.

## What is LMbox?

LMbox is an on-prem AI appliance for European mid-market companies
(ETI) that need generative AI without their data leaving their
infrastructure. The Agent SDK is the toolkit that integrators
(ESN, consultancies) use to build vertical agents on top of the
LMbox runtime.

Read the partner program detail at
[lmbox.eu/partenaires](https://lmbox.eu/partenaires).
