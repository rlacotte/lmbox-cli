# {{display_name}}

Generated on {{today}} by `lmbox agent new {{slug}}`.

## Quick start

```bash
# 1. Edit the manifest + prompt to your taste
$EDITOR manifest.yaml
$EDITOR prompts/system.md

# 2. Validate the manifest against the LMbox schema
lmbox agent validate

# 3. Run the golden evals locally against your dev box
lmbox agent test

# 4. Deploy to a customer LMbox (when you're ready)
lmbox agent deploy --box BOX-S-XXXXX
```

## File layout

| Path | Purpose |
|------|---------|
| `manifest.yaml` | The single source of truth — model, prompts, tools, evals, deployment. |
| `prompts/system.md` | System prompt sent to the LLM at every turn. |
| `tools/` | Optional Python tool implementations (function calling). |
| `evals/golden.jsonl` | Golden test cases. One JSON object per line. |

## Edit, then `lmbox agent validate`

Most issues partners hit on day 1 are caught by `lmbox agent validate`:
schema typos, missing files, wrong model name. Run it after every
change.
