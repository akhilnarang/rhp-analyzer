import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from rhp_analyzer.benchmark import (
    _validate_semantics,
    run_text_sections,
    score_evidence,
)
from rhp_analyzer.pdf_text import (
    SECTION_SPECS,
    locate_section,
    render_page_window,
    schedule_pages,
)
from rhp_analyzer.schemas import Answer, Evidence, SectionAnalysis


class PdfTextTests(TestCase):
    def test_locate_section_ignores_toc_and_late_references(self) -> None:
        spec = next(spec for spec in SECTION_SPECS if spec.name == "objects")
        pages = [
            "cover",
            "TABLE OF CONTENTS\nOBJECTS OF THE OFFER",
            "filler",
            "filler",
            "reference\n" + "\n".join(["line"] * 20) + "\nOBJECTS OF THE OFFER",
            "OBJECTS OF THE OFFER\nactual section",
        ]
        self.assertEqual(locate_section(pages, spec), 5)

    def test_render_page_window_adds_physical_page_markers(self) -> None:
        rendered = render_page_window(["one", "two", "three"], 1, 2)
        self.assertIn("[PDF_PAGE_2]", rendered)
        self.assertIn("[PDF_PAGE_3]", rendered)

    def test_schedule_pages_finds_detailed_calendar(self) -> None:
        pages = [
            "cover: issue opens on Monday and closes on Wednesday",
            (
                "Bid/Offer opens on Monday\nBid/Offer closes on Wednesday\n"
                "Finalisation of Basis of Allotment on Thursday"
            ),
        ]
        self.assertEqual(schedule_pages(pages), [1])


class EvidenceScoringTests(TestCase):
    def test_exact_normalized_quote_is_verified(self) -> None:
        output = SectionAnalysis(
            document_name="example.pdf",
            section_name="offer",
            answers=[
                Answer(
                    question_id="offer_structure",
                    status="found",
                    answer="Fresh issue is ₹100 crore.",
                    confidence="high",
                    evidence=[Evidence(pdf_page=2, quote="Fresh issue is ₹ 100 crore")],
                )
            ],
        )
        score = score_evidence(output, ["cover", "The Fresh Issue is ₹100 crore."])
        self.assertEqual(score["verified_evidence"], 1)
        self.assertEqual(score["evidence_precision"], 1.0)

    def test_non_contiguous_quote_is_rejected_before_scoring(self) -> None:
        output = SectionAnalysis(
            document_name="example.pdf",
            section_name="offer",
            answers=[
                Answer(
                    question_id="offer_structure",
                    status="found",
                    answer="Fresh issue is ₹100 crore.",
                    confidence="high",
                    evidence=[Evidence(pdf_page=2, quote="Fresh issue ... ₹100 crore")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "continuous source text"):
            _validate_semantics(output)


class ConcurrentExtractionTests(TestCase):
    def test_section_concurrency_is_bounded_and_order_is_stable(self) -> None:
        state = {"active": 0, "peak": 0}

        class FakeAgent:
            async def run(self, _prompt: str) -> SimpleNamespace:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                await asyncio.sleep(0.01)
                state["active"] -= 1
                return SimpleNamespace(
                    output=SectionAnalysis(
                        document_name="example.pdf",
                        section_name="test",
                        answers=[],
                    ),
                    usage={"requests": 1},
                )

        specs = SECTION_SPECS[:3]
        windows = {
            spec.name: (spec, index + 1, f"content {index}")
            for index, spec in enumerate(specs)
        }
        with (
            patch("rhp_analyzer.benchmark.extract_pages", return_value=["page"]),
            patch("rhp_analyzer.benchmark.section_windows", return_value=windows),
            patch("rhp_analyzer.benchmark.make_agent", return_value=FakeAgent()),
        ):
            records = asyncio.run(
                run_text_sections(
                    Path("example.pdf"),
                    model="test-model",
                    retries=0,
                    selected_sections=None,
                    concurrency=2,
                )
            )

        self.assertEqual(state["peak"], 2)
        self.assertEqual(
            [record["section"] for record in records],
            [spec.name for spec in specs],
        )
