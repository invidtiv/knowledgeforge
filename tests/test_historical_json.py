import json
import base64
import sqlite3
import os
import unittest
from pathlib import Path

from typer.testing import CliRunner

from knowledgeforge.ingestion.historical_json import (
    HistoricalSource,
    build_jsonl_source_extraction,
    build_vscode_storage_source_extraction,
    build_unsupported_source_extraction,
    parse_codex_jsonl_file,
)
from knowledgeforge.interfaces.cli import app


class HistoricalJsonTests(unittest.TestCase):
    def test_build_jsonl_source_extraction_emits_low_trust_atomic_cards(self) -> None:
        source_dir = Path(self._tmpdir.name) / "claude" / "projects" / "ProjectA"
        source_dir.mkdir(parents=True)
        session = source_dir / "session.jsonl"
        session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-04-01T00:00:00Z",
                    "sessionId": "session",
                    "message": {
                        "content": "Objective: build atomic cards. Never store secrets or tokens."
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Todo: verify the JSON before import. Fixed the parser bug.",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        payload = build_jsonl_source_extraction(
            HistoricalSource("claude", str(source_dir.parent), "jsonl-supported"),
            max_cards=4,
        )

        self.assertGreaterEqual(len(payload["memory_cards"]), 2)
        for card in payload["memory_cards"]:
            self.assertFalse(card["current_truth"])
            self.assertTrue(card["needs_repo_confirmation"])
            self.assertIn(card["type"], {"objective", "security_rule", "todo", "resolution"})

    def test_source_extraction_includes_subagent_jsonl_files(self) -> None:
        source_dir = Path(self._tmpdir.name) / "claude" / "projects" / "ProjectA"
        subagent_dir = source_dir / "session" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-abc.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-04-01T00:00:00Z",
                    "sessionId": "agent-abc",
                    "message": {"content": "Todo: capture subagent historical facts."},
                }
            )
            + "\n"
            + json.dumps({"type": "assistant", "message": {"content": "Fixed: included subagent files."}})
            + "\n",
            encoding="utf-8",
        )

        payload = build_jsonl_source_extraction(
            HistoricalSource("claude", str(source_dir.parent), "jsonl-supported"),
            max_cards=4,
        )

        self.assertIn("Scanned 1 claude JSONL session file", payload["conversation_summary"]["summary"])
        self.assertTrue(payload["memory_cards"])

    def test_secret_assignments_are_not_copied_to_cards(self) -> None:
        source_dir = Path(self._tmpdir.name) / "claude" / "projects" / "ProjectA"
        source_dir.mkdir(parents=True)
        session = source_dir / "session.jsonl"
        session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-04-01T00:00:00Z",
                    "message": {
                        "content": "TODO: keep this. OPENAI_API_KEY=sk-proj-shouldnotappear"
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": "Next step: write a clean extraction artifact."},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        payload = build_jsonl_source_extraction(
            HistoricalSource("claude", str(source_dir.parent), "jsonl-supported"),
            max_cards=4,
        )
        serialized = json.dumps(payload)

        self.assertNotIn("sk-proj-shouldnotappear", serialized)
        self.assertNotIn("OPENAI_API_KEY=", serialized)
        self.assertTrue(payload["memory_cards"])

    def test_adb_device_selectors_are_redacted(self) -> None:
        source_dir = Path(self._tmpdir.name) / "codex"
        source_dir.mkdir()
        session = source_dir / "rollout.jsonl"
        session.write_text(
            json.dumps(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "python check.py\nadb -s LHSPG19C02000136 shell am start",
                        }
                    ],
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Fixed: command path works."}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        payload = build_jsonl_source_extraction(
            HistoricalSource("codex", str(source_dir), "jsonl-supported"),
            max_cards=4,
        )
        serialized = json.dumps(payload)

        self.assertNotIn("LHSPG19C02000136", serialized)
        self.assertIn("[REDACTED_DEVICE]", serialized)

    def test_parse_codex_jsonl_file_pairs_user_and_assistant_messages(self) -> None:
        session = Path(self._tmpdir.name) / "rollout.jsonl"
        session.write_text(
            json.dumps({"id": "rollout", "timestamp": "2025-09-02T00:00:00Z"})
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Goal: inspect the adapter."}],
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Decision: keep raw files immutable."}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        exchanges = parse_codex_jsonl_file(session)

        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0].source_agent, "codex")
        self.assertIn("inspect the adapter", exchanges[0].user_message)
        self.assertIn("raw files immutable", exchanges[0].assistant_message)

    def test_unsupported_source_json_contains_adapter_blocker(self) -> None:
        payload = build_unsupported_source_extraction(
            HistoricalSource("windsurf", r"C:\Users\tiaz\AppData\Roaming\Windsurf", "unsupported-vscode-storage")
        )

        self.assertEqual(len(payload["memory_cards"]), 2)
        self.assertEqual(payload["memory_cards"][0]["type"], "blocker")
        self.assertFalse(payload["memory_cards"][0]["current_truth"])
        self.assertTrue(payload["memory_cards"][0]["needs_repo_confirmation"])

    def test_windsurf_vscode_storage_extracts_gemini_chat_threads(self) -> None:
        root = Path(self._tmpdir.name) / "Windsurf"
        db = root / "User" / "globalStorage" / "state.vscdb"
        db.parent.mkdir(parents=True)
        self._write_vscdb(
            db,
            {
                "google.geminicodeassist": {
                    "geminiCodeAssist.chatThreads": {
                        "person@example.com": {
                            "thread-1": {
                                "id": "thread-1",
                                "title": "Fix exporter",
                                "create_time": "2026-01-01T00:00:00Z",
                                "update_time": "2026-01-01T00:00:00Z",
                                "history": [
                                    {
                                        "entity": "USER",
                                        "markdownText": "Objective: build the exporter. TODO: verify leakage audit.",
                                    },
                                    {
                                        "entity": "MODEL",
                                        "markdownText": "Resolved: parser now writes low-trust JSON cards.",
                                    },
                                ],
                            }
                        }
                    }
                }
            },
        )

        payload = build_vscode_storage_source_extraction(
            HistoricalSource("windsurf", str(root), "vscode-storage"),
            max_cards=6,
        )

        self.assertGreaterEqual(len(payload["memory_cards"]), 2)
        self.assertNotIn("person@example.com", json.dumps(payload))
        for card in payload["memory_cards"]:
            self.assertFalse(card["current_truth"])
            self.assertTrue(card["needs_repo_confirmation"])

    def test_antigravity_adapter_extracts_brain_markdown_and_redacts_tokens(self) -> None:
        root = Path(self._tmpdir.name) / "Antigravity"
        db = root / "User" / "globalStorage" / "state.vscdb"
        db.parent.mkdir(parents=True)
        self._write_vscdb(
            db,
            {
                "antigravityUnifiedStateSync.trajectorySummaries": base64.b64encode(
                    b"Objective: summarize trajectory. ya29.shouldnotappear0123456789"
                ).decode("ascii")
            },
        )
        companion = Path(self._tmpdir.name) / "gemini-antigravity"
        brain_task = companion / "brain" / "task-1" / "task.md"
        brain_task.parent.mkdir(parents=True)
        brain_task.write_text(
            "# Task: Build adapter\n\n- [ ] TODO: parse Antigravity markdown artifacts.\n- [x] Fixed: token redaction.",
            encoding="utf-8",
        )
        old_companion = os.environ.get("KNOWLEDGEFORGE_ANTIGRAVITY_HOME")
        os.environ["KNOWLEDGEFORGE_ANTIGRAVITY_HOME"] = str(companion)

        try:
            payload = build_vscode_storage_source_extraction(
                HistoricalSource("antigravity", str(root), "vscode-storage"),
                max_cards=4,
            )
        finally:
            if old_companion is None:
                os.environ.pop("KNOWLEDGEFORGE_ANTIGRAVITY_HOME", None)
            else:
                os.environ["KNOWLEDGEFORGE_ANTIGRAVITY_HOME"] = old_companion
        serialized = json.dumps(payload)

        self.assertNotIn("ya29.shouldnotappear", serialized)
        self.assertTrue(payload["memory_cards"])

    def test_extract_json_cli_writes_artifact_without_importing(self) -> None:
        source_dir = Path(self._tmpdir.name) / "codex"
        source_dir.mkdir()
        session = source_dir / "rollout.jsonl"
        session.write_text(
            json.dumps(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Todo: build exporter JSON."}],
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Fixed: command now writes JSON."}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        output = Path(self._tmpdir.name) / "out.json"

        result = CliRunner().invoke(
            app,
            [
                "historical",
                "extract-json",
                str(source_dir),
                str(output),
                "--agent",
                "codex",
                "--adapter-status",
                "jsonl-supported",
                "--max-cards",
                "4",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(output.exists())
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(payload["memory_cards"])

    def setUp(self) -> None:
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_vscdb(self, path: Path, values: dict[str, object]) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
            for key, value in values.items():
                raw = value if isinstance(value, str) else json.dumps(value)
                conn.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", (key, raw))
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
