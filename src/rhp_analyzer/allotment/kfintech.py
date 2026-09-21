from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from ..api_schemas import (
    AllotmentIssue,
    AllotmentLookupResult,
    AllotmentOutcome,
    AllotmentRecord,
    ProviderName,
)
from .base import AllotmentProvider, UpstreamChanged, make_result, parse_int

_STATUS_PAGE = "https://ipostatus.kfintech.com/"
_API_URL = "https://0uz601ms56.execute-api.ap-south-1.amazonaws.com/prod/api/query"
_BUNDLE_PATTERN = re.compile(
    r"""(?:src=["'])?([^"'<>]*main\.[0-9a-f]+\.js)""",
    re.IGNORECASE,
)
_JSON_PARSE_PATTERN = re.compile(
    r"""JSON\.parse\(\s*(?P<quote>['"])(?P<body>(?:\\.|(?!\1).)*)(?P=quote)\s*\)""",
    re.DOTALL,
)


def extract_bundle_url(html: str, base_url: str) -> str | None:
    """Find the current hashed JavaScript bundle referenced by the page."""

    match = _BUNDLE_PATTERN.search(html)
    if match is None:
        return None
    path = match.group(1).strip()
    if not path:
        return None
    return urljoin(base_url, path)


def _decode_js_string(body: str) -> str:
    if "\\'" not in body:
        return body
    return body.replace("\\'", "'")


def parse_issue_catalogue(bundle: str) -> list[AllotmentIssue]:
    """Extract the embedded ``{clientId, name}`` issue list from the bundle.

    The bundle wraps the array in ``JSON.parse('...')``. We read the literal
    without evaluating the bundle and validate each row before using it.
    """

    issues: list[AllotmentIssue] = []
    for match in _JSON_PARSE_PATTERN.finditer(bundle):
        body = match.group("body")
        if "clientId" not in body:
            continue
        try:
            payload = json.loads(_decode_js_string(body))
        except ValueError:
            continue
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            client_id = row.get("clientId")
            name = row.get("name")
            if client_id is None or not str(name or "").strip():
                continue
            close_date = row.get("closeDate") or row.get("close_date")
            issues.append(
                _issue(
                    str(client_id).strip(),
                    " ".join(str(name).split()),
                    " ".join(str(close_date).split()) if close_date else None,
                )
            )
        if issues:
            break
    if not issues:
        raise UpstreamChanged(
            "KFintech did not expose an issue catalogue in its current bundle."
        )
    return issues


def _issue(client_id: str, name: str, close_date: str | None = None) -> AllotmentIssue:
    return AllotmentIssue(
        issue_id=f"{ProviderName.KFINTECH.value}:{client_id}",
        provider=ProviderName.KFINTECH,
        provider_label="KFintech",
        provider_issue_id=client_id,
        company_name=name,
        close_date=close_date,
        requires_challenge=False,
        status_page_url=_STATUS_PAGE,
    )


def _record(row: dict[str, Any]) -> AllotmentRecord:
    return AllotmentRecord(
        application_no=_text(row.get("Appln_No")),
        name=_text(row.get("Name")),
        dp_id=_text(row.get("DP_CLID")),
        category=_text(row.get("category")),
        applied_shares=parse_int(row.get("App_Shares")),
        allotted_shares=parse_int(row.get("All_Shares")),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


_KNOWN_FIELDS = (
    "Appln_No",
    "Name",
    "DP_CLID",
    "Pan_No",
    "App_Shares",
    "All_Shares",
    "category",
)


def _known_field(row: dict[str, Any]) -> bool:
    return any(field in row for field in _KNOWN_FIELDS)


def parse_lookup_payload(payload: Any, issue: AllotmentIssue) -> AllotmentLookupResult:
    """Normalize the ``{data: [...]}`` KFintech response."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise UpstreamChanged(
            "KFintech returned a lookup response without a data list."
        )
    raw_rows = payload["data"]
    if not raw_rows:
        return make_result(
            AllotmentOutcome.NOT_FOUND,
            issue,
            "KFintech has no application for this PAN in the selected issue.",
        )
    if any(not isinstance(row, dict) for row in raw_rows):
        raise UpstreamChanged("KFintech returned malformed applicant rows.")
    rows: list[dict[str, Any]] = raw_rows
    if not any(_known_field(row) for row in rows):
        raise UpstreamChanged(
            "KFintech returned applicant rows with unrecognized fields."
        )
    records = [_record(row) for row in rows]
    allotted = [
        record.allotted_shares
        for record in records
        if record.allotted_shares is not None and record.allotted_shares > 0
    ]
    explicit_zero = [
        record.allotted_shares for record in records if record.allotted_shares == 0
    ]
    if allotted:
        outcome = AllotmentOutcome.ALLOTTED
        message = "KFintech shows an allotment for this PAN."
    elif len(explicit_zero) == len(records):
        outcome = AllotmentOutcome.NOT_ALLOTTED
        message = "KFintech shows no allotment for this PAN."
    else:
        outcome = AllotmentOutcome.PENDING
        message = "KFintech returned a record without a confirmed allotment quantity."
    return make_result(
        outcome,
        issue,
        message,
        records=records,
        allocated_shares=sum(allotted) if allotted else None,
    )


class KFintechProvider(AllotmentProvider):
    name = ProviderName.KFINTECH
    label = "KFintech"
    status_page_url = _STATUS_PAGE
    allowed_hosts = frozenset(
        {
            "ipostatus.kfintech.com",
            "0uz601ms56.execute-api.ap-south-1.amazonaws.com",
        }
    )

    async def list_issues(self) -> list[AllotmentIssue]:
        async with self._client_factory() as client:
            page = await self._request(
                client, "GET", _STATUS_PAGE, retry_transient=True
            )
            bundle_url = extract_bundle_url(page.text, str(page.url))
            if bundle_url is None:
                raise UpstreamChanged("KFintech did not reference a JavaScript bundle.")
            bundle = await self._request(
                client, "GET", bundle_url, retry_transient=True
            )
        return parse_issue_catalogue(bundle.text)

    async def _lookup(
        self,
        issue: AllotmentIssue,
        pan: str,
        *,
        captcha_token: str | None,
        captcha_answer: str | None,
    ) -> AllotmentLookupResult:
        del captcha_token, captcha_answer
        async with self._client_factory() as client:
            response = await self._request(
                client,
                "GET",
                _API_URL,
                params={"type": "pan"},
                headers={
                    "reqparam": pan,
                    "client_id": issue.provider_issue_id,
                    "Accept": "application/json",
                },
                retry_transient=True,
            )
        payload = self._json(response)
        return parse_lookup_payload(payload, issue)
