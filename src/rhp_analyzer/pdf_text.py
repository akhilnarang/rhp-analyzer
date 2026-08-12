from __future__ import annotations

import re
import resource
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SectionSpec:
    name: str
    aliases: tuple[str, ...]
    max_pages: int
    questions: tuple[str, ...]


SECTION_SPECS: tuple[SectionSpec, ...] = (
    SectionSpec(
        name="offer",
        aliases=("THE OFFER", "THE ISSUE"),
        max_pages=18,
        questions=(
            "offer_structure: Give the fresh issue and OFS parts.",
            "issue_size: Give the total issue size and currency unit.",
            "price_band: Give the price band or issue price.",
            "lot_size: Give the minimum bid lot and minimum investment.",
            "issue_schedule: Give the open, close, allotment, and listing dates.",
            "post_offer_market_cap: Give the stated market capitalization after the offer. Calculate it only from stated values.",
        ),
    ),
    SectionSpec(
        name="financial_summary",
        aliases=("SUMMARY OF FINANCIAL INFORMATION", "SUMMARY FINANCIAL INFORMATION"),
        max_pages=14,
        questions=(
            "revenue: Give revenue from operations for each annual period.",
            "ebitda: Give EBITDA and its margin only if the issuer defines them.",
            "pat: Give profit after tax for each annual period.",
            "net_worth: Give net worth for each annual period.",
            "borrowings: Give total borrowings or financial debt for each annual period.",
            "cash_flow: Give operating cash flow for each annual period.",
            "roe_roce: Give ROE and ROCE. Include the issuer definitions and periods.",
        ),
    ),
    SectionSpec(
        name="objects",
        aliases=("OBJECTS OF THE OFFER", "OBJECTS OF THE ISSUE"),
        max_pages=24,
        questions=(
            "fresh_issue_use: Give each planned use of fresh-issue money and its amount.",
            "deployment_schedule: Give the planned use schedule.",
            "funding_shortfall: Give the source for each amount that the proceeds do not cover.",
        ),
    ),
    SectionSpec(
        name="business",
        aliases=("OUR BUSINESS",),
        max_pages=38,
        questions=(
            "business_model: State what the company sells and who buys it. State how the company makes money.",
            "segments: Give each product or revenue segment and its stated share.",
            "facilities: Give each current or planned factory and its capacity.",
            "customer_concentration: Give the top-customer share for each period.",
            "competitive_strengths: List the issuer claims about its strengths. Identify them as issuer claims.",
        ),
    ),
    SectionSpec(
        name="risks",
        aliases=(
            "RISK FACTORS",
            "SECTION II: RISK FACTORS",
            "SECTION II - RISK FACTORS",
        ),
        max_pages=55,
        questions=(
            "specific_dependencies: Give each stated dependence on a customer, supplier, product, area, or key person.",
            "financial_risks: Give important debt, working capital, cash flow, covenant, and profit risks.",
            "operational_risks: Give company-specific operation and project risks.",
        ),
    ),
    SectionSpec(
        name="basis_for_price",
        aliases=(
            "BASIS FOR OFFER PRICE",
            "BASIS FOR THE OFFER PRICE",
            "BASIS FOR ISSUE PRICE",
        ),
        max_pages=20,
        questions=(
            "eps: Give basic and diluted EPS for each period.",
            "nav: Give NAV per share and its date.",
            "peer_comparison: Give the named peers and the stated valuation values.",
            "implied_valuation: State if the price band and EPS permit a direct P/E calculation.",
        ),
    ),
    SectionSpec(
        name="litigation",
        aliases=(
            "OUTSTANDING LITIGATION AND MATERIAL DEVELOPMENTS",
            "OUTSTANDING LITIGATIONS AND MATERIAL DEVELOPMENTS",
        ),
        max_pages=18,
        questions=(
            "company_litigation: Summarize important company cases and their amounts.",
            "promoter_litigation: Summarize important promoter or director cases and their amounts.",
            "regulatory_actions: Give each regulatory, tax, criminal, or disciplinary case.",
        ),
    ),
)


PDF_PARSE_TIMEOUT_SECONDS = 120
MAX_EXTRACTED_TEXT_BYTES = 200_000_000


def limit_pdf_parser() -> None:
    """Limit CPU, memory, files, processes, and output for the PDF parser."""

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
    resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_EXTRACTED_TEXT_BYTES,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def extract_pages(pdf_path: Path) -> list[str]:
    """Extract the text. Keep the page limits and the text layout."""

    source_path = pdf_path.resolve(strict=True)
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
        "/input.pdf",
        "/usr/bin/pdftotext",
        "-q",
        "-layout",
        "/input.pdf",
        "-",
    ]
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=PDF_PARSE_TIMEOUT_SECONDS,
            preexec_fn=limit_pdf_parser,
            start_new_session=True,
        )
        size = output.tell()
        if size > MAX_EXTRACTED_TEXT_BYTES:
            raise ValueError("The PDF produces too much extracted text.")
        output.seek(0)
        text = output.read(MAX_EXTRACTED_TEXT_BYTES + 1)
    if len(text) > MAX_EXTRACTED_TEXT_BYTES:
        raise ValueError("The PDF produces too much extracted text.")
    pages = text.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().upper().replace("–", "-")


def locate_section(pages: list[str], spec: SectionSpec) -> int | None:
    """Get the physical start page. Do not use a table-of-contents match."""

    aliases = {normalize_line(alias) for alias in spec.aliases}
    candidates: list[int] = []
    for page_index, page in enumerate(pages):
        if page_index < 4 or "TABLE OF CONTENTS" in page.upper():
            continue
        lines = {
            normalize_line(line) for line in page.splitlines()[:10] if line.strip()
        }
        if aliases & lines:
            candidates.append(page_index)
    return candidates[0] if candidates else None


def render_page_window(pages: list[str], start: int, count: int) -> str:
    chunks: list[str] = []
    for page_index in range(start, min(start + count, len(pages))):
        chunks.append(f"\n[PDF_PAGE_{page_index + 1}]\n{pages[page_index].strip()}\n")
    return "".join(chunks)


def next_major_section_page(pages: list[str], start: int) -> int | None:
    for page_index in range(start + 1, len(pages)):
        for line in pages[page_index].splitlines()[:10]:
            normalized = normalize_line(line)
            if normalized.startswith("SECTION ") and "RISK FACTORS" not in normalized:
                return page_index
    return None


def schedule_pages(pages: list[str]) -> list[int]:
    """Find offer schedule pages outside the Offer section."""

    matches: list[int] = []
    for page_index, page in enumerate(pages):
        normalized = normalize_line(page)
        has_opening = "OPENS ON" in normalized and "CLOSES ON" in normalized
        if has_opening and "BASIS OF ALLOTMENT" in normalized:
            matches.append(page_index)
    return matches[:2]


def section_windows(pdf_path: Path) -> dict[str, tuple[SectionSpec, int, str]]:
    pages = extract_pages(pdf_path)
    windows: dict[str, tuple[SectionSpec, int, str]] = {}
    for spec in SECTION_SPECS:
        start = locate_section(pages, spec)
        if start is not None:
            count = spec.max_pages
            if spec.name == "risks":
                next_section = next_major_section_page(pages, start)
                if next_section is not None:
                    count = min(count, next_section - start)
            content = render_page_window(pages, start, count)
            if spec.name == "offer" and start >= 4:
                content = render_page_window(pages, 0, 4) + content
                included = set(range(4)) | set(
                    range(start, min(start + count, len(pages)))
                )
                for page_index in schedule_pages(pages):
                    if page_index not in included:
                        content += render_page_window(pages, page_index, 1)
            windows[spec.name] = (
                spec,
                start + 1,
                content,
            )
    return windows
