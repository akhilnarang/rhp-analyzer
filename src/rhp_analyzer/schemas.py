from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Evidence(BaseModel):
    """A short source text that supports an answer."""

    pdf_page: int = Field(ge=1, description="Physical PDF page. The first page is 1.")
    printed_page: str | None = Field(
        default=None,
        description="Page number that is printed in the document, if it is visible.",
    )
    quote: str = Field(
        min_length=12,
        max_length=900,
        description="One continuous source quote. Do not join text with ellipses.",
    )


class Answer(BaseModel):
    question_id: str
    status: Literal["found", "not_found", "ambiguous"]
    answer: str = Field(min_length=1, max_length=2_000)
    normalized_value: str | None = None
    unit: str | None = None
    period_or_basis: str | None = None
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def require_support_for_found_answers(self) -> Answer:
        if self.status == "found" and not self.evidence:
            raise ValueError("A found answer must have evidence.")
        if self.normalized_value is not None and not self.unit:
            raise ValueError("A normalized value must have a unit.")
        return self


class MaterialRisk(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    explanation: str = Field(min_length=10, max_length=1_200)
    severity: Literal["high", "medium", "low"]
    company_specific: bool
    evidence: list[Evidence] = Field(min_length=1, max_length=3)


class SectionAnalysis(BaseModel):
    document_name: str
    section_name: str
    answers: list[Answer]
    material_risks: list[MaterialRisk] = Field(default_factory=list, max_length=15)
    unresolved_ambiguities: list[str] = Field(default_factory=list, max_length=20)
    source_warnings: list[str] = Field(default_factory=list, max_length=20)
