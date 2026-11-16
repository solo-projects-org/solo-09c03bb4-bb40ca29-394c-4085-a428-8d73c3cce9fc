"""累计抄码导入的原子性、崩溃恢复与并发补传。"""
import os
import threading

import pytest

from app import db as db_module
from app import imports, originals, services

DATE = "2026-11-15"
HOURS = [f"{DATE}T{h:02d}:00:00Z" for h in range(24)]


def csv_bytes(rows, header="point_id,hour,cumulative_kwh"):
    lines = [header] + [",".join(str(x) for x in r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def setup_config(db_path):
    services.submit_config({
        "effective_hour": f"{DATE}T00:00:00Z",
        "points": [
            {"point_id": "IN1", "role": "incoming"},
            {"point_id": "OUT1", "role": "outgoing"},
        ],
    }, db_path)


def snapshot(db_path):
    """库内可见状态：批次、示数、读数、日报失效计数。"""
    conn = db_module.connect(db_path)
    try:
        return {
            "batches": [dict(r) for r in conn.execute("SELECT * FROM import_batch")],
            "meter": [dict(r) for r in conn.execute(
                "SELECT * FROM meter_reading ORDER BY point_id, hour")],
            "readings": [dict(r) for r in conn.execute(
                "SELECT * FROM reading ORDER BY point_id, hour, revision")],
            "reports": [dict(r) for r in conn.execute(
                "SELECT id, invalidated_at FROM report ORDER BY id")],
        }
    finally:
        conn.close()


def test_failure_inside_transaction_rolls_back_everything(tmp_db, monkeypatch):
    setup_config(tmp_db)
    data = csv_bytes([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110),
                      ("OUT1", HOURS[0], 50), ("OUT1", HOURS[1], 55)])

    # 故障注入：派生读数已 INSERT、失效动作之前抛错，模拟落库中途失败
    def boom(*_a, **_k):
        raise RuntimeError("injected failure inside import transaction")

    monkeypatch.setattr(imports, "_invalidate_reports_for_hours", boom)
    with pytest.raises(RuntimeError):
        imports.import_cumulative_csv("a.csv", data, tmp_db)

    state = snapshot(tmp_db)
    assert state["batches"] == []
    assert state["meter"] == []
    assert state["readings"] == []

    # 相同内容恢复后重试：整批成功，且只产生一份数据
    monkeypatch.undo()
    res = imports.import_cumulative_csv("a.csv", data, tmp_db)
    assert res["status"] == "created" and res["new_derived_readings"] == 2
    assert len(snapshot(tmp_db)["batches"]) == 1


def test_crash_after_file_write_before_db_commit_leaves_only_orphan(tmp_db, monkeypatch):
    setup_config(tmp_db)
    data = csv_bytes([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)])
    sha = originals.sha256_bytes(data)

    def crash_connect(*_a, **_k):
        raise RuntimeError("simulated process crash before transaction")

    monkeypatch.setattr(imports, "connect", crash_connect)
    with pytest.raises(RuntimeError):
        imports.import_cumulative_csv("a.csv", data, tmp_db)
    monkeypatch.undo()

    # 原件已按字节落盘，但库内没有任何批次/示数/派生（不存在半批）
    assert (originals.originals_dir(tmp_db) / f"{sha}.csv").read_bytes() == data
    assert snapshot(tmp_db) == {"batches": [], "meter": [], "readings": [], "reports": []}

    # 启动清理：新文件在年龄保护期内不删；过期后才删
    assert imports.reconcile_originals(tmp_db)["orphans_removed"] == 0
    orphan = originals.originals_dir(tmp_db) / f"{sha}.csv"
    os.utime(orphan, (1, 1))
    assert imports.reconcile_originals(tmp_db)["orphans_removed"] == 1
    assert not orphan.exists()

    # 同一内容重传可正常入库（能重新写盘并由库引用）
    assert imports.import_cumulative_csv("a.csv", data, tmp_db)["status"] == "created"


def test_restart_after_successful_commit_keeps_original_and_readings(tmp_db):
    setup_config(tmp_db)
    data = csv_bytes([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)])
    res = imports.import_cumulative_csv("网关导出.csv", data, tmp_db)

    # 模拟重启：重新建表 + 原件核验 + 孤儿清理
    db_module.init_db(tmp_db)
    info = imports.reconcile_originals(tmp_db)
    assert info["batches"] == 1 and info["originals_verified"] == 1
    again = imports.read_import_original(res["batch_id"], tmp_db)
    assert again[0] == data and again[1]["filename"] == "网关导出.csv"


def test_concurrent_distinct_supplemental_imports(tmp_db):
    setup_config(tmp_db)
    # 先落 00:00 端点作为基线
    imports.import_cumulative_csv(
        "base.csv", csv_bytes([("IN1", HOURS[0], 1000), ("OUT1", HOURS[0], 500)]), tmp_db)

    errors: list[Exception] = []

    def worker(i):
        try:
            # 每个线程补传自己那一小时的端点，与前一小时配对
            hour = HOURS[i + 1]
            data = csv_bytes([
                ("IN1", hour, 1000 + i + 1),
                ("OUT1", hour, 500 + i + 1),
            ])
            imports.import_cumulative_csv(f"w{i}.csv", data, tmp_db)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    conn = db_module.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT hour, revision, energy, source FROM reading "
            "WHERE point_id='IN1' ORDER BY hour").fetchall()
        # 每个已配对的起始小时恰好一个派生修订，修订号从 1 连续
        assert len(rows) == 10
        assert all(r["source"] == "derived" and r["revision"] == 1 for r in rows)
        assert [r["hour"] for r in rows] == HOURS[:10]
        # 示数不重不丢
        n = conn.execute("SELECT COUNT(*) AS n FROM meter_reading").fetchone()["n"]
        assert n == 22
    finally:
        conn.close()


def test_concurrent_same_file_import_yields_single_batch(tmp_db):
    setup_config(tmp_db)
    data = csv_bytes([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110),
                      ("OUT1", HOURS[0], 50), ("OUT1", HOURS[1], 55)])
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(i):
        try:
            barrier.wait()
            results.append(imports.import_cumulative_csv(
                f"same-{i}.csv", data, tmp_db))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len({r["batch_id"] for r in results}) == 1
    created = [r for r in results if r["status"] == "created"]
    duplicates = [r for r in results if r["status"] == "duplicate_ignored"]
    assert len(created) == 1 and len(duplicates) == 7
    state = snapshot(tmp_db)
    assert len(state["batches"]) == 1
    assert len(state["readings"]) == 2
    # 两次派生读数（IN1@00、OUT1@00），无重复修订
    assert {r["revision"] for r in state["readings"]} == {1}


def test_concurrent_retry_same_endpoint_keeps_first_source(tmp_db):
    setup_config(tmp_db)
    imports.import_cumulative_csv(
        "b1.csv", csv_bytes([("IN1", HOURS[0], 100)]), tmp_db)
    results: list[dict] = []
    barrier = threading.Barrier(4)

    def worker(i):
        barrier.wait()
        results.append(imports.import_cumulative_csv(
            f"b{i}.csv", csv_bytes([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 105)]),
            tmp_db))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = snapshot(tmp_db)
    # 四份内容相同的文件按 SHA-256 去重，只保留首次来源的一个批次
    assert len(state["batches"]) == 2
    assert sorted(r["status"] for r in results) == ["created"] + ["duplicate_ignored"] * 3
    reading = state["readings"][0]
    # 起始端点保留首批来源；读数只派生一次
    assert reading["start_batch_id"] == 1
    assert reading["energy"] == 5.0
    assert len(state["readings"]) == 1


def test_import_retry_after_partial_visibility_uses_single_revision(tmp_db):
    """同批重试（首次提交成功但响应丢失）不产生重复修订，也不重复失效日报。"""
    setup_config(tmp_db)
    data = csv_bytes([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)])
    imports.import_cumulative_csv("a.csv", data, tmp_db)
    services.generate_report({"report_date": DATE, "tolerance": 0.0}, tmp_db)

    # 重试前日报可能已因首次导入失效；重试本身不得再新增任何失效/派生
    second = imports.import_cumulative_csv("a.csv", data, tmp_db)
    assert second["status"] == "duplicate_ignored"
    assert second["invalidated_reports"] == 0
    assert len(snapshot(tmp_db)["readings"]) == 1
