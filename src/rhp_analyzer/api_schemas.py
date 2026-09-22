from __future__ import annotations

from enum import StrEnum
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
    company_name: str | None = None
    model: str
    sections: list[str]
    extraction_prompt_version: str
    report_prompt_version: str
    created_at: str
    elapsed_seconds: float = Field(ge=0)
    usage: UsageSummary
    current_request_usage: UsageSummary
    market_data: dict[str, str] = Field(default_factory=dict)


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


class ProviderName(StrEnum):
    KFINTECH = "kfintech"
    MUFG = "mufg"
    BIGSHARE = "bigshare"
    PURVA = "purva"


class AllotmentOutcome(StrEnum):
    """Normalized applicant allotment outcomes.

    ``allotted`` and ``not_allotted`` are confirmed applicant results.
    ``not_found`` means the registrar holds no matching application.
    ``pending`` means the registrar returned rows without a usable allotment
    quantity yet. The remaining values describe lookup failures that callers
    should surface rather than treat as an allotment decision.
    """

    ALLOTTED = "allotted"
    NOT_ALLOTTED = "not_allotted"
    NOT_FOUND = "not_found"
    PENDING = "pending"
    CHALLENGE_REQUIRED = "challenge_required"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UPSTREAM_CHANGED = "upstream_changed"
    UPSTREAM_ERROR = "upstream_error"
    AMBIGUOUS = "ambiguous"
    NOT_SUPPORTED = "not_supported"


class AllotmentIssue(BaseModel):
    """An IPO issue listed by a registrar."""

    issue_id: str
    provider: ProviderName
    provider_label: str
    provider_issue_id: str
    company_name: str
    close_date: str | None = None
    requires_challenge: bool = False
    status_page_url: str


class AllotmentProviderStatus(BaseModel):
    name: ProviderName
    label: str
    status_page_url: str
    requires_challenge: bool
    catalogue_available: bool = True
    error: str | None = None


class AllotmentRecord(BaseModel):
    """One applicant row from a registrar response. Applicant PAN is omitted."""

    application_no: str | None = None
    name: str | None = None
    dp_id: str | None = None
    category: str | None = None
    applied_shares: int | None = None
    allotted_shares: int | None = None
    remarks: str | None = None


class AllotmentChallenge(BaseModel):
    token: str
    image_data_uri: str
    hint: str = "Enter the characters shown in the image."
    expires_in_seconds: int | None = None


class AllotmentLookupResult(BaseModel):
    outcome: AllotmentOutcome
    provider: ProviderName | None = None
    issue: AllotmentIssue | None = None
    message: str = ""
    records: list[AllotmentRecord] = Field(default_factory=list)
    allocated_shares: int | None = None
    retry_after_seconds: int | None = None
    candidates: list[AllotmentIssue] = Field(default_factory=list)
    challenge: AllotmentChallenge | None = None
    status_page_url: str | None = None


class AllotmentIssueListResponse(BaseModel):
    issues: list[AllotmentIssue] = Field(default_factory=list)
    providers: list[AllotmentProviderStatus] = Field(default_factory=list)


class AllotmentChallengeRequest(BaseModel):
    issue_id: str | None = Field(default=None, max_length=200)
    query: str | None = Field(default=None, max_length=200)


class AllotmentLookupRequest(BaseModel):
    issue_id: str | None = Field(default=None, max_length=200)
    query: str | None = Field(default=None, max_length=200)
    # The service normalizes and validates the PAN in one place. Keeping it a
    # plain string here avoids echoing a PAN in a validation error.
    pan: str = Field(max_length=64)
    captcha_token: str | None = Field(default=None, max_length=4000)
    captcha_answer: str | None = Field(default=None, max_length=32)
