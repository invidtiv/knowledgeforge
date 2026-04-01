"""Deterministic queue-backed project ingestion runner for KnowledgeForge."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.core.engine import KnowledgeForgeEngine


def _state_path(config: KnowledgeForgeConfig) -> Path:
    return Path(config.data_dir) / "ingest_queue.json"


def _lock_path(config: KnowledgeForgeConfig) -> Path:
    return Path(config.data_dir) / "ingest_queue.lock"


def load_state(config: KnowledgeForgeConfig) -> dict[str, Any]:
    path = _state_path(config)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    projects = []
    for proj in config.project_paths:
        projects.append(
            {
                "name": proj.get("name", Path(proj["path"]).name),
                "path": proj["path"],
                "status": "pending",
                "attempts": 0,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_error": "",
            }
        )

    state = {
        "created_at": time.time(),
        "updated_at": time.time(),
        "projects": projects,
    }
    save_state(config, state)
    return state


def save_state(config: KnowledgeForgeConfig, state: dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    _state_path(config).write_text(json.dumps(state, indent=2), encoding="utf-8")


def pick_next_project(state: dict[str, Any]) -> dict[str, Any] | None:
    pending = [p for p in state["projects"] if p["status"] in {"pending", "retry"}]
    if not pending:
        return None
    pending.sort(key=lambda p: (p["attempts"], p["last_attempt_at"] or 0))
    return pending[0]


def _acquire_lock(config: KnowledgeForgeConfig, stale_seconds: int = 3600) -> tuple[bool, str]:
    path = _lock_path(config)
    now = time.time()

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(data.get("created_at", 0))
            pid = int(data.get("pid", 0))
            if created_at and (now - created_at) < stale_seconds:
                return False, f"Queue already running (pid={pid})"
        except Exception:
            pass
        # stale/bad lock -> remove
        path.unlink(missing_ok=True)

    path.write_text(json.dumps({"pid": os.getpid(), "created_at": now}), encoding="utf-8")
    return True, "acquired"


def _release_lock(config: KnowledgeForgeConfig) -> None:
    _lock_path(config).unlink(missing_ok=True)


def run_once() -> dict[str, Any]:
    config = KnowledgeForgeConfig.load_config()
    acquired, reason = _acquire_lock(config)
    if not acquired:
        return {"status": "locked", "message": reason}

    try:
        engine = KnowledgeForgeEngine(config)
        state = load_state(config)

        # Recover any stale running entries from interrupted executions
        for p in state["projects"]:
            if p["status"] == "running":
                p["status"] = "retry"
                if not p["last_error"]:
                    p["last_error"] = "Recovered from interrupted previous run"
        save_state(config, state)

        project = pick_next_project(state)
        if not project:
            return {
                "status": "idle",
                "message": "No pending projects remain in the ingestion queue.",
            }

        name = project["name"]
        project["status"] = "running"
        project["attempts"] += 1
        project["last_attempt_at"] = time.time()
        save_state(config, state)

        before = next((p for p in engine.list_projects() if p.name == name), None)

        try:
            result = engine.ingest_registered_project(name, full_reindex=False)
            after = next((p for p in engine.list_projects() if p.name == name), None)

            success = (result.files_processed > 0 or result.chunks_created > 0) and not result.errors
            if success:
                project["status"] = "done"
                project["last_success_at"] = time.time()
                project["last_error"] = ""
            else:
                project["status"] = "retry"
                project["last_error"] = "; ".join(result.errors) if result.errors else "No measurable indexing delta yet"

            save_state(config, state)

            return {
                "status": "ok" if success else "retry",
                "project": name,
                "before": {
                    "chunks": getattr(before, "total_chunks", 0),
                    "files": getattr(before, "file_count", 0),
                    "status": getattr(before, "status", "unknown"),
                },
                "after": {
                    "chunks": getattr(after, "total_chunks", 0),
                    "files": getattr(after, "file_count", 0),
                    "status": getattr(after, "status", "unknown"),
                },
                "ingest_result": {
                    "files_processed": result.files_processed,
                    "files_skipped": result.files_skipped,
                    "chunks_created": result.chunks_created,
                    "errors": result.errors,
                    "duration_seconds": result.duration_seconds,
                },
            }
        except Exception as e:
            project["status"] = "retry"
            project["last_error"] = f"Exception during ingest: {e}"
            save_state(config, state)
            return {
                "status": "retry",
                "project": name,
                "before": {
                    "chunks": getattr(before, "total_chunks", 0),
                    "files": getattr(before, "file_count", 0),
                    "status": getattr(before, "status", "unknown"),
                },
                "after": None,
                "error": str(e),
            }
    finally:
        _release_lock(config)


if __name__ == "__main__":
    import json
    print(json.dumps(run_once(), indent=2))
