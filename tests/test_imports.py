"""累计抄码 CSV 导入：派生规则、拒收规则、追溯与原件核验。"""
import hashlib
import io

import pytest

from app import imports, originals, services

from tests.conftest import cfg, gen_report, readings

DATE = "2026-11-15"
HOURS = [f"{DATE}T{h:02d}:00:00Z" for h in range(24)]


def make_csv(rows, header="point_id,hour,cumulative_kwh", extra=None):
    lines = [] if header is None else [header]
    for r in rows:
        lines.append(",".join(str(x) for x in r))
    if extra:
        lines.extend(extra)
    return ("\n".join(lines) + "\n").encode("utf-8")


def post_csv(client, data, filename="readings.csv", expected=200):
    resp = client.post(
        "/api/imports",
        files={"file": (filename, io.BytesIO(data), "text/csv")},
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def setup_points(client):
    cfg(client, f"{DATE}T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])


# ---------------------------------------------------------------------------
# 派生规则
# ---------------------------------------------------------------------------

def test_import_derives_adjacent_hours_attributing_start_hour(client):
    setup_points(client)
    data = make_csv([
        ("IN1", HOURS[0], 1000),
        ("IN1", HOURS[1], 1010),
        ("IN1", HOURS[2], 1025),
        ("OUT1", HOURS[0], 500),
        ("OUT1", HOURS[1], 508),
    ])
    res = post_csv(client, data)
    assert res["status"] == "created"
    assert res["new_endpoints"] == 5
    assert res["new_derived_readings"] == 3  # IN1: h0,h1；OUT1: h0

    rows = client.get("/api/readings", params={"point_id": "IN1"}).json()
    derived = {(r["hour"], r["revision"]): r for r in rows if r["source"] == "derived"}
    assert derived[(HOURS[0], 1)]["energy"] == 10.0   # 计入起始小时
    assert derived[(HOURS[1], 1)]["energy"] == 15.0
    # 末尾端点没有下一小时，不生成 H2 的读数
    assert all(r["hour"] != HOURS[2] for r in rows)


def test_import_missing_endpoint_produces_nothing(client):
    setup_points(client)
    # 只有一个端点：无相邻整点，不生成任何派生
    res = post_csv(client, make_csv([("IN1", HOURS[0], 1000)]))
    assert res["new_derived_readings"] == 0
    assert client.get("/api/readings").json() == []


def test_import_non_adjacent_hours_do_not_pair(client):
    setup_points(client)
    res = post_csv(client, make_csv([
        ("IN1", HOURS[0], 1000),
        ("IN1", HOURS[2], 1030),  # 隔了一小时，非相邻
    ]))
    assert res["new_derived_readings"] == 0


def test_import_without_header_accepts_three_columns(client):
    setup_points(client)
    data = make_csv([("IN1", HOURS[0], 10), ("IN1", HOURS[1], 13)], header=None)
    res = post_csv(client, data)
    assert res["new_derived_readings"] == 1


def test_import_chinese_header_and_extra_columns(client):
    setup_points(client)
    data = make_csv(
        [("IN1", HOURS[0], 10, "备注"), ("IN1", HOURS[1], 14, "")],
        header="计量点,抄码时间,累计示数(kWh),备注",
    )
    res = post_csv(client, data)
    assert res["new_endpoints"] == 2
    assert res["new_derived_readings"] == 1


def test_import_supplemental_batch_pairs_with_existing_readings(client):
    setup_points(client)
    post_csv(client, make_csv([("IN1", HOURS[0], 100)]))
    # 补传下一小时，与库内端点配成一对
    res = post_csv(client, make_csv([("IN1", HOURS[1], 108)]))
    assert res["new_derived_readings"] == 1
    view = client.get("/api/readings", params={"point_id": "IN1"}).json()
    assert view[0]["energy"] == 8.0 and view[0]["hour"] == HOURS[0]


# ---------------------------------------------------------------------------
# 拒收规则（整批）
# ---------------------------------------------------------------------------

def test_import_negative_delta_rejects_whole_batch(client):
    setup_points(client)
    data = make_csv([
        ("IN1", HOURS[0], 100),
        ("IN1", HOURS[1], 90),      # 负差值
        ("OUT1", HOURS[0], 1),      # 即使本身合法也必须整批回滚
        ("OUT1", HOURS[1], 2),
    ])
    post_csv(client, data, expected=400)
    assert client.get("/api/readings").json() == []
    assert client.get("/api/imports").json() == []


def test_import_conflicting_same_point_hour_rejects(client):
    setup_points(client)
    post_csv(client, make_csv([("IN1", HOURS[0], 100)]))
    # 同一 (点, 整点) 不同示数 -> 整批拒收，原有示数不变
    post_csv(client, make_csv([("IN1", HOURS[0], 101), ("IN1", HOURS[1], 110)]),
             expected=400)
    batches = client.get("/api/imports").json()
    assert len(batches) == 1
    rows = client.get("/api/readings", params={"point_id": "IN1"}).json()
    assert rows == []


def test_import_conflict_with_existing_manual_reading_rejects(client):
    setup_points(client)
    readings(client, [{"point_id": "IN1", "hour": HOURS[0], "revision": 1,
                       "energy": 5.0}])
    post_csv(client, make_csv([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 120)]),
             expected=400)
    # 手工读数原样保留，没有派生修订，也没有批次
    rows = client.get("/api/readings", params={"point_id": "IN1"}).json()
    assert len(rows) == 1 and rows[0]["source"] == "manual"
    assert client.get("/api/imports").json() == []


def test_import_rejects_duplicate_key_with_different_value_inside_file(client):
    setup_points(client)
    data = make_csv([
        ("IN1", HOURS[0], 100),
        ("IN1", HOURS[0], 100.5),
    ])
    post_csv(client, data, expected=400)
    assert client.get("/api/imports").json() == []


def test_import_rejects_more_than_500_rows(client):
    setup_points(client)
    rows = [("IN1", HOURS[0], i) for i in range(501)]
    post_csv(client, make_csv(rows), expected=400)


def test_import_rejects_non_top_of_hour_and_negative(client):
    setup_points(client)
    post_csv(client, make_csv([("IN1", f"{DATE}T00:30:00Z", 1)]), expected=400)
    post_csv(client, make_csv([("IN1", HOURS[0], -1)]), expected=400)
    post_csv(client, make_csv([("IN1", HOURS[0], "nan")]), expected=400)


def test_import_rejects_bad_encoding_and_empty(client):
    setup_points(client)
    post_csv(client, b"\xff\xfe\x00bad", expected=400)
    post_csv(client, b"", expected=400)


# ---------------------------------------------------------------------------
# 重传幂等与“保留首次来源”
# ---------------------------------------------------------------------------

def test_same_file_retransmit_is_idempotent_and_keeps_first_source(client):
    setup_points(client)
    data = make_csv([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)])
    first = post_csv(client, data)
    second = post_csv(client, data)
    assert second["status"] == "duplicate_ignored"
    assert second["batch_id"] == first["batch_id"]
    assert second["new_derived_readings"] == 0
    # 读数只有一份，来源仍是首批
    rows = client.get("/api/readings", params={"point_id": "IN1"}).json()
    assert len(rows) == 1
    assert rows[0]["start_batch_id"] == first["batch_id"]


def test_same_endpoint_in_new_file_keeps_first_source(client):
    setup_points(client)
    b1 = post_csv(client, make_csv([("IN1", HOURS[0], 100)]))
    b2 = post_csv(client, make_csv([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 105)]))
    assert b2["status"] == "created"
    assert b2["duplicate_endpoints"] == 1
    assert b2["new_endpoints"] == 1
    trace = client.get(f"/api/imports/{b2['batch_id']}").json()
    assert len(trace["derived_readings"]) == 1
    derived = trace["derived_readings"][0]
    # 起始端点来自首批，结束端点来自本批
    assert derived["start_batch_id"] == b1["batch_id"]
    assert derived["end_batch_id"] == b2["batch_id"]


def test_derived_revision_traceability_and_manual_correction_revision(client):
    setup_points(client)
    b1 = post_csv(client, make_csv([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)]))
    detail = client.get(f"/api/imports/{b1['batch_id']}").json()
    d = detail["derived_readings"][0]
    assert d["start_cumulative_kwh"] == 100.0 and d["end_cumulative_kwh"] == 110.0
    assert d["start_line_no"] == 2 and d["end_line_no"] == 3
    assert d["energy"] == 10.0 and d["revision"] == 1

    # 手工更正派生小时：新修订，保留派生修订可查
    res = readings(client, [{"point_id": "IN1", "hour": HOURS[0], "revision": 2,
                             "energy": 9.5}])
    assert len(res["accepted"]) == 1
    rows = client.get("/api/readings", params={"point_id": "IN1"}).json()
    by_rev = {r["revision"]: r for r in rows}
    assert by_rev[1]["source"] == "derived" and by_rev[2]["source"] == "manual"


# ---------------------------------------------------------------------------
# 原件：按字节保存、SHA-256 下载核验、缺失拒绝
# ---------------------------------------------------------------------------

def test_original_stored_byte_exact_and_download_verified(client, tmp_db):
    setup_points(client)
    data = make_csv([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)])
    res = post_csv(client, data, filename="网关导出 2026-11-15.csv")
    sha = hashlib.sha256(data).hexdigest()
    assert res["sha256"] == sha

    path = originals.originals_dir(tmp_db) / f"{sha}.csv"
    assert path.read_bytes() == data

    dl = client.get(f"/api/imports/{res['batch_id']}/download")
    assert dl.status_code == 200
    assert dl.content == data
    assert dl.headers["x-content-sha256"] == sha
    assert "attachment" in dl.headers["content-disposition"]

    verify = client.get(f"/api/imports/{res['batch_id']}/verify").json()
    assert verify["verified"] is True and verify["recomputed_sha256"] == sha


def test_corrupted_original_is_not_served(client, tmp_db):
    setup_points(client)
    data = make_csv([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)])
    res = post_csv(client, data)
    sha = hashlib.sha256(data).hexdigest()
    path = originals.originals_dir(tmp_db) / f"{sha}.csv"
    path.write_bytes(b"tampered")

    dl = client.get(f"/api/imports/{res['batch_id']}/download")
    assert dl.status_code == 500
    assert "SHA-256" in dl.json()["error"]
    assert client.get(f"/api/imports/{res['batch_id']}/verify").status_code == 500

    with pytest.raises(RuntimeError):
        imports.reconcile_originals(tmp_db)


def test_missing_original_fails_startup_check(client, tmp_db):
    setup_points(client)
    data = make_csv([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)])
    res = post_csv(client, data)
    sha = hashlib.sha256(data).hexdigest()
    (originals.originals_dir(tmp_db) / f"{sha}.csv").unlink()
    with pytest.raises(RuntimeError, match="原件一致性检查失败"):
        imports.reconcile_originals(tmp_db)
    # 批次仍可查询，但不提供未核验字节
    assert client.get(f"/api/imports/{res['batch_id']}/download").status_code == 500


def test_reconcile_cleans_orphans_older_than_threshold(client, tmp_db):
    setup_points(client)
    data = make_csv([("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110)])
    res = post_csv(client, data)
    orphan = originals.originals_dir(tmp_db) / ("a" * 64 + ".csv")
    orphan.write_bytes(b"orphan")
    old = 1
    import os
    os.utime(orphan, (old, old))
    info = imports.reconcile_originals(tmp_db)
    assert info["orphans_removed"] == 1 and not orphan.exists()
    assert client.get(f"/api/imports/{res['batch_id']}/download").status_code == 200


# ---------------------------------------------------------------------------
# 与日报的衔接：派生读数参与质检、失败不发失效、成功才失效
# ---------------------------------------------------------------------------

def test_derived_readings_drive_reports_and_invalidate_before_regeneration(client):
    setup_points(client)
    rows = []
    for h in range(24):
        rows.append(("IN1", HOURS[h], 1000 + h * 10))
        rows.append(("OUT1", HOURS[h], 500 + h * 10))
    # 23 个派生小时（最后一小时缺终点）
    b = post_csv(client, make_csv(rows))
    assert b["new_derived_readings"] == 23 * 2

    report = gen_report(client, DATE, tolerance=0.0)
    assert report["status"] == "incomplete"  # 23:00 缺读数
    r1 = report

    # 补传次日 00:00 端点，与 23:00 配对；派生 h23 后旧日报失效
    b2 = post_csv(client, make_csv([
        ("IN1", f"{DATE}T23:00:00Z", 1000 + 23 * 10),
        ("IN1", "2026-11-16T00:00:00Z", 1000 + 24 * 10),
        ("OUT1", f"{DATE}T23:00:00Z", 500 + 23 * 10),
        ("OUT1", "2026-11-16T00:00:00Z", 500 + 24 * 10),
    ]))
    assert b2["new_derived_readings"] == 2
    assert b2["invalidated_reports"] == 1
    assert client.get(f"/api/reports/{r1['id']}").json()["valid"] is False

    r2 = gen_report(client, DATE, tolerance=0.0)
    assert r2["status"] == "complete"
    h23 = r2["detail"][23]
    assert h23["status"] == "normal"
    assert all(x["source"] == "derived" for x in h23["readings"])
    assert "derived_from" in h23["readings"][0]["basis"]


def test_failed_import_does_not_invalidate_reports(client):
    setup_points(client)
    rows = [("IN1", HOURS[h], 1000 + h) for h in range(24)]
    rows += [("OUT1", HOURS[h], 500 + h) for h in range(24)]
    # 补上 24:00 端点，使次日 00:00 前所有小时都有派生值
    rows += [("IN1", "2026-11-16T00:00:00Z", 1000 + 24),
             ("OUT1", "2026-11-16T00:00:00Z", 500 + 24)]
    post_csv(client, make_csv(rows))
    r = gen_report(client, DATE)
    assert r["status"] == "complete"

    # 负差值批次整批拒收 -> 日报仍有效
    post_csv(client, make_csv([
        ("IN1", "2026-11-16T00:00:00Z", 1000 + 24),
        ("IN1", "2026-11-16T01:00:00Z", 5),
    ]), expected=400)
    assert client.get(f"/api/reports/{r['id']}").json()["valid"] is True


def test_import_rejects_derived_reading_for_unconfigured_point(client):
    # 派生读数即正式小时电量，必须与手工读数一样要求计量点已登记
    post_csv(client, make_csv([("GHOST", HOURS[0], 1), ("GHOST", HOURS[1], 2)]),
             expected=400)
    assert client.get("/api/readings").json() == []
    assert client.get("/api/imports").json() == []


def test_import_rejects_unconfigured_point_but_keeps_configured_pair(client):
    setup_points(client)
    post_csv(client, make_csv([
        ("IN1", HOURS[0], 100), ("IN1", HOURS[1], 110),
        ("GHOST", HOURS[0], 1), ("GHOST", HOURS[1], 2),
    ]), expected=400)
    assert client.get("/api/readings").json() == []
    assert client.get("/api/imports").json() == []


@pytest.mark.parametrize("hour_text", [
    "2026-11-15T00:00:00Z",
    "2026-11-15 00:00:00",
    "2026-11-15T00:00Z",
    "2026-11-15T00:00:00+00:00",
    "2026-11-15 00:00:00Z",
])
def test_import_accepts_common_gateway_hour_formats(client, hour_text):
    setup_points(client)
    data = ("point_id,hour,cumulative_kwh\n"
            f"IN1,{hour_text},100\n"
            "IN1,2026-11-15T01:00:00Z,110\n").encode()
    res = post_csv(client, data)
    assert res["new_derived_readings"] == 1
    rows = client.get("/api/readings", params={"point_id": "IN1"}).json()
    assert rows[0]["hour"] == "2026-11-15T00:00:00Z"


@pytest.mark.parametrize("hour_text", [
    "2026-11-15T00:30:00Z",
    "2026-11-15T00:00:01Z",
    "2026-11-15T08:00:00+08:00",
    "15/11/2026 00:00",
])
def test_import_rejects_non_utc_top_of_hour_formats(client, hour_text):
    setup_points(client)
    data = ("point_id,hour,cumulative_kwh\n"
            f"IN1,{hour_text},100\n"
            "IN1,2026-11-15T01:00:00Z,110\n").encode()
    post_csv(client, data, expected=400)
    assert client.get("/api/imports").json() == []


def test_old_database_is_migrated_and_manual_readings_preserved(tmp_path, monkeypatch):
    """旧库（无派生来源列）启动时可平滑升级，历史手工读数与日报保持可用。"""
    import sqlite3

    from app import db as db_module

    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript("""
        CREATE TABLE config_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            effective_hour TEXT NOT NULL, created_at TEXT NOT NULL,
            submitted_text TEXT NOT NULL, content_hash TEXT NOT NULL);
        CREATE TABLE config_point (version_id INTEGER NOT NULL REFERENCES config_version(id),
            point_id TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY (version_id, point_id));
        CREATE TABLE reading (point_id TEXT NOT NULL, hour TEXT NOT NULL,
            revision INTEGER NOT NULL, energy REAL NOT NULL,
            submitted_text TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (point_id, hour, revision));
        CREATE TABLE report (id INTEGER PRIMARY KEY AUTOINCREMENT, report_date TEXT NOT NULL,
            tolerance REAL NOT NULL, status TEXT NOT NULL, config_snapshot TEXT NOT NULL,
            reading_snapshot TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
            invalidated_reason TEXT, invalidated_at TEXT);
    """)
    conn.execute(
        "INSERT INTO config_version(effective_hour, created_at, submitted_text, content_hash) "
        "VALUES ('2026-11-15T00:00:00Z','2026-11-15T00:00:00Z','x','h')")
    conn.execute("INSERT INTO config_point VALUES (1,'IN1','incoming')")
    conn.execute("INSERT INTO config_point VALUES (1,'OUT1','outgoing')")
    conn.execute(
        "INSERT INTO reading VALUES ('IN1','2026-11-15T00:00:00Z',1,10.0,'t','2026-11-15T01:00:00Z')")
    conn.commit()
    conn.close()

    db_module.init_db(old_db)
    monkeypatch.setattr(db_module, "DB_PATH", old_db)
    rows = services.list_readings(db_path=old_db)
    assert len(rows) == 1 and rows[0]["source"] == "manual"
    assert "derived_from" not in rows[0]["basis"]

    # 新导入可在旧库上正常工作，并与旧手工读数共存
    res = imports.import_cumulative_csv(
        "new.csv",
        b"point_id,hour,cumulative_kwh\nOUT1,2026-11-15T00:00:00Z,50\n"
        b"OUT1,2026-11-15T01:00:00Z,55\n",
        old_db,
    )
    assert res["new_derived_readings"] == 1
    assert imports.reconcile_originals(old_db)["batches"] == 1
