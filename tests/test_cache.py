import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from rhp_analyzer.cache import CacheStore, analysis_slug, canonical_hash


class CacheTests(TestCase):
    def test_analysis_slug_removes_prospectus_filename_noise(self) -> None:
        cases = {
            "RHP-V6-Sumax-17.08.2026.pdf": "sumax",
            "RHP-Technocrats_10.08.2026.pdf": "technocrats",
            "FinalRHP-Sunshine Pictures Ltd_GYR.pdf": "sunshine-pictures-ltd-gyr",
            "ABH_healthcare-_RHP_13.08.2026_0381.pdf": "abh-healthcare",
            "Registration_21072025200223_MeridianDRHP.pdf": "meridian",
            "Shiprocket Limited - RHP - August 5, 2026.PDF": "shiprocket-limited",
            "Issuer Limited.RHP.2026.pdf": "issuer-limited",
            "V2 Retail Limited.pdf": "v2-retail-limited",
        }

        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(analysis_slug(filename), expected)

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
            cache.update_job_company_name("analysis", "Issuer Industries Limited")
            job = cache.get_job("analysis")
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["completed_sections"], 1)
            self.assertEqual(job["market_data"], {"gmp": "₹39"})
            self.assertEqual(job["company_name"], "Issuer Industries Limited")
            self.assertEqual(cache.pending_job_ids(), ["analysis"])
            listed_job = cache.list_jobs()[0]
            self.assertEqual(listed_job["analysis_id"], "analysis")
            self.assertEqual(listed_job["report_markdown"], None)

    def test_checked_company_name_promotes_slug_and_preserves_alias(self) -> None:
        with TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / "cache.sqlite3")
            cache.initialize()
            cache.put_job(
                analysis_id="a" * 64,
                extraction_key="extraction",
                pdf_sha256="b" * 64,
                filename="RHP-V6-Sumax-17.08.2026.pdf",
                pdf_bytes=123,
                model="test-model",
                retries=2,
                sections=["offer"],
                market_data={},
                pdf_path=None,
                status="completed",
            )

            self.assertEqual(cache.public_analysis_id("a" * 64), "sumax")

            cache.update_job_company_name(
                "a" * 64,
                "SUMAX ENGINEERING LIMITED",
            )

            self.assertEqual(
                cache.public_analysis_id("a" * 64),
                "sumax-engineering-limited",
            )
            self.assertEqual(cache.resolve_analysis_id("sumax"), "a" * 64)
            self.assertEqual(
                cache.resolve_analysis_id("sumax-engineering-limited"),
                "a" * 64,
            )

    def test_company_name_slug_collision_gets_a_stable_hash_suffix(self) -> None:
        with TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / "cache.sqlite3")
            cache.initialize()
            for analysis_id, filename in [
                ("a" * 64, "first.pdf"),
                ("b" * 64, "second.pdf"),
            ]:
                cache.put_job(
                    analysis_id=analysis_id,
                    extraction_key=f"extraction-{analysis_id[0]}",
                    pdf_sha256=analysis_id,
                    filename=filename,
                    pdf_bytes=123,
                    model="test-model",
                    retries=2,
                    sections=["offer"],
                    market_data={},
                    pdf_path=None,
                    status="completed",
                )
                cache.update_job_company_name(analysis_id, "Issuer Limited")

            self.assertEqual(cache.public_analysis_id("a" * 64), "issuer-limited")
            self.assertEqual(
                cache.public_analysis_id("b" * 64),
                "issuer-limited-bbbbbb",
            )
            self.assertEqual(cache.resolve_analysis_id("first"), "a" * 64)
            self.assertEqual(cache.resolve_analysis_id("second"), "b" * 64)

    def test_slug_migration_keeps_the_previous_slug_as_an_alias(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "cache.sqlite3"
            cache = CacheStore(database)
            cache.initialize()
            cache.put_job(
                analysis_id="a" * 64,
                extraction_key="extraction",
                pdf_sha256="b" * 64,
                filename="RHP-V6-Sumax-17.08.2026.pdf",
                pdf_bytes=123,
                model="test-model",
                retries=2,
                sections=["offer"],
                market_data={},
                pdf_path=None,
                status="completed",
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE analysis_jobs SET public_slug = ?, "
                    "public_slug_version = 2, company_name = ?",
                    ("rhp-v6-sumax-17-08-2026", "SUMAX ENGINEERING LIMITED"),
                )
                connection.execute("DELETE FROM analysis_job_slug_aliases")

            cache.initialize()

            self.assertEqual(
                cache.public_analysis_id("a" * 64),
                "sumax-engineering-limited",
            )
            self.assertEqual(
                cache.resolve_analysis_id("rhp-v6-sumax-17-08-2026"),
                "a" * 64,
            )
