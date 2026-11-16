"""质检日报生成、状态判定、快照一致性与失效行为测试。"""
from tests.conftest import basic_setup, cfg, gen_report, readings

DATE = "2026-11-15"


def test_report_complete_balanced(client):
    basic_setup(client, DATE)
    r = gen_report(client, DATE, tolerance=0.0)
    assert r["status"] == "complete"
    assert len(r["detail"]) == 24
    assert all(h["status"] == "normal" for h in r["detail"])
    # 依据里必须列出计量点、读数与修订依据
    h0 = r["detail"][0]
    assert {x["point_id"] for x in h0["readings"]} == {"IN1", "OUT1"}
    assert all("rev1" in x["basis"] for x in h0["readings"] if not x.get("missing"))


def test_report_anomaly_when_diff_exceeds_tolerance(client):
    basic_setup(client, DATE)
    # 把 08:00 进线改成 130，出线仍 100 -> 差 30
    readings(client, [{
        "point_id": "IN1", "hour": f"{DATE}T08:00:00Z",
        "revision": 2, "energy": 130.0,
    }])
    r = gen_report(client, DATE, tolerance=10.0)
    assert r["status"] == "anomaly"
    h8 = next(h for h in r["detail"] if h["hour"] == f"{DATE}T08:00:00Z")
    assert h8["status"] == "anomaly"
    assert h8["abs_difference"] == 30.0
    # 容差 30 时不算异常（绝对差“超过”容差才异常，等于不算）
    r2 = gen_report(client, DATE, tolerance=30.0)
    assert r2["status"] == "complete"


def test_report_incomplete_when_reading_missing(client):
    cfg(client, f"{DATE}T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    # 只录 3 个小时
    items = []
    for h in range(3):
        hour = f"{DATE}T{h:02d}:00:00Z"
        items.append({"point_id": "IN1", "hour": hour, "revision": 1, "energy": 10.0})
        items.append({"point_id": "OUT1", "hour": hour, "revision": 1, "energy": 10.0})
    readings(client, items)
    r = gen_report(client, DATE)
    assert r["status"] == "incomplete"
    assert sum(1 for h in r["detail"] if h["status"] == "incomplete") == 21
    h3 = r["detail"][3]
    assert "缺少计量点读数" in h3["reason"]


def test_report_incomplete_when_config_missing(client):
    # 配置从 10:00 才生效 -> 00-09 点缺配置
    cfg(client, f"{DATE}T10:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    items = []
    for h in range(10, 24):
        hour = f"{DATE}T{h:02d}:00:00Z"
        items.append({"point_id": "IN1", "hour": hour, "revision": 1, "energy": 10.0})
        items.append({"point_id": "OUT1", "hour": hour, "revision": 1, "energy": 10.0})
    readings(client, items)
    r = gen_report(client, DATE)
    assert r["status"] == "incomplete"
    early = r["detail"][:10]
    assert all(h["status"] == "incomplete" and "配置" in h["reason"] for h in early)
    late = r["detail"][10:]
    assert all(h["status"] == "normal" for h in late)


def test_report_uses_config_effective_at_each_hour(client):
    # 00:00 配置 IN1/OUT1；12:00 换成 IN2/OUT2
    cfg(client, f"{DATE}T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    cfg(client, f"{DATE}T12:00:00Z", [
        {"point_id": "IN2", "role": "incoming"},
        {"point_id": "OUT2", "role": "outgoing"},
    ])
    items = []
    for h in range(12):
        hour = f"{DATE}T{h:02d}:00:00Z"
        items += [
            {"point_id": "IN1", "hour": hour, "revision": 1, "energy": 5.0},
            {"point_id": "OUT1", "hour": hour, "revision": 1, "energy": 5.0},
        ]
    for h in range(12, 24):
        hour = f"{DATE}T{h:02d}:00:00Z"
        items += [
            {"point_id": "IN2", "hour": hour, "revision": 1, "energy": 7.0},
            {"point_id": "OUT2", "hour": hour, "revision": 1, "energy": 7.0},
        ]
    readings(client, items)
    r = gen_report(client, DATE)
    assert r["status"] == "complete"
    assert r["detail"][0]["config_version_id"] != r["detail"][12]["config_version_id"]
    assert {x["point_id"] for x in r["detail"][12]["readings"]} == {"IN2", "OUT2"}


def test_report_idempotent_same_date_tolerance(client):
    basic_setup(client, DATE)
    r1 = gen_report(client, DATE, tolerance=5.0)
    r2 = gen_report(client, DATE, tolerance=5.0)
    assert r1["id"] == r2["id"]
    assert r2["generation"] == "returned_existing_valid"
    # 不同容差是另一份报告
    r3 = gen_report(client, DATE, tolerance=6.0)
    assert r3["id"] != r1["id"]


def test_report_snapshot_immutable_after_correction(client):
    basic_setup(client, DATE)
    r1 = gen_report(client, DATE)
    old_energy = r1["detail"][8]["readings"][0]["energy"]
    assert old_energy == 100.0

    # 更正读数 -> 旧日报失效，但其快照保持原值
    readings(client, [{
        "point_id": "IN1", "hour": f"{DATE}T08:00:00Z",
        "revision": 2, "energy": 999.0,
    }])
    stale = client.get(f"/api/reports/{r1['id']}").json()
    assert stale["valid"] is False
    assert stale["detail"][8]["readings"][0]["energy"] == 100.0
    assert "999" not in str(stale["reading_snapshot"])

    # 重新生成的日报反映新读数
    r2 = gen_report(client, DATE)
    assert r2["id"] != r1["id"] and r2["valid"] is True
    assert r2["detail"][8]["readings"][0]["energy"] == 999.0


def test_report_listing_and_404(client):
    basic_setup(client, DATE)
    gen_report(client, DATE)
    assert len(client.get("/api/reports").json()) == 1
    assert client.get("/api/reports/9999").status_code == 400
    assert client.get("/api/reports", params={"include_invalid": "false"}).json()


def test_report_validation_errors(client):
    gen_report(client, "2026-11-15", tolerance=-1, expected=422)
    gen_report(client, "15-11-2026", tolerance=0, expected=400)
