from __future__ import annotations

import asyncio
import json
from collections import Counter
from unittest import TestCase
from unittest.mock import patch

import httpx
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from rhp_analyzer.allotment.service import AllotmentService, build_providers
from rhp_analyzer.api import create_app
from rhp_analyzer.config import Settings

PAN = "AAAAA0000A"
KFIN_API_HOST = "0uz601ms56.execute-api.ap-south-1.amazonaws.com"

KFIN_HOME = '<script src="./static/js/main.abc123.js"></script>'
KFIN_BUNDLE = 'const issues=JSON.parse(\'[{"clientId":"101","name":"ACME LIMITED"}]\');'
KFIN_ALLOTTED = {
    "data": [
        {
            "Appln_No": "K-1",
            "Name": "TEST APPLICANT",
            "DP_CLID": "1200000000000001",
            "App_Shares": "100",
            "All_Shares": "50",
        }
    ]
}
MUFG_ISSUES = {
    "d": (
        "<NewDataSet><Table><company_id>202</company_id>"
        "<companyname>BRAVO LIMITED</companyname></Table></NewDataSet>"
    )
}
MUFG_ALLOTTED = {
    "d": (
        "<NewDataSet><Table><APPNO>M-1</APPNO><NAME1>TEST APPLICANT</NAME1>"
        "<SHARES>100</SHARES><ALLOT>50</ALLOT></Table></NewDataSet>"
    )
}
BIGSHARE_PAGE = (
    '<select id="ddlCompany"><option>--Select Company--</option>'
    '<option value="303">CHARLIE LIMITED</option></select>'
)
BIGSHARE_CAPTCHA = {
    "token": "captcha-token",
    "image": "data:image/png;base64,aW1hZ2U=",
}
BIGSHARE_ALLOTTED = {
    "d": {
        "Status": "OK",
        "APPLICATION_NO": "B-1",
        "Name": "TEST APPLICANT",
        "APPLIED": "100",
        "ALLOTED": "50",
    }
}
PURVA_PAGE = (
    '<input type="hidden" name="csrfmiddlewaretoken" value="csrf-token">'
    '<select name="company_id"><option value="404">DELTA LIMITED</option></select>'
)
PURVA_ALLOTTED = (
    "<table><tr><th>Application No</th><td>P-1</td></tr>"
    "<tr><th>Name</th><td>TEST APPLICANT</td></tr>"
    "<tr><th>Applied Shares</th><td>100</td></tr>"
    "<tr><th>Allotted Shares</th><td>50</td></tr></table>"
)


class RegistrarStub:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.kfin_catalogue_status = 200
        self.kfin_lookup = KFIN_ALLOTTED
        self.bigshare_lookup: object = BIGSHARE_ALLOTTED
        self.bigshare_lookup_status = 200
        self.kfin_headers: httpx.Headers | None = None
        self.mufg_lookup_body: dict[str, object] = {}
        self.bigshare_lookup_body: dict[str, object] = {}
        self.purva_lookup_body: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path

        if host == "ipostatus.kfintech.com":
            self.calls["kfin_catalogue"] += 1
            if self.kfin_catalogue_status != 200:
                return httpx.Response(self.kfin_catalogue_status)
            if path.endswith(".js"):
                return httpx.Response(200, text=KFIN_BUNDLE)
            return httpx.Response(200, text=KFIN_HOME)
        if host == KFIN_API_HOST:
            self.calls["kfin_lookup"] += 1
            self.kfin_headers = request.headers
            return httpx.Response(200, json=self.kfin_lookup)
        if host == "in.mpms.mufg.com":
            if path.endswith("generateToken"):
                return httpx.Response(200, json={"d": "476604358"})
            if path.endswith("SearchOnPan"):
                self.mufg_lookup_body = json.loads(request.content)
                return httpx.Response(200, json=MUFG_ALLOTTED)
            return httpx.Response(200, json=MUFG_ISSUES)
        if host == "ipo.bigshareonline.com":
            if path.endswith("Captcha.ashx"):
                return httpx.Response(200, json=BIGSHARE_CAPTCHA)
            if path.endswith("FetchIpodetails"):
                self.calls["bigshare_lookup"] += 1
                self.bigshare_lookup_body = json.loads(request.content)
                return httpx.Response(
                    self.bigshare_lookup_status,
                    json=self.bigshare_lookup,
                )
            return httpx.Response(200, text=BIGSHARE_PAGE)
        if host in {"purvashare.com", "www.purvashare.com"}:
            if request.method == "POST":
                self.purva_lookup_body = dict(
                    httpx.QueryParams(request.content.decode())
                )
                return httpx.Response(200, text=PURVA_ALLOTTED)
            return httpx.Response(
                200,
                text=PURVA_PAGE,
                headers={"Set-Cookie": "csrftoken=csrf-token; Path=/"},
            )
        return httpx.Response(404)


def make_app(stub: RegistrarStub):
    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(stub),
            follow_redirects=False,
        )

    service = AllotmentService(build_providers(client_factory))
    settings = Settings(
        openai_api_key=SecretStr("unused-test-key"),
        _env_file=None,
    )
    return create_app(settings, allotment_service=service)


class AllotmentTests(TestCase):
    def test_catalogue_and_kfintech_lookup_hot_path(self) -> None:
        async def scenario() -> None:
            stub = RegistrarStub()
            async with AsyncClient(
                transport=ASGITransport(app=make_app(stub)),
                base_url="http://test",
            ) as client:
                page = await client.get("/allotment")
                catalogue = await client.get("/v1/allotment/issues")
                lookup = await client.post(
                    "/v1/allotment/lookup",
                    json={"issue_id": "kfintech:101", "pan": PAN},
                )

            self.assertEqual(page.status_code, 200)
            self.assertIn('id="allotment-form"', page.text)
            self.assertEqual(
                {issue["issue_id"] for issue in catalogue.json()["issues"]},
                {"kfintech:101", "mufg:202", "bigshare:303", "purva:404"},
            )
            self.assertEqual(lookup.json()["outcome"], "allotted")
            self.assertEqual(lookup.json()["allocated_shares"], 50)
            self.assertNotIn(PAN, lookup.text)
            self.assertEqual(lookup.headers["cache-control"], "no-store")
            self.assertEqual(stub.kfin_headers["reqparam"], PAN)  # type: ignore[index]
            self.assertEqual(stub.kfin_headers["client_id"], "101")  # type: ignore[index]

        asyncio.run(scenario())

    def test_mufg_lookup_sends_the_encrypted_session_token(self) -> None:
        async def scenario() -> None:
            stub = RegistrarStub()
            async with AsyncClient(
                transport=ASGITransport(app=make_app(stub)),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/allotment/lookup",
                    json={"issue_id": "mufg:202", "pan": PAN},
                )

            self.assertEqual(response.json()["outcome"], "allotted")
            self.assertEqual(stub.mufg_lookup_body["PAN"], PAN)
            self.assertEqual(stub.mufg_lookup_body["CHKVAL"], "1")
            self.assertEqual(
                stub.mufg_lookup_body["token"],
                "uNRcWuGKNe4zzwZHyIBQ6g==",
            )

        asyncio.run(scenario())

    def test_purva_lookup_preserves_csrf_and_result_values(self) -> None:
        async def scenario() -> None:
            stub = RegistrarStub()
            async with AsyncClient(
                transport=ASGITransport(app=make_app(stub)),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/allotment/lookup",
                    json={"issue_id": "purva:404", "pan": PAN},
                )

            self.assertEqual(response.json()["outcome"], "allotted")
            self.assertEqual(response.json()["records"][0]["application_no"], "P-1")
            self.assertEqual(
                stub.purva_lookup_body["csrfmiddlewaretoken"], "csrf-token"
            )
            self.assertEqual(stub.purva_lookup_body["panNumber"], PAN)

        asyncio.run(scenario())

    def test_bigshare_human_captcha_flow_and_pending_result(self) -> None:
        async def scenario() -> None:
            stub = RegistrarStub()
            async with AsyncClient(
                transport=ASGITransport(app=make_app(stub)),
                base_url="http://test",
            ) as client:
                blocked = await client.post(
                    "/v1/allotment/lookup",
                    json={"issue_id": "bigshare:303", "pan": PAN},
                )
                challenge = await client.post(
                    "/v1/allotment/challenge",
                    json={"issue_id": "bigshare:303"},
                )
                lookup = await client.post(
                    "/v1/allotment/lookup",
                    json={
                        "issue_id": "bigshare:303",
                        "pan": PAN,
                        "captcha_token": challenge.json()["challenge"]["token"],
                        "captcha_answer": "AB12",
                    },
                )

                stub.bigshare_lookup = {
                    "d": {"Status": "OK", "ALLOTED": "Allotment pending"}
                }
                pending = await client.post(
                    "/v1/allotment/lookup",
                    json={
                        "issue_id": "bigshare:303",
                        "pan": PAN,
                        "captcha_token": "fresh-token",
                        "captcha_answer": "CD34",
                    },
                )

            self.assertEqual(blocked.json()["outcome"], "challenge_required")
            self.assertTrue(
                challenge.json()["challenge"]["image_data_uri"].startswith("data:")
            )
            self.assertEqual(lookup.json()["outcome"], "allotted")
            self.assertEqual(stub.bigshare_lookup_body["CaptchaAnswer"], "CD34")
            self.assertEqual(pending.json()["outcome"], "pending")

        asyncio.run(scenario())

    def test_captcha_ocr_submits_read_digits_and_falls_back_on_a_miss(self) -> None:
        async def scenario() -> None:
            stub = RegistrarStub()
            with patch(
                "rhp_analyzer.allotment.bigshare.read_captcha_digits",
                side_effect=["654321", None],
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=make_app(stub)),
                    base_url="http://test",
                ) as client:
                    solved = await client.post(
                        "/v1/allotment/lookup",
                        json={"issue_id": "bigshare:303", "pan": PAN},
                    )
                    blocked = await client.post(
                        "/v1/allotment/lookup",
                        json={"issue_id": "bigshare:303", "pan": PAN},
                    )

            self.assertEqual(solved.json()["outcome"], "allotted")
            self.assertEqual(stub.bigshare_lookup_body["CaptchaToken"], "captcha-token")
            self.assertEqual(stub.bigshare_lookup_body["CaptchaAnswer"], "654321")
            self.assertEqual(blocked.json()["outcome"], "challenge_required")

        asyncio.run(scenario())

    def test_invalid_pan_is_rejected_without_echoing_it(self) -> None:
        async def scenario() -> None:
            value = "ABCDE1234"
            async with AsyncClient(
                transport=ASGITransport(app=make_app(RegistrarStub())),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/allotment/lookup",
                    json={"issue_id": "kfintech:101", "pan": value},
                )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(value, response.text)

        asyncio.run(scenario())

    def test_schema_drift_is_reported_instead_of_becoming_not_found(self) -> None:
        async def scenario() -> None:
            stub = RegistrarStub()
            stub.kfin_lookup = {"data": ["new upstream shape"]}
            async with AsyncClient(
                transport=ASGITransport(app=make_app(stub)),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/allotment/lookup",
                    json={"issue_id": "kfintech:101", "pan": PAN},
                )
            self.assertEqual(response.json()["outcome"], "upstream_changed")

        asyncio.run(scenario())

    def test_catalogue_failure_is_isolated_and_captcha_lookup_is_not_retried(
        self,
    ) -> None:
        async def scenario() -> None:
            stub = RegistrarStub()
            stub.kfin_catalogue_status = 503
            stub.bigshare_lookup_status = 503
            async with AsyncClient(
                transport=ASGITransport(app=make_app(stub)),
                base_url="http://test",
            ) as client:
                catalogue = await client.get("/v1/allotment/issues")
                lookup = await client.post(
                    "/v1/allotment/lookup",
                    json={
                        "issue_id": "bigshare:303",
                        "pan": PAN,
                        "captcha_token": "single-use-token",
                        "captcha_answer": "AB12",
                    },
                )

            self.assertEqual(stub.calls["kfin_catalogue"], 2)
            self.assertNotIn(
                "kfintech:101",
                {issue["issue_id"] for issue in catalogue.json()["issues"]},
            )
            kfintech = next(
                provider
                for provider in catalogue.json()["providers"]
                if provider["name"] == "kfintech"
            )
            self.assertFalse(kfintech["catalogue_available"])
            self.assertEqual(lookup.json()["outcome"], "provider_unavailable")
            self.assertEqual(stub.calls["bigshare_lookup"], 1)

        asyncio.run(scenario())
