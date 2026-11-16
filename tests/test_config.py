"""配置提交相关规则测试。"""
from tests.conftest import cfg, readings  # noqa: F401


def test_config_happy_path_and_listing(client):
    res = cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "IN2", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    assert res["status"] == "created"
    listing = client.get("/api/configs").json()
    assert len(listing) == 1
    assert {p["point_id"] for p in listing[0]["points"]} == {"IN1", "IN2", "OUT1"}


def test_config_reject_non_top_of_hour(client):
    cfg(client, "2026-11-15T08:30:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ], expected=400)


def test_config_reject_dual_role_same_point(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "DUP", "role": "incoming"},
        {"point_id": "DUP", "role": "outgoing"},
    ], expected=400)


def test_config_reject_duplicate_point_row(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ], expected=400)


def test_config_must_have_both_roles(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
    ], expected=400)


def test_config_same_hour_same_content_is_idempotent(client):
    payload = [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ]
    first = cfg(client, "2026-11-15T00:00:00Z", payload)
    second = cfg(client, "2026-11-15T00:00:00Z", payload)
    assert second["status"] == "duplicate_ignored"
    assert second["version_id"] == first["version_id"]
    assert len(client.get("/api/configs").json()) == 1


def test_config_same_hour_different_content_rejected(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT2", "role": "outgoing"},
    ], expected=400)
    # 拒收后原配置完好
    points = {p["point_id"] for p in client.get("/api/configs").json()[0]["points"]}
    assert points == {"IN1", "OUT1"}


def test_config_unknown_point_reading_rejected(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    readings(client, [{
        "point_id": "GHOST", "hour": "2026-11-15T00:00:00Z",
        "revision": 1, "energy": 1.0,
    }], expected=400)


def test_backdated_config_invalidates_affected_reports_only(client):
    from tests.conftest import basic_setup, gen_report
    basic_setup(client, "2026-11-15")
    basic_setup(client, "2026-11-16")
    r1 = gen_report(client, "2026-11-15")
    r2 = gen_report(client, "2026-11-16")
    assert r1["valid"] and r2["valid"]

    # 追溯：在 11-15 12:00 补一版配置 -> 11-15 与 11-16 都受影响
    # （新版本在 11-16 全天仍为当时生效配置，没有更新版本覆盖）
    res = cfg(client, "2026-11-15T12:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    assert res["invalidated_reports"] == 2
    assert client.get(f"/api/reports/{r1['id']}").json()["valid"] is False
    assert client.get(f"/api/reports/{r2['id']}").json()["valid"] is False


def test_backdated_config_leaves_earlier_dates_intact(client):
    from tests.conftest import basic_setup, gen_report
    basic_setup(client, "2026-11-15")
    basic_setup(client, "2026-11-16")
    r1 = gen_report(client, "2026-11-15")
    r2 = gen_report(client, "2026-11-16")

    # 在 11-16T06:00 补配置：11-15 日报不受影响，11-16 日报失效
    res = cfg(client, "2026-11-16T06:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    assert res["invalidated_reports"] == 1
    assert client.get(f"/api/reports/{r1['id']}").json()["valid"] is True
    assert client.get(f"/api/reports/{r2['id']}").json()["valid"] is False
