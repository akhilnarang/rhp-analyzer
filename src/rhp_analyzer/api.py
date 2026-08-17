from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import secrets
import socket
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote

import httpcore
import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import HttpUrl

from . import web
from .api_schemas import (
    AnalysisLinkResponse,
    AnalysisResponse,
    AnalysisStatusResponse,
    HealthResponse,
)
from .cache import CacheStore
from .config import Settings, get_settings
from .service import AnalysisService

logger = logging.getLogger("uvicorn.error")
bearer_auth = HTTPBearer(
    auto_error=False,
    scheme_name="RHPBearerAuth",
    description="Bearer token for POST /v1/analyze.",
)

MARKET_DATA_FIELDS = (
    "lot_size",
    "price",
    "issue_size",
    "gmp",
    "gmp_percent",
    "open_date",
    "close_date",
    "allotment_date",
    "subscription",
    "qib_subscription",
    "nii_subscription",
    "snii_subscription",
    "bnii_subscription",
    "rii_subscription",
    "employee_subscription",
)


class UnsafeRemoteAddress(ValueError):
    pass


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        async with asyncio.timeout(10):
            answers = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
    except TimeoutError as exc:
        raise UnsafeRemoteAddress("The PDF URL host lookup timed out.") from exc
    except socket.gaierror as exc:
        raise UnsafeRemoteAddress("The PDF URL host could not be resolved.") from exc
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise UnsafeRemoteAddress(
            "The PDF URL must resolve only to public internet addresses."
        )
    return addresses[:8]


class PublicInternetBackend(httpcore.AsyncNetworkBackend):
    """Resolve and pin each connection to a public internet address."""

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await resolve_public_addresses(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeRemoteAddress("Unix sockets are not valid PDF URL targets.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def public_internet_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    transport._pool._network_backend = PublicInternetBackend()  # type: ignore[attr-defined]
    return transport


def schedule_analysis(app: FastAPI, analysis_id: str) -> None:
    tasks: dict[str, asyncio.Task[None]] = app.state.analysis_tasks
    if analysis_id in tasks:
        return
    service: AnalysisService = app.state.analysis_service
    task = asyncio.create_task(
        service.run_job(analysis_id),
        name=f"analysis-{analysis_id}",
    )
    tasks[analysis_id] = task
    task.add_done_callback(lambda _task: tasks.pop(analysis_id, None))


async def get_request_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def require_analyze_auth(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_auth),
    ],
) -> None:
    configured_tokens = request.app.state.settings.api_tokens()
    if not configured_tokens:
        return
    supplied_token = credentials.credentials if credentials is not None else ""
    valid = any(
        secrets.compare_digest(supplied_token, configured_token)
        for configured_token in configured_tokens
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The bearer token is not valid.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def safe_filename(value: str | None) -> str:
    return (value or "upload.pdf").replace("\\", "/").rsplit("/", 1)[-1]


async def persist_upload(
    upload: UploadFile,
    *,
    max_bytes: int,
) -> tuple[Path, str, int]:
    digest = hashlib.sha256()
    size = 0
    first_bytes = b""
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
            path = Path(temporary.name)
            while chunk := await upload.read(1024 * 1024):
                if not first_bytes:
                    first_bytes = chunk[:5]
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"The PDF is larger than the {max_bytes}-byte limit.",
                    )
                digest.update(chunk)
                temporary.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The file is empty.")
        if first_bytes != b"%PDF-":
            raise HTTPException(status_code=415, detail="The file is not a PDF.")
        assert path is not None
        return path, digest.hexdigest(), size
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def validate_remote_url(url: httpx.URL) -> None:
    if url.scheme not in {"http", "https"} or not url.host:
        raise HTTPException(
            status_code=422,
            detail="The PDF URL must use HTTP or HTTPS.",
        )
    if url.userinfo:
        raise HTTPException(
            status_code=422,
            detail="The PDF URL must not contain user information.",
        )


async def persist_remote_pdf(
    source_url: HttpUrl,
    *,
    max_bytes: int,
) -> tuple[Path, str, int, str]:
    current_url = httpx.URL(str(source_url))
    headers = {
        "Accept": "application/pdf",
        "User-Agent": "RHP-Analyzer/0.1",
    }
    path: Path | None = None
    try:
        async with httpx.AsyncClient(
            timeout=60,
            follow_redirects=False,
            transport=public_internet_transport(),
        ) as client:
            for _ in range(6):
                validate_remote_url(current_url)
                async with client.stream(
                    "GET", current_url, headers=headers
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise HTTPException(
                                status_code=status.HTTP_424_FAILED_DEPENDENCY,
                                detail=(
                                    "The PDF server returned an invalid redirect. "
                                    "Upload the PDF instead."
                                ),
                            )
                        current_url = response.url.join(location)
                        continue
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        logger.warning(
                            "PDF download refused: host=%s status=%d",
                            current_url.host,
                            response.status_code,
                        )
                        raise HTTPException(
                            status_code=status.HTTP_424_FAILED_DEPENDENCY,
                            detail=(
                                "The PDF server refused the download with "
                                f"HTTP {response.status_code}. Upload the PDF instead."
                            ),
                        ) from exc
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=f"The PDF is larger than the {max_bytes}-byte limit.",
                        )
                    digest = hashlib.sha256()
                    size = 0
                    first_bytes = b""
                    with tempfile.NamedTemporaryFile(
                        suffix=".pdf",
                        delete=False,
                    ) as temporary:
                        path = Path(temporary.name)
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if not first_bytes:
                                first_bytes = chunk[:5]
                            size += len(chunk)
                            if size > max_bytes:
                                raise HTTPException(
                                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                                    detail=(
                                        "The PDF is larger than the "
                                        f"{max_bytes}-byte limit."
                                    ),
                                )
                            digest.update(chunk)
                            temporary.write(chunk)
                    if size == 0:
                        raise HTTPException(
                            status_code=status.HTTP_424_FAILED_DEPENDENCY,
                            detail=(
                                "The PDF server returned an empty file. "
                                "Upload the PDF instead."
                            ),
                        )
                    if first_bytes != b"%PDF-":
                        raise HTTPException(
                            status_code=415,
                            detail="The remote file is not a PDF.",
                        )
                    filename = safe_filename(unquote(Path(current_url.path).name))
                    return path, digest.hexdigest(), size, filename
            raise HTTPException(
                status_code=status.HTTP_424_FAILED_DEPENDENCY,
                detail=(
                    "The PDF server returned too many redirects. "
                    "Upload the PDF instead."
                ),
            )
    except HTTPException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    except UnsafeRemoteAddress as exc:
        if path is not None:
            path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        if path is not None:
            path.unlink(missing_ok=True)
        logger.warning(
            "PDF download failed: host=%s error=%s",
            current_url.host,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail=("The service could not download the PDF. Upload the PDF instead."),
        ) from exc


def parse_sections(value: str | None, service: AnalysisService) -> list[str]:
    available = service.available_sections()
    if value is None or value.strip().lower() == "all":
        return sorted(available)
    requested = {item.strip() for item in value.split(",") if item.strip()}
    invalid = requested - available
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"These sections are not valid: {', '.join(sorted(invalid))}.",
        )
    if not requested:
        raise HTTPException(status_code=422, detail="Select at least one section.")
    return sorted(requested)


def collect_market_data(**values: str | None) -> dict[str, str]:
    return {
        name: value.strip()
        for name, value in values.items()
        if name in MARKET_DATA_FIELDS and value is not None and value.strip()
    }


def create_app(
    settings: Settings | None = None,
    analysis_service: AnalysisService | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if analysis_service is None:
            cache = CacheStore(app_settings.rhp_database_path)
            cache.initialize()
            app.state.analysis_service = AnalysisService(
                cache,
                section_concurrency=app_settings.rhp_section_concurrency,
                job_concurrency=app_settings.rhp_job_concurrency,
            )
        else:
            app.state.analysis_service = analysis_service
        for analysis_id in app.state.analysis_service.pending_job_ids():
            schedule_analysis(app, analysis_id)
        try:
            yield
        finally:
            tasks = list(app.state.analysis_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(
        title="RHP Analyzer API",
        version="0.1.0",
        lifespan=lifespan,
    )
    if analysis_service is not None:
        app.state.analysis_service = analysis_service
    app.state.analysis_tasks = {}
    app.state.settings = app_settings
    app.mount("/static", StaticFiles(directory=web.STATIC_DIRECTORY), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request) -> HTMLResponse:
        return web.templates.TemplateResponse(request=request, name="home.html")

    @app.get("/analysis", response_class=HTMLResponse, include_in_schema=False)
    async def analysis_list(request: Request) -> HTMLResponse:
        service: AnalysisService = request.app.state.analysis_service
        return web.templates.TemplateResponse(
            request=request,
            name="analysis_list.html",
            context={"jobs": web.analysis_list_items(service.list_jobs())},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.post(
        "/v1/analyze",
        response_model=AnalysisLinkResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def analyze(
        request: Request,
        response: Response,
        _authorization: Annotated[None, Depends(require_analyze_auth)],
        request_base_url: Annotated[str, Depends(get_request_base_url)],
        file: Annotated[
            UploadFile | None,
            File(description="RHP or DRHP PDF file."),
        ] = None,
        url: Annotated[
            HttpUrl | None,
            Form(description="Public URL for an RHP or DRHP PDF file."),
        ] = None,
        sections: Annotated[str | None, Form()] = None,
        lot_size: Annotated[str | None, Form(max_length=100)] = None,
        price: Annotated[str | None, Form(max_length=100)] = None,
        issue_size: Annotated[str | None, Form(max_length=100)] = None,
        gmp: Annotated[str | None, Form(max_length=100)] = None,
        gmp_percent: Annotated[str | None, Form(max_length=100)] = None,
        open_date: Annotated[str | None, Form(max_length=100)] = None,
        close_date: Annotated[str | None, Form(max_length=100)] = None,
        allotment_date: Annotated[str | None, Form(max_length=100)] = None,
        subscription: Annotated[str | None, Form(max_length=100)] = None,
        qib_subscription: Annotated[str | None, Form(max_length=100)] = None,
        nii_subscription: Annotated[str | None, Form(max_length=100)] = None,
        snii_subscription: Annotated[str | None, Form(max_length=100)] = None,
        bnii_subscription: Annotated[str | None, Form(max_length=100)] = None,
        rii_subscription: Annotated[str | None, Form(max_length=100)] = None,
        employee_subscription: Annotated[str | None, Form(max_length=100)] = None,
    ) -> AnalysisLinkResponse:
        service: AnalysisService = request.app.state.analysis_service
        if (file is None) == (url is None):
            raise HTTPException(
                status_code=422,
                detail="Send one PDF source. Use either file or url.",
            )
        parsed_sections = parse_sections(sections, service)
        market_data = collect_market_data(
            lot_size=lot_size,
            price=price,
            issue_size=issue_size,
            gmp=gmp,
            gmp_percent=gmp_percent,
            open_date=open_date,
            close_date=close_date,
            allotment_date=allotment_date,
            subscription=subscription,
            qib_subscription=qib_subscription,
            nii_subscription=nii_subscription,
            snii_subscription=snii_subscription,
            bnii_subscription=bnii_subscription,
            rii_subscription=rii_subscription,
            employee_subscription=employee_subscription,
        )
        filename = safe_filename(file.filename) if file is not None else "remote.pdf"
        started = time.monotonic()
        logger.info(
            "Analysis request started: file=%s sections=%s",
            filename,
            ",".join(parsed_sections),
        )
        path: Path | None = None
        try:
            if file is not None:
                path, checksum, size = await persist_upload(
                    file,
                    max_bytes=app_settings.rhp_max_pdf_bytes,
                )
            else:
                assert url is not None
                path, checksum, size, filename = await persist_remote_pdf(
                    url,
                    max_bytes=app_settings.rhp_max_pdf_bytes,
                )
            logger.info(
                "PDF upload complete: file=%s bytes=%d sha256=%s",
                filename,
                size,
                checksum,
            )
            analysis_id, should_schedule = service.enqueue(
                path,
                filename=filename,
                pdf_sha256=checksum,
                pdf_bytes=size,
                model=app_settings.rhp_default_model,
                retries=app_settings.rhp_default_retries,
                sections=parsed_sections,
                market_data=market_data,
            )
            path = None
            job = service.get_job_status(analysis_id)
            if job is not None and job["status"] != "completed":
                schedule_analysis(request.app, analysis_id)
            public_id = service.public_analysis_id(analysis_id)
            url = f"{request_base_url}/analysis/{public_id}"
            response.headers["Location"] = url
            logger.info(
                "Analysis link ready: file=%s analysis_id=%s new_job=%s elapsed=%.1fs",
                filename,
                analysis_id,
                should_schedule,
                time.monotonic() - started,
            )
            return AnalysisLinkResponse(url=url)
        finally:
            if file is not None:
                await file.close()
            if path is not None:
                path.unlink(missing_ok=True)

    @app.get(
        "/v1/analyses/{analysis_id}/status",
        response_model=AnalysisStatusResponse,
    )
    async def get_analysis_status(
        analysis_id: str,
        request: Request,
        response: Response,
    ) -> AnalysisStatusResponse:
        service: AnalysisService = request.app.state.analysis_service
        result = service.get_job_status(analysis_id)
        if result is None:
            raise HTTPException(status_code=404, detail="The analysis does not exist.")
        response.headers["Cache-Control"] = "no-store"
        return AnalysisStatusResponse.model_validate(
            {
                **result,
                "analysis_id": service.public_analysis_id(str(result["analysis_id"])),
            }
        )

    @app.get(
        "/v1/analyses/{analysis_id}",
        response_model=AnalysisResponse,
    )
    async def get_analysis(analysis_id: str, request: Request) -> AnalysisResponse:
        service: AnalysisService = request.app.state.analysis_service
        result = service.get_cached(analysis_id)
        if result is None:
            raise HTTPException(status_code=404, detail="The analysis does not exist.")
        return result.model_copy(
            update={"analysis_id": service.public_analysis_id(result.analysis_id)}
        )

    @app.get(
        "/analysis/{analysis_id}",
        response_class=HTMLResponse,
        name="analysis_page_route",
    )
    async def analysis_page_route(analysis_id: str, request: Request) -> Response:
        service: AnalysisService = request.app.state.analysis_service
        job = service.get_job_status(analysis_id)
        if job is None:
            raise HTTPException(status_code=404, detail="The analysis does not exist.")
        full_analysis_id = str(job["analysis_id"])
        short_analysis_id = service.public_analysis_id(full_analysis_id)
        if analysis_id != short_analysis_id:
            return RedirectResponse(
                request.url_for("analysis_page_route", analysis_id=short_analysis_id),
                status_code=status.HTTP_308_PERMANENT_REDIRECT,
            )
        analysis = (
            service.get_cached(full_analysis_id)
            if job["status"] == "completed"
            else None
        )
        report_title = (
            web.analysis_title(
                {
                    "filename": job.get("filename", "RHP analysis"),
                    "company_name": job.get("company_name"),
                    "report_markdown": analysis.report_markdown,
                }
            )
            if analysis is not None
            else None
        )
        report_body_html = ""
        report_toc: list[dict[str, str | int]] = []
        if analysis is not None:
            report_body_html, report_toc = web.report_content(analysis)
        cache_control = "public, max-age=300" if analysis is not None else "no-store"
        return web.templates.TemplateResponse(
            request=request,
            name="analysis.html",
            context={
                "job": job,
                "analysis": analysis,
                "filename": job.get("filename", "RHP analysis"),
                "report_title": report_title,
                "progress": min(
                    round(job["completed_sections"] * 100 / job["total_sections"]),
                    100,
                ),
                "report_body_html": report_body_html,
                "report_toc": report_toc,
                "report_description": web.report_description(report_title)
                if report_title is not None
                else "",
                "report_date": web.format_timestamp(analysis.metadata.created_at)
                if analysis is not None
                else "",
                "canonical_url": str(request.url),
                "status_url": str(
                    request.url_for(
                        "get_analysis_status", analysis_id=short_analysis_id
                    )
                ),
            },
            headers={"Cache-Control": cache_control},
        )

    return app


app = create_app()
