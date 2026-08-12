from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from starlette.templating import Jinja2Templates

from .api_schemas import AnalysisResponse

PACKAGE_DIRECTORY = Path(__file__).parent
STATIC_DIRECTORY = PACKAGE_DIRECTORY / "static"
templates = Jinja2Templates(directory=PACKAGE_DIRECTORY / "templates")
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
