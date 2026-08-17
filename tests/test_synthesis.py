from unittest import TestCase

from rhp_analyzer.synthesis import (
    clean_company_name,
    company_name_from_records,
)


class CompanyNameTests(TestCase):
    def test_reads_a_company_name_with_checked_evidence(self) -> None:
        records = [
            {
                "score": {"evidence_failures": []},
                "output": {
                    "answers": [
                        {
                            "question_id": "company_name",
                            "status": "found",
                            "answer": "Credent Connect N Care Limited",
                            "confidence": "high",
                            "evidence": [
                                {
                                    "pdf_page": 1,
                                    "quote": "CREDENT CONNECT N CARE LIMITED",
                                }
                            ],
                        }
                    ],
                    "material_risks": [],
                },
            }
        ]

        self.assertEqual(
            company_name_from_records(records),
            "Credent Connect N Care Limited",
        )

    def test_rejects_generic_titles(self) -> None:
        self.assertIsNone(clean_company_name("1. Executive Summary"))
        self.assertIsNone(clean_company_name("IPO"))
        self.assertIsNone(clean_company_name("IPO Research Report"))

    def test_removes_a_report_suffix(self) -> None:
        self.assertEqual(
            clean_company_name("Sham Foam Limited — IPO Research Report"),
            "Sham Foam Limited",
        )
