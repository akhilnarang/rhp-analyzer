from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UsageSummary(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    requests: int = 0
    reported_cost_usd: str | None = None


class CacheMetadata(BaseModel):
    extraction_hit: bool
    report_hit: bool
    extraction_key: str
    report_key: str


class PdfMetadata(BaseModel):
    filename: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=1)


class AnalysisMetadata(BaseModel):
    model: str
    sections: list[str]
    extraction_prompt_version: str
    report_prompt_version: str
    created_at: str
    elapsed_seconds: float = Field(ge=0)
    usage: UsageSummary
    current_request_usage: UsageSummary


class AnalysisResponse(BaseModel):
    analysis_id: str
    pdf: PdfMetadata
    cache: CacheMetadata
    report_markdown: str
    section_records: list[dict[str, Any]]
    metadata: AnalysisMetadata


class AnalysisLinkResponse(BaseModel):
    url: str


class AnalysisStatusResponse(BaseModel):
    analysis_id: str
    status: str
    stage: str
    message: str
    completed_sections: int = Field(ge=0)
    total_sections: int = Field(ge=1)
    error: str | None = None
    created_at: str
    updated_at: str


class HealthResponse(BaseModel):
    status: str = "ok"
