"""pytest 公共夹具：每个测试使用独立临时 SQLite 库，并通过 HTTP 打应用。"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_file)
    db_module.init_db(db_file)
    return db_file


@pytest.fixture()
def client(tmp_db):
    with TestClient(app) as c:
        yield c


# ---- 便捷构造函数 ----

def cfg(client, hour, points, expected=200):
    resp = client.post("/api/configs", json={"effective_hour": hour, "points": points})
    assert resp.status_code == expected, resp.text
    return resp.json()


def readings(client, items, expected=200):
    resp = client.post("/api/readings", json={"readings": items})
    assert resp.status_code == expected, resp.text
    return resp.json()


def gen_report(client, date, tolerance=0.0, expected=200):
    resp = client.post("/api/reports/generate",
                       json={"report_date": date, "tolerance": tolerance})
    assert resp.status_code == expected, resp.text
    return resp.json()


def basic_setup(client, date="2026-11-15"):
    """单日 24 小时完整数据，进出线电量完全平衡。"""
    cfg(client, f"{date}T00:00:00Z", [
        {"point_id": "IN1", "role": "incoming"},
        {"point_id": "OUT1", "role": "outgoing"},
    ])
    items = []
    for h in range(24):
        hour = f"{date}T{h:02d}:00:00Z"
        items.append({"point_id": "IN1", "hour": hour, "revision": 1, "energy": 100.0})
        items.append({"point_id": "OUT1", "hour": hour, "revision": 1, "energy": 100.0})
    readings(client, items)
