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


def _project_defaults(name: str, path: str) -> dict[str, Any]:
    return {
        "name": name,
        "path": path,
        "status": "pending",
        "attempts": 0,
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error": "",
        "phase": "markdown",
        "markdown_index": 0,
        "code_index": 0,
        "markdown_total": 0,
        "code_total": 0,
    }


def load_state(config: KnowledgeForgeConfig) -> dict[str, Any]:
    path = _state_path(config)
    raw_state: dict[str, Any] = {}
    if path.exists():
        raw_state = json.loads(path.read_text(encoding="utf-8"))

    existing_projects = {
        str(project.get("name")): project
        for project in raw_state.get("projects", [])
        if project.get("name")
    }

    projects: list[dict[str, Any]] = []
    for proj in config.project_paths:
        name = proj.get("name", Path(proj["path"]).name)
        merged = _project_defaults(name, proj["path"])
        merged.update(existing_projects.get(name, {}))
        merged["name"] = name
        merged["path"] = proj["path"]
        projects.append(merged)

    state = {
        "created_at": raw_state.get("created_at", time.time()),
        "updated_at": raw_state.get("updated_at", time.time()),
        "projects": projects,
    }
    if not path.exists() or raw_state.get("projects") != projects:
        save_state(config, state)
    return state


def save_state(config: KnowledgeForgeConfig, state: dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    _state_path(config).write_text(json.dumps(state, indent=2), encoding="utf-8")


def pick_next_project(state: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [p for p in state["projects"] if p["status"] in {"running", "pending", "retry"}]
    if not candidates:
        return None
    priority = {"running": 0, "retry": 1, "pending": 2}
    candidates.sort(key=lambda p: (priority.get(p["status"], 99), p["attempts"], p["last_attempt_at"] or 0))
    return candidates[0]


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_lock(config: KnowledgeForgeConfig, stale_seconds: int = 3600) -> tuple[bool, str]:
    path = _lock_path(config)
    now = time.time()

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(data.get("created_at", 0))
            pid = int(data.get("pid", 0))
            if pid and _pid_exists(pid):
                return False, f"Queue already running (pid={pid})"
            if created_at and (now - created_at) < stale_seconds:
                path.unlink(missing_ok=True)
                path.write_text(json.dumps({"pid": os.getpid(), "created_at": now}), encoding="utf-8")
                return True, "reclaimed-dead-lock"
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

        project = pick_next_project(state)
        if not project:
            return {
                "status": "idle",
                "message": "No pending projects remain in the ingestion queue.",
            }

        name = project["name"]
        if project["status"] != "running":
            project["status"] = "running"
            project["attempts"] += 1
        project["last_attempt_at"] = time.time()
        save_state(config, state)

        before = next((p for p in engine.list_projects() if p.name == name), None)

        try:
            def persist_progress(snapshot: dict[str, Any]) -> None:
                project["phase"] = snapshot["phase"]
                project["markdown_index"] = snapshot["markdown_index"]
                project["code_index"] = snapshot["code_index"]
                project["markdown_total"] = snapshot["markdown_total"]
                project["code_total"] = snapshot["code_total"]
                project["last_error"] = "; ".join(snapshot["errors"]) if snapshot["errors"] else ""
                project["status"] = "running"
                save_state(config, state)

            result = engine.ingest_project_batch(
                project["path"],
                name,
                state=project,
                full_reindex=False,
                max_files=config.queue_max_files_per_run,
                max_chunks=config.queue_max_chunks_per_run,
                time_budget_seconds=config.queue_time_budget_seconds,
                progress_callback=persist_progress,
            )
            after = next((p for p in engine.list_projects() if p.name == name), None)

            project["phase"] = result["phase"]
            project["markdown_index"] = result["markdown_index"]
            project["code_index"] = result["code_index"]
            project["markdown_total"] = result["markdown_total"]
            project["code_total"] = result["code_total"]

            if result["status"] == "done":
                project["status"] = "done"
                project["last_success_at"] = time.time()
            else:
                project["status"] = "running"

            project["last_error"] = "; ".join(result["errors"]) if result["errors"] else ""

            if result["status"] == "done" and not result["errors"]:
                project["last_error"] = ""

            save_state(config, state)

            status = "ok" if result["status"] == "done" else "partial"
            if result["errors"] and result["status"] == "done":
                status = "ok_with_errors"

            return {
                "status": status,
                "project": name,
                "queue_progress": {
                    "phase": project["phase"],
                    "markdown_index": project["markdown_index"],
                    "code_index": project["code_index"],
                    "markdown_total": project["markdown_total"],
                    "code_total": project["code_total"],
                },
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
                    "files_processed": result["files_processed"],
                    "files_skipped": result["files_skipped"],
                    "chunks_created": result["chunks_created"],
                    "errors": result["errors"],
                    "duration_seconds": result["duration_seconds"],
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
