# Veritas Kanban Task Description Template

Use this template in the real `description` field when creating tasks with `vk create --description`.

## Recommended structure

```text
Objective:
<what must be achieved>

Scope:
- In scope: <what is included>
- Out of scope: <what is explicitly excluded>

Constraints:
- <dependencies, risks, environment limits, deadlines, security concerns>

Expected outputs:
- <code changes>
- <docs>
- <config>
- <reports / validation artifacts>

Acceptance criteria:
- <observable condition 1>
- <observable condition 2>
- <observable condition 3>

Done criteria:
- <what must be true to close the task>
- <what must be verified>
- <what must be documented or reported>
```

## Example CLI usage

```bash
vk create "Stabilize Tailscale Funnel routing for KnowledgeForge auth gateway" \
  --type infra \
  --priority high \
  --project knowledgeforge \
  --description "Objective:\nStabilize the public HTTPS Funnel route so it consistently reaches the auth gateway without 502s.\n\nScope:\n- In scope: Funnel mapping, serve config, proxy target verification, remote validation\n- Out of scope: redesign of the auth gateway itself\n\nConstraints:\n- localhost MCP must remain private\n- Funnel must point to the auth gateway, not raw MCP\n\nExpected outputs:\n- stable Funnel route\n- documented setup/recovery procedure\n- remote verification result\n\nAcceptance criteria:\n- HTTPS endpoint returns auth-gated response, not 502\n- mapping survives re-check/restart\n\nDone criteria:\n- route is verified remotely\n- task comment summarizes final known-good configuration" \
  --json
```

## Guidance
- Put the full task contract in the description field.
- Use comments for progress notes, blockers, and implementation updates.
- Do not rely on comments as the primary task definition when `--description` is available.
