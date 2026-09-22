from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Callable, Iterable

import httpx

from ..api_schemas import (
    AllotmentIssue,
    AllotmentLookupResult,
    AllotmentOutcome,
    AllotmentProviderStatus,
    ProviderName,
)
from ..config import Settings
from .base import (
    USER_AGENT,
    AllotmentProvider,
    ClientFactory,
    InvalidPan,
    ProviderRateLimited,
    ProviderUnavailable,
    UpstreamChanged,
)
from .bigshare import BigshareProvider
from .kfintech import KFintechProvider
from .mufg import MufgProvider
from .purva import PurvaProvider

PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# Words removed before comparing issuer names. They identify the security
# rather than the issuer and differ between registrars.
_NOISE_WORDS = frozenset(
    {
        "ipo",
        "sme",
        "ncd",
        "ltd",
        "limited",
        "pvt",
        "private",
        "india",
        "the",
    }
)


def normalize_pan(value: str) -> str:
    pan = (value or "").strip().upper()
    if not PAN_PATTERN.fullmatch(pan):
        raise InvalidPan("A PAN has five letters, four digits, then one letter.")
    return pan


def normalize_company_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(token for token in text.split() if token not in _NOISE_WORDS)


def find_issue_matches(
    issues: Iterable[AllotmentIssue], query: str
) -> list[AllotmentIssue]:
    normalized_query = normalize_company_name(query)
    if not normalized_query:
        return []
    exact = [
        issue
        for issue in issues
        if normalize_company_name(issue.company_name) == normalized_query
    ]
    if exact:
        return exact
    tokens = normalized_query.split()
    return [
        issue
        for issue in issues
        if all(token in normalize_company_name(issue.company_name) for token in tokens)
    ]


class AllotmentService:
    """Aggregate registrar catalogues and dispatch applicant lookups.

    The service holds a short-lived in-memory catalogue cache only. It does not
    touch the analysis SQLite database or the report cache.
    """

    def __init__(
        self,
        providers: Iterable[AllotmentProvider],
        *,
        catalogue_ttl_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._providers = {provider.name: provider for provider in providers}
        self._catalogue_ttl_seconds = max(catalogue_ttl_seconds, 0)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._catalogue: (
            tuple[float, list[AllotmentIssue], dict[ProviderName, str]] | None
        ) = None

    def provider_statuses(self) -> list[AllotmentProviderStatus]:
        errors = self._catalogue[2] if self._catalogue is not None else {}
        return [
            AllotmentProviderStatus(
                name=provider.name,
                label=provider.label,
                status_page_url=provider.status_page_url,
                requires_challenge=provider.requires_challenge,
                catalogue_available=provider.name not in errors,
                error=errors.get(provider.name),
            )
            for provider in self._providers.values()
        ]

    async def load_catalogue(
        self, *, refresh: bool = False
    ) -> tuple[list[AllotmentIssue], dict[ProviderName, str]]:
        now = self._clock()
        if not refresh and self._fresh(now):
            return self._catalogue[1], self._catalogue[2]  # type: ignore[index]
        async with self._lock:
            now = self._clock()
            if not refresh and self._fresh(now):
                return self._catalogue[1], self._catalogue[2]  # type: ignore[index]
            providers = list(self._providers.values())
            results = await asyncio.gather(
                *(provider.list_issues() for provider in providers),
                return_exceptions=True,
            )
            issues: list[AllotmentIssue] = []
            errors: dict[ProviderName, str] = {}
            for provider, result in zip(providers, results, strict=True):
                if isinstance(result, BaseException):
                    errors[provider.name] = _error_text(result)
                else:
                    issues.extend(result)
            # Keep each registrar's own order; do not re-sort the catalogue.
            self._catalogue = (
                now + self._catalogue_ttl_seconds,
                issues,
                errors,
            )
            return issues, errors

    def _fresh(self, now: float) -> bool:
        return self._catalogue is not None and self._catalogue[0] > now

    async def list_issues(
        self,
        *,
        query: str | None = None,
        provider: ProviderName | None = None,
    ) -> tuple[list[AllotmentIssue], dict[ProviderName, str]]:
        issues, errors = await self.load_catalogue()
        if provider is not None:
            issues = [issue for issue in issues if issue.provider is provider]
        if query and query.strip():
            issues = find_issue_matches(issues, query)
        return issues, errors

    async def lookup(
        self,
        *,
        pan: str,
        issue_id: str | None = None,
        query: str | None = None,
        captcha_token: str | None = None,
        captcha_answer: str | None = None,
    ) -> AllotmentLookupResult:
        normalized_pan = normalize_pan(pan)
        issue, failure = await self.resolve_issue(issue_id=issue_id, query=query)
        if failure is not None:
            return failure
        if issue is None:
            return self._error(
                AllotmentOutcome.UPSTREAM_ERROR,
                "The issue could not be resolved.",
            )
        provider = self._providers.get(issue.provider)
        if provider is None:
            return self._error(
                AllotmentOutcome.PROVIDER_UNAVAILABLE,
                f"{issue.provider_label} is not configured.",
                issue=issue,
            )
        if provider.requires_challenge and not (captcha_token and captcha_answer):
            captcha_token, captcha_answer = await self._auto_solved_challenge(
                provider, issue
            )
        if provider.requires_challenge and not (captcha_token and captcha_answer):
            return AllotmentLookupResult(
                outcome=AllotmentOutcome.CHALLENGE_REQUIRED,
                provider=issue.provider,
                issue=issue,
                message="Enter the CAPTCHA answer before looking up this issue.",
                status_page_url=issue.status_page_url,
            )
        return await provider.lookup(
            issue,
            normalized_pan,
            captcha_token=captcha_token,
            captcha_answer=captcha_answer,
        )

    async def _auto_solved_challenge(
        self, provider: AllotmentProvider, issue: AllotmentIssue
    ) -> tuple[str | None, str | None]:
        """Solve a CAPTCHA with OCR when possible. Returns (None, None) on any miss."""

        try:
            solved = await provider.auto_solve_challenge(issue)
        except Exception:  # noqa: BLE001 - fall back to the human flow
            return None, None
        if solved is None:
            return None, None
        return solved

    async def challenge(
        self,
        *,
        issue_id: str | None = None,
        query: str | None = None,
    ) -> AllotmentLookupResult:
        issue, failure = await self.resolve_issue(issue_id=issue_id, query=query)
        if failure is not None:
            return failure
        if issue is None:
            return self._error(
                AllotmentOutcome.UPSTREAM_ERROR,
                "The issue could not be resolved.",
            )
        provider = self._providers.get(issue.provider)
        if provider is None:
            return self._error(
                AllotmentOutcome.PROVIDER_UNAVAILABLE,
                f"{issue.provider_label} is not configured.",
                issue=issue,
            )
        return await provider.challenge(issue)

    async def resolve_issue(
        self,
        *,
        issue_id: str | None = None,
        query: str | None = None,
    ) -> tuple[AllotmentIssue | None, AllotmentLookupResult | None]:
        issues, errors = await self.load_catalogue()
        if issue_id:
            for issue in issues:
                if issue.issue_id == issue_id:
                    return issue, None
            fallback = self._issue_from_id(issue_id, errors, query)
            if fallback is not None:
                return fallback, None
            return None, self._error(
                AllotmentOutcome.NOT_FOUND,
                "The selected issue is not in the current catalogue.",
            )
        if not query or not query.strip():
            return None, self._error(
                AllotmentOutcome.NOT_FOUND,
                "Select an IPO or enter a company name.",
            )
        matches = find_issue_matches(issues, query)
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            unavailable = ", ".join(
                provider.label
                for provider in self._providers.values()
                if provider.name in errors
            )
            message = f"No issue matches {query.strip()!r}."
            if unavailable:
                message += f" Some catalogues were unavailable: {unavailable}."
            return None, self._error(AllotmentOutcome.NOT_FOUND, message)
        return None, self._error(
            AllotmentOutcome.AMBIGUOUS,
            f"{len(matches)} issues match {query.strip()!r}. Select one.",
            candidates=matches[:20],
        )

    def _issue_from_id(
        self,
        issue_id: str,
        errors: dict[ProviderName, str],
        query: str | None,
    ) -> AllotmentIssue | None:
        """Build an issue for a provider whose catalogue could not be loaded."""

        prefix, _, raw = issue_id.partition(":")
        if not raw:
            return None
        try:
            provider_name = ProviderName(prefix)
        except ValueError:
            return None
        provider = self._providers.get(provider_name)
        if provider is None or provider_name not in errors:
            return None
        return AllotmentIssue(
            issue_id=issue_id,
            provider=provider.name,
            provider_label=provider.label,
            provider_issue_id=raw,
            company_name=(query or raw).strip(),
            requires_challenge=provider.requires_challenge,
            status_page_url=provider.status_page_url,
        )

    @staticmethod
    def _error(
        outcome: AllotmentOutcome,
        message: str,
        *,
        issue: AllotmentIssue | None = None,
        candidates: list[AllotmentIssue] | None = None,
    ) -> AllotmentLookupResult:
        return AllotmentLookupResult(
            outcome=outcome,
            provider=issue.provider if issue is not None else None,
            issue=issue,
            message=message,
            candidates=candidates or [],
            status_page_url=issue.status_page_url if issue is not None else None,
        )


def _error_text(error: BaseException) -> str:
    if isinstance(error, ProviderRateLimited):
        return "The registrar is rate limiting catalogue requests."
    if isinstance(error, ProviderUnavailable):
        return str(error)
    if isinstance(error, UpstreamChanged):
        return str(error)
    return f"Unexpected registrar error: {type(error).__name__}."


def build_providers(client_factory: ClientFactory) -> list[AllotmentProvider]:
    return [
        KFintechProvider(client_factory),
        MufgProvider(client_factory),
        BigshareProvider(client_factory),
        PurvaProvider(client_factory),
    ]


def build_allotment_service(settings: Settings) -> AllotmentService:
    timeout_seconds = settings.ipo_allotment_timeout_seconds
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-IN,en;q=0.9",
            },
            trust_env=False,
        )

    return AllotmentService(
        build_providers(client_factory),
        catalogue_ttl_seconds=settings.ipo_allotment_catalogue_ttl_seconds,
    )
