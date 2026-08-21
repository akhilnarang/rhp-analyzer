from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, BinaryContent, ModelRetry, NativeOutput
from pydantic_ai.models.openai import (
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.providers.openai import OpenAIProvider

from .config import get_settings
from .pdf_text import SectionSpec, extract_pages, section_windows
from .schemas import SectionAnalysis

logger = logging.getLogger("uvicorn.error")

SYSTEM_INSTRUCTIONS = """\
Extract facts from an Indian IPO RHP or DRHP.

Rules:
- Use only the supplied document.
- Answer each listed question for the specified section.
- Identify each company strength as an issuer claim.
- Give a short source quote for each found answer and each risk.
- Give the physical PDF page for each quote. The first page is page 1.
- Use each [PDF_PAGE_N] marker as the page source.
- Keep the source currency, unit, period, accounting basis, and sign.
- Do not estimate a missing value.
- Use `not_found` when the supplied pages do not contain the answer.
- Use `ambiguous` when source values conflict.
- Copy each evidence quote as one continuous text string.
"""

EXTRACTION_PROMPT_VERSION = "rhp-extraction-v5"


def build_section_prompt(document_name: str, spec: SectionSpec, content: str) -> str:
    questions = "\n".join(f"- {question}" for question in spec.questions)
    return f"""\
Document: {document_name}
Section: {spec.name}

Questions:
{questions}

Document content follows:
{content}
"""


def _validate_semantics(output: SectionAnalysis) -> None:
    expected_ids = {answer.question_id for answer in output.answers}
    if len(expected_ids) != len(output.answers):
        raise ValueError("Each question_id must be unique.")
    for answer in output.answers:
        if (
            answer.status == "found"
            and answer.confidence == "high"
            and any(len(item.quote.split()) < 4 for item in answer.evidence)
        ):
            raise ValueError("A high-confidence quote must have at least four words.")
    evidence = [item for answer in output.answers for item in answer.evidence]
    evidence.extend(item for risk in output.material_risks for item in risk.evidence)
    if any("..." in item.quote or "…" in item.quote for item in evidence):
        raise ValueError(
            "Each evidence quote must be one continuous source text. "
            "Do not add ellipses."
        )


def question_ids(spec: SectionSpec) -> set[str]:
    return {question.split(":", 1)[0] for question in spec.questions}


def validate_section_output(
    output: SectionAnalysis,
    expected_question_ids: set[str],
) -> SectionAnalysis:
    """Check required answers and discard extra model-generated answers."""

    unexpected = sorted(
        {
            answer.question_id
            for answer in output.answers
            if answer.question_id not in expected_question_ids
        }
    )
    if unexpected:
        logger.warning(
            "Discarding unexpected section answers: question_ids=%s",
            ",".join(unexpected),
        )
        output = output.model_copy(
            update={
                "answers": [
                    answer
                    for answer in output.answers
                    if answer.question_id in expected_question_ids
                ]
            }
        )
    _validate_semantics(output)
    actual_question_ids = {answer.question_id for answer in output.answers}
    missing = sorted(expected_question_ids - actual_question_ids)
    if missing:
        raise ValueError(f"Answer each question one time. Missing: {missing}.")
    return output


def make_agent(
    model: str,
    retries: int,
    *,
    expected_question_ids: set[str],
) -> Agent[None, SectionAnalysis]:
    settings = get_settings()
    provider = OpenAIProvider(
        api_key=settings.require_openai_api_key(),
        base_url=settings.openai_base_url,
    )
    openai_model = OpenAIResponsesModel(model, provider=provider)
    agent: Agent[None, SectionAnalysis] = Agent(
        openai_model,
        output_type=NativeOutput(SectionAnalysis, strict=True),
        instructions=SYSTEM_INSTRUCTIONS,
        retries=retries,
        model_settings=OpenAIResponsesModelSettings(
            openai_reasoning_effort="medium",
            openai_reasoning_context="current_turn",
            openai_text_verbosity="low",
            openai_store=False,
            timeout=600,
        ),
    )

    @agent.output_validator
    def validate_output(output: SectionAnalysis) -> SectionAnalysis:
        try:
            return validate_section_output(output, expected_question_ids)
        except ValueError as exc:
            raise ModelRetry(str(exc)) from exc

    return agent


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value):
        return {
            field.name: _json_safe(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _normalise_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def score_evidence(output: SectionAnalysis, pages: list[str]) -> dict[str, Any]:
    evidence = [item for answer in output.answers for item in answer.evidence]
    evidence.extend(item for risk in output.material_risks for item in risk.evidence)
    verified = 0
    invalid_pages = 0
    failures: list[dict[str, Any]] = []
    for item in evidence:
        if item.pdf_page > len(pages):
            invalid_pages += 1
            failures.append(
                {
                    "page": item.pdf_page,
                    "quote": item.quote,
                    "reason": "page_out_of_range",
                }
            )
            continue
        quote = _normalise_for_match(item.quote)
        page = _normalise_for_match(pages[item.pdf_page - 1])
        if quote and quote in page:
            verified += 1
        else:
            failures.append(
                {
                    "page": item.pdf_page,
                    "quote": item.quote,
                    "reason": "quote_not_found",
                }
            )
    found = sum(answer.status == "found" for answer in output.answers)
    return {
        "answers": len(output.answers),
        "found_answers": found,
        "evidence_items": len(evidence),
        "verified_evidence": verified,
        "evidence_precision": verified / len(evidence) if evidence else None,
        "invalid_pages": invalid_pages,
        "evidence_failures": failures,
    }


async def run_text_sections(
    pdf_path: Path,
    *,
    model: str,
    retries: int,
    selected_sections: set[str] | None,
    document_name: str | None = None,
    concurrency: int = 3,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> list[dict[str, Any]]:
    logger.info("Reading PDF text: file=%s", document_name or pdf_path.name)
    pages = extract_pages(pdf_path)
    windows = section_windows(pdf_path)
    display_name = document_name or pdf_path.name
    found_sections = sorted(windows)
    logger.info(
        "PDF text ready: file=%s pages=%d sections=%s",
        display_name,
        len(pages),
        ",".join(found_sections),
    )
    selected_windows = [
        (section_name, spec, start_page, content)
        for section_name, (spec, start_page, content) in windows.items()
        if not selected_sections or section_name in selected_sections
    ]
    limit = min(concurrency, len(selected_windows)) if selected_windows else 1
    semaphore = asyncio.Semaphore(limit)
    logger.info(
        "Section extraction plan: file=%s sections=%d concurrency=%d",
        display_name,
        len(selected_windows),
        limit,
    )
    completed_sections = 0

    async def extract_section(
        section_name: str,
        spec: SectionSpec,
        start_page: int,
        content: str,
    ) -> dict[str, Any]:
        nonlocal completed_sections
        async with semaphore:
            agent = make_agent(
                model,
                retries,
                expected_question_ids=question_ids(spec),
            )
            logger.info(
                "Section extraction started: file=%s section=%s "
                "start_page=%d characters=%d",
                display_name,
                section_name,
                start_page,
                len(content),
            )
            started = time.monotonic()
            result = await agent.run(build_section_prompt(display_name, spec, content))
            elapsed = time.monotonic() - started
            scoring = score_evidence(result.output, pages)
            usage = _json_safe(result.usage)
            logger.info(
                "Section extraction complete: file=%s section=%s elapsed=%.1fs "
                "requests=%d evidence=%d/%d",
                display_name,
                section_name,
                elapsed,
                usage.get("requests", 0),
                scoring["verified_evidence"],
                scoring["evidence_items"],
            )
            completed_sections += 1
            if progress_callback is not None:
                progress_callback(
                    section_name,
                    completed_sections,
                    len(selected_windows),
                )
            return {
                "document": display_name,
                "strategy": "text_sections",
                "model": model,
                "section": section_name,
                "start_pdf_page": start_page,
                "input_characters": len(content),
                "elapsed_seconds": round(elapsed, 3),
                "usage": usage,
                "score": scoring,
                "output": result.output.model_dump(mode="json"),
            }

    tasks: list[asyncio.Task[dict[str, Any]]] = []
    async with asyncio.TaskGroup() as task_group:
        for section_name, spec, start_page, content in selected_windows:
            tasks.append(
                task_group.create_task(
                    extract_section(section_name, spec, start_page, content)
                )
            )
    return [task.result() for task in tasks]


async def run_whole_pdf(
    pdf_path: Path,
    *,
    model: str,
    retries: int,
) -> list[dict[str, Any]]:
    pages = extract_pages(pdf_path)
    approx_text_tokens = sum(len(page) for page in pages) // 4
    if approx_text_tokens > 500_000:
        return [
            {
                "document": pdf_path.name,
                "strategy": "whole_pdf",
                "model": model,
                "status": "skipped",
                "reason": (
                    "The text estimate is more than 500,000 tokens. "
                    "PDF page images can add more tokens."
                ),
                "approx_text_tokens": approx_text_tokens,
            }
        ]

    specs = [item[0] for item in section_windows(pdf_path).values()]
    questions = "\n".join(
        f"- {question}" for spec in specs for question in spec.questions
    )
    expected_ids = {question_id for spec in specs for question_id in question_ids(spec)}
    agent = make_agent(
        model,
        retries,
        expected_question_ids=expected_ids,
    )
    prompt = f"""\
Document: {pdf_path.name}
Section: whole_document

Read the attached PDF. Answer these questions:
{questions}
"""
    started = time.monotonic()
    result = await agent.run(
        [
            prompt,
            BinaryContent(
                data=pdf_path.read_bytes(),
                media_type="application/pdf",
                identifier=pdf_path.name,
                vendor_metadata={"detail": "low"},
            ),
        ]
    )
    elapsed = time.monotonic() - started
    return [
        {
            "document": pdf_path.name,
            "strategy": "whole_pdf",
            "model": model,
            "section": "whole_document",
            "approx_text_tokens": approx_text_tokens,
            "elapsed_seconds": round(elapsed, 3),
            "usage": _json_safe(result.usage),
            "score": score_evidence(result.output, pages),
            "output": result.output.model_dump(mode="json"),
        }
    ]


def inspect_document(pdf_path: Path) -> dict[str, Any]:
    pages = extract_pages(pdf_path)
    windows = section_windows(pdf_path)
    return {
        "document": pdf_path.name,
        "bytes": pdf_path.stat().st_size,
        "pages": len(pages),
        "extracted_characters": sum(len(page) for page in pages),
        "approx_text_tokens": sum(len(page) for page in pages) // 4,
        "sections": {
            name: {
                "start_pdf_page": start_page,
                "window_pages": spec.max_pages,
                "characters": len(content),
                "questions": list(spec.questions),
            }
            for name, (spec, start_page, content) in windows.items()
        },
    }


def write_run(records: list[dict[str, Any]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"benchmark-{stamp}.json"
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


async def benchmark(
    pdf_paths: list[Path],
    *,
    model: str,
    retries: int,
    selected_sections: set[str] | None,
    strategy: str,
    output_dir: Path,
) -> Path:
    settings = get_settings()
    settings.require_openai_api_key()
    records: list[dict[str, Any]] = []
    for pdf_path in pdf_paths:
        if strategy in {"text_sections", "both"}:
            records.extend(
                await run_text_sections(
                    pdf_path,
                    model=model,
                    retries=retries,
                    selected_sections=selected_sections,
                    concurrency=settings.rhp_section_concurrency,
                )
            )
        if strategy in {"whole_pdf", "both"}:
            records.extend(await run_whole_pdf(pdf_path, model=model, retries=retries))
    return write_run(records, output_dir)


def run_benchmark(**kwargs: Any) -> Path:
    return asyncio.run(benchmark(**kwargs))
