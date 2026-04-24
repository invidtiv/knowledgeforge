from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from knowledgeforge import ingest_queue
from knowledgeforge.config import KnowledgeForgeConfig


def _make_config(tmp_path: Path, *project_names: str) -> KnowledgeForgeConfig:
    project_paths = []
    for name in project_names:
        project_path = tmp_path / name
        project_path.mkdir(parents=True, exist_ok=True)
        project_paths.append({"path": str(project_path), "name": name})

    return KnowledgeForgeConfig(
        data_dir=str(tmp_path / "data"),
        project_paths=project_paths,
    )


class IngestQueueTests(unittest.TestCase):
    def test_load_state_backfills_progress_fields_and_new_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = _make_config(tmp_path, "alpha")
            state = ingest_queue.load_state(config)
            project = state["projects"][0]

            self.assertEqual(project["phase"], "markdown")
            self.assertEqual(project["markdown_index"], 0)
            self.assertEqual(project["code_index"], 0)

            raw_state = {
                "created_at": time.time(),
                "updated_at": time.time(),
                "projects": [
                    {
                        "name": "alpha",
                        "path": str(tmp_path / "alpha"),
                        "status": "running",
                        "attempts": 4,
                        "last_attempt_at": 123.0,
                        "last_success_at": None,
                        "last_error": "old",
                    }
                ],
            }
            ingest_queue._state_path(config).write_text(json.dumps(raw_state), encoding="utf-8")

            expanded_config = _make_config(tmp_path, "alpha", "beta")
            merged_state = ingest_queue.load_state(expanded_config)
            projects = {project["name"]: project for project in merged_state["projects"]}

            self.assertEqual(projects["alpha"]["attempts"], 4)
            self.assertEqual(projects["alpha"]["phase"], "markdown")
            self.assertEqual(projects["beta"]["status"], "pending")
            self.assertEqual(projects["beta"]["markdown_index"], 0)

    def test_pick_next_project_prefers_running_resume(self) -> None:
        state = {
            "projects": [
                {"name": "pending", "status": "pending", "attempts": 0, "last_attempt_at": None},
                {"name": "retry", "status": "retry", "attempts": 2, "last_attempt_at": 50},
                {"name": "running", "status": "running", "attempts": 1, "last_attempt_at": 100},
            ]
        }

        project = ingest_queue.pick_next_project(state)
        self.assertIsNotNone(project)
        self.assertEqual(project["name"], "running")

    def test_acquire_lock_reclaims_dead_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = _make_config(Path(tmp_dir), "alpha")
            lock_path = ingest_queue._lock_path(config)
            lock_path.write_text(
                json.dumps({"pid": 424242, "created_at": time.time()}),
                encoding="utf-8",
            )

            with mock.patch.object(
                ingest_queue.os,
                "kill",
                side_effect=ProcessLookupError(424242),
            ):
                acquired, reason = ingest_queue._acquire_lock(config)

            self.assertTrue(acquired)
            self.assertEqual(reason, "reclaimed-dead-lock")
            ingest_queue._release_lock(config)

    def test_run_once_resumes_running_project_without_incrementing_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = _make_config(tmp_path, "alpha")
            ingest_queue.save_state(
                config,
                {
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "projects": [
                        {
                            "name": "alpha",
                            "path": str(tmp_path / "alpha"),
                            "status": "running",
                            "attempts": 3,
                            "last_attempt_at": 10.0,
                            "last_success_at": None,
                            "last_error": "",
                            "phase": "markdown",
                            "markdown_index": 1,
                            "code_index": 0,
                            "markdown_total": 5,
                            "code_total": 7,
                        }
                    ],
                },
            )

            class DummyEngine:
                def __init__(self, config: KnowledgeForgeConfig):
                    self.config = config

                def list_projects(self) -> list[object]:
                    return []

                def ingest_project_batch(self, *args, **kwargs) -> dict[str, object]:
                    return {
                        "status": "partial",
                        "phase": "code",
                        "markdown_index": 5,
                        "code_index": 2,
                        "markdown_total": 5,
                        "code_total": 7,
                        "files_processed": 2,
                        "files_skipped": 0,
                        "chunks_created": 24,
                        "errors": [],
                        "duration_seconds": 1.2,
                    }

            with mock.patch.object(ingest_queue, "KnowledgeForgeEngine", DummyEngine):
                with mock.patch.object(
                    ingest_queue.KnowledgeForgeConfig,
                    "load_config",
                    classmethod(lambda cls, config_path=None: config),
                ):
                    result = ingest_queue.run_once()

            state = ingest_queue.load_state(config)
            project = state["projects"][0]

            self.assertEqual(result["status"], "partial")
            self.assertEqual(project["status"], "running")
            self.assertEqual(project["attempts"], 3)
            self.assertEqual(project["phase"], "code")
            self.assertEqual(project["markdown_index"], 5)
            self.assertEqual(project["code_index"], 2)


if __name__ == "__main__":
    unittest.main()
