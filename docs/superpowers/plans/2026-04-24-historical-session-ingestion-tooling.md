# Historical Session Ingestion Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe tooling for historical AI-session inventory, extraction batching, and Codex SQLite export without importing unreviewed historical memory.

**Architecture:** Add small ingestion-support modules under `src/knowledgeforge/ingestion/` and expose operator commands through the existing Typer CLI. The tools produce JSON artifacts, preserve raw sources as read-only inputs, default historical memory to low-trust metadata, and leave actual card import to the existing `memory import-json` review gate.

**Tech Stack:** Python 3.10+, Typer CLI, standard-library `json`, `sqlite3`, `hashlib`, `pathlib`, and existing KnowledgeForge conversation/memory extraction helpers.

---

## File Structure

- Create `src/knowledgeforge/ingestion/source_inventory.py`
  - Read-only source inventory utilities.
  - Emits counts, size totals, latest modification times, candidate samples, and adapter status.
  - Never reads file contents.
- Create `src/knowledgeforge/ingestion/batch_extraction.py`
  - Selects bounded JSONL batches.
  - Parses supported sessions with `parse_jsonl_file`.
  - Writes prompt files and manifest JSON for operator/LLM extraction.
  - Validates/imports extraction JSON into cards only through existing normalization functions.
- Create `src/knowledgeforge/ingestion/codex_sqlite.py`
  - Inspects SQLite schema read-only.
  - Exports grouped Codex log rows to normalized JSONL after schema inspection.
  - Avoids secret-looking fields and supports dry-run metadata output.
- Modify `src/knowledgeforge/interfaces/cli.py`
  - Add `historical` Typer command group.
  - Add `historical inventory`, `historical batch-prompts`, and `historical codex-sqlite-export`.
- Create tests:
  - `tests/test_source_inventory.py`
  - `tests/test_batch_extraction.py`
  - `tests/test_codex_sqlite.py`

---

### Task 1: Source Inventory JSON

**Files:**
- Create: `src/knowledgeforge/ingestion/source_inventory.py`
- Modify: `src/knowledgeforge/interfaces/cli.py`
- Test: `tests/test_source_inventory.py`

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

from knowledgeforge.ingestion.source_inventory import (
    SourceSpec,
    build_inventory,
    write_inventory,
)


def test_build_inventory_counts_candidate_files_without_reading_contents(tmp_path: Path) -> None:
    root = tmp_path / "claude" / "projects"
    root.mkdir(parents=True)
    (root / "session.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    (root / "notes.txt").write_text("not a candidate", encoding="utf-8")
    (root / "secret-token.json").write_text("must not be sampled", encoding="utf-8")

    inventory = build_inventory(
        [SourceSpec(agent="claude", path=str(root), adapter_status="jsonl-supported")],
        host="test-host",
        sample_limit=10,
    )

    source = inventory["known_sources"][0]
    assert source["exists"] is True
    assert source["total_files"] == 3
    assert source["jsonl_files"] == 1
    assert source["candidate_files"] == 1
    assert source["adapter_status"] == "jsonl-supported"
    assert source["likely_candidate_samples"][0]["path"].endswith("session.jsonl")
    assert "secret-token.json" not in str(source["likely_candidate_samples"])
    assert inventory["raw_files_read"] is False


def test_write_inventory_creates_json_artifact(tmp_path: Path) -> None:
    source = tmp_path / "codex" / "sessions"
    source.mkdir(parents=True)
    (source / "rollout.jsonl").write_text("{}\n", encoding="utf-8")

    output = tmp_path / "inventory.json"
    payload = write_inventory(
        [SourceSpec(agent="codex", path=str(source), adapter_status="jsonl-supported")],
        output,
        host="test-host",
    )

    assert output.exists()
    assert payload["known_sources"][0]["agent"] == "codex"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `powershell -Command "$env:PYTHONPATH='src'; python -m unittest tests.test_source_inventory -v"`

Expected: FAIL with `ModuleNotFoundError: No module named 'knowledgeforge.ingestion.source_inventory'`.

- [ ] **Step 3: Implement the module and CLI command**

Implement `SourceSpec`, `build_inventory()`, and `write_inventory()` with standard-library filesystem traversal. Use a secret-looking path-name deny pattern and sample only metadata: path, length, and last write time.

Add a CLI group:

```python
historical_app = typer.Typer(help="Historical session ingestion tooling")
app.add_typer(historical_app, name="historical")
```

Add command:

```python
@historical_app.command("inventory")
def historical_inventory(
    output: str = typer.Argument(..., help="Output JSON path"),
    host: str = typer.Option("local", "--host", help="Inventory host label"),
    source: list[str] = typer.Option([], "--source", help="agent=path=adapter source spec"),
):
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `powershell -Command "$env:PYTHONPATH='src'; python -m unittest tests.test_source_inventory -v"`

Expected: PASS.

---

### Task 2: Batch Extraction Runner

**Files:**
- Create: `src/knowledgeforge/ingestion/batch_extraction.py`
- Modify: `src/knowledgeforge/interfaces/cli.py`
- Test: `tests/test_batch_extraction.py`

- [ ] **Step 1: Write the failing tests**

```python
import json
from pathlib import Path

from knowledgeforge.ingestion.batch_extraction import (
    build_prompt_batch,
    normalize_extraction_defaults,
)


def test_build_prompt_batch_writes_bounded_prompts_and_manifest(tmp_path: Path) -> None:
    session = tmp_path / "projects" / "ProjectA" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps({
            "type": "user",
            "timestamp": "2026-04-01T00:00:00Z",
            "message": {"content": "remember the constraint"},
        }) + "\n" +
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Use low trust."}]},
        }) + "\n",
        encoding="utf-8",
    )

    manifest = build_prompt_batch(
        [str(session)],
        output_dir=tmp_path / "out",
        limit=1,
        max_chars=2000,
    )

    assert manifest["session_count"] == 1
    assert Path(manifest["sessions"][0]["prompt_path"]).exists()
    prompt_text = Path(manifest["sessions"][0]["prompt_path"]).read_text(encoding="utf-8")
    assert "Past Conversation Knowledge Extraction Prompt" in prompt_text
    assert "current truth" in prompt_text.lower()


def test_normalize_extraction_defaults_forces_historical_low_trust() -> None:
    payload = {
        "conversation_summary": {"title": "Old session", "projects_detected": ["ProjectA"]},
        "memory_cards": [
            {
                "type": "decision",
                "project": "ProjectA",
                "title": "Use X",
                "body": "A past session said to use X.",
                "status": "active_verified",
                "current_truth": True,
                "needs_repo_confirmation": False,
            }
        ],
    }

    normalized = normalize_extraction_defaults(payload)
    card = normalized["memory_cards"][0]
    assert card["status"] == "active_unverified"
    assert card["current_truth"] is False
    assert card["needs_repo_confirmation"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `powershell -Command "$env:PYTHONPATH='src'; python -m unittest tests.test_batch_extraction -v"`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement the module and CLI command**

Implement:

- `build_prompt_batch(session_paths, output_dir, limit=20, max_chars=60000)`
- `normalize_extraction_defaults(payload)`

Write manifest JSON with session path, session id/path stem, exchange count, prompt path, and status `prompt_ready`.

Add CLI command:

```python
@historical_app.command("batch-prompts")
def historical_batch_prompts(source_dir: str, output_dir: str, limit: int = 20, max_chars: int = 60000):
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `powershell -Command "$env:PYTHONPATH='src'; python -m unittest tests.test_batch_extraction -v"`

Expected: PASS.

---

### Task 3: Codex SQLite Schema Inspection and Export

**Files:**
- Create: `src/knowledgeforge/ingestion/codex_sqlite.py`
- Modify: `src/knowledgeforge/interfaces/cli.py`
- Test: `tests/test_codex_sqlite.py`

- [ ] **Step 1: Write the failing tests**

```python
import json
import sqlite3
from pathlib import Path

from knowledgeforge.ingestion.codex_sqlite import inspect_sqlite_schema, export_codex_logs


def test_inspect_sqlite_schema_reads_tables_without_exporting_values(tmp_path: Path) -> None:
    db = tmp_path / "logs.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table logs (thread_id text, created_at text, feedback_log_body text)")
    conn.execute("insert into logs values ('thread-1', '2026-04-01', 'secret body')")
    conn.commit()
    conn.close()

    schema = inspect_sqlite_schema(db)

    assert schema["tables"][0]["name"] == "logs"
    assert schema["tables"][0]["row_count"] == 1
    assert "feedback_log_body" in schema["tables"][0]["columns"]
    assert "secret body" not in json.dumps(schema)


def test_export_codex_logs_groups_rows_by_thread_id(tmp_path: Path) -> None:
    db = tmp_path / "logs.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table logs (thread_id text, created_at text, feedback_log_body text, module_path text)")
    conn.execute("insert into logs values ('thread-1', '2026-04-01T00:00:00Z', 'first', 'codex')")
    conn.execute("insert into logs values ('thread-1', '2026-04-01T00:01:00Z', 'second', 'codex')")
    conn.commit()
    conn.close()

    out_dir = tmp_path / "out"
    manifest = export_codex_logs(db, out_dir, limit_threads=5)

    assert manifest["thread_count"] == 1
    export_path = Path(manifest["threads"][0]["jsonl_path"])
    rows = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["type"] == "user"
    assert rows[0]["source_agent"] == "codex"
    assert rows[0]["thread_id"] == "thread-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `powershell -Command "$env:PYTHONPATH='src'; python -m unittest tests.test_codex_sqlite -v"`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement schema inspection and export**

Use SQLite URI `mode=ro` for real DB paths. Export only selected non-secret columns into normalized JSONL. Group by `thread_id`; if absent, use a deterministic fallback group from row id.

Add CLI command:

```python
@historical_app.command("codex-sqlite-export")
def historical_codex_sqlite_export(db_path: str, output_dir: str, limit_threads: int = 20, schema_only: bool = False):
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `powershell -Command "$env:PYTHONPATH='src'; python -m unittest tests.test_codex_sqlite -v"`

Expected: PASS.

---

### Task 4: Full Verification and Pilot Gate

**Files:**
- No new files unless test failures identify a missing scoped fix.

- [ ] **Step 1: Run focused tests**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_source_inventory tests.test_batch_extraction tests.test_codex_sqlite tests.test_ingest_queue -v
```

Expected: PASS.

- [ ] **Step 2: Run CLI smoke checks**

Run:

```powershell
$env:PYTHONPATH='src'
python -m knowledgeforge.interfaces.cli historical --help
python -m knowledgeforge.interfaces.cli memory audit
```

Expected: `historical` command group appears; memory audit still shows no surprise imported cards from tooling tests.

- [ ] **Step 3: Generate a fresh read-only inventory artifact**

Run:

```powershell
$env:PYTHONPATH='src'
python -m knowledgeforge.interfaces.cli historical inventory data/historical_ingestion/inventory-homepc-cli-2026-04-24.json --host HomePC-Windows --source "claude=C:\Users\tiaz\.claude\projects=jsonl-supported" --source "codex=C:\Users\tiaz\.codex\sessions=jsonl-supported" --source "windsurf-roaming=C:\Users\tiaz\AppData\Roaming\Windsurf=unsupported-needs-vscode-storage-parser" --source "antigravity-roaming=C:\Users\tiaz\AppData\Roaming\Antigravity=unsupported-needs-vscode-storage-parser"
```

Expected: JSON artifact created; raw files remain untouched.

- [ ] **Step 4: Stop before pilot import**

Do not run `memory import-json` or full ingestion. Report that tooling is pilot-ready and ask for explicit pilot authorization.
