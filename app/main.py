"""FastAPI 入口：配置提交、读数录入、质检日报生成/查看，以及静态网页。"""
from __future__ import annotations

from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import services
from .db import init_db
from .schemas import DomainError, ReadingSubmit, ReportRequest, ConfigSubmit


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="变电站逐小时电量质检系统",
    description=(
        "按 UTC 整点整版提交进/出线计量点配置；按计量点和小时录入带递增修订号的 "
        "非负电量；选择日期与容差生成质检日报，配置或读数追溯变更会自动失效受影响日报。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.exception_handler(DomainError)
def _domain_error_handler(_request, exc: DomainError):
    return JSONResponse(status_code=400, content={"error": exc.message})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/api/configs", summary="整版提交计量点配置（按生效 UTC 小时）")
def api_submit_config(payload: ConfigSubmit):
    return services.submit_config(payload.model_dump())


@app.get("/api/configs", summary="查看全部配置版本")
def api_list_configs():
    return services.list_config_versions()


@app.post("/api/readings", summary="录入逐小时电量（递增修订号、非负）")
def api_submit_readings(payload: ReadingSubmit):
    return services.submit_readings(payload.model_dump())


@app.get("/api/readings", summary="查看读数（可按计量点/小时过滤）")
def api_list_readings(
    point_id: str | None = Query(default=None),
    hour: str | None = Query(default=None),
):
    return services.list_readings(point_id=point_id, hour=hour)


@app.post("/api/reports/generate", summary="生成（或幂等取回）质检日报")
def api_generate_report(payload: ReportRequest):
    return services.generate_report(payload.model_dump())


@app.get("/api/reports", summary="列出日报（默认含已失效日报）")
def api_list_reports(
    report_date: str | None = Query(default=None),
    include_invalid: bool = Query(default=True),
):
    return services.list_reports(report_date=report_date, include_invalid=include_invalid)


@app.get("/api/reports/{report_id}", summary="查看单份日报及其依据快照")
def api_get_report(report_id: int):
    return services.get_report(report_id)


@app.get("/healthz", summary="健康检查")
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 静态网页
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
