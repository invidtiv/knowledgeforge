# AGENTS.md - Agent Workflow Guide

## Kanban Integration (MANDATORY)

All work MUST be tracked on the Veritas Kanban board using the `vk` CLI.

### CLI Setup
Environment variables in ~/.bashrc:
```bash
export VK_API_URL="https://localhost:3001"
export VERITAS_ADMIN_KEY="vk_prod_7f8a9b2c1d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"
export VK_API_KEY="$VERITAS_ADMIN_KEY"
export NODE_TLS_REJECT_UNAUTHORIZED=0
```

### Before Starting
1. Create a task:
   ```bash
   vk create "Task title" -t code -p PROJECT_NAME
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
- Update task details:
  ```bash
  vk update <task-id> --description "Updated scope"
  ```

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
