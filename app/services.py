"""业务服务层：每个公开方法内部自行管理事务，保证原子性。

关键不变量：
1. 配置按 UTC 小时整版提交；同一版本内同一计量点不得兼任进线与出线。
2. 读数按 (计量点, 小时) 维护递增修订号；同修订号异文拒收，旧修订号拒收。
3. 换配置（含对历史生效时间的补录/覆盖）或更正读数，都会在同一事务内
   把受影响小时所在日期的旧日报置为失效；其他日期不受影响。
4. 日报生成在单个 IMMEDIATE 事务内完成，快照与结论来自同一已提交状态；
   事务失败回滚，不会留下有效的半份日报。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .db import connect
from .schemas import DomainError, hours_of_day, now_utc_iso, validate_date, validate_hour


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _canonical_config_text(effective_hour: str, points: list[dict[str, str]]) -> str:
    ordered = sorted(points, key=lambda p: (p["point_id"], p["role"]))
    return json.dumps(
        {"effective_hour": effective_hour, "points": ordered},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _canonical_reading_text(point_id: str, hour: str, revision: int, energy: float) -> str:
    return json.dumps(
        {"point_id": point_id, "hour": hour, "revision": revision, "energy": energy},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _config_at(conn: sqlite3.Connection, hour: str) -> dict[str, Any] | None:
    """取某小时当时生效的最新配置版本（effective_hour <= hour 中最大者）。"""
    row = conn.execute(
        """
        SELECT cv.* FROM config_version cv
        WHERE cv.effective_hour = (
            SELECT MAX(cv2.effective_hour) FROM config_version cv2
            WHERE cv2.effective_hour <= ?
        )
        ORDER BY cv.id DESC LIMIT 1
        """,
        (hour,),
    ).fetchone()
    if row is None:
        return None
    pts = conn.execute(
        "SELECT point_id, role FROM config_point WHERE version_id=? ORDER BY point_id",
        (row["id"],),
    ).fetchall()
    return {
        "version_id": row["id"],
        "effective_hour": row["effective_hour"],
        "created_at": row["created_at"],
        "points": [{"point_id": p["point_id"], "role": p["role"]} for p in pts],
    }


def _latest_reading(conn: sqlite3.Connection, point_id: str, hour: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT point_id, hour, revision, energy, created_at, source,
               start_batch_id, start_line_no, start_cumulative_kwh,
               end_batch_id, end_line_no, end_cumulative_kwh
        FROM reading
        WHERE point_id=? AND hour=?
        ORDER BY revision DESC LIMIT 1
        """,
        (point_id, hour),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _reading_basis(reading: dict[str, Any]) -> str:
    """人工读数引用修订与提交时刻；派生读数额外引用两端示数/批次/行号。"""
    pid, hour = reading["point_id"], reading["hour"]
    head = (
        f"reading {pid}@{hour} rev{reading['revision']} "
        f"submitted_at {reading['created_at']}"
    )
    if reading.get("source") != "derived":
        return head
    return (
        f"{head} derived_from {pid}@{hour} cumulative "
        f"{reading['start_cumulative_kwh']} (batch {reading['start_batch_id']} "
        f"line {reading['start_line_no']}) -> "
        f"{reading['end_cumulative_kwh']} (batch {reading['end_batch_id']} "
        f"line {reading['end_line_no']})"
    )


def _invalidate_reports_for_hours(
    conn: sqlite3.Connection,
    hours: list[str],
    reason: str,
    as_of: str,
) -> int:
    """把受影响小时所在自然日（UTC）的仍有效日报置为失效。同一事务内调用。"""
    affected_dates = sorted({h[:10] for h in hours})
    return _invalidate_reports_for_dates(conn, affected_dates, reason, as_of)


def _invalidate_reports_for_dates(
    conn: sqlite3.Connection,
    dates: list[str],
    reason: str,
    as_of: str,
) -> int:
    """按 UTC 自然日失效仍有效日报。同一事务内调用。"""
    if not dates:
        return 0
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"SELECT id FROM report WHERE invalidated_at IS NULL AND report_date IN ({placeholders})",
        dates,
    ).fetchall()
    for r in rows:
        conn.execute(
            """
            UPDATE report
            SET invalidated_reason = ?,
                invalidated_at = ?
            WHERE id=? AND invalidated_at IS NULL
            """,
            (reason, as_of, r["id"]),
        )
    return len(rows)


def _point_exists(conn: sqlite3.Connection, point_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM config_point WHERE point_id=? LIMIT 1", (point_id,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# 配置提交
# ---------------------------------------------------------------------------

def submit_config(payload: dict[str, Any], db_path: str | None = None) -> dict[str, Any]:
    effective_hour = validate_hour(payload["effective_hour"])
    raw_points = payload.get("points") or []
    if not raw_points:
        raise DomainError("points 不能为空，配置必须整版提交")

    points: list[dict[str, str]] = []
    seen: set[str] = set()
    for p in raw_points:
        pid = str(p["point_id"]).strip()
        role = p["role"]
        if role not in ("incoming", "outgoing"):
            raise DomainError("role 只能是 incoming（进线）或 outgoing（出线）")
        if not pid:
            raise DomainError("point_id 不能为空")
        if pid in seen:
            raise DomainError(f"计量点 {pid} 在同一版本中重复出现，不得同时兼任进线与出线")
        seen.add(pid)
        points.append({"point_id": pid, "role": role})

    incoming = [p for p in points if p["role"] == "incoming"]
    outgoing = [p for p in points if p["role"] == "outgoing"]
    if not incoming:
        raise DomainError("配置至少需要一个进线计量点")
    if not outgoing:
        raise DomainError("配置至少需要一个出线计量点")
    # 同点不得兼任（point_id 全局唯一，上面的去重已同时拦截兼任与重复）
    if {p["point_id"] for p in incoming} & {p["point_id"] for p in outgoing}:
        raise DomainError("同一计量点不得同时兼任进线与出线")

    text = _canonical_config_text(effective_hour, points)
    digest = _hash(text)
    created_at = now_utc_iso()

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        # 同一生效小时已有版本：同文幂等，异文拒收（需带新生效小时重新整版提交）
        existing = conn.execute(
            "SELECT id, content_hash FROM config_version WHERE effective_hour=? ORDER BY id DESC",
            (effective_hour,),
        ).fetchall()
        if existing:
            if any(r["content_hash"] == digest for r in existing):
                conn.execute("COMMIT")
                return {
                    "status": "duplicate_ignored",
                    "version_id": existing[0]["id"],
                    "effective_hour": effective_hour,
                    "points": points,
                    "invalidated_reports": 0,
                }
            raise DomainError(
                f"生效小时 {effective_hour} 已存在不同内容的配置版本；"
                "同版异文拒收，请使用新的生效小时整版提交"
            )

        cur = conn.execute(
            "INSERT INTO config_version(effective_hour, created_at, submitted_text, content_hash) "
            "VALUES (?,?,?,?)",
            (effective_hour, created_at, text, digest),
        )
        version_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO config_point(version_id, point_id, role) VALUES (?,?,?)",
            [(version_id, p["point_id"], p["role"]) for p in points],
        )

        # 追溯换配置：effective_hour 当日及其后的全部日报都依赖过可能受影响的配置，
        # 直接按日期范围失效（即使当日尚无读数，已生成的“不完整”日报同样要失效）
        from_date = effective_hour[:10]
        rows = conn.execute(
            "SELECT DISTINCT report_date FROM report "
            "WHERE invalidated_at IS NULL AND report_date >= ? ORDER BY report_date",
            (from_date,),
        ).fetchall()
        invalidated = _invalidate_reports_for_dates(
            conn, [r["report_date"] for r in rows],
            f"配置版本变更（新生效小时 {effective_hour}，版本 {version_id}）",
            created_at,
        )

        conn.execute("COMMIT")
        return {
            "status": "created",
            "version_id": version_id,
            "effective_hour": effective_hour,
            "points": points,
            "invalidated_reports": invalidated,
        }
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 读数录入
# ---------------------------------------------------------------------------

def submit_readings(payload: dict[str, Any], db_path: str | None = None) -> dict[str, Any]:
    items = payload.get("readings") or []
    if not items:
        raise DomainError("readings 不能为空")

    norm: list[dict[str, Any]] = []
    batch_keys: set[tuple[str, str]] = set()
    for it in items:
        pid = str(it["point_id"]).strip()
        hour = validate_hour(it["hour"])
        revision = int(it["revision"])
        if revision < 1:
            raise DomainError("revision 必须为 >=1 的递增整数")
        energy = float(it["energy"])
        if energy < 0:
            raise DomainError(f"电量必须非负，收到 {energy}")
        key = (pid, hour)
        if key in batch_keys:
            raise DomainError(f"同一请求中计量点 {pid} 在 {hour} 出现多次")
        batch_keys.add(key)
        norm.append({"point_id": pid, "hour": hour, "revision": revision, "energy": energy})

    created_at = now_utc_iso()
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        accepted: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        affected_hours: list[str] = []

        for item in norm:
            pid, hour, revision, energy = (
                item["point_id"], item["hour"], item["revision"], item["energy"],
            )
            if not _point_exists(conn, pid):
                raise DomainError(f"计量点 {pid} 尚未在任何已提交配置中登记，读数拒收")

            text = _canonical_reading_text(pid, hour, revision, energy)
            digest = _hash(text)

            row = conn.execute(
                "SELECT energy, submitted_text FROM reading WHERE point_id=? AND hour=? AND revision=?",
                (pid, hour, revision),
            ).fetchone()
            if row is not None:
                if _hash(row["submitted_text"]) == digest:
                    duplicates.append({**item, "status": "duplicate_ignored"})
                    continue
                raise DomainError(
                    f"计量点 {pid} {hour} 的修订号 {revision} 已存在且内容不同，同版异文拒收"
                )

            max_row = conn.execute(
                "SELECT MAX(revision) AS m FROM reading WHERE point_id=? AND hour=?",
                (pid, hour),
            ).fetchone()
            max_rev = max_row["m"] or 0
            expected_rev = max_rev + 1
            if revision != expected_rev:
                raise DomainError(
                    f"计量点 {pid} {hour} 修订号必须严格递增："
                    f"当前最大修订号为 {max_rev}，下一个应为 {expected_rev}，收到 {revision}"
                )

            conn.execute(
                "INSERT INTO reading(point_id, hour, revision, energy, submitted_text, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (pid, hour, revision, energy, text, created_at),
            )
            accepted.append({**item, "status": "created"})
            affected_hours.append(hour)

        invalidated = _invalidate_reports_for_hours(
            conn, affected_hours,
            "电量读数更正/新增（受影响小时重新质检）",
            created_at,
        )
        conn.execute("COMMIT")
        return {
            "status": "ok",
            "accepted": accepted,
            "duplicates": duplicates,
            "invalidated_reports": invalidated,
        }
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 日报生成 / 查询
# ---------------------------------------------------------------------------

def _serialize_report(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "report_date": row["report_date"],
        "tolerance": row["tolerance"],
        "status": row["status"],
        "detail": json.loads(row["detail"]),
        "config_snapshot": json.loads(row["config_snapshot"]),
        "reading_snapshot": json.loads(row["reading_snapshot"]),
        "created_at": row["created_at"],
        "valid": row["invalidated_at"] is None,
        "invalidated_reason": row["invalidated_reason"],
        "invalidated_at": row["invalidated_at"],
    }


def generate_report(payload: dict[str, Any], db_path: str | None = None) -> dict[str, Any]:
    report_date = validate_date(payload["report_date"])
    tolerance = float(payload["tolerance"])
    if tolerance < 0:
        raise DomainError("容差必须非负")

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")  # 与上传互斥，保证快照来自单一已提交状态

        # 幂等：同日期同容差已有有效日报则直接返回（同一已提交状态的重放）
        existing = conn.execute(
            "SELECT * FROM report WHERE report_date=? AND tolerance=? AND invalidated_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (report_date, tolerance),
        ).fetchone()
        if existing is not None:
            result = _serialize_report(existing)
            result["generation"] = "returned_existing_valid"
            conn.execute("COMMIT")
            return result

        hours = hours_of_day(report_date)
        detail: list[dict[str, Any]] = []
        config_snapshot: dict[str, Any] = {}
        reading_snapshot: dict[str, Any] = {}
        overall = "complete"
        anomaly_count = 0
        incomplete_count = 0

        for hour in hours:
            cfg = _config_at(conn, hour)
            hour_entry: dict[str, Any] = {"hour": hour}

            if cfg is None:
                hour_entry["status"] = "incomplete"
                hour_entry["reason"] = "该小时缺少生效配置"
                incomplete_count += 1
                detail.append(hour_entry)
                config_snapshot[hour] = None
                reading_snapshot[hour] = {}
                continue

            config_snapshot[hour] = cfg
            incoming = [p["point_id"] for p in cfg["points"] if p["role"] == "incoming"]
            outgoing = [p["point_id"] for p in cfg["points"] if p["role"] == "outgoing"]

            readings_hour: dict[str, dict[str, Any]] = {}
            missing: list[str] = []
            for pid in incoming + outgoing:
                r = _latest_reading(conn, pid, hour)
                if r is None:
                    missing.append(pid)
                else:
                    readings_hour[pid] = r
            reading_snapshot[hour] = readings_hour

            hour_entry["config_version_id"] = cfg["version_id"]
            hour_entry["config_effective_hour"] = cfg["effective_hour"]
            hour_entry["readings"] = [
                {
                    "point_id": pid,
                    "role": next(p["role"] for p in cfg["points"] if p["point_id"] == pid),
                    "energy": readings_hour[pid]["energy"],
                    "revision": readings_hour[pid]["revision"],
                    "source": readings_hour[pid]["source"],
                    "basis": _reading_basis(readings_hour[pid]),
                }
                if pid in readings_hour else
                {"point_id": pid, "role": (
                    "incoming" if pid in incoming else "outgoing"), "missing": True}
                for pid in incoming + outgoing
            ]

            if missing:
                hour_entry["status"] = "incomplete"
                hour_entry["reason"] = f"缺少计量点读数: {', '.join(missing)}"
                incomplete_count += 1
                detail.append(hour_entry)
                continue

            in_sum = sum(readings_hour[pid]["energy"] for pid in incoming)
            out_sum = sum(readings_hour[pid]["energy"] for pid in outgoing)
            diff = in_sum - out_sum
            hour_entry["incoming_sum"] = in_sum
            hour_entry["outgoing_sum"] = out_sum
            hour_entry["difference"] = diff
            hour_entry["abs_difference"] = abs(diff)
            hour_entry["tolerance"] = tolerance

            if abs(diff) > tolerance:
                hour_entry["status"] = "anomaly"
                hour_entry["reason"] = (
                    f"进线合计 {in_sum} - 出线合计 {out_sum} = {diff}，"
                    f"绝对差 {abs(diff)} 超过容差 {tolerance}"
                )
                anomaly_count += 1
            else:
                hour_entry["status"] = "normal"
            detail.append(hour_entry)

        if incomplete_count:
            overall = "incomplete"
        elif anomaly_count:
            overall = "anomaly"

        created_at = now_utc_iso()
        cur = conn.execute(
            """
            INSERT INTO report(report_date, tolerance, status, config_snapshot,
                               reading_snapshot, detail, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                report_date, tolerance, overall,
                json.dumps(config_snapshot, ensure_ascii=False),
                json.dumps(reading_snapshot, ensure_ascii=False),
                json.dumps(detail, ensure_ascii=False),
                created_at,
            ),
        )
        report_id = cur.lastrowid
        conn.execute("COMMIT")

        row = conn.execute("SELECT * FROM report WHERE id=?", (report_id,)).fetchone()
        result = _serialize_report(row)
        result["generation"] = "created"
        return result
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def get_report(report_id: int, db_path: str | None = None) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM report WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise DomainError(f"日报 {report_id} 不存在")
        return _serialize_report(row)
    finally:
        conn.close()


def list_reports(
    report_date: str | None = None,
    include_invalid: bool = True,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM report WHERE 1=1"
        params: list[Any] = []
        if report_date:
            sql += " AND report_date=?"
            params.append(validate_date(report_date))
        if not include_invalid:
            sql += " AND invalidated_at IS NULL"
        sql += " ORDER BY id DESC"
        rows = conn.execute(sql, params).fetchall()
        return [_serialize_report(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 配置 / 读数查询（供网页与核对使用）
# ---------------------------------------------------------------------------

def list_config_versions(db_path: str | None = None) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        versions = conn.execute(
            "SELECT id, effective_hour, created_at FROM config_version ORDER BY id DESC"
        ).fetchall()
        out = []
        for v in versions:
            pts = conn.execute(
                "SELECT point_id, role FROM config_point WHERE version_id=? ORDER BY point_id",
                (v["id"],),
            ).fetchall()
            out.append({
                "version_id": v["id"],
                "effective_hour": v["effective_hour"],
                "created_at": v["created_at"],
                "points": [{"point_id": p["point_id"], "role": p["role"]} for p in pts],
            })
        return out
    finally:
        conn.close()


def list_readings(
    point_id: str | None = None,
    hour: str | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        sql = (
            "SELECT point_id, hour, revision, energy, created_at, source, "
            "start_batch_id, start_line_no, start_cumulative_kwh, "
            "end_batch_id, end_line_no, end_cumulative_kwh "
            "FROM reading WHERE 1=1"
        )
        params: list[Any] = []
        if point_id:
            sql += " AND point_id=?"
            params.append(point_id.strip())
        if hour:
            sql += " AND hour=?"
            params.append(validate_hour(hour))
        sql += " ORDER BY hour DESC, point_id, revision DESC"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            r["basis"] = _reading_basis(r)
        return rows
    finally:
        conn.close()
