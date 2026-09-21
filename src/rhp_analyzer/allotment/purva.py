from __future__ import annotations

from ..api_schemas import (
    AllotmentIssue,
    AllotmentLookupResult,
    AllotmentOutcome,
    AllotmentRecord,
    ProviderName,
)
from .base import (
    AllotmentProvider,
    UpstreamChanged,
    make_result,
    outcome_from_allotment_text,
    parse_int,
)
from .html import hidden_input, select_options, table_rows, visible_text

_QUERY_URL = "https://www.purvashare.com/investor-service/ipo-query"
_SENTINELS = (
    "invalid pan",
    "please enter valid",
    "no record",
    "record not found",
    "not found",
    "no data",
)
_NAME_LABELS = {"name", "applicant name", "investor name", "shareholder name"}
_APPLICATION_LABELS = {
    "application no",
    "application number",
    "appl no",
    "appln no",
    "applicationno",
}
_DP_LABELS = {"dp id", "dp_id", "dpid", "dpclid", "dp client id", "beneficiary id"}
_CATEGORY_LABELS = {"category", "applicant category", "investor category"}
_APPLIED_LABELS = {
    "applied",
    "shares applied",
    "application shares",
    "no of shares applied",
    "applied shares",
}
_ALLOTTED_LABELS = {
    "allotted",
    "allotment",
    "shares allotted",
    "allotted shares",
    "no of shares allotted",
    "allotment shares",
}
_STATUS_LABELS = {"status", "allotment status", "allot status", "result"}


def parse_issues(html: str) -> list[AllotmentIssue]:
    options = select_options(html, select_name="company_id")
    return [_issue(value, label) for value, label in options if value and label]


def _issue(company_id: str, company_name: str) -> AllotmentIssue:
    return AllotmentIssue(
        issue_id=f"{ProviderName.PURVA.value}:{company_id}",
        provider=ProviderName.PURVA,
        provider_label="Purva Sharegistry",
        provider_issue_id=company_id,
        company_name=company_name,
        requires_challenge=False,
        status_page_url=_QUERY_URL,
    )


def csrf_token(html: str) -> str | None:
    return hidden_input(html, "csrfmiddlewaretoken")


def _normalize_label(value: str) -> str:
    return " ".join(value.strip().lower().rstrip(":").split())


def parse_lookup_html(html: str, issue: AllotmentIssue) -> AllotmentLookupResult:
    fields: dict[str, str] = {}
    status_text = ""
    for row in table_rows(html):
        if len(row) < 2:
            continue
        label = _normalize_label(row[0])
        value = " ".join(row[1].split())
        if not value:
            continue
        if label in _NAME_LABELS:
            fields["name"] = value
        elif label in _APPLICATION_LABELS:
            fields["application_no"] = value
        elif label in _DP_LABELS:
            fields["dp_id"] = value
        elif label in _CATEGORY_LABELS:
            fields["category"] = value
        elif label in _APPLIED_LABELS:
            fields["applied_shares"] = value
        elif label in _ALLOTTED_LABELS:
            fields["allotted_shares"] = value
        elif label in _STATUS_LABELS:
            status_text = value

    status_lowered = status_text.lower()
    if status_text and any(sentinel in status_lowered for sentinel in _SENTINELS):
        return make_result(
            AllotmentOutcome.NOT_FOUND,
            issue,
            "Purva Sharegistry has no application for this PAN in the selected issue.",
        )
    if not fields and not status_text:
        rendered_text = visible_text(html).lower()
        if any(sentinel in rendered_text for sentinel in _SENTINELS):
            return make_result(
                AllotmentOutcome.NOT_FOUND,
                issue,
                "Purva Sharegistry has no application for this PAN in the selected issue.",
            )
        raise UpstreamChanged(
            "Purva returned result markup this adapter does not recognize."
        )

    record = AllotmentRecord(
        application_no=fields.get("application_no"),
        name=fields.get("name"),
        dp_id=fields.get("dp_id"),
        category=fields.get("category"),
        applied_shares=parse_int(fields.get("applied_shares")),
        allotted_shares=parse_int(fields.get("allotted_shares")),
        remarks=status_text or None,
    )
    outcome = _outcome(record)
    if outcome is AllotmentOutcome.ALLOTTED:
        message = "Purva Sharegistry shows an allotment for this PAN."
    elif outcome is AllotmentOutcome.NOT_ALLOTTED:
        message = "Purva Sharegistry shows no allotment for this PAN."
    else:
        message = "Purva Sharegistry returned a record without a confirmed allotment."
    return make_result(
        outcome,
        issue,
        message,
        records=[record],
        allocated_shares=record.allotted_shares
        if outcome is AllotmentOutcome.ALLOTTED
        else None,
    )


def _outcome(record: AllotmentRecord) -> AllotmentOutcome:
    if record.allotted_shares is not None:
        return (
            AllotmentOutcome.ALLOTTED
            if record.allotted_shares > 0
            else AllotmentOutcome.NOT_ALLOTTED
        )
    return outcome_from_allotment_text(record.remarks)


class PurvaProvider(AllotmentProvider):
    name = ProviderName.PURVA
    label = "Purva Sharegistry"
    status_page_url = _QUERY_URL
    allowed_hosts = frozenset({"www.purvashare.com", "purvashare.com"})

    async def list_issues(self) -> list[AllotmentIssue]:
        async with self._client_factory() as client:
            response = await self._request(
                client, "GET", _QUERY_URL, retry_transient=True
            )
        return parse_issues(response.text)

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
            page = await self._request(client, "GET", _QUERY_URL, retry_transient=True)
            token = csrf_token(page.text)
            if token is None:
                raise UpstreamChanged(
                    "Purva did not include a CSRF token on its lookup page."
                )
            response = await self._request(
                client,
                "POST",
                _QUERY_URL,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": _QUERY_URL,
                },
                data={
                    "csrfmiddlewaretoken": token,
                    "company_id": issue.provider_issue_id,
                    "applicationNumber": "",
                    "panNumber": pan,
                },
            )
        return parse_lookup_html(response.text, issue)
