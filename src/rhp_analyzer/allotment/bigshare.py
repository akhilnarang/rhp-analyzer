from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from ..api_schemas import (
    AllotmentChallenge,
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
from .html import select_options

_STATUS_PAGE = "https://ipo.bigshareonline.com/ipo_status.html"
_BASE = "https://ipo.bigshareonline.com"
_LOOKUP_URL = f"{_BASE}/Data.aspx/FetchIpodetails"
_CAPTCHA_URL = f"{_BASE}/Captcha.ashx"
_CAPTCHA_HINT = "Enter the characters shown in the CAPTCHA image."

# Legacy Bigshare responses put human-readable errors inside data fields
# instead of using the newer ``Status`` contract.
_SENTINELS = (
    "please enter valid pan",
    "please enter valid",
    "invalid pan",
    "no record found",
    "record not found",
    "not found",
)

_WARMING_STATUSES = {"WARMING", "PROCESSING", "INITIALIZING"}


def parse_issues(html: str) -> list[AllotmentIssue]:
    options = select_options(html, select_id="ddlCompany")
    return [_issue(value, label) for value, label in options if value and label]


def _issue(company_id: str, company_name: str) -> AllotmentIssue:
    return AllotmentIssue(
        issue_id=f"{ProviderName.BIGSHARE.value}:{company_id}",
        provider=ProviderName.BIGSHARE,
        provider_label="Bigshare",
        provider_issue_id=company_id,
        company_name=company_name,
        requires_challenge=True,
        status_page_url=_STATUS_PAGE,
    )


def parse_challenge(payload: Any) -> AllotmentChallenge:
    data = _object(payload)
    token = _first_text(data, ("token", "Token", "CaptchaToken", "captchaToken"))
    image = _first_text(
        data,
        ("image", "Image", "CaptchaImage", "captchaImage", "ImageData"),
    )
    if token is None or image is None:
        raise UpstreamChanged(
            "Bigshare returned a CAPTCHA response without a token and image."
        )
    if not image.startswith("data:"):
        image = f"data:image/png;base64,{image}"
    return AllotmentChallenge(token=token, image_data_uri=image, hint=_CAPTCHA_HINT)


def parse_lookup_payload(payload: Any, issue: AllotmentIssue) -> AllotmentLookupResult:
    data = _object(payload)
    status = (_first_text(data, ("Status", "status")) or "").upper()
    if status == "OK":
        return _ok_result(data, issue)
    if status == "NOTFOUND":
        return make_result(
            AllotmentOutcome.NOT_FOUND,
            issue,
            _message(data)
            or "Bigshare has no application for this PAN in the selected issue.",
        )
    if status == "CAPTCHA":
        return make_result(
            AllotmentOutcome.CHALLENGE_REQUIRED,
            issue,
            _message(data)
            or "Bigshare rejected the CAPTCHA answer. Request a new challenge.",
        )
    if status == "RATELIMIT":
        return make_result(
            AllotmentOutcome.RATE_LIMITED,
            issue,
            _message(data) or "Bigshare is rate limiting requests.",
        )
    if status in _WARMING_STATUSES:
        return make_result(
            AllotmentOutcome.PROVIDER_UNAVAILABLE,
            issue,
            _message(data) or "Bigshare is still preparing the allotment data.",
        )
    if status:
        raise UpstreamChanged(f"Bigshare returned an unrecognized status: {status}.")
    # Only responses without an explicit Status may fall back to legacy
    # free-text sentinels, so a Status never loses to unrelated message text.
    sentinel = _sentinel_message(data)
    if sentinel is not None:
        return make_result(AllotmentOutcome.NOT_FOUND, issue, sentinel)
    raise UpstreamChanged("Bigshare returned a lookup response without a status.")


def _ok_result(data: dict[str, Any], issue: AllotmentIssue) -> AllotmentLookupResult:
    rows = _record_rows(data)
    if not rows:
        unrecognized = any(
            key not in {"Status", "Message", "MatchCount", "ResultToken"}
            for key in data
        )
        if unrecognized:
            raise UpstreamChanged(
                "Bigshare returned an OK response without recognized applicant fields."
            )
        return make_result(
            AllotmentOutcome.NOT_FOUND,
            issue,
            _message(data)
            or "Bigshare has no application for this PAN in the selected issue.",
        )
    records = [_record(row) for row in rows]
    outcomes = [_outcome(record) for record in records]
    allotted = [
        record.allotted_shares
        for record in records
        if record.allotted_shares is not None and record.allotted_shares > 0
    ]
    if AllotmentOutcome.ALLOTTED in outcomes:
        outcome = AllotmentOutcome.ALLOTTED
        message = "Bigshare shows an allotment for this PAN."
    elif outcomes and all(item == AllotmentOutcome.NOT_ALLOTTED for item in outcomes):
        outcome = AllotmentOutcome.NOT_ALLOTTED
        message = "Bigshare shows no allotment for this PAN."
    else:
        outcome = AllotmentOutcome.PENDING
        message = "Bigshare returned a record without a confirmed allotment."
    return make_result(
        outcome,
        issue,
        message,
        records=records,
        allocated_shares=sum(allotted) if allotted else None,
    )


def _object(payload: Any) -> dict[str, Any]:
    candidate: Any = payload
    if isinstance(candidate, dict) and "d" in candidate:
        candidate = candidate["d"]
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except ValueError as exc:
            raise UpstreamChanged(
                "Bigshare returned a lookup response that is not valid JSON."
            ) from exc
    if not isinstance(candidate, dict):
        raise UpstreamChanged("Bigshare returned an unexpected response shape.")
    return candidate


def _record_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("Records")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    if any(key in data for key in ("APPLICATION_NO", "ALLOTED", "DPID", "Name")):
        return [data]
    return []


def _record(row: dict[str, Any]) -> AllotmentRecord:
    allotted_text = _first_text(row, ("ALLOTED", "Alloted", "Allotted"))
    allotted_shares = parse_int(allotted_text)
    return AllotmentRecord(
        application_no=_first_text(row, ("APPLICATION_NO", "ApplicationNo")),
        name=_first_text(row, ("Name", "NAME", "NAME1")),
        dp_id=_first_text(row, ("DPID", "DpId")),
        category=_first_text(row, ("Category", "category")),
        applied_shares=parse_int(
            _first_text(row, ("APPLIED", "Applied", "App_Shares"))
        ),
        allotted_shares=allotted_shares,
        remarks=allotted_text if allotted_shares is None else None,
    )


def _outcome(record: AllotmentRecord) -> AllotmentOutcome:
    if record.allotted_shares is not None:
        return (
            AllotmentOutcome.ALLOTTED
            if record.allotted_shares > 0
            else AllotmentOutcome.NOT_ALLOTTED
        )
    return outcome_from_allotment_text(record.remarks)


def _sentinel_message(data: dict[str, Any]) -> str | None:
    for value in data.values():
        if not isinstance(value, str):
            continue
        lowered = value.strip().lower()
        for sentinel in _SENTINELS:
            if sentinel in lowered:
                return value.strip()
    return None


def _message(data: dict[str, Any]) -> str | None:
    value = _first_text(data, ("Message", "message", "Msg"))
    return value


def _first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = " ".join(str(value).split())
        if text:
            return text
    return None


def read_captcha_digits(image_bytes: bytes) -> str | None:
    """Read the CAPTCHA digits with Tesseract.

    Bigshare permits automation for personal use. Tesseract and Pillow are
    optional: any missing dependency, unreadable image, or uncertain read
    returns None so the caller falls back to the human CAPTCHA flow.
    """

    try:
        import io

        import pytesseract
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes)).convert("L")
        text = pytesseract.image_to_string(
            image,
            config="--psm 7 -c tessedit_char_whitelist=0123456789",
        )
    except Exception:  # noqa: BLE001 - OCR is best effort only
        return None
    digits = "".join(character for character in text if character.isdigit())
    return digits if len(digits) == 6 else None


def captcha_image_bytes(challenge: AllotmentChallenge) -> bytes | None:
    prefix = "base64,"
    payload = challenge.image_data_uri
    index = payload.find(prefix)
    if index == -1:
        return None
    try:
        return base64.b64decode(payload[index + len(prefix) :], validate=True)
    except ValueError, binascii.Error:
        return None


class BigshareProvider(AllotmentProvider):
    name = ProviderName.BIGSHARE
    label = "Bigshare"
    status_page_url = _STATUS_PAGE
    requires_challenge = True
    allowed_hosts = frozenset(
        {
            "ipo.bigshareonline.com",
            "ipo1.bigshareonline.com",
            "ipo2.bigshareonline.com",
        }
    )

    async def list_issues(self) -> list[AllotmentIssue]:
        async with self._client_factory() as client:
            response = await self._request(
                client, "GET", _STATUS_PAGE, retry_transient=True
            )
        issues = parse_issues(response.text)
        if not issues:
            raise UpstreamChanged("Bigshare listed no companies on its status page.")
        return issues

    async def start_challenge(self, issue: AllotmentIssue) -> AllotmentChallenge:
        del issue
        async with self._client_factory() as client:
            response = await self._request(
                client, "GET", _CAPTCHA_URL, retry_transient=True
            )
        return parse_challenge(self._json(response))

    async def auto_solve_challenge(
        self, issue: AllotmentIssue
    ) -> tuple[str, str] | None:
        """Fetch a challenge and read its digits with Tesseract.

        Returns the token and answer, or None when OCR is unavailable or
        could not read six digits. Local experiments only.
        """

        challenge = await self.start_challenge(issue)
        image = captcha_image_bytes(challenge)
        if image is None:
            return None
        digits = read_captcha_digits(image)
        if digits is None:
            return None
        return challenge.token, digits

    async def _lookup(
        self,
        issue: AllotmentIssue,
        pan: str,
        *,
        captcha_token: str | None,
        captcha_answer: str | None,
    ) -> AllotmentLookupResult:
        if not captcha_token or not captcha_answer:
            return make_result(
                AllotmentOutcome.CHALLENGE_REQUIRED,
                issue,
                "Bigshare requires a CAPTCHA answer before lookup.",
            )
        body = {
            "Applicationno": "",
            "Company": issue.provider_issue_id,
            "SelectionType": "PN",
            "PanNo": pan,
            "txtcsdl": "",
            "txtDPID": "",
            "txtClId": "",
            "ddlType": "0",
            "lang": "en",
            "CaptchaToken": captcha_token,
            "CaptchaAnswer": captcha_answer,
            "ResultToken": "",
        }
        async with self._client_factory() as client:
            response = await self._request(
                client,
                "POST",
                _LOOKUP_URL,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "Referer": _STATUS_PAGE,
                },
                json=body,
            )
        return parse_lookup_payload(self._json(response), issue)
