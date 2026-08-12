from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote

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
from fastapi.responses import HTMLResponse
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


def validate_remote_host(url: httpx.URL, allowed_hosts: set[str]) -> None:
    host = (url.host or "").lower().rstrip(".")
    if host not in allowed_hosts:
        allowed = ", ".join(sorted(allowed_hosts))
        raise HTTPException(
            status_code=422,
            detail=f"The PDF URL host is not allowed. Allowed hosts: {allowed}.",
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
    allowed_hosts: set[str],
) -> tuple[Path, str, int, str]:
    current_url = httpx.URL(str(source_url))
    headers = {
        "Accept": "application/pdf",
        "User-Agent": "RHP-Analyzer/0.1",
    }
    path: Path | None = None
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for _ in range(6):
                validate_remote_host(current_url, allowed_hosts)
                async with client.stream(
                    "GET", current_url, headers=headers
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise HTTPException(
                                status_code=502,
                                detail="The PDF server returned an invalid redirect.",
                            )
                        current_url = response.url.join(location)
                        continue
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise HTTPException(
                            status_code=502,
                            detail=(
                                f"The PDF server returned HTTP {response.status_code}."
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
                            status_code=502,
                            detail="The PDF server returned an empty file.",
                        )
                    if first_bytes != b"%PDF-":
                        raise HTTPException(
                            status_code=415,
                            detail="The remote file is not a PDF.",
                        )
                    filename = safe_filename(unquote(Path(current_url.path).name))
                    return path, digest.hexdigest(), size, filename
            raise HTTPException(
                status_code=502,
                detail="The PDF server returned too many redirects.",
            )
    except HTTPException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    except (httpx.HTTPError, ValueError) as exc:
        if path is not None:
            path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=502,
            detail="The service could not download the PDF.",
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
    ) -> AnalysisLinkResponse:
        service: AnalysisService = request.app.state.analysis_service
        if (file is None) == (url is None):
            raise HTTPException(
                status_code=422,
                detail="Send one PDF source. Use either file or url.",
            )
        parsed_sections = parse_sections(sections, service)
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
                    allowed_hosts=app_settings.allowed_pdf_hosts(),
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
            )
            path = None
            job = service.get_job_status(analysis_id)
            if job is not None and job["status"] != "completed":
                schedule_analysis(request.app, analysis_id)
            url = f"{request_base_url}/analysis/{analysis_id}"
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
        return AnalysisStatusResponse.model_validate(result)

    @app.get(
        "/v1/analyses/{analysis_id}",
        response_model=AnalysisResponse,
    )
    async def get_analysis(analysis_id: str, request: Request) -> AnalysisResponse:
        service: AnalysisService = request.app.state.analysis_service
        result = service.get_cached(analysis_id)
        if result is None:
            raise HTTPException(status_code=404, detail="The analysis does not exist.")
        return result

    @app.get(
        "/analysis/{analysis_id}",
        response_class=HTMLResponse,
        name="analysis_page_route",
    )
    async def analysis_page_route(analysis_id: str, request: Request) -> HTMLResponse:
        service: AnalysisService = request.app.state.analysis_service
        job = service.get_job_status(analysis_id)
        if job is None:
            raise HTTPException(status_code=404, detail="The analysis does not exist.")
        analysis = (
            service.get_cached(analysis_id) if job["status"] == "completed" else None
        )
        cache_control = "public, max-age=300" if analysis is not None else "no-store"
        return web.templates.TemplateResponse(
            request=request,
            name="analysis.html",
            context={
                "job": job,
                "analysis": analysis,
                "filename": job.get("filename", "RHP analysis"),
                "progress": min(
                    round(job["completed_sections"] * 100 / job["total_sections"]),
                    100,
                ),
                "report_html": web.report_html(analysis)
                if analysis is not None
                else "",
                "status_url": str(
                    request.url_for("get_analysis_status", analysis_id=analysis_id)
                ),
            },
            headers={"Cache-Control": cache_control},
        )

    return app


app = create_app()
