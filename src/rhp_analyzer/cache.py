from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEGACY_ANALYSIS_PREFIX_LENGTH = 12
PUBLIC_SLUG_VERSION = 2


def analysis_slug(filename: str) -> str:
    """Make a stable public slug from a prospectus filename."""

    name = Path(filename).stem
    name = re.sub(r"(?i)^registration[_-]?\d+[_-]?", "", name)
    name = re.sub(
        r"(?i)[\s_-]+(?:(?:draft[\s_-]+)?red[\s_-]+herring[\s_-]+prospectus|"
        r"drhp|rhp|prospectus)(?:[\s_-].*)?$",
        "",
        name,
    )
    name = re.sub(r"(?i)(?:drhp|rhp)$", "", name)
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii").lower()
    name = name.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "analysis"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class CacheStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS extraction_cache (
                    cache_key TEXT PRIMARY KEY,
                    pdf_sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    pdf_bytes INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    retries INTEGER NOT NULL,
                    sections_json TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    records_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS extraction_pdf_sha256_idx
                    ON extraction_cache(pdf_sha256);

                CREATE TABLE IF NOT EXISTS report_cache (
                    cache_key TEXT PRIMARY KEY,
                    extraction_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    report_markdown TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(extraction_key) REFERENCES extraction_cache(cache_key)
                );
                CREATE INDEX IF NOT EXISTS report_extraction_key_idx
                    ON report_cache(extraction_key);

                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    analysis_id TEXT PRIMARY KEY,
                    public_slug TEXT,
                    public_slug_version INTEGER NOT NULL DEFAULT 2,
                    extraction_key TEXT NOT NULL,
                    pdf_sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    pdf_bytes INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    retries INTEGER NOT NULL,
                    sections_json TEXT NOT NULL,
                    market_data_json TEXT NOT NULL DEFAULT '{}',
                    pdf_path TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    completed_sections INTEGER NOT NULL,
                    total_sections INTEGER NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS analysis_job_status_idx
                    ON analysis_jobs(status);
                """
            )
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(analysis_jobs)")
            }
            if "market_data_json" not in job_columns:
                connection.execute(
                    "ALTER TABLE analysis_jobs "
                    "ADD COLUMN market_data_json TEXT NOT NULL DEFAULT '{}'"
                )
            needs_slug_migration = "public_slug_version" not in job_columns
            if "public_slug" not in job_columns:
                connection.execute("ALTER TABLE analysis_jobs ADD COLUMN public_slug TEXT")
            if needs_slug_migration:
                connection.execute(
                    "ALTER TABLE analysis_jobs ADD COLUMN "
                    "public_slug_version INTEGER NOT NULL DEFAULT 1"
                )
                connection.execute("DROP INDEX IF EXISTS analysis_job_public_slug_idx")
            used_slugs: set[str] = set()
            rows = connection.execute(
                "SELECT analysis_id, filename, public_slug, public_slug_version "
                "FROM analysis_jobs "
                "ORDER BY created_at"
            ).fetchall()
            for row in rows:
                public_slug = str(row["public_slug"] or "")
                if (
                    int(row["public_slug_version"]) < PUBLIC_SLUG_VERSION
                    or not public_slug
                    or public_slug in used_slugs
                ):
                    public_slug = self._available_public_slug(
                        connection,
                        filename=str(row["filename"]),
                        analysis_id=str(row["analysis_id"]),
                        used_slugs=used_slugs,
                    )
                    connection.execute(
                        "UPDATE analysis_jobs "
                        "SET public_slug = ?, public_slug_version = ? "
                        "WHERE analysis_id = ?",
                        (public_slug, PUBLIC_SLUG_VERSION, row["analysis_id"]),
                    )
                used_slugs.add(public_slug)
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS analysis_job_public_slug_idx "
                "ON analysis_jobs(public_slug)"
            )

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["sections"] = json.loads(result.pop("sections_json"))
        result["market_data"] = json.loads(result.pop("market_data_json", "{}"))
        return result

    def get_job(self, analysis_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?",
                (analysis_id,),
            ).fetchone()
        return None if row is None else self._decode_job(row)

    def get_job_by_public_slug(self, public_slug: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE public_slug = ?",
                (public_slug,),
            ).fetchone()
        return None if row is None else self._decode_job(row)

    def resolve_analysis_id(self, analysis_id: str) -> str | None:
        """Resolve a public slug or an unambiguous legacy hash."""

        job = self.get_job_by_public_slug(analysis_id)
        if job is not None:
            return str(job["analysis_id"])
        if len(analysis_id) == 64:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT analysis_id FROM analysis_jobs WHERE analysis_id = ?
                    UNION
                    SELECT cache_key AS analysis_id FROM report_cache WHERE cache_key = ?
                    """,
                    (analysis_id, analysis_id),
                ).fetchone()
            return analysis_id if row is not None else None
        if not (
            LEGACY_ANALYSIS_PREFIX_LENGTH <= len(analysis_id) < 64
            and all(character in "0123456789abcdef" for character in analysis_id)
        ):
            return None
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH ids AS (
                    SELECT analysis_id FROM analysis_jobs
                    UNION
                    SELECT cache_key AS analysis_id FROM report_cache
                )
                SELECT analysis_id FROM ids
                WHERE analysis_id LIKE ?
                LIMIT 2
                """,
                (f"{analysis_id}%",),
            ).fetchall()
        if len(rows) != 1:
            return None
        return str(rows[0]["analysis_id"])

    def public_analysis_id(self, analysis_id: str) -> str:
        """Return the stable public slug for one analysis."""

        job = self.get_job(analysis_id)
        if job is not None and job.get("public_slug"):
            return str(job["public_slug"])
        return analysis_id

    @staticmethod
    def _available_public_slug(
        connection: sqlite3.Connection,
        *,
        filename: str,
        analysis_id: str,
        used_slugs: set[str] | None = None,
    ) -> str:
        base = analysis_slug(filename)
        reserved = used_slugs or set()

        def is_available(candidate: str) -> bool:
            if candidate in reserved:
                return False
            row = connection.execute(
                "SELECT analysis_id FROM analysis_jobs WHERE public_slug = ?",
                (candidate,),
            ).fetchone()
            return row is None or row["analysis_id"] == analysis_id

        if is_available(base):
            return base
        for length in range(6, len(analysis_id) + 1):
            candidate = f"{base}-{analysis_id[:length]}"
            if is_available(candidate):
                return candidate
        return f"{base}-{analysis_id}"

    def pending_job_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT analysis_id FROM analysis_jobs "
                "WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
        return [row["analysis_id"] for row in rows]

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT analysis_jobs.*, report_cache.report_markdown "
                "FROM analysis_jobs "
                "LEFT JOIN report_cache "
                "ON report_cache.cache_key = analysis_jobs.analysis_id "
                "ORDER BY analysis_jobs.updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def put_job(
        self,
        *,
        analysis_id: str,
        extraction_key: str,
        pdf_sha256: str,
        filename: str,
        pdf_bytes: int,
        model: str,
        retries: int,
        sections: list[str],
        market_data: dict[str, str],
        pdf_path: Path | None,
        status: str,
    ) -> dict[str, Any]:
        now = utc_now()
        message = (
            "The analysis is ready." if status == "completed" else "Waiting to start."
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT public_slug FROM analysis_jobs WHERE analysis_id = ?",
                (analysis_id,),
            ).fetchone()
            public_slug = (
                str(existing["public_slug"])
                if existing is not None and existing["public_slug"]
                else self._available_public_slug(
                    connection, filename=filename, analysis_id=analysis_id
                )
            )
            connection.execute(
                """
                INSERT INTO analysis_jobs (
                    analysis_id, public_slug, public_slug_version,
                    extraction_key, pdf_sha256, filename,
                    pdf_bytes, model, retries, sections_json, market_data_json,
                    pdf_path,
                    status, stage, message, completed_sections,
                    total_sections, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(analysis_id) DO UPDATE SET
                    filename = excluded.filename,
                    pdf_bytes = excluded.pdf_bytes,
                    market_data_json = excluded.market_data_json,
                    pdf_path = excluded.pdf_path,
                    status = excluded.status,
                    stage = excluded.stage,
                    message = excluded.message,
                    completed_sections = excluded.completed_sections,
                    total_sections = excluded.total_sections,
                    error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    analysis_id,
                    public_slug,
                    PUBLIC_SLUG_VERSION,
                    extraction_key,
                    pdf_sha256,
                    filename,
                    pdf_bytes,
                    model,
                    retries,
                    json.dumps(sections),
                    json.dumps(market_data, ensure_ascii=False),
                    str(pdf_path) if pdf_path is not None else None,
                    status,
                    status,
                    message,
                    len(sections) if status == "completed" else 0,
                    len(sections),
                    None,
                    now,
                    now,
                ),
            )
        result = self.get_job(analysis_id)
        assert result is not None
        return result

    def update_job(
        self,
        analysis_id: str,
        *,
        status: str,
        stage: str,
        message: str,
        completed_sections: int,
        error: str | None = None,
        clear_pdf_path: bool = False,
    ) -> None:
        pdf_update = ", pdf_path = NULL" if clear_pdf_path else ""
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE analysis_jobs
                SET status = ?, stage = ?, message = ?,
                    completed_sections = ?, error = ?, updated_at = ?
                    {pdf_update}
                WHERE analysis_id = ?
                """,
                (
                    status,
                    stage,
                    message,
                    completed_sections,
                    error,
                    utc_now(),
                    analysis_id,
                ),
            )

    def get_extraction(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM extraction_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["sections"] = json.loads(result.pop("sections_json"))
        result["records"] = json.loads(result.pop("records_json"))
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def put_extraction(
        self,
        *,
        cache_key: str,
        pdf_sha256: str,
        filename: str,
        pdf_bytes: int,
        model: str,
        retries: int,
        sections: list[str],
        prompt_version: str,
        records: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO extraction_cache (
                    cache_key, pdf_sha256, filename, pdf_bytes, model, retries,
                    sections_json, prompt_version, records_json, metadata_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    filename = excluded.filename,
                    pdf_bytes = excluded.pdf_bytes,
                    records_json = excluded.records_json,
                    metadata_json = excluded.metadata_json,
                    created_at = excluded.created_at
                """,
                (
                    cache_key,
                    pdf_sha256,
                    filename,
                    pdf_bytes,
                    model,
                    retries,
                    json.dumps(sections),
                    prompt_version,
                    json.dumps(records, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False),
                    created_at,
                ),
            )
        result = self.get_extraction(cache_key)
        assert result is not None
        return result

    def get_report(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM report_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def put_report(
        self,
        *,
        cache_key: str,
        extraction_key: str,
        model: str,
        prompt_version: str,
        report_markdown: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO report_cache (
                    cache_key, extraction_key, model, prompt_version,
                    report_markdown, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    report_markdown = excluded.report_markdown,
                    metadata_json = excluded.metadata_json,
                    created_at = excluded.created_at
                """,
                (
                    cache_key,
                    extraction_key,
                    model,
                    prompt_version,
                    report_markdown,
                    json.dumps(metadata, ensure_ascii=False),
                    created_at,
                ),
            )
        result = self.get_report(cache_key)
        assert result is not None
        return result
