# AGENTS.md - Agent Workflow Guide

## Kanban Integration (MANDATORY)

All work MUST be tracked on the Veritas Kanban board using the `vk` CLI.

### CLI Setup
Environment variables in ~/.bashrc:
```bash
export VK_API_URL="http://localhost:3001"
export VERITAS_ADMIN_KEY="vk_prod_7f8a9b2c1d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"
export VK_API_KEY="$VERITAS_ADMIN_KEY"
export NODE_TLS_REJECT_UNAUTHORIZED=0
```

### Before Starting
1. Create a task with a structured description:
   ```bash
   vk create "Task title" -t code -p PROJECT_NAME \
     --description "Objective:\n<what must be achieved>\n\nScope:\n- In scope: ...\n- Out of scope: ...\n\nConstraints:\n- ...\n\nExpected outputs:\n- ...\n\nAcceptance criteria:\n- ...\n\nDone criteria:\n- ..."
   ```

2. Begin work (moves to in-progress, starts timer):
   ```bash
   vk begin <task-id>
   ```

### During Work
- Add progress comments:
  ```bash
  vk comment <task-id> "Progress update..."
  ```
- Use comments for updates, blockers, and implementation notes.
- Do not rely on `vk update` for the main task description field; put the structured task contract into `vk create --description` at creation time.

### After Commit/Push
1. Complete task (stops timer, sets done, adds summary):
   ```bash
   vk done <task-id> "Summary of what was done"
   ```

### Useful Commands
- `vk list` - List all tasks
- `vk list -s in-progress` - List by status
- `vk show <id>` - Show task details
- `vk archive <id>` - Archive completed task


## Project: knowledgeforge
