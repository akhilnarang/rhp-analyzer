from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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
    UpstreamResponseError,
    make_result,
    parse_int,
)

_STATUS_PAGE = "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"
_BASE = "https://in.mpms.mufg.com/Initial_Offer"
_GET_DETAILS = f"{_BASE}/IPO.aspx/GetDetails"
_GENERATE_TOKEN = f"{_BASE}/IPO.aspx/generateToken"
_SEARCH_ON_PAN = f"{_BASE}/IPO.aspx/SearchOnPan"
_JSON_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json",
}
_NAME_FIELDS = ("NAME1", "Name", "NAME")
_ALLOT_FIELDS = ("ALLOT", "Allot", "Allotted")
_SHARES_FIELDS = ("SHARES", "Shares", "App_Shares")
_APP_NO_FIELDS = ("APPNO", "APP_NO", "ApplicationNo", "APPLICATION_NO")
_CLOSE_DATE_FIELDS = (
    "close_date",
    "closedate",
    "issue_close_date",
    "CLOSE_DATE",
)
_NO_RECORD_MARKERS = (
    "no record",
    "record not found",
    "not found",
    "invalid pan",
    "no data",
    "does not exist",
    "no details",
    "enter valid",
)
_TOKEN_KEY = b"8080808080808080"
_TOKEN_IV = b"8080808080808080"


def mufg_token(value: str) -> str:
    """Encrypt the session token as the MUFG page does with CryptoJS."""

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(value.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_TOKEN_KEY), modes.CBC(_TOKEN_IV)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode()


def parse_issue_catalogue(payload: Any) -> list[AllotmentIssue]:
    xml_text = _xml_field(payload)
    root = _parse_xml(xml_text)
    issues: list[AllotmentIssue] = []
    for table in root.iter("Table"):
        company_id = _child_text(table, "company_id")
        company_name = _child_text(table, "companyname")
        if company_id is None or company_name is None:
            continue
        issues.append(
            AllotmentIssue(
                issue_id=f"{ProviderName.MUFG.value}:{company_id}",
                provider=ProviderName.MUFG,
                provider_label="MUFG / Link Intime",
                provider_issue_id=company_id,
                company_name=company_name,
                close_date=_first_child_text(table, _CLOSE_DATE_FIELDS),
                requires_challenge=False,
                status_page_url=_STATUS_PAGE,
            )
        )
    if not issues:
        raise UpstreamChanged("MUFG did not return an issue catalogue.")
    return issues


def parse_token(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise UpstreamChanged("MUFG did not return a token response.")
    token = payload.get("d")
    if token is None or not str(token).strip():
        raise UpstreamChanged("MUFG returned an empty lookup token.")
    return str(token).strip()


def parse_lookup_xml(xml_text: str, issue: AllotmentIssue) -> AllotmentLookupResult:
    root = _parse_xml(xml_text)
    for text in _message_texts(root):
        lowered = text.lower()
        if "captcha" in lowered:
            return make_result(
                AllotmentOutcome.CHALLENGE_REQUIRED,
                issue,
                "MUFG requires a CAPTCHA answer for this lookup.",
            )
        if any(marker in lowered for marker in _NO_RECORD_MARKERS):
            return make_result(AllotmentOutcome.NOT_FOUND, issue, text)
        raise UpstreamResponseError(f"MUFG returned an error: {text}")
    tables = [table for table in root.iter("Table") if len(list(table)) > 0]
    records: list[AllotmentRecord] = []
    for table in tables:
        fields = {child.tag: (child.text or "").strip() for child in table}
        if any(
            key in fields
            for key in (*_NAME_FIELDS, *_ALLOT_FIELDS, *_SHARES_FIELDS, *_APP_NO_FIELDS)
        ):
            records.append(_record(fields))
    if not records:
        if tables:
            raise UpstreamChanged(
                "MUFG returned applicant rows with unrecognized fields."
            )
        return make_result(
            AllotmentOutcome.NOT_FOUND,
            issue,
            "MUFG has no application for this PAN in the selected issue.",
        )
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
        message = "MUFG shows an allotment for this PAN."
    elif len(explicit_zero) == len(records):
        outcome = AllotmentOutcome.NOT_ALLOTTED
        message = "MUFG shows no allotment for this PAN."
    else:
        outcome = AllotmentOutcome.PENDING
        message = "MUFG returned a record without a confirmed allotment quantity."
    return make_result(
        outcome,
        issue,
        message,
        records=records,
        allocated_shares=sum(allotted) if allotted else None,
    )


def _record(fields: dict[str, str]) -> AllotmentRecord:
    awarded_text = _first(fields, _ALLOT_FIELDS)
    return AllotmentRecord(
        application_no=_first(fields, _APP_NO_FIELDS),
        name=_first(fields, _NAME_FIELDS),
        applied_shares=parse_int(_first(fields, _SHARES_FIELDS)),
        allotted_shares=parse_int(awarded_text),
        remarks=_non_numeric(fields, ("PEMNDG", "RFNDAMT", "Remarks")),
    )


def _message_texts(root: ET.Element) -> list[str]:
    """Return the text of upstream error/message elements only.

    Challenge and not-found detection must never look at applicant row text,
    which can legitimately contain those words.
    """

    texts: list[str] = []
    for element in root.iter():
        if element.tag.lower() not in {"msg", "message"}:
            continue
        text = (element.text or "").strip()
        if text:
            texts.append(text)
    return texts


def _xml_field(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise UpstreamChanged("MUFG returned an unexpected response shape.")
    xml_text = payload.get("d")
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise UpstreamChanged("MUFG returned an empty XML document.")
    return xml_text


def _parse_xml(xml_text: str) -> ET.Element:
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise UpstreamChanged("MUFG returned an XML document with a DTD.")
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise UpstreamChanged("MUFG returned malformed XML.") from exc


def _child_text(element: ET.Element, tag: str) -> str | None:
    for child in element:
        if child.tag == tag:
            text = " ".join((child.text or "").split())
            return text or None
    return None


def _first_child_text(element: ET.Element, tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        value = _child_text(element, tag)
        if value is not None:
            return value
    return None


def _first(fields: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = fields.get(key)
        if value is None:
            continue
        text = " ".join(value.split())
        if text:
            return text
    return None


def _non_numeric(fields: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = fields.get(key)
        if not value:
            continue
        if parse_int(value) is None:
            return " ".join(value.split())
    return None


class MufgProvider(AllotmentProvider):
    name = ProviderName.MUFG
    label = "MUFG / Link Intime"
    status_page_url = _STATUS_PAGE
    allowed_hosts = frozenset({"in.mpms.mufg.com"})

    async def list_issues(self) -> list[AllotmentIssue]:
        async with self._client_factory() as client:
            response = await self._request(
                client,
                "POST",
                _GET_DETAILS,
                headers=_JSON_HEADERS,
                content=b"{}",
                retry_transient=True,
            )
        return parse_issue_catalogue(self._json(response))

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
            # Initialization sets the ASP.NET session cookie used below.
            await self._request(
                client,
                "POST",
                _GET_DETAILS,
                headers=_JSON_HEADERS,
                content=b"{}",
                retry_transient=True,
            )
            token_response = await self._request(
                client,
                "POST",
                _GENERATE_TOKEN,
                headers=_JSON_HEADERS,
                content=b"{}",
                retry_transient=True,
            )
            token = parse_token(self._json(token_response))
            response = await self._request(
                client,
                "POST",
                _SEARCH_ON_PAN,
                headers=_JSON_HEADERS,
                json={
                    "clientid": issue.provider_issue_id,
                    "PAN": pan,
                    "IFSC": "",
                    "CHKVAL": "1",
                    "token": mufg_token(token),
                },
            )
        return parse_lookup_xml(_xml_field(self._json(response)), issue)
