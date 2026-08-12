import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from rhp_analyzer.cache import CacheStore, canonical_hash


class CacheTests(TestCase):
    def test_canonical_hash_ignores_dictionary_order(self) -> None:
        self.assertEqual(
            canonical_hash({"a": 1, "b": 2}), canonical_hash({"b": 2, "a": 1})
        )

    def test_sqlite_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / "nested" / "cache.sqlite3")
            cache.initialize()
            extraction = cache.put_extraction(
                cache_key="extraction",
                pdf_sha256="a" * 64,
                filename="issuer.pdf",
                pdf_bytes=123,
                model="test-model",
                retries=2,
                sections=["offer"],
                prompt_version="v1",
                records=[{"section": "offer"}],
                metadata={"usage": {"requests": 1}},
            )
            self.assertEqual(extraction["pdf_sha256"], "a" * 64)
            cache.put_report(
                cache_key="report",
                extraction_key="extraction",
                model="test-model",
                prompt_version="v2",
                report_markdown="# Report",
                metadata={"usage": {"requests": 1}},
            )
            self.assertEqual(cache.get_report("report")["report_markdown"], "# Report")

    def test_cache_operations_close_database_connections(self) -> None:
        with TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / "cache.sqlite3")
            connections: list[sqlite3.Connection] = []
            sqlite_connect = sqlite3.connect

            def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
                connection = sqlite_connect(*args, **kwargs)
                connections.append(connection)
                return connection

            with patch("rhp_analyzer.cache.sqlite3.connect", side_effect=connect):
                cache.initialize()
                for _ in range(10):
                    self.assertEqual(cache.pending_job_ids(), [])

            self.assertEqual(len(connections), 11)
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_analysis_job_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "upload.pdf"
            pdf_path.touch()
            cache = CacheStore(Path(directory) / "cache.sqlite3")
            cache.initialize()
            cache.put_job(
                analysis_id="analysis",
                extraction_key="extraction",
                pdf_sha256="a" * 64,
                filename="issuer.pdf",
                pdf_bytes=123,
                model="test-model",
                retries=2,
                sections=["offer", "business"],
                market_data={"gmp": "₹39"},
                pdf_path=pdf_path,
                status="queued",
            )
            cache.update_job(
                "analysis",
                status="running",
                stage="extracting",
                message="Completed section: offer.",
                completed_sections=1,
            )
            job = cache.get_job("analysis")
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["completed_sections"], 1)
            self.assertEqual(job["market_data"], {"gmp": "₹39"})
            self.assertEqual(cache.pending_job_ids(), ["analysis"])
            listed_job = cache.list_jobs()[0]
            self.assertEqual(listed_job["analysis_id"], "analysis")
            self.assertEqual(listed_job["report_markdown"], None)
