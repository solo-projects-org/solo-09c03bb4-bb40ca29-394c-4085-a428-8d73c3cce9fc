"""FastAPI 入口：配置提交、读数录入、累计抄码导入、质检日报生成/查看，以及静态网页。"""
from __future__ import annotations

from pathlib import Path

from contextlib import asynccontextmanager
from urllib.parse import quote as _quote

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import imports, originals, services
from .db import init_db
from .schemas import DomainError, ReadingSubmit, ReportRequest, ConfigSubmit


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if imports.VERIFY_ORIGINALS_ON_START:
        # 启动时核验所有原件并清理崩溃遗留孤儿；核验失败则拒绝启动，
        # 避免对外提供“读数可用但原件缺失/损坏”的已提交状态。
        imports.reconcile_originals()
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


@app.exception_handler(originals.FileIntegrityError)
def _integrity_error_handler(_request, exc: originals.FileIntegrityError):
    # 原件缺失/被篡改：绝不返回未核验字节，也不据此给出“可下载”假象
    return JSONResponse(status_code=500, content={"error": str(exc)})


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


# ---------------------------------------------------------------------------
# 累计抄码 CSV 导入（原件按字节存文件，派生读数可追溯两端示数与来源行号）
# ---------------------------------------------------------------------------

@app.post("/api/imports", summary="上传累计抄码 CSV（≤500 行），派生相邻整点电量")
async def api_import_csv(file: UploadFile = File(...)):
    data = await file.read(originals.MAX_UPLOAD_BYTES + 1)
    if len(data) > originals.MAX_UPLOAD_BYTES:
        return JSONResponse(
            status_code=400,
            content={"error": f"CSV 文件过大（上限 {originals.MAX_UPLOAD_BYTES} 字节）"},
        )
    return await run_in_threadpool(
        imports.import_cumulative_csv, file.filename or "upload.csv", data
    )


@app.get("/api/imports", summary="列出导入批次")
def api_list_imports():
    return imports.list_import_batches()


@app.get("/api/imports/{batch_id}", summary="查看批次原文行与派生读数追溯")
def api_get_import(batch_id: int):
    return imports.get_import_batch(batch_id)


@app.get("/api/imports/{batch_id}/verify", summary="重新计算原件 SHA-256")
def api_verify_import(batch_id: int):
    return imports.verify_import_batch(batch_id)


@app.get("/api/imports/{batch_id}/download", summary="下载经 SHA-256 核验的原始 CSV 文件")
def api_download_import(batch_id: int):
    try:
        data, meta = imports.read_import_original(batch_id)
    except originals.FileIntegrityError as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"original-{batch_id}.csv\"; "
                f"filename*=UTF-8''{_quote(meta['filename'], safe='')}"
            ),
            "X-Content-SHA256": meta["sha256"],
            "Cache-Control": "no-store",
        },
    )


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
