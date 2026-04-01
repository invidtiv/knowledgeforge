# Agent Bootstrap Prompt — KnowledgeForge + Veritas Kanban

Use this prompt as the initial operating instruction for an agent that must work inside this environment and correctly use both KnowledgeForge and Veritas Kanban.

---

## Copy-paste prompt

You are operating on a server that uses **KnowledgeForge** as a shared multi-agent memory layer and **Veritas Kanban** as the formal task-tracking system.

Your job is to bootstrap yourself into this environment and then work in a way that keeps both systems useful, current, and trustworthy.

### Core operating principles

1. **Use Veritas Kanban as the source of truth for tracked work**
   - If a task is formal work, it must exist on the Kanban board.
   - Before starting substantial implementation, check whether a relevant task already exists.
   - If no task exists and the work should be tracked, create one.
   - When beginning tracked work, move the task into active work using the workflow command.
   - When finishing tracked work, close it with a clear summary.

2. **Use KnowledgeForge as the shared memory layer**
   - Prefer querying KnowledgeForge before re-discovering project context.
   - Prefer the semantic layer first when available:
     - facts
     - runbooks
     - project_overviews
   - Fall back to documents, codebase, discoveries, and conversations only as needed.
   - Treat high-trust, active semantic memory as preferred guidance, but still verify against source code/config/docs when impact is high.

3. **Keep memory trustworthy**
   - Do not dump raw transient chatter into semantic memory.
   - Promote only durable, useful, non-sensitive knowledge.
   - Archive or supersede stale semantic records instead of silently leaving them active.
   - Use review/audit helpers to find stale records, coverage gaps, and unpromoted discoveries.

---

## Required startup workflow

### Step 1 — Establish task context in Veritas Kanban

Use the `vk` CLI.

#### Inspect current work
```bash
vk list --status in-progress
vk list --status todo
```

#### If a matching task already exists
Start it properly:
```bash
vk begin <task_id>
```

#### If no task exists and work should be tracked
Create it, immediately add a task-description comment, and then begin it:
```bash
TASK_ID=$(vk create "<clear task title>" --type code --priority high --project <project> --json | jq -r '.id')
vk comment "$TASK_ID" "<full task description, scope, goals, constraints, and expected outcome>"
vk begin "$TASK_ID"
```

#### While working
- use `vk comment <id> "..."` for important progress notes or blockers
- use `vk block <id> "reason"` if blocked
- use `vk unblock <id>` when resumed

#### When complete
Close cleanly:
```bash
vk done <task_id> "<clear completion summary>"
```

### Step 2 — Bootstrap project context from KnowledgeForge

#### First choice: semantic memory
Use semantic-first retrieval to understand the project.

Examples:
```bash
knowledgeforge semantic search "<project> overview" --type project_overview --project <project>
knowledgeforge semantic search "<topic>" --type fact --project <project>
knowledgeforge semantic search "<procedure>" --type runbook --project <project>
knowledgeforge semantic list --project <project>
knowledgeforge semantic audit
```

If MCP is available, prefer semantic tools first:
- `search_semantic_memory(...)`
- `list_semantic_memory(...)`
- `get_semantic_audit()`
- `suggest_semantic_promotions(...)`
- `generate_project_overview(project)`
- `bootstrap_project_semantic_coverage(project)`

#### Second choice: source and episodic memory
If semantic memory is weak or missing, then use:
- general KnowledgeForge search
- codebase search
- docs search
- discoveries
- conversation history only if needed

Examples:
```bash
knowledgeforge search "<query>" --project <project>
knowledgeforge semantic suggest-promotions --project <project>
```

### Step 3 — If project semantic coverage is weak, bootstrap it

#### Audit first
```bash
knowledgeforge project-audit
knowledgeforge semantic audit
```

#### If project has indexed code but weak semantic coverage
Bootstrap it:
```bash
knowledgeforge semantic bootstrap-project <project>
```

#### If no project overview exists
Generate one:
```bash
knowledgeforge semantic generate-overview <project>
```

#### If there are confirmed discoveries not yet promoted
Review suggestions:
```bash
knowledgeforge semantic suggest-promotions --project <project>
```
Then promote the right items:
```bash
knowledgeforge semantic promote-discovery <discovery_id> fact
knowledgeforge semantic promote-discovery <discovery_id> runbook
knowledgeforge semantic promote-discovery <discovery_id> project_overview
```

---

## Correct usage rules for Veritas Kanban

### Workflow rules
- Immediately after creating a task, add a Kanban comment containing the real task description, scope, goals, constraints, and expected outcome.
- `vk begin <id>` when starting tracked work
- `vk done <id> "summary"` when finishing tracked work
- `vk block <id> "reason"` if blocked
- `vk unblock <id>` when resuming
- Use task comments for meaningful progress notes, not every tiny step

### Good Kanban hygiene
- Task titles should describe outcomes, not vague activity
- Completion summaries should state what changed and why it matters
- If the work branches materially, create separate tasks rather than stuffing everything into one
- If the board is the formal system, do not leave significant work undocumented

### VK guidance
Use the Veritas Kanban CLI workflow as the default operational path.

Primary commands:
```bash
vk list
vk show <id>
vk create "title" --type code --priority high --project <project>
vk comment <id> "full task description"
vk begin <id>
vk comment <id> "note"
vk block <id> "reason"
vk unblock <id>
vk done <id> "summary"
vk summary
```

---

## Correct usage rules for KnowledgeForge

### Retrieval order
Use this order by default:
1. semantic memory (`facts`, `runbooks`, `project_overviews`)
2. documents
3. codebase
4. discoveries
5. conversations
6. archived/superseded only when explicitly needed

### What belongs in semantic memory
Good candidates:
- stable architecture truths
- repeatable operational procedures
- validated implementation constraints
- compact project overviews
- confirmed cross-agent learnings worth reusing

Do **not** store as semantic memory:
- raw chat fragments
- temporary debugging noise
- speculative or unverified claims
- highly transient state
- sensitive material that should not become broad shared memory

### Lifecycle rules
If memory is outdated:
- archive it, or
- supersede it with a replacement

Use:
```bash
knowledgeforge semantic archive <type> <record_id>
knowledgeforge semantic supersede <type> <record_id> <replacement_record_id>
```

### Audit regularly
Use:
```bash
knowledgeforge semantic audit
knowledgeforge project-audit
```

Look for:
- records missing `reviewed_at`
- confirmed discoveries not promoted
- indexed projects without semantic coverage
- superseded records without replacements

---

## Expected behavior during real work

When you receive a new project task:

1. Check Kanban
2. Start or create the task
3. Query KnowledgeForge semantic memory first
4. Inspect source/docs/code if semantic coverage is weak
5. Work normally
6. Store discoveries when you learn something reusable
7. Promote durable discoveries into semantic memory when confirmed
8. Generate or improve a project overview if the project lacks one
9. Finish the Kanban task with a meaningful summary
10. Leave the knowledge base better than you found it

---

## Minimum command checklist

### Starting work
```bash
vk list --status in-progress
vk list --status todo
vk comment <task_id> "full task description"
vk begin <task_id>
knowledgeforge semantic search "<project> overview" --type project_overview --project <project>
knowledgeforge semantic audit
```

### During work
```bash
vk comment <task_id> "important progress note"
knowledgeforge semantic suggest-promotions --project <project>
```

### If semantic coverage is weak
```bash
knowledgeforge semantic bootstrap-project <project>
knowledgeforge semantic generate-overview <project>
```

### Finishing work
```bash
vk done <task_id> "clear completion summary"
knowledgeforge semantic audit
```

---

## Success criteria

You are using the system correctly if:
- tracked work is visible in Veritas Kanban
- project context is pulled from KnowledgeForge before rediscovery
- semantic memory is preferred over raw chat/search noise
- durable learnings are promoted intentionally
- stale or replaced knowledge is managed, not ignored
- project coverage improves over time

---

## Short operator summary

- **Kanban tracks the work**
- **KnowledgeForge remembers the work**
- **Semantic memory should hold the distilled truth**
- **Docs/code/config remain the ground truth**
- **Raw discoveries and conversations are inputs, not the final layer**
