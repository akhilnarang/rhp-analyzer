import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from rhp_analyzer.api import UnsafeRemoteAddress, create_app, resolve_public_addresses
from rhp_analyzer.cache import CacheStore
from rhp_analyzer.config import Settings
from rhp_analyzer.service import AnalysisService
from rhp_analyzer.web import analysis_title


class FakePipeline:
    def __init__(self) -> None:
        self.extractions = 0
        self.syntheses = 0
        self.market_data: list[dict[str, str]] = []
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def extract(
        self, _pdf_path: Path, **kwargs: object
    ) -> list[dict[str, object]]:
        self.extractions += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return [
            {
                "document": kwargs["document_name"],
                "section": "offer",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "requests": 1,
                    "cost": "0.01",
                    "details": {"reasoning_tokens": 3},
                },
                "score": {
                    "evidence_failures": [],
                    "evidence_items": 1,
                    "verified_evidence": 1,
                },
                "output": {"answers": [], "material_risks": []},
            }
        ]

    async def synthesize(
        self,
        _records: list[dict[str, object]],
        **kwargs: object,
    ) -> tuple[str, dict[str, object]]:
        self.syntheses += 1
        self.market_data.append(kwargs.get("market_data", {}))
        return (
            f"# Report\n\nModel: {kwargs['model']}",
            {
                "input_tokens": 50,
                "output_tokens": 10,
                "requests": 1,
                "cost": "0.02",
                "details": {"reasoning_tokens": 2},
            },
        )


class ApiTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        database = Path(self.temporary.name) / "cache.sqlite3"
        cache = CacheStore(database)
        cache.initialize()
        self.pipeline = FakePipeline()
        service = AnalysisService(
            cache,
            extractor=self.pipeline.extract,
            synthesizer=self.pipeline.synthesize,
        )
        settings = Settings(
            openai_api_key=SecretStr("unused-test-key"),
            rhp_database_path=database,
            _env_file=None,
        )
        self.app = create_app(settings, service)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_duplicate_pdf_uses_both_cache_layers(self) -> None:
        async def scenario() -> None:
            payload = b"%PDF-1.4\nminimal test content\n%%EOF"
            form = {"sections": "offer"}
            async with AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                first = await client.post(
                    "/v1/analyze",
                    files={"file": ("issuer.pdf", payload, "application/pdf")},
                    data=form,
                )
                self.assertEqual(first.status_code, 202)
                first_body = first.json()
                self.assertEqual(first.headers["location"], first_body["url"])
                analysis_id = first_body["url"].rsplit("/", 1)[-1]
                for _ in range(20):
                    status_response = await client.get(
                        f"/v1/analyses/{analysis_id}/status"
                    )
                    if status_response.json()["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("The analysis job did not finish.")

                second = await client.post(
                    "/v1/analyze",
                    files={"file": ("renamed.pdf", payload, "application/pdf")},
                    data=form,
                )
                self.assertEqual(second.status_code, 202)
                second_body = second.json()
                self.assertEqual(second_body["url"], first_body["url"])
                self.assertEqual(self.pipeline.extractions, 1)
                self.assertEqual(self.pipeline.syntheses, 1)

                fetched = await client.get(f"/v1/analyses/{analysis_id}")
                self.assertEqual(fetched.status_code, 200)
                self.assertEqual(fetched.json()["analysis_id"], analysis_id)

                page = await client.get(first_body["url"])
                self.assertEqual(page.status_code, 200)
                self.assertIn("<h1>Report</h1>", page.text)

                analysis_list = await client.get("/analysis")
                self.assertEqual(analysis_list.status_code, 200)
                self.assertIn("Report", analysis_list.text)
                self.assertIn("Ready", analysis_list.text)
                self.assertIn("View analysis", analysis_list.text)
                self.assertIn(f"/analysis/{analysis_id}", analysis_list.text)

        asyncio.run(scenario())

    def test_analysis_link_returns_before_the_job_finishes(self) -> None:
        async def scenario() -> None:
            self.pipeline.started = asyncio.Event()
            self.pipeline.release = asyncio.Event()
            payload = b"%PDF-1.4\nminimal test content\n%%EOF"
            async with AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/analyze",
                    files={"file": ("issuer.pdf", payload, "application/pdf")},
                    data={"sections": "offer"},
                )
                self.assertEqual(response.status_code, 202)
                await asyncio.wait_for(self.pipeline.started.wait(), timeout=1)
                page = await client.get(response.json()["url"])
                self.assertIn("Analysis in progress", page.text)
                analysis_id = response.json()["url"].rsplit("/", 1)[-1]
                current = await client.get(f"/v1/analyses/{analysis_id}/status")
                self.assertEqual(current.json()["status"], "running")
                self.pipeline.release.set()
                for _ in range(20):
                    current = await client.get(f"/v1/analyses/{analysis_id}/status")
                    if current.json()["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(current.json()["status"], "completed")

        asyncio.run(scenario())

    def test_non_pdf_is_rejected(self) -> None:
        async def scenario() -> None:
            async with AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/analyze",
                    files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
                )
            self.assertEqual(response.status_code, 415)

        asyncio.run(scenario())

    def test_remote_pdf_url_creates_an_analysis_job(self) -> None:
        async def scenario() -> None:
            remote_pdf = Path(self.temporary.name) / "remote.pdf"
            payload = b"%PDF-1.4\nremote test content\n%%EOF"
            remote_pdf.write_bytes(payload)
            fetch = AsyncMock(
                return_value=(
                    remote_pdf,
                    "a" * 64,
                    len(payload),
                    "Offer Document.pdf",
                )
            )
            with patch("rhp_analyzer.api.persist_remote_pdf", fetch):
                async with AsyncClient(
                    transport=ASGITransport(app=self.app),
                    base_url="http://test",
                ) as client:
                    response = await client.post(
                        "/v1/analyze",
                        data={
                            "url": "https://www.bseindia.com/downloads/ipo/document.pdf",
                            "sections": "offer",
                        },
                    )
                    self.assertEqual(response.status_code, 202)
                    analysis_id = response.json()["url"].rsplit("/", 1)[-1]
                    for _ in range(20):
                        current = await client.get(f"/v1/analyses/{analysis_id}/status")
                        if current.json()["status"] == "completed":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(current.json()["status"], "completed")
            fetch.assert_awaited_once()

        asyncio.run(scenario())

    def test_analyze_requires_exactly_one_pdf_source(self) -> None:
        async def scenario() -> None:
            payload = b"%PDF-1.4\ntest\n%%EOF"
            async with AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                missing = await client.post(
                    "/v1/analyze",
                    data={"sections": "offer"},
                )
                both = await client.post(
                    "/v1/analyze",
                    files={"file": ("issuer.pdf", payload, "application/pdf")},
                    data={
                        "url": "https://www.bseindia.com/document.pdf",
                        "sections": "offer",
                    },
                )
            self.assertEqual(missing.status_code, 422)
            self.assertEqual(both.status_code, 422)

        asyncio.run(scenario())

    def test_only_analyze_post_requires_bearer_token(self) -> None:
        async def scenario() -> None:
            self.app.state.settings.rhp_api_tokens = SecretStr(
                "token-one,token-two"
            )
            payload = b"%PDF-1.4\ntest\n%%EOF"
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=self.app),
                    base_url="http://test",
                ) as client:
                    missing = await client.post(
                        "/v1/analyze",
                        files={
                            "file": ("issuer.pdf", payload, "application/pdf")
                        },
                        data={"sections": "offer"},
                    )
                    wrong = await client.post(
                        "/v1/analyze",
                        headers={"Authorization": "Bearer wrong-token"},
                        files={
                            "file": ("issuer.pdf", payload, "application/pdf")
                        },
                        data={"sections": "offer"},
                    )
                    allowed = await client.post(
                        "/v1/analyze",
                        headers={"Authorization": "Bearer token-two"},
                        files={
                            "file": ("issuer.pdf", payload, "application/pdf")
                        },
                        data={"sections": "offer"},
                    )
                    public_page = await client.get("/analysis")
                    public_health = await client.get("/health")
            finally:
                self.app.state.settings.rhp_api_tokens = None
            self.assertEqual(missing.status_code, 401)
            self.assertEqual(missing.headers["www-authenticate"], "Bearer")
            self.assertEqual(wrong.status_code, 401)
            self.assertEqual(allowed.status_code, 202)
            self.assertEqual(public_page.status_code, 200)
            self.assertEqual(public_health.status_code, 200)

        asyncio.run(scenario())

    def test_remote_pdf_host_must_not_resolve_to_a_private_address(self) -> None:
        async def scenario() -> None:
            answer = (
                2,
                1,
                6,
                "",
                ("127.0.0.1", 80),
            )
            loop = asyncio.get_running_loop()
            with (
                patch.object(
                    loop,
                    "getaddrinfo",
                    AsyncMock(return_value=[answer]),
                ),
                self.assertRaisesRegex(UnsafeRemoteAddress, "public internet"),
            ):
                await resolve_public_addresses("internal.example", 80)

        asyncio.run(scenario())

    def test_market_data_changes_only_the_report_cache_key(self) -> None:
        async def wait_for_report(client: AsyncClient, url: str) -> dict[str, object]:
            analysis_id = url.rsplit("/", 1)[-1]
            for _ in range(20):
                response = await client.get(f"/v1/analyses/{analysis_id}/status")
                if response.json()["status"] == "completed":
                    result = await client.get(f"/v1/analyses/{analysis_id}")
                    return result.json()
                await asyncio.sleep(0.01)
            self.fail("The analysis job did not finish.")

        async def scenario() -> None:
            payload = b"%PDF-1.4\nmarket data cache test\n%%EOF"
            async with AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                first = await client.post(
                    "/v1/analyze",
                    files={"file": ("issuer.pdf", payload, "application/pdf")},
                    data={
                        "sections": "offer",
                        "lot_size": "1200",
                        "price": "₹94",
                        "gmp": "₹39",
                        "gmp_percent": "41.49%",
                        "qib_subscription": "94.62x",
                    },
                )
                first_result = await wait_for_report(client, first.json()["url"])
                second = await client.post(
                    "/v1/analyze",
                    files={"file": ("issuer.pdf", payload, "application/pdf")},
                    data={
                        "sections": "offer",
                        "lot_size": "1200",
                        "price": "₹94",
                        "gmp": "₹40",
                        "gmp_percent": "42.55%",
                        "qib_subscription": "94.62x",
                    },
                )
                second_result = await wait_for_report(client, second.json()["url"])
            self.assertNotEqual(first.json()["url"], second.json()["url"])
            self.assertEqual(self.pipeline.extractions, 1)
            self.assertEqual(self.pipeline.syntheses, 2)
            self.assertEqual(self.pipeline.market_data[0]["gmp"], "₹39")
            self.assertEqual(self.pipeline.market_data[1]["gmp"], "₹40")
            self.assertEqual(
                first_result["metadata"]["market_data"]["lot_size"], "1200"
            )
            self.assertEqual(second_result["metadata"]["market_data"]["gmp"], "₹40")

        asyncio.run(scenario())

    def test_health_route_is_available(self) -> None:
        async def scenario() -> None:
            async with AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                health = await client.get("/health")
            self.assertEqual(health.status_code, 200)

        asyncio.run(scenario())

    def test_home_page_links_to_docs_and_analysis_list(self) -> None:
        async def scenario() -> None:
            async with AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                home = await client.get("/")
                analysis_list = await client.get("/analysis")
            self.assertEqual(home.status_code, 200)
            self.assertIn('href="http://test/docs"', home.text)
            self.assertIn('href="http://test/analysis"', home.text)
            self.assertIn("https://oat.ink/oat.min.css", home.text)
            self.assertEqual(analysis_list.status_code, 200)
            self.assertIn("No analyses are available", analysis_list.text)

        asyncio.run(scenario())

    def test_analysis_title_uses_the_company_name(self) -> None:
        job = {
            "filename": "Registration_21072025200223_MeridianDRHP.pdf",
            "report_markdown": (
                "# Milky Mist Dairy Food Limited — DRHP Research Report\n\n"
                "## Executive Summary"
            ),
        }
        self.assertEqual(analysis_title(job), "Milky Mist Dairy Food Limited")

    def test_openapi_has_the_analysis_routes(self) -> None:
        schema = self.app.openapi()
        self.assertIn("/v1/analyze", schema["paths"])
        self.assertIn("/v1/analyses/{analysis_id}", schema["paths"])
        self.assertIn("/v1/analyses/{analysis_id}/status", schema["paths"])
        self.assertIn("/analysis/{analysis_id}", schema["paths"])
        self.assertIn("RHPBearerAuth", schema["components"]["securitySchemes"])
        self.assertEqual(
            schema["paths"]["/v1/analyze"]["post"]["security"],
            [{"RHPBearerAuth": []}],
        )
        self.assertNotIn(
            "security",
            schema["paths"]["/v1/analyses/{analysis_id}"]["get"],
        )
        body_schema = schema["paths"]["/v1/analyze"]["post"]["requestBody"]["content"][
            "multipart/form-data"
        ]["schema"]
        body_schema = schema["components"]["schemas"][
            body_schema["$ref"].rsplit("/", 1)[-1]
        ]
        self.assertNotIn("model", body_schema["properties"])
        self.assertNotIn("force_refresh", body_schema["properties"])
        self.assertIn("gmp", body_schema["properties"])
        self.assertIn("qib_subscription", body_schema["properties"])
        response_schema = schema["paths"]["/v1/analyze"]["post"]["responses"]["202"][
            "content"
        ]["application/json"]["schema"]
        response_schema = schema["components"]["schemas"][
            response_schema["$ref"].rsplit("/", 1)[-1]
        ]
        self.assertEqual(set(response_schema["properties"]), {"url"})
