"""累计抄码 CSV 导入：原件落盘 + 示数入库 + 相邻整点作差派生 + 日报失效。

一次导入的原子边界：

1. 解析校验（编码/表头/整点/非负/≤500 行/文件内同点同刻同值）；
2. 原件按字节写入内容寻址文件（写临时文件 → fsync → 原子 rename → fsync 目录）；
3. ``BEGIN IMMEDIATE`` 后在一个事务内完成：
   批次与行原文落库、示数配对/插入、派生读数插入、受影响日报失效；
   任一条业务校验失败则整批回滚（文件成为无引用孤儿，启动时清理）。

因此不存在“库内有可用派生读数但原件缺失”或“半批示数/派生”的已提交状态。
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import db as db_module
from . import originals
from .csv_import import ParsedCsv, distinct_key_count, parse_meter_csv
from .db import connect
from .schemas import DomainError, now_utc_iso, shift_hour
from .services import _invalidate_reports_for_hours, _point_exists

# 同一文件内同点同刻同值视为重传；异值在解析阶段已拒收。
_ORIGINAL_NAME_SAFE_RE = re.compile(r"[^0-9A-Za-z一-鿿._（）()\- ]+")

# 启动时若发现数据库引用的原件缺失/损坏，拒绝启动，避免“读数可用但无原件”。
VERIFY_ORIGINALS_ON_START = os.environ.get(
    "SUBSTATION_VERIFY_ORIGINALS_ON_START", "1"
) not in ("0", "false", "False")


def _path(db_path: str | Path | None) -> str | Path:
    """未显式传入时回落到 db.DB_PATH（测试会 monkeypatch 该模块属性）。"""
    return db_path if db_path is not None else db_module.DB_PATH


def _safe_original_name(name: str) -> str:
    base = Path(name or "upload.csv").name
    base = _ORIGINAL_NAME_SAFE_RE.sub("_", base).strip(" .")
    if not base:
        base = "upload.csv"
    return base[:120]


def _summary_row(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
    batch = conn.execute(
        "SELECT * FROM import_batch WHERE id=?", (batch_id,)
    ).fetchone()
    if batch is None:
        raise DomainError(f"导入批次 {batch_id} 不存在")
    derived = conn.execute(
        "SELECT COUNT(*) AS n FROM import_row ir "
        "WHERE ir.batch_id=? AND ir.is_first_for_key=1",
        (batch_id,),
    ).fetchone()["n"]
    return {
        "batch_id": batch["id"],
        "sha256": batch["sha256"],
        "original_name": batch["original_name"],
        "byte_size": batch["byte_size"],
        "row_count": batch["row_count"],
        "created_at": batch["created_at"],
        "stored_name": originals.stored_name_for(batch["sha256"]),
        "download_url": f"/api/imports/{batch['id']}/download",
        "distinct_endpoints": derived,
    }


def _endpoint_view(
    conn: sqlite3.Connection, point_id: str, hour: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT point_id, hour, cumulative_kwh, batch_id, line_no, created_at "
        "FROM meter_reading WHERE point_id=? AND hour=?",
        (point_id, hour),
    ).fetchone()
    return None if row is None else dict(row)


def _has_manual_reading(conn: sqlite3.Connection, point_id: str, hour: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM reading WHERE point_id=? AND hour=? AND source='manual' LIMIT 1",
        (point_id, hour),
    ).fetchone() is not None


def _max_revision(conn: sqlite3.Connection, point_id: str, hour: str) -> int:
    row = conn.execute(
        "SELECT MAX(revision) AS m FROM reading WHERE point_id=? AND hour=?",
        (point_id, hour),
    ).fetchone()
    return int(row["m"] or 0)


def _derived_revision(
    conn: sqlite3.Connection, point_id: str, hour: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM reading WHERE point_id=? AND hour=? AND source='derived' "
        "ORDER BY revision DESC LIMIT 1",
        (point_id, hour),
    ).fetchone()
    return None if row is None else dict(row)


def import_cumulative_csv(
    original_name: str,
    data: bytes,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    if not data:
        raise DomainError("上传的 CSV 为空")
    if len(data) > originals.MAX_UPLOAD_BYTES:
        raise DomainError(
            f"CSV 文件过大（{len(data)} 字节，上限 {originals.MAX_UPLOAD_BYTES} 字节）"
        )

    # 1) 与数据库无关的严格校验：任何一条不合规则不产生任何落盘/落库副作用
    parsed: ParsedCsv = parse_meter_csv(data)
    sha256 = originals.sha256_bytes(data)
    safe_name = _safe_original_name(original_name)

    # 2) 先把原件原子落盘并 fsync，再开启写事务；崩溃只会留下孤儿文件，不会缺原件
    originals.store_original(data, sha256, _path(db_path))

    created_at = now_utc_iso()
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing_batch = conn.execute(
            "SELECT * FROM import_batch WHERE sha256=?", (sha256,)
        ).fetchone()
        if existing_batch is not None:
            # 同内容重传：整份文件已在库，保留首次来源，不新增任何派生/失效。
            # 顺便用相同字节修复可能丢失的原件（内容寻址，安全幂等）。
            originals.ensure_original(
                data, sha256,
                originals.stored_name_for(existing_batch["sha256"]),
                _path(db_path),
            )
            summary = _summary_row(conn, existing_batch["id"])
            conn.execute("COMMIT")
            summary.update({
                "status": "duplicate_ignored",
                "new_endpoints": 0,
                "duplicate_endpoints": distinct_key_count(parsed),
                "new_derived_readings": 0,
                "duplicate_derived_readings": 0,
                "invalidated_reports": 0,
                "derived": [],
            })
            return summary

        cur = conn.execute(
            "INSERT INTO import_batch(sha256, original_name, byte_size, row_count, created_at) "
            "VALUES (?,?,?,?,?)",
            (sha256, safe_name, len(data), len(parsed.rows), created_at),
        )
        batch_id = cur.lastrowid

        conn.executemany(
            "INSERT INTO import_row(batch_id, line_no, point_id, hour, cumulative_kwh, "
            "is_first_for_key) VALUES (?,?,?,?,?,?)",
            [
                (batch_id, r.line_no, r.point_id, r.hour, r.cumulative_kwh,
                 1 if r.first_in_file else 0)
                for r in parsed.rows
            ],
        )

        # 3) 示数配对：同点同刻同值保留首次来源；异值整批拒收
        new_keys: list[tuple[str, str]] = []
        duplicate_endpoints = 0
        for r in parsed.rows:
            if not r.first_in_file:
                continue
            row = conn.execute(
                "SELECT cumulative_kwh FROM meter_reading WHERE point_id=? AND hour=?",
                (r.point_id, r.hour),
            ).fetchone()
            if row is not None:
                if row["cumulative_kwh"] != r.cumulative_kwh:
                    raise DomainError(
                        f"计量点 {r.point_id} 在 {r.hour} 的累计示数已存在且不同"
                        f"（库内 {row['cumulative_kwh']}，本次第 {r.line_no} 行 "
                        f"{r.cumulative_kwh}），整批拒收"
                    )
                duplicate_endpoints += 1
                continue
            conn.execute(
                "INSERT INTO meter_reading(point_id, hour, cumulative_kwh, batch_id, "
                "line_no, created_at) VALUES (?,?,?,?,?,?)",
                (r.point_id, r.hour, r.cumulative_kwh, batch_id, r.line_no, created_at),
            )
            new_keys.append((r.point_id, r.hour))

        # 4) 仅相邻整点作差，计入起始小时；缺少端点不生成
        candidates: set[tuple[str, str]] = set()
        for pid, hour in new_keys:
            candidates.add((pid, hour))          # 与 hour+1 配对
            candidates.add((pid, shift_hour(hour, -1)))  # 作为上一小时的终点

        new_derived: list[dict[str, Any]] = []
        duplicate_derived = 0
        derived_hours: list[str] = []
        for pid, hour in sorted(candidates):
            start = _endpoint_view(conn, pid, hour)
            end = _endpoint_view(conn, pid, shift_hour(hour, 1))
            if start is None or end is None:
                continue  # 缺少端点，不生成
            energy = end["cumulative_kwh"] - start["cumulative_kwh"]
            if energy < 0:
                raise DomainError(
                    f"计量点 {pid} 在 {hour} 的派出差值为负"
                    f"（{start['cumulative_kwh']} → {end['cumulative_kwh']}），整批拒收"
                )
            # 派生读数即正式小时电量，遵循与手工读数一致的登记要求：
            # 计量点必须已在某版已提交配置中登记，否则整批拒收
            if not _point_exists(conn, pid):
                raise DomainError(
                    f"计量点 {pid} 尚未在任何已提交配置中登记，"
                    f"其 {hour} 的派生电量拒收（整批回滚）"
                )

            existing_derived = _derived_revision(conn, pid, hour)
            if existing_derived is not None:
                # 两端示数一旦落库不可改写，同一对端点的派生值唯一
                if existing_derived["energy"] != energy:
                    raise DomainError(
                        f"计量点 {pid} 在 {hour} 的派生电量与已有派生修订不一致，整批拒收"
                    )
                duplicate_derived += 1
                continue

            if _has_manual_reading(conn, pid, hour):
                raise DomainError(
                    f"计量点 {pid} 在 {hour} 新派生的电量与已有手工读数冲突，整批拒收"
                )

            revision = _max_revision(conn, pid, hour) + 1
            submitted = (
                f"derived cumulative {pid}@{hour} {start['cumulative_kwh']} -> "
                f"{end['cumulative_kwh']} = {energy}"
            )
            conn.execute(
                """
                INSERT INTO reading(point_id, hour, revision, energy, submitted_text,
                                    created_at, source, start_batch_id, start_line_no,
                                    start_cumulative_kwh, end_batch_id, end_line_no,
                                    end_cumulative_kwh)
                VALUES (?,?,?,?,?,?,'derived',?,?,?,?,?,?)
                """,
                (
                    pid, hour, revision, energy, submitted, created_at,
                    start["batch_id"], start["line_no"], start["cumulative_kwh"],
                    end["batch_id"], end["line_no"], end["cumulative_kwh"],
                ),
            )
            derived_hours.append(hour)
            new_derived.append({
                "point_id": pid,
                "hour": hour,
                "revision": revision,
                "energy": energy,
                "start": {
                    "cumulative_kwh": start["cumulative_kwh"],
                    "batch_id": start["batch_id"],
                    "line_no": start["line_no"],
                },
                "end": {
                    "cumulative_kwh": end["cumulative_kwh"],
                    "batch_id": end["batch_id"],
                    "line_no": end["line_no"],
                },
            })

        # 5) 派生读数与“旧日报失效”同一事务提交
        invalidated = _invalidate_reports_for_hours(
            conn,
            derived_hours,
            f"累计抄码导入派生/更正电量（批次 {batch_id}，原件 SHA-256 {sha256[:12]}…）",
            created_at,
        )

        conn.execute("COMMIT")
        summary = _summary_row(conn, batch_id)
        summary.update({
            "status": "created",
            "new_endpoints": len(new_keys),
            "duplicate_endpoints": duplicate_endpoints,
            "new_derived_readings": len(new_derived),
            "duplicate_derived_readings": duplicate_derived,
            "invalidated_reports": invalidated,
            "derived": new_derived,
        })
        return summary
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        # 事务失败：本次原件若无其他批次引用，交由启动清理；不在此删除，
        # 避免并发实例恰好引用同一内容寻址文件时误删。
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 查询：批次、行原文、原件下载
# ---------------------------------------------------------------------------

def list_import_batches(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM import_batch ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            item = _summary_row(conn, r["id"])
            item["derived_reading_count"] = conn.execute(
                "SELECT COUNT(*) AS n FROM reading WHERE source='derived' "
                "AND (start_batch_id=? OR end_batch_id=?)",
                (r["id"], r["id"]),
            ).fetchone()["n"]
            out.append(item)
        return out
    finally:
        conn.close()


def get_import_batch(batch_id: int, db_path: str | Path | None = None) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        summary = _summary_row(conn, batch_id)
        rows = conn.execute(
            "SELECT line_no, point_id, hour, cumulative_kwh, is_first_for_key "
            "FROM import_row WHERE batch_id=? ORDER BY line_no",
            (batch_id,),
        ).fetchall()
        summary["rows"] = [
            {
                "line_no": r["line_no"],
                "point_id": r["point_id"],
                "hour": r["hour"],
                "cumulative_kwh": r["cumulative_kwh"],
                "first_in_file": bool(r["is_first_for_key"]),
            }
            for r in rows
        ]
        summary["derived_readings"] = [
            {
                "point_id": r["point_id"],
                "hour": r["hour"],
                "revision": r["revision"],
                "energy": r["energy"],
                "source": r["source"],
                "start_batch_id": r["start_batch_id"],
                "start_line_no": r["start_line_no"],
                "start_cumulative_kwh": r["start_cumulative_kwh"],
                "end_batch_id": r["end_batch_id"],
                "end_line_no": r["end_line_no"],
                "end_cumulative_kwh": r["end_cumulative_kwh"],
            }
            for r in conn.execute(
                "SELECT * FROM reading WHERE source='derived' "
                "AND (start_batch_id=? OR end_batch_id=?) ORDER BY hour, point_id",
                (batch_id, batch_id),
            ).fetchall()
        ]
        return summary
    finally:
        conn.close()


def read_import_original(
    batch_id: int, db_path: str | Path | None = None
) -> tuple[bytes, dict[str, Any]]:
    """读取并 SHA-256 核验原件；核验失败抛 FileIntegrityError，绝不返回未核验字节。"""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, sha256, original_name FROM import_batch WHERE id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise DomainError(f"导入批次 {batch_id} 不存在")
        data = originals.read_verified(
            originals.stored_name_for(row["sha256"]), row["sha256"], _path(db_path)
        )
        return data, {"filename": row["original_name"], "sha256": row["sha256"]}
    finally:
        conn.close()


def verify_import_batch(batch_id: int, db_path: str | Path | None = None) -> dict[str, Any]:
    data, meta = read_import_original(batch_id, db_path)
    return {
        "batch_id": batch_id,
        "sha256": meta["sha256"],
        "recomputed_sha256": originals.sha256_bytes(data),
        "byte_size": len(data),
        "verified": True,
    }


# ---------------------------------------------------------------------------
# 启动时一致性检查 / 孤儿清理
# ---------------------------------------------------------------------------

def reconcile_originals(db_path: str | Path | None = None) -> dict[str, Any]:
    """校验所有被引用的原件存在且 SHA-256 相符，并清理无引用孤儿文件。

    校验失败时抛出 RuntimeError（应用拒绝启动），确保不会对外提供
    “无原件可核验”的可用读数。孤儿清理有最小年龄保护，避免误删并发上传。
    """
    conn = connect(db_path)
    try:
        batches = conn.execute("SELECT id, sha256 FROM import_batch").fetchall()
    finally:
        conn.close()

    path = _path(db_path)
    referenced: set[str] = set()
    missing: list[int] = []
    corrupt: list[int] = []
    for b in batches:
        name = originals.stored_name_for(b["sha256"])
        referenced.add(name)
        try:
            data = originals.read_verified(name, b["sha256"], path)
            del data
        except originals.FileIntegrityError as exc:
            if "缺失" in str(exc):
                missing.append(b["id"])
            else:
                corrupt.append(b["id"])

    removed = originals.cleanup_orphans(path, referenced)

    if missing or corrupt:
        raise RuntimeError(
            "原件一致性检查失败，拒绝启动："
            f"缺失批次 {missing or '无'}，SHA-256 不符批次 {corrupt or '无'}；"
            "请从备份恢复 data/originals 下对应 <sha256>.csv 后重启"
        )
    return {
        "batches": len(batches),
        "originals_verified": len(referenced) - len(missing) - len(corrupt),
        "orphans_removed": removed,
    }
