"""事务原子性：日报生成 / 上传失败时不得留下有效半份数据。"""
from app import services

from tests.conftest import cfg, gen_report, basic_setup, readings  # noqa: F401

DATE = "2026-11-15"
HOUR = f"{DATE}T08:00:00Z"


def test_report_generation_failure_leaves_no_partial_report(tmp_db, monkeypatch):
    db_path = tmp_db
    # 用服务层准备数据
    services.submit_config({
        "effective_hour": f"{DATE}T00:00:00Z",
        "points": [
            {"point_id": "IN1", "role": "incoming"},
            {"point_id": "OUT1", "role": "outgoing"},
        ],
    }, db_path)
    services.submit_readings({"readings": [
        {"point_id": "IN1", "hour": f"{DATE}T{h:02d}:00:00Z", "revision": 1, "energy": 5.0}
        for h in range(24)
    ] + [
        {"point_id": "OUT1", "hour": f"{DATE}T{h:02d}:00:00Z", "revision": 1, "energy": 5.0}
        for h in range(24)
    ]}, db_path)

    # 故障注入：INSERT 成功后、COMMIT 前抛错，模拟落库阶段失败
    real_now = services.now_utc_iso
    state = {"n": 0}

    def boom():
        state["n"] += 1
        raise RuntimeError("injected failure before commit")

    # 提交时刻只在最终 INSERT 处取一次（读数/配置时刻此前已生成）
    monkeypatch.setattr(services, "now_utc_iso", boom)

    try:
        try:
            services.generate_report({"report_date": DATE, "tolerance": 0.0}, db_path)
            raised = False
        except RuntimeError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(services, "now_utc_iso", real_now)

    assert state["n"] == 1
    # 库里不得存在任何日报行（失败被回滚，没有半份）
    assert services.list_reports(db_path=db_path) == []

    # 恢复后可正常生成
    ok = services.generate_report({"report_date": DATE, "tolerance": 0.0}, db_path)
    assert ok["valid"] is True and len(ok["detail"]) == 24


def test_reading_batch_failure_leaves_no_partial_data(client):
    cfg(client, f"{DATE}T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    # 第二批含非法跳号 -> 整批回滚
    resp = client.post("/api/readings", json={"readings": [
        {"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 1.0},
        {"point_id": "OUT1", "hour": HOUR, "revision": 9, "energy": 1.0},
    ]})
    assert resp.status_code == 400
    assert client.get("/api/readings").json() == []


def test_config_failure_leaves_no_partial_version(client):
    # 同版异文拒收后，库内不产生新版本
    cfg(client, f"{DATE}T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    cfg(client, f"{DATE}T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT2", "role": "outgoing"},
    ], expected=400)
    versions = client.get("/api/configs").json()
    assert len(versions) == 1
    assert {p["point_id"] for p in versions[0]["points"]} == {"IN1", "OUT1"}


def test_invalidated_report_kept_for_audit_but_not_valid(client):
    basic_setup(client, DATE)
    r1 = gen_report(client, DATE)
    readings(client, [{
        "point_id": "IN1", "hour": HOUR, "revision": 2, "energy": 42.0,
    }])
    stale = client.get(f"/api/reports/{r1['id']}").json()
    assert stale["valid"] is False
    assert stale["invalidated_reason"] and stale["invalidated_at"]
    # 默认列表含失效件；过滤后不含
    assert any(x["id"] == r1["id"] for x in client.get("/api/reports").json())
    assert all(x["id"] != r1["id"]
               for x in client.get("/api/reports",
                                   params={"include_invalid": "false"}).json())
    # 新日报有效，且与旧日报同时可追溯
    r2 = gen_report(client, DATE)
    assert r2["valid"] is True and r2["id"] != r1["id"]
