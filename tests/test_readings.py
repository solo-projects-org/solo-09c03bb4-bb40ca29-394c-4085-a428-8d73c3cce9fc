"""读数录入：非负、修订递增、异文拒收、整批原子性、失效联动。"""
from tests.conftest import basic_setup, cfg, gen_report, readings


HOUR = "2026-11-15T08:00:00Z"


def test_reading_happy_path_and_listing(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    res = readings(client, [
        {"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 10.5},
        {"point_id": "OUT1", "hour": HOUR, "revision": 1, "energy": 9.0},
    ])
    assert len(res["accepted"]) == 2
    rows = client.get("/api/readings").json()
    assert len(rows) == 2


def test_reading_reject_negative_energy(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    readings(client, [{
        "point_id": "IN1", "hour": HOUR, "revision": 1, "energy": -0.01,
    }], expected=422)  # pydantic 校验层拦截


def test_reading_reject_non_top_hour(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    readings(client, [{
        "point_id": "IN1", "hour": "2026-11-15T08:15:00Z",
        "revision": 1, "energy": 1.0,
    }], expected=400)


def test_revision_must_increment_and_gap_rejected(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    readings(client, [{"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 1.0}])
    # 旧修订号
    readings(client, [{"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 2.0}],
             expected=400)
    # 跳号（必须严格 +1）
    readings(client, [{"point_id": "IN1", "hour": HOUR, "revision": 3, "energy": 2.0}],
             expected=400)
    # 正确递增
    ok = readings(client, [{"point_id": "IN1", "hour": HOUR, "revision": 2, "energy": 2.0}])
    assert len(ok["accepted"]) == 1
    latest = [r for r in client.get("/api/readings", params={"point_id": "IN1"}).json()
              if r["hour"] == HOUR]
    assert latest[0]["revision"] == 2 and latest[0]["energy"] == 2.0


def test_same_revision_different_content_rejected(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    readings(client, [{"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 1.0}])
    readings(client, [{"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 1.5}],
             expected=400)


def test_same_revision_same_content_idempotent(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    payload = [{"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 1.0}]
    first = readings(client, payload)
    second = readings(client, payload)
    assert len(first["accepted"]) == 1
    assert len(second["duplicates"]) == 1
    assert len(client.get("/api/readings").json()) == 1


def test_batch_is_atomic_on_validation_failure(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    # 第二条非法（跳修订号），第一条也必须回滚
    readings(client, [
        {"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 1.0},
        {"point_id": "OUT1", "hour": HOUR, "revision": 5, "energy": 1.0},
    ], expected=400)
    assert client.get("/api/readings").json() == []


def test_batch_reject_duplicate_point_hour_in_one_request(client):
    cfg(client, "2026-11-15T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    readings(client, [
        {"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 1.0},
        {"point_id": "IN1", "hour": HOUR, "revision": 1, "energy": 2.0},
    ], expected=400)


def test_reading_correction_invalidates_same_day_report_only(client):
    basic_setup(client, "2026-11-15")
    basic_setup(client, "2026-11-16")
    r1 = gen_report(client, "2026-11-15")
    r2 = gen_report(client, "2026-11-16")

    res = readings(client, [
        {"point_id": "IN1", "hour": "2026-11-15T10:00:00Z",
         "revision": 2, "energy": 150.0},
    ])
    assert res["invalidated_reports"] == 1
    assert client.get(f"/api/reports/{r1['id']}").json()["valid"] is False
    assert client.get(f"/api/reports/{r2['id']}").json()["valid"] is True
