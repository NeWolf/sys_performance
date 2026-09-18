"""Loopback-only HTTP API and production frontend hosting."""
from contextlib import asynccontextmanager
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from pydantic import BaseModel, ConfigDict, Field, model_validator

from perf_metrics import resource_settings

from perf_adb import AdbController, AdbError
from perf_report import render_report
from perf_store import Store

MAX_FILE_BYTES = 1024 * 1024 * 1024
# 最多 5 个文件，额外预留 1 MiB multipart 表单开销。
MAX_BODY_BYTES = 5 * MAX_FILE_BYTES + 1024 * 1024


class LocalBoundary:
    """Reject untrusted requests before multipart parsing or disk writes."""
    def __init__(self, app, token, port, development=False):
        self.app = app
        self.token = token
        ports = [port] + ([5173] if development else [])
        self.hosts = {f"{host}:{p}" for host in ("127.0.0.1", "localhost") for p in ports}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode("latin1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        error = None
        if host not in self.hosts:
            error = (403, "仅允许本机访问")
        elif headers.get("origin") not in (None, "http://" + host):
            error = (403, "不允许跨来源访问")
        elif headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            error = (403, "不允许跨站访问")
        elif scope["path"].startswith("/api/") and scope["path"] != "/api/token":
            supplied = headers.get("x-session-token", "")
            if not secrets.compare_digest(supplied.encode(), self.token.encode()):
                error = (403, "本地会话令牌无效")
        try:
            size = int(headers.get("content-length", "0"))
            if size < 0 or size > MAX_BODY_BYTES:
                error = (413, "请求超过 5 GB 日志容量及表单开销限制")
        except ValueError:
            error = (400, "无效的请求长度")
        if error:
            return await JSONResponse({"detail": error[1]}, status_code=error[0])(scope, receive, send)
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_BODY_BYTES:
                raise HTTPException(413, "请求超过 5 GB 日志容量及表单开销限制")
            return message

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                ])
            await send(message)
        await self.app(scope, bounded_receive, secure_send)


@asynccontextmanager
async def upload_form(request):
    """Close spooled files even on body limits, disconnects or cancellation."""
    parser = MultiPartParser(request.headers, request.stream(), max_files=5,
                             max_fields=1, max_part_size=4096)
    try:
        yield await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(400, exc.message) from exc
    finally:
        # Starlette only performs this cleanup for MultiPartException itself.
        # Keep covered by regression tests when upgrading the pinned dependency.
        for file in parser._files_to_close_on_error:
            file.close()


class AdbRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    serial: str = Field(min_length=1, max_length=200)
    confirm: Literal[True]


class AdbStartRequest(AdbRequest):
    interval: int = Field(default=1, ge=1, le=3600, strict=True)
    tologcat: bool = False
    async_write: bool = False


class AdbPullRequest(AdbRequest):
    title: str = Field(default="", max_length=120)


class GroupMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pid: int = Field(ge=0, le=2**63 - 1, strict=True)
    name: str = Field(min_length=1, max_length=1024 * 1024)
    service_category: Literal["未分类", "应用服务", "系统服务"] = "未分类"
    related_service: str = Field(default="", max_length=2000)
    process_type: str = Field(default="", max_length=120)
    purpose: str = Field(default="", max_length=2000)
    foreground: Literal["待填写", "Y", "N"] = "待填写"
    background: Literal["待填写", "Y", "N"] = "待填写"


class GroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    segment: int = Field(ge=0, le=2**63 - 1, strict=True)
    members: list[GroupMember] = Field(min_length=1, max_length=50)
    scene: Literal["未标注", "前台", "后台"] = "未标注"
    start: Optional[int] = Field(default=None, ge=-(2**63), le=2**63 - 1, strict=True)
    end: Optional[int] = Field(default=None, ge=-(2**63), le=2**63 - 1, strict=True)
    description: str = Field(default="", max_length=4000)


class ReportSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu_platform: str = Field(default="", max_length=120)
    kdmips_per_core: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def validate_calibration(self):
        resource_settings(self.model_dump())
        return self

    hardware: str = Field(default="", max_length=2000)
    software: str = Field(default="", max_length=2000)
    tester: str = Field(default="", max_length=200)
    test_notes: str = Field(default="", max_length=4000)
    worst_scenarios: str = Field(default="", max_length=8000)
    analysis: str = Field(default="", max_length=8000)
    criteria: str = Field(default="", max_length=8000)
    conclusion: Literal["待评估", "准入", "有条件准入", "不准入"] = "待评估"
    conclusion_notes: str = Field(default="", max_length=8000)


def create_app(database, port=8765, development=False, frontend=None):
    store = Store(database)
    token = secrets.token_urlsafe(32)
    import_lock = threading.Lock()
    app = FastAPI(title="sysmonitor 本地分析", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    adb = AdbController(Path(database).resolve().parent / "adb_logs")
    app.state.adb = adb

    @app.exception_handler(AdbError)
    async def adb_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/api/adb/devices")
    def adb_devices():
        return adb.devices()

    @app.get("/api/adb/status")
    def adb_status(serial: str = Query(min_length=1, max_length=200)):
        return adb.status(serial)

    @app.post("/api/adb/start")
    def adb_start(body: AdbStartRequest):
        return adb.start(body.serial, body.interval, body.tologcat, body.async_write)

    @app.post("/api/adb/stop")
    def adb_stop(body: AdbRequest):
        return adb.stop(body.serial)

    @app.post("/api/adb/pull")
    def adb_pull(body: AdbPullRequest):
        if not import_lock.acquire(blocking=False):
            raise HTTPException(409, "已有导入正在进行，请稍后重试")
        try:
            return adb.pull(body.serial, store, body.title.strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            import_lock.release()

    app.add_middleware(LocalBoundary, token=token, port=port, development=development)

    @app.exception_handler(KeyError)
    async def missing(_request, _exc):
        return JSONResponse({"detail": "会话或进程组不存在或已删除"}, status_code=404)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(_request, _exc):
        return JSONResponse({"detail": "本地数据库忙碌或存储空间不足，请稍后重试"}, status_code=503)

    @app.get("/api/token")
    def session_token():
        return {"token": token}

    @app.get("/api/sessions")
    def sessions():
        return store.sessions()

    @app.post("/api/import")
    async def import_logs(request: Request):
        if not import_lock.acquire(blocking=False):
            raise HTTPException(409, "已有导入正在进行，请稍后重试")
        try:
            async with upload_form(request) as form:
                files = form.getlist("files")
                title = form.get("title", "")
                if not files or any(not isinstance(file, UploadFile) for file in files):
                    raise HTTPException(400, "请选择 1 至 5 个日志文件")
                if not isinstance(title, str) or len(title) > 120:
                    raise HTTPException(400, "会话名称最多 120 个字符")
                if any(key not in ("files", "title") for key in form):
                    raise HTTPException(400, "包含不支持的表单字段")
                if any(file.size is None or file.size > MAX_FILE_BYTES for file in files):
                    raise HTTPException(413, "每个日志文件最多 1 GB（1024 MB）")
                sources = [(file.filename or "perf.log", file.file) for file in files]
                try:
                    return await run_in_threadpool(store.import_files, sources, title.strip())
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
        finally:
            import_lock.release()

    @app.get("/api/sessions/{session_id}")
    def overview(session_id: str):
        return store.overview(session_id)

    @app.delete("/api/sessions/{session_id}")
    def delete(session_id: str):
        if not store.delete(session_id):
            raise HTTPException(404, "会话不存在或已删除")
        return {"deleted": True}

    @app.get("/api/sessions/{session_id}/series")
    def series(session_id: str, kind: Literal["S", "P", "D", "DE", "DP"],
               limit: int = Query(1500, ge=64, le=5000),
               pid: Optional[int] = Query(None, ge=0, le=2**63 - 1),
               segment: Optional[int] = Query(None, ge=0, le=2**63 - 1),
               name: Optional[str] = Query(None, max_length=1024 * 1024)):
        return store.series(session_id, kind, limit, pid, segment, name)

    @app.get("/api/sessions/{session_id}/statistics")
    def statistics(session_id: str, kind: Literal["S", "P", "D", "DE", "DP"],
                   pid: Optional[int] = Query(None, ge=0, le=2**63 - 1),
                   segment: Optional[int] = Query(None, ge=0, le=2**63 - 1),
                   name: Optional[str] = Query(None, max_length=1024 * 1024)):
        return store.statistics(session_id, kind, pid, segment, name)

    @app.get("/api/sessions/{session_id}/processes")
    def processes(session_id: str,
                  sort: Literal["cpu_peak", "cpu_avg", "cpu_p95", "cpu_p99", "rss_peak_kb", "rss_avg_kb", "rss_p95_kb", "rss_p99_kb", "read_kb", "write_kb", "wait_peak", "dmabuf_peak_kb"] = "cpu_peak",
                  limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0, le=2**63 - 1),
                  q: str = Query("", max_length=256), with_total: bool = False, merge_names: bool = False,
                  segment: Optional[int] = Query(None, ge=0,le=2**63 - 1)):
        return store.processes(session_id, sort, limit, offset, q, with_total, merge_names, segment)

    @app.get("/api/sessions/{session_id}/groups")
    def groups(session_id: str):
        return store.groups(session_id)

    @app.post("/api/sessions/{session_id}/groups", status_code=201)
    def create_group(session_id: str, body: GroupRequest):
        try:
            return store.save_group(session_id, body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/groups/{group_id}")
    def group(session_id: str, group_id: str):
        return store.group(session_id, group_id)

    @app.put("/api/sessions/{session_id}/groups/{group_id}")
    def update_group(session_id: str, group_id: str, body: GroupRequest):
        try:
            return store.save_group(session_id, body.model_dump(), group_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/sessions/{session_id}/groups/{group_id}")
    def delete_group(session_id: str, group_id: str):
        if not store.delete_group(session_id, group_id):
            raise HTTPException(404, "进程组不存在或已删除")
        return {"deleted": True}

    @app.get("/api/sessions/{session_id}/groups/{group_id}/analysis")
    def group_analysis(session_id: str, group_id: str, kind: Literal["P", "DP"] = "P",
                       limit: int = Query(600, ge=64, le=5000)):
        return store.group_analysis(session_id, group_id, kind, limit)

    @app.get("/api/sessions/{session_id}/groups/{group_id}/resources")
    def group_resources(session_id: str, group_id: str):
        return store.group_resources(session_id, group_id)

    @app.get("/api/sessions/{session_id}/report-settings")
    def report_settings(session_id: str):
        return store.report_settings(session_id)

    @app.put("/api/sessions/{session_id}/report-settings")
    def save_report_settings(session_id: str, body: ReportSettingsRequest):
        try:
            return store.save_report_settings(session_id, body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/report")
    def report(session_id: str):
        return HTMLResponse(render_report(store, session_id), headers={
            "Content-Disposition": 'attachment; filename="sysmonitor-report.html"',
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        })

    @app.get("/api/{remaining:path}")
    def unknown_api(remaining: str):
        raise HTTPException(404, "接口不存在")

    assets = Path(frontend) if frontend else Path(__file__).parent / "frontend" / "dist"
    if (assets / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(assets), html=True), name="frontend")
    else:
        @app.get("/")
        def not_built():
            return Response("前端尚未构建，请先在 frontend 目录执行 npm run build。", media_type="text/plain", status_code=503)
    return app