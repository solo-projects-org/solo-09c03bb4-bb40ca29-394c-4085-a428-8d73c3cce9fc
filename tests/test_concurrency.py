"""并发交错测试：生成 / 上传 / 查看同时发生时的一致性不变量。

SQLite 单写锁 + WAL 使写事务串行；这里用线程制造真实交错，验证：
1. 不会出现“半份”有效日报（要么没有，要么 24 小时齐全且快照自洽）；
2. 并发更正读数后，所有仍有效的日报都与当前库状态一致；
3. 并发提交同版本读数不会产生重复或错乱修订号。
"""
import threading
from datetime import datetime, timezone

import pytest

from app import db as db_module
from app import services

DATE = "2026-11-16"
HOURS = [f"{DATE}T{h:02d}:00:00Z" for h in range(24)]


def _assert_report_coherent(report):
    """有效日报不变量：24 小时齐全，快照与结论一致，正常/异常小时有完整依据。"""
    assert report["valid"] is True
    assert len(report["detail"]) == 24
    for h in report["detail"]:
        if h["status"] in ("normal", "anomaly"):
            assert h["config_version_id"] is not None
            assert all(not x.get("missing") for x in h["readings"])
            # 快照里的读数必须等于结论里引用的读数与修订号
            snap = report["reading_snapshot"][h["hour"]]
            for x in h["readings"]:
                assert snap[x["point_id"]]["energy"] == x["energy"]
                assert snap[x["point_id"]]["revision"] == x["revision"]
            cfg_snap = report["config_snapshot"][h["hour"]]
            snap_ids = {p["point_id"] for p in cfg_snap["points"]}
            assert {x["point_id"] for x in h["readings"]} == snap_ids


def _current_latest_energy(db_path, point_id, hour):
    conn = db_module.connect(db_path)
    try:
        row = conn.execute(
            "SELECT energy FROM reading WHERE point_id=? AND hour=? "
            "ORDER BY revision DESC LIMIT 1",
            (point_id, hour),
        ).fetchone()
        return None if row is None else row["energy"]
    finally:
        conn.close()


def test_interleaved_generation_and_uploads(tmp_db):
    db_path = tmp_db
    # 初始平衡数据
    services.submit_config({
        "effective_hour": f"{DATE}T00:00:00Z",
        "points": [
            {"point_id": "IN1", "role": "incoming"},
            {"point_id": "OUT1", "role": "outgoing"},
        ],
    }, db_path)
    services.submit_readings({"readings": [
        {"point_id": "IN1", "hour": h, "revision": 1, "energy": 100.0} for h in HOURS
    ] + [
        {"point_id": "OUT1", "hour": h, "revision": 1, "energy": 100.0} for h in HOURS
    ]}, db_path)

    errors: list[Exception] = []
    seen_reports: list[dict] = []
    seen_lock = threading.Lock()

    def worker_generate(i):
        try:
            for _ in range(8):
                r = services.generate_report(
                    {"report_date": DATE, "tolerance": float(i)}, db_path)
                with seen_lock:
                    seen_reports.append(r)
        except Exception as exc:  # pragma: no cover - 测试失败时输出
            errors.append(exc)

    def worker_correct(hour_index):
        try:
            hour = HOURS[hour_index]
            # 并发下严格递增可能撞号：撞号即重试下一个修订号，模拟排队更正
            for attempt in range(2, 12):
                try:
                    services.submit_readings({"readings": [{
                        "point_id": "IN1", "hour": hour,
                        "revision": attempt, "energy": 100.0 + attempt,
                    }]}, db_path)
                    break
                except Exception:
                    continue
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def worker_view():
        try:
            for _ in range(20):
                for r in services.list_reports(report_date=DATE, db_path=db_path):
                    full = services.get_report(r["id"], db_path)
                    if full["valid"]:
                        _assert_report_coherent(full)
                        with seen_lock:
                            seen_reports.append(full)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker_generate, args=(i,)) for i in range(3)]
    threads += [threading.Thread(target=worker_correct, args=(h,)) for h in range(24)]
    threads += [threading.Thread(target=worker_view) for _ in range(2)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []

    # 所有被观察到的“有效”日报都必须自洽（无半份日报）
    for r in seen_reports:
        if r["valid"]:
            _assert_report_coherent(r)

    # 并发更正会把旧日报全部失效；在所有写入落定后重新生成一份，
    # 验证它与当前已提交状态完全一致
    final_report = services.generate_report(
        {"report_date": DATE, "tolerance": 0.0}, db_path)
    assert final_report["valid"] is True
    _assert_report_coherent(final_report)

    # 最终不变量：每个仍有效日报引用的读数必须等于数据库当前最新读数
    final_reports = services.list_reports(report_date=DATE, db_path=db_path)
    valid = [r for r in final_reports if r["valid"]]
    assert valid, "至少应存在一份有效日报"
    for r in valid:
        _assert_report_coherent(r)
        for h in r["detail"]:
            if h["status"] in ("normal", "anomaly"):
                for x in h["readings"]:
                    db_energy = _current_latest_energy(db_path, x["point_id"], h["hour"])
                    assert x["energy"] == db_energy, (
                        f"有效日报 {r['id']} 与当前库状态不一致: {h['hour']} {x['point_id']}")

    # 被失效的日报都记录了原因与时间
    for r in final_reports:
        if not r["valid"]:
            assert r["invalidated_reason"] and r["invalidated_at"]

    # 读数修订号连续无重复（主键 + 严格递增在并发下仍然成立）
    conn = db_module.connect(db_path)
    try:
        for hour in HOURS:
            revs = [row["revision"] for row in conn.execute(
                "SELECT revision FROM reading WHERE point_id='IN1' AND hour=? ORDER BY revision",
                (hour,))]
            assert revs == list(range(1, len(revs) + 1))
    finally:
        conn.close()


def test_no_partial_report_when_generation_contended(tmp_db):
    """纯生成并发：同日期同容差只能得到同一份有效日报（幂等）。"""
    db_path = tmp_db
    services.submit_config({
        "effective_hour": f"{DATE}T00:00:00Z",
        "points": [
            {"point_id": "IN1", "role": "incoming"},
            {"point_id": "OUT1", "role": "outgoing"},
        ],
    }, db_path)
    services.submit_readings({"readings": [
        {"point_id": "IN1", "hour": h, "revision": 1, "energy": 1.0} for h in HOURS
    ] + [
        {"point_id": "OUT1", "hour": h, "revision": 1, "energy": 1.0} for h in HOURS
    ]}, db_path)

    results: list[dict] = []
    barrier = threading.Barrier(6)
    errors: list[Exception] = []

    def gen():
        try:
            barrier.wait()
            results.append(services.generate_report(
                {"report_date": DATE, "tolerance": 0.0}, db_path))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=gen) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    ids = {r["id"] for r in results}
    assert len(ids) == 1
    rid = ids.pop()
    valid = [r for r in services.list_reports(report_date=DATE, db_path=db_path)
             if r["valid"]]
    assert len(valid) == 1 and valid[0]["id"] == rid
    # 生成时间戳必须是合法 UTC 时间
    datetime.strptime(valid[0]["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc)
