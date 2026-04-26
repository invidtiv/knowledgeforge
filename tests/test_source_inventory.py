import unittest
from pathlib import Path

from knowledgeforge.ingestion.source_inventory import (
    SourceSpec,
    build_inventory,
    write_inventory,
)


class SourceInventoryTests(unittest.TestCase):
    def test_build_inventory_counts_candidate_files_without_reading_contents(self) -> None:
        root = Path(self._tmpdir.name) / "claude" / "projects"
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
        self.assertIs(source["exists"], True)
        self.assertEqual(source["total_files"], 3)
        self.assertEqual(source["jsonl_files"], 1)
        self.assertEqual(source["candidate_files"], 1)
        self.assertEqual(source["adapter_status"], "jsonl-supported")
        self.assertTrue(source["likely_candidate_samples"][0]["path"].endswith("session.jsonl"))
        self.assertNotIn("secret-token.json", str(source["likely_candidate_samples"]))
        self.assertIs(inventory["raw_files_read"], False)

    def test_write_inventory_creates_json_artifact(self) -> None:
        source = Path(self._tmpdir.name) / "codex" / "sessions"
        source.mkdir(parents=True)
        (source / "rollout.jsonl").write_text("{}\n", encoding="utf-8")

        output = Path(self._tmpdir.name) / "inventory.json"
        payload = write_inventory(
            [SourceSpec(agent="codex", path=str(source), adapter_status="jsonl-supported")],
            output,
            host="test-host",
        )

        self.assertTrue(output.exists())
        self.assertEqual(payload["known_sources"][0]["agent"], "codex")

    def setUp(self) -> None:
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
