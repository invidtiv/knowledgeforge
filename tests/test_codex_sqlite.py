import json
import sqlite3
import unittest
from pathlib import Path

from typer.testing import CliRunner

from knowledgeforge.ingestion.codex_sqlite import inspect_sqlite_schema, export_codex_logs
from knowledgeforge.interfaces.cli import app


class CodexSqliteTests(unittest.TestCase):
    def test_inspect_sqlite_schema_reads_tables_without_exporting_values(self) -> None:
        db = Path(self._tmpdir.name) / "logs.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("create table logs (thread_id text, created_at text, feedback_log_body text)")
        conn.execute("insert into logs values ('thread-1', '2026-04-01', 'secret body')")
        conn.commit()
        conn.close()

        schema = inspect_sqlite_schema(db)

        self.assertEqual(schema["tables"][0]["name"], "logs")
        self.assertEqual(schema["tables"][0]["row_count"], 1)
        self.assertIn("feedback_log_body", schema["tables"][0]["columns"])
        self.assertNotIn("secret body", json.dumps(schema))

    def test_export_codex_logs_groups_rows_by_thread_id(self) -> None:
        db = Path(self._tmpdir.name) / "logs.sqlite"
        conn = sqlite3.connect(db)
        conn.execute(
            "create table logs (thread_id text, created_at text, feedback_log_body text, module_path text)"
        )
        conn.execute(
            "insert into logs values ('thread-1', '2026-04-01T00:00:00Z', 'first', 'codex')"
        )
        conn.execute(
            "insert into logs values ('thread-1', '2026-04-01T00:01:00Z', 'second', 'codex')"
        )
        conn.commit()
        conn.close()

        out_dir = Path(self._tmpdir.name) / "out"
        manifest = export_codex_logs(db, out_dir, limit_threads=5)

        self.assertEqual(manifest["thread_count"], 1)
        export_path = Path(manifest["threads"][0]["jsonl_path"])
        rows = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["type"], "user")
        self.assertEqual(rows[0]["source_agent"], "codex")
        self.assertEqual(rows[0]["thread_id"], "thread-1")
        self.assertEqual(rows[0]["message"]["content"], "first")
        self.assertEqual(rows[1]["message"]["content"], "second")
        self.assertEqual(rows[0]["module_path"], "codex")

    def test_export_codex_logs_skips_secret_columns_and_redacts_secret_values(self) -> None:
        db = Path(self._tmpdir.name) / "logs.sqlite"
        conn = sqlite3.connect(db)
        conn.execute(
            "create table logs (thread_id text, created_at text, feedback_log_body text, api_key text)"
        )
        conn.execute(
            "insert into logs values "
            "('thread-1', '2026-04-01T00:00:00Z', "
            "'token=sk-live-secret123 and password=hunter2', 'do-not-export')"
        )
        conn.commit()
        conn.close()

        manifest = export_codex_logs(db, Path(self._tmpdir.name) / "out-secrets", limit_threads=5)

        self.assertEqual(manifest["redacted_rows"], 1)
        export_path = Path(manifest["threads"][0]["jsonl_path"])
        text = export_path.read_text(encoding="utf-8")
        self.assertNotIn("do-not-export", text)
        self.assertNotIn("sk-live-secret123", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("[REDACTED]", text)

    def test_export_codex_logs_uses_deterministic_fallback_without_thread_id(self) -> None:
        db = Path(self._tmpdir.name) / "logs.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("create table logs (created_at text, body text)")
        conn.execute("insert into logs values ('2026-04-01T00:00:00Z', 'first')")
        conn.execute("insert into logs values ('2026-04-01T00:01:00Z', 'second')")
        conn.commit()
        conn.close()

        manifest = export_codex_logs(db, Path(self._tmpdir.name) / "out-fallback", limit_threads=5)

        self.assertEqual(manifest["thread_count"], 2)
        self.assertEqual(
            [thread["thread_id"] for thread in manifest["threads"]],
            ["logs-row-1", "logs-row-2"],
        )

    def test_historical_codex_sqlite_export_schema_only_cli_prints_schema(self) -> None:
        db = Path(self._tmpdir.name) / "logs.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("create table logs (thread_id text, feedback_log_body text)")
        conn.commit()
        conn.close()
        out_dir = Path(self._tmpdir.name) / "out-cli"

        result = CliRunner().invoke(
            app,
            [
                "historical",
                "codex-sqlite-export",
                str(db),
                str(out_dir),
                "--schema-only",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["tables"][0]["name"], "logs")
        self.assertFalse(out_dir.exists())

    def setUp(self) -> None:
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
