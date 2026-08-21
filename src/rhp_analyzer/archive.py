from __future__ import annotations

import hashlib
import logging
import re
import resource
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .pdf_text import extract_pdf_sample

logger = logging.getLogger("uvicorn.error")

MAX_ARCHIVE_ENTRIES = 1_000
MAX_ARCHIVE_PDFS = 20
MAX_ARCHIVE_PATH_LENGTH = 512
MAX_COMPRESSION_RATIO = 200
ARCHIVE_EXTRACT_TIMEOUT_SECONDS = 60
MAX_TITLE_MARKER_OFFSET = 500


class ArchiveError(ValueError):
    """Base error for an unsafe or unsupported ZIP archive."""


class ArchiveLimitError(ArchiveError):
    """The ZIP archive exceeds a resource limit."""


class ArchiveSelectionError(ArchiveError):
    """The ZIP archive does not identify one offer document."""


@dataclass(frozen=True)
class ExtractedPdf:
    path: Path
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ProspectusCandidate:
    pdf: ExtractedPdf
    document_type: str
    priority: int


def _safe_member_name(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename
    if (
        not name
        or len(name) > MAX_ARCHIVE_PATH_LENGTH
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or "//" in name
    ):
        raise ArchiveError("The ZIP contains an unsafe member path.")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} or part.endswith(":") for part in path.parts):
        raise ArchiveError("The ZIP contains an unsafe member path.")
    return path


def inspect_archive(
    archive_path: Path,
    *,
    max_pdf_bytes: int,
) -> list[zipfile.ZipInfo]:
    """Check all ZIP records before any member is extracted."""

    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ArchiveError("The file is not a valid ZIP archive.") from exc

    if not infos:
        raise ArchiveSelectionError("The ZIP archive is empty.")
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ArchiveLimitError(
            f"The ZIP contains more than {MAX_ARCHIVE_ENTRIES} entries."
        )

    seen_names: set[str] = set()
    pdf_infos: list[zipfile.ZipInfo] = []
    total_size = 0
    for info in infos:
        path = _safe_member_name(info)
        if info.filename in seen_names:
            raise ArchiveError("The ZIP contains duplicate member paths.")
        seen_names.add(info.filename)
        if info.flag_bits & 0x1:
            raise ArchiveError("The ZIP contains an encrypted member.")

        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type and not info.is_dir() and not stat.S_ISREG(mode):
            raise ArchiveError("The ZIP contains a link or special file.")
        if info.file_size < 0 or info.compress_size < 0:
            raise ArchiveError("The ZIP contains an invalid member size.")
        total_size += info.file_size
        if total_size > max_pdf_bytes * 5:
            raise ArchiveLimitError("The ZIP expands beyond the archive limit.")
        if info.is_dir():
            continue
        if path.suffix.casefold() != ".pdf":
            continue
        if info.file_size == 0:
            continue
        if info.file_size > max_pdf_bytes:
            raise ArchiveLimitError(
                f"A PDF in the ZIP is larger than the {max_pdf_bytes}-byte limit."
            )
        if info.compress_size == 0:
            raise ArchiveLimitError("A PDF in the ZIP has an unsafe compression ratio.")
        if info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise ArchiveLimitError("A PDF in the ZIP has an unsafe compression ratio.")
        pdf_infos.append(info)

    if not pdf_infos:
        raise ArchiveSelectionError("The ZIP does not contain a PDF file.")
    if len(pdf_infos) > MAX_ARCHIVE_PDFS:
        raise ArchiveLimitError(
            f"The ZIP contains more than {MAX_ARCHIVE_PDFS} PDF files."
        )
    return pdf_infos


def _limit_archive_extractor(max_pdf_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_AS, (600_000_000, 600_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_pdf_bytes, max_pdf_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _extract_member(
    archive_path: Path,
    info: zipfile.ZipInfo,
    *,
    max_pdf_bytes: int,
) -> ExtractedPdf:
    """Extract one checked member to stdout in a Bubblewrap sandbox."""

    source_path = archive_path.resolve(strict=True)
    script = (
        "import shutil,sys,zipfile; "
        "z=zipfile.ZipFile(sys.argv[1]); "
        "s=z.open(z.getinfo(sys.argv[2])); "
        "shutil.copyfileobj(s,sys.stdout.buffer,1024*1024)"
    )
    command = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib",
        "/lib64",
        "--dir",
        "/tmp",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--ro-bind",
        str(source_path),
        "/input.zip",
        "/usr/bin/python3",
        "-I",
        "-c",
        script,
        "/input.zip",
        info.filename,
    ]
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as output:
            path = Path(output.name)
            subprocess.run(
                command,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=ARCHIVE_EXTRACT_TIMEOUT_SECONDS,
                preexec_fn=lambda: _limit_archive_extractor(max_pdf_bytes),
                start_new_session=True,
            )
        size = path.stat().st_size
        if size != info.file_size or size > max_pdf_bytes:
            raise ArchiveLimitError("A PDF did not match its checked ZIP size.")
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise ArchiveError("A .pdf member does not contain PDF data.")
            source.seek(0)
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        return ExtractedPdf(
            path=path,
            filename=PurePosixPath(info.filename).name,
            size=size,
            sha256=digest,
        )
    except subprocess.TimeoutExpired as exc:
        if path is not None:
            path.unlink(missing_ok=True)
        raise ArchiveLimitError("ZIP extraction exceeded the time limit.") from exc
    except subprocess.CalledProcessError as exc:
        if path is not None:
            path.unlink(missing_ok=True)
        raise ArchiveError("The ZIP member could not be extracted safely.") from exc
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def classify_prospectus(pdf_path: Path) -> tuple[str, int] | None:
    """Classify a PDF from its first pages, not from its filename."""

    pages = extract_pdf_sample(pdf_path)
    opening = re.sub(r"\s+", " ", " ".join(pages[:3])).upper()
    sample = re.sub(r"\s+", " ", " ".join(pages)).upper()
    title_markers = {
        "GENERAL INFORMATION DOCUMENT": None,
        "ABRIDGED PROSPECTUS": None,
        "DRAFT RED HERRING PROSPECTUS": ("DRHP", 1),
        "RED HERRING PROSPECTUS": ("RHP", 3),
    }
    first_markers = [
        (position, classification)
        for marker, classification in title_markers.items()
        if 0 <= (position := opening.find(marker)) <= MAX_TITLE_MARKER_OFFSET
    ]
    if first_markers:
        return min(first_markers, key=lambda item: item[0])[1]
    offer_markers = (
        "INITIAL PUBLIC OFFER",
        "PUBLIC ISSUE OF",
        "THE ISSUE",
        "THE OFFER",
    )
    prospectus_position = opening.find("PROSPECTUS")
    if 0 <= prospectus_position <= MAX_TITLE_MARKER_OFFSET and any(
        marker in sample for marker in offer_markers
    ):
        return "prospectus", 2
    return None


def extract_prospectus_from_zip(
    archive_path: Path,
    *,
    max_pdf_bytes: int,
) -> ExtractedPdf:
    """Inspect every PDF and return one content-verified offer document."""

    pdf_infos = inspect_archive(archive_path, max_pdf_bytes=max_pdf_bytes)
    extracted: list[ExtractedPdf] = []
    candidates: list[ProspectusCandidate] = []
    try:
        for info in pdf_infos:
            try:
                pdf = _extract_member(
                    archive_path,
                    info,
                    max_pdf_bytes=max_pdf_bytes,
                )
                extracted.append(pdf)
                classification = classify_prospectus(pdf.path)
            except ArchiveError, OSError, subprocess.SubprocessError, ValueError:
                logger.warning(
                    "ZIP PDF rejected during content check: member=%s",
                    info.filename,
                )
                continue
            if classification is not None:
                document_type, priority = classification
                candidates.append(
                    ProspectusCandidate(
                        pdf=pdf,
                        document_type=document_type,
                        priority=priority,
                    )
                )

        if not candidates:
            raise ArchiveSelectionError(
                "The ZIP has no content-verified RHP, DRHP, or prospectus PDF."
            )
        highest_priority = max(candidate.priority for candidate in candidates)
        selected = [
            candidate
            for candidate in candidates
            if candidate.priority == highest_priority
        ]
        if len(selected) != 1:
            names = ", ".join(candidate.pdf.filename for candidate in selected)
            raise ArchiveSelectionError(
                "The ZIP has several equally valid offer documents: " + names
            )
        result = selected[0].pdf
        logger.info(
            "ZIP offer document selected: file=%s type=%s checked_pdfs=%d",
            result.filename,
            selected[0].document_type,
            len(pdf_infos),
        )
        extracted.remove(result)
        return result
    finally:
        for pdf in extracted:
            pdf.path.unlink(missing_ok=True)
