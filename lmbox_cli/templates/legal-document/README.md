# {{display_name}}

Legal document review agent — generated on {{today}} from the
`legal-document` template.

## What this agent does

Given an incoming contract / NDA / SOW:

1. Identifies the document type.
2. Compares clauses against the customer's internal templates.
3. Flags deviations and risks.
4. Cites past matters when relevant.
5. Drafts a memo for the responsible lawyer.

## Required connectors

| Connector | Required | Purpose |
|-----------|----------|---------|
| `sharepoint` | **yes** | Internal clause library + jurisprudence. |
| `outlook` | optional | Sends the final memo. |
| `jira` | optional | Links the memo to a deal ticket. |

The agent fails fast at deployment if a required connector is not
active on the target box.

## Next steps

1. Edit `prompts/system.md` to encode your firm's house style.
2. Replace the 3 sample golden cases in `evals/golden.jsonl` with
   real anonymised matters from your portfolio.
3. `lmbox agent validate` to check the manifest.
4. `lmbox agent test` to run evals against a dev box.
5. `lmbox agent deploy --box BOX-XXXXX` when ready.

## Customising the prompt

The system prompt in `prompts/system.md` is intentionally generic.
For a real client deployment, you'll want to:

- Inject the firm's preferred clause vocabulary
- Set the language explicitly if the client's documents are mono-lingual
- Add escalation rules (e.g. "always escalate to the partner if the
  counterparty is on the sanctions list")
