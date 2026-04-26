import json
import unittest
from pathlib import Path

from typer.testing import CliRunner

from knowledgeforge.ingestion.batch_extraction import (
    build_prompt_batch,
    normalize_extraction_defaults,
)
from knowledgeforge.interfaces.cli import app


class BatchExtractionTests(unittest.TestCase):
    def test_build_prompt_batch_writes_bounded_prompts_and_manifest(self) -> None:
        session = Path(self._tmpdir.name) / "projects" / "ProjectA" / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-04-01T00:00:00Z",
                    "message": {"content": "remember the constraint"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Use low trust."}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        manifest = build_prompt_batch(
            [str(session)],
            output_dir=Path(self._tmpdir.name) / "out",
            limit=1,
            max_chars=2000,
        )

        self.assertEqual(manifest["session_count"], 1)
        self.assertEqual(manifest["sessions"][0]["session_id"], "session")
        self.assertEqual(manifest["sessions"][0]["exchange_count"], 1)
        self.assertEqual(manifest["sessions"][0]["status"], "prompt_ready")
        self.assertTrue((Path(self._tmpdir.name) / "out" / "manifest.json").exists())
        self.assertTrue(Path(manifest["sessions"][0]["prompt_path"]).exists())
        prompt_text = Path(manifest["sessions"][0]["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn("Past Conversation Knowledge Extraction Prompt", prompt_text)
        self.assertIn("current truth", prompt_text.lower())

    def test_build_prompt_batch_respects_limit(self) -> None:
        sessions = []
        for index in range(2):
            session = Path(self._tmpdir.name) / f"session-{index}.jsonl"
            session.write_text(
                json.dumps({"type": "user", "message": {"content": f"user {index}"}})
                + "\n"
                + json.dumps({"type": "assistant", "message": {"content": f"assistant {index}"}})
                + "\n",
                encoding="utf-8",
            )
            sessions.append(str(session))

        manifest = build_prompt_batch(
            sessions,
            output_dir=Path(self._tmpdir.name) / "out-limit",
            limit=1,
            max_chars=2000,
        )

        self.assertEqual(manifest["session_count"], 1)
        self.assertEqual(len(manifest["sessions"]), 1)
        self.assertEqual(manifest["sessions"][0]["session_id"], "session-0")

    def test_normalize_extraction_defaults_forces_historical_low_trust(self) -> None:
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
        self.assertEqual(card["status"], "active_unverified")
        self.assertIs(card["current_truth"], False)
        self.assertIs(card["needs_repo_confirmation"], True)

    def test_normalize_extraction_defaults_preserves_allowed_status(self) -> None:
        payload = {
            "memory_cards": [
                {
                    "title": "Old bug",
                    "status": "resolved",
                    "current_truth": True,
                    "needs_repo_confirmation": False,
                }
            ],
        }

        normalized = normalize_extraction_defaults(payload)
        card = normalized["memory_cards"][0]
        self.assertEqual(card["status"], "resolved")
        self.assertIs(card["current_truth"], False)
        self.assertIs(card["needs_repo_confirmation"], True)
        self.assertEqual(card["title"], "Old bug")

    def test_historical_batch_prompts_cli_scans_supported_sessions(self) -> None:
        source = Path(self._tmpdir.name) / "source"
        source.mkdir()
        self._write_session(source / "keep.jsonl", "keep")
        self._write_session(source / "subagents" / "skip.jsonl", "skip")
        self._write_session(source / "agent-skip.jsonl", "skip-agent")

        result = CliRunner().invoke(
            app,
            [
                "historical",
                "batch-prompts",
                str(source),
                str(Path(self._tmpdir.name) / "out-cli"),
                "--limit",
                "20",
                "--max-chars",
                "2000",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["session_count"], 1)
        self.assertTrue(payload["sessions"][0]["source_path"].endswith("keep.jsonl"))

    def _write_session(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"type": "user", "message": {"content": text}})
            + "\n"
            + json.dumps({"type": "assistant", "message": {"content": text}})
            + "\n",
            encoding="utf-8",
        )

    def setUp(self) -> None:
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
