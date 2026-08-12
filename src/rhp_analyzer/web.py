from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from starlette.templating import Jinja2Templates

from .api_schemas import AnalysisResponse

PACKAGE_DIRECTORY = Path(__file__).parent
STATIC_DIRECTORY = PACKAGE_DIRECTORY / "static"
STATIC_VERSION = hashlib.sha256(
    (STATIC_DIRECTORY / "site.css").read_bytes()
).hexdigest()[:12]
templates = Jinja2Templates(directory=PACKAGE_DIRECTORY / "templates")
templates.env.globals["static_version"] = STATIC_VERSION
markdown = MarkdownIt("commonmark", {"html": False}).enable("table")


def analysis_title(job: dict[str, Any]) -> str:
    report = str(job.get("report_markdown") or "")
    match = re.search(r"^#\s+(.+?)\s*$", report, flags=re.MULTILINE)
    if match is None:
        return str(job["filename"])
    title = match.group(1).strip().strip("*_`")
    title = re.sub(
        r"\s+[—–-]\s+(?:(?:RHP|DRHP|IPO)\s+)?(?:research\s+)?(?:report|analysis).*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return title or str(job["filename"])


def analysis_list_items(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for job in jobs:
        status = str(job["status"])
        completed = int(job["completed_sections"])
        total = int(job["total_sections"])
        if status == "completed":
            summary = f"{total} sections analyzed. The report is ready to view."
        elif status == "running":
            summary = f"Analysis in progress. {completed} of {total} sections complete."
        elif status == "queued":
            summary = "Waiting for analysis to start."
        else:
            summary = "The analysis did not complete. Open it for more information."
        items.append(
            {
                **job,
                "title": analysis_title(job),
                "status_label": {
                    "completed": "Ready",
                    "running": "Analyzing",
                    "queued": "Queued",
                    "failed": "Failed",
                }.get(status, status.title()),
                "variant": {
                    "completed": "success",
                    "running": "warning",
                    "failed": "danger",
                }.get(status, "secondary"),
                "summary": summary,
                "updated_label": format_timestamp(str(job["updated_at"])),
            }
        )
    return items


def format_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d %b %Y, %H:%M UTC")


def report_html(analysis: AnalysisResponse) -> str:
    return markdown.render(analysis.report_markdown)


def _heading_text(token: Any) -> str:
    if token.children is None:
        return str(token.content).strip()
    parts = []
    for child in token.children:
        if child.type in {"text", "code_inline", "image"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append(" ")
    return "".join(parts).strip()


def _heading_id(title: str, used_ids: set[str]) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii").lower()
    base_id = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-") or "section"
    heading_id = base_id
    number = 2
    while heading_id in used_ids:
        heading_id = f"{base_id}-{number}"
        number += 1
    used_ids.add(heading_id)
    return heading_id


def report_content(
    analysis: AnalysisResponse,
) -> tuple[str, list[dict[str, str | int]]]:
    """Render the report body and make links for its section headings."""

    tokens = markdown.parse(analysis.report_markdown)
    if (
        len(tokens) >= 3
        and tokens[0].type == "heading_open"
        and tokens[0].tag == "h1"
        and tokens[2].type == "heading_close"
    ):
        tokens = tokens[3:]

    contents: list[dict[str, str | int]] = []
    used_ids: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open" or token.tag not in {"h2", "h3"}:
            continue
        title = _heading_text(tokens[index + 1])
        if not title:
            continue
        heading_id = _heading_id(title, used_ids)
        token.attrs["id"] = heading_id
        contents.append(
            {"id": heading_id, "title": title, "level": int(token.tag[1])}
        )

    return markdown.renderer.render(tokens, markdown.options, {}), contents


def report_body_html(analysis: AnalysisResponse) -> str:
    """Render the report without its first heading."""

    return report_content(analysis)[0]


def report_description(title: str) -> str:
    return f"IPO RHP or DRHP research report for {title}, with source-page evidence."
