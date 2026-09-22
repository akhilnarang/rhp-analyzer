from __future__ import annotations

import abc
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import httpx

from ..api_schemas import (
    AllotmentChallenge,
    AllotmentIssue,
    AllotmentLookupResult,
    AllotmentOutcome,
    AllotmentRecord,
    ProviderName,
)

logger = logging.getLogger("uvicorn.error")

ClientFactory = Callable[[], httpx.AsyncClient]


class ProviderError(Exception):
    """Base class for registrar adapter failures."""


class ProviderUnavailable(ProviderError):
    """The registrar could not be reached or returned a server error."""

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderRateLimited(ProviderUnavailable):
    """The registrar asked us to slow down."""


class UpstreamChanged(ProviderError):
    """The registrar response no longer matches the adapter."""


class UpstreamResponseError(ProviderError):
    """The registrar returned an explicit error."""


class ChallengeNotSupported(ProviderError):
    """The registrar does not use a CAPTCHA challenge."""


class InvalidPan(ValueError):
    """The supplied PAN format is invalid."""


USER_AGENT = "RHP-Analyzer/0.1 (best-effort allotment status lookup)"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 4
_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_DECIMAL_PATTERN = re.compile(r"^[+-]?\d+\.\d+$")
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
_PENDING_ALLOTMENT_MARKERS = (
    "awaiting",
    "not finalized",
    "not finalised",
    "not confirmed",
    "not yet",
    "pending",
    "processing",
    "under process",
)
_NEGATIVE_ALLOTMENT_MARKERS = (
    "no allotment",
    "not allot",
    "not successful",
    "rejected",
    "unsuccessful",
)
_POSITIVE_ALLOTMENT_MARKERS = (
    "allotted",
    "confirmed",
    "successful",
)


def retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = int(float(value.strip()))
    except ValueError:
        return None
    return max(seconds, 0)


def parse_int(value: Any) -> int | None:
    """Parse a registrar integer without silently rounding.

    Accepts plain integers and integral decimals such as ``"50.0"``. Rejects
    malformed values and non-integral values such as ``"50.5"``.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if _INTEGER_PATTERN.fullmatch(text):
        return int(text)
    if _DECIMAL_PATTERN.fullmatch(text):
        number = float(text)
        if number.is_integer():
            return int(number)
    return None


def outcome_from_allotment_text(value: str | None) -> AllotmentOutcome:
    """Interpret registrar status text without treating any mention of "allot" as confirmed."""

    text = " ".join((value or "").lower().split())
    if not text:
        return AllotmentOutcome.PENDING
    if any(marker in text for marker in _PENDING_ALLOTMENT_MARKERS):
        return AllotmentOutcome.PENDING
    if any(marker in text for marker in _NEGATIVE_ALLOTMENT_MARKERS):
        return AllotmentOutcome.NOT_ALLOTTED
    if text in {"y", "yes"} or any(
        marker in text for marker in _POSITIVE_ALLOTMENT_MARKERS
    ):
        return AllotmentOutcome.ALLOTTED
    return AllotmentOutcome.PENDING


def make_result(
    outcome: AllotmentOutcome,
    issue: AllotmentIssue,
    message: str,
    *,
    records: list[AllotmentRecord] | None = None,
    allocated_shares: int | None = None,
    retry_after: int | None = None,
    challenge: AllotmentChallenge | None = None,
) -> AllotmentLookupResult:
    """Build the normalized result shared by all registrar adapters."""

    return AllotmentLookupResult(
        outcome=outcome,
        provider=issue.provider,
        issue=issue,
        message=message,
        records=records or [],
        allocated_shares=allocated_shares,
        retry_after_seconds=retry_after,
        challenge=challenge,
        status_page_url=issue.status_page_url,
    )


class AllotmentProvider(abc.ABC):
    """One registrar adapter.

    Subclasses do the registrar-specific parsing and raise a
    :class:`ProviderError` for infrastructure
    problems. The public :meth:`lookup` entry point turns those errors into a
    normalized :class:`AllotmentLookupResult` so callers never see a raw
    transport error.
    """

    name: ClassVar[ProviderName]
    label: ClassVar[str]
    status_page_url: ClassVar[str]
    requires_challenge: ClassVar[bool] = False
    allowed_hosts: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, client_factory: ClientFactory) -> None:
        self._client_factory = client_factory

    @abc.abstractmethod
    async def list_issues(self) -> list[AllotmentIssue]:
        """Return this registrar's current issues, or raise a provider error."""

    @abc.abstractmethod
    async def _lookup(
        self,
        issue: AllotmentIssue,
        pan: str,
        *,
        captcha_token: str | None,
        captcha_answer: str | None,
    ) -> AllotmentLookupResult:
        """Perform the registrar request and parse its response."""

    async def start_challenge(self, issue: AllotmentIssue) -> AllotmentChallenge:
        raise ChallengeNotSupported(f"{self.label} does not issue a CAPTCHA challenge.")

    async def auto_solve_challenge(
        self, issue: AllotmentIssue
    ) -> tuple[str, str] | None:
        """Return a solved (token, answer), or None when unsupported."""
        del issue
        return None

    async def lookup(
        self,
        issue: AllotmentIssue,
        pan: str,
        *,
        captcha_token: str | None = None,
        captcha_answer: str | None = None,
    ) -> AllotmentLookupResult:
        return await self._guard(
            self._lookup(
                issue,
                pan,
                captcha_token=captcha_token,
                captcha_answer=captcha_answer,
            ),
            issue,
        )

    async def challenge(self, issue: AllotmentIssue) -> AllotmentLookupResult:
        """Fetch and return a human CAPTCHA challenge, normalized like a lookup."""

        if not self.requires_challenge:
            return make_result(
                AllotmentOutcome.NOT_SUPPORTED,
                issue,
                f"{self.label} does not require a CAPTCHA.",
            )
        return await self._guard(
            self.start_challenge(issue),
            issue,
            operation="challenge",
            on_success=lambda challenge: make_result(
                AllotmentOutcome.CHALLENGE_REQUIRED,
                issue,
                "Enter the characters shown in the CAPTCHA image.",
                challenge=challenge,
            ),
        )

    async def _guard(
        self,
        call: Awaitable[Any],
        issue: AllotmentIssue,
        *,
        operation: str = "request",
        on_success: Callable[[Any], AllotmentLookupResult] | None = None,
    ) -> AllotmentLookupResult:
        try:
            value = await call
        except ChallengeNotSupported as exc:
            return self._failure(AllotmentOutcome.NOT_SUPPORTED, issue, str(exc))
        except ProviderRateLimited as exc:
            return self._failure(
                AllotmentOutcome.RATE_LIMITED,
                issue,
                str(exc),
                retry_after=exc.retry_after,
            )
        except UpstreamChanged as exc:
            return self._failure(AllotmentOutcome.UPSTREAM_CHANGED, issue, str(exc))
        except UpstreamResponseError as exc:
            return self._failure(AllotmentOutcome.UPSTREAM_ERROR, issue, str(exc))
        except ProviderUnavailable as exc:
            return self._failure(
                AllotmentOutcome.PROVIDER_UNAVAILABLE,
                issue,
                str(exc),
                retry_after=exc.retry_after,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Allotment %s failed: provider=%s error=%s",
                operation,
                self.name.value,
                type(exc).__name__,
            )
            return self._failure(
                AllotmentOutcome.PROVIDER_UNAVAILABLE,
                issue,
                "The registrar did not respond.",
            )
        except Exception as exc:  # noqa: BLE001 - normalize adapter bugs
            logger.warning(
                "Allotment %s parser failed: provider=%s error=%s",
                operation,
                self.name.value,
                type(exc).__name__,
            )
            return self._failure(
                AllotmentOutcome.UPSTREAM_ERROR,
                issue,
                "The registrar response could not be processed.",
            )
        if on_success is not None:
            return on_success(value)
        if not isinstance(value, AllotmentLookupResult):
            return self._failure(
                AllotmentOutcome.UPSTREAM_ERROR,
                issue,
                "The registrar response could not be processed.",
            )
        return value

    def _failure(
        self,
        outcome: AllotmentOutcome,
        issue: AllotmentIssue,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> AllotmentLookupResult:
        return make_result(
            outcome,
            issue,
            message,
            retry_after=retry_after,
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        retry_transient: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request, keeping every redirect inside the registrar."""

        attempts = 2 if retry_transient else 1
        for attempt in range(attempts):
            try:
                response = await self._request_once(client, method, url, **kwargs)
            except httpx.TransportError:
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))
                continue
            if response.status_code in _RETRYABLE_STATUSES and attempt + 1 < attempts:
                await asyncio.sleep(0.1 * (attempt + 1))
                continue
            self._raise_for_status(response)
            return response
        raise ProviderUnavailable("The registrar did not respond after a retry.")

    async def _request_once(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send one request and follow only redirects within the registrar."""

        current = url
        request_kwargs = dict(kwargs)
        for _ in range(_MAX_REDIRECTS + 1):
            host = httpx.URL(current).host
            if host not in self.allowed_hosts:
                raise UpstreamChanged(f"Unexpected registrar host: {host}.")
            response = await client.request(
                method,
                current,
                follow_redirects=False,
                **request_kwargs,
            )
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise UpstreamChanged(
                        "The registrar returned a redirect without a location."
                    )
                current = str(response.url.join(location))
                # The resolved Location is joined against response.url, which
                # already contains the original query parameters.
                request_kwargs.pop("params", None)
                if response.status_code == 303 or (
                    response.status_code in {301, 302} and method.upper() != "GET"
                ):
                    method = "GET"
                    for key in ("content", "json", "data"):
                        request_kwargs.pop(key, None)
                    headers = dict(request_kwargs.get("headers") or {})
                    request_kwargs["headers"] = {
                        key: value
                        for key, value in headers.items()
                        if key.lower() != "content-type"
                    }
                continue
            return response
        raise ProviderUnavailable("The registrar returned too many redirects.")

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise ProviderRateLimited(
                "The registrar is rate limiting requests.",
                retry_after=retry_after_seconds(response),
            )
        if response.status_code >= 500:
            raise ProviderUnavailable(
                f"The registrar returned HTTP {response.status_code}.",
                retry_after=retry_after_seconds(response),
            )
        if response.status_code >= 400:
            raise UpstreamChanged(
                f"The registrar rejected the request with HTTP {response.status_code}."
            )

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamChanged(
                "The registrar returned a response that is not valid JSON."
            ) from exc
