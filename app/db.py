"""SQLite 连接与建表（schema）。

所有时间均以 ISO8601 的 UTC 整点字符串存储，例如 ``2026-11-15T08:00:00Z``。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(
    os.environ.get(
        "SUBSTATION_DB_PATH",
        str(Path(__file__).resolve().parent.parent / "data" / "substation.db"),
    )
)

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 计量点配置版本（整版提交：一次提交包含该版本全部进/出线计量点）
CREATE TABLE IF NOT EXISTS config_version (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_hour   TEXT NOT NULL,          -- 生效 UTC 小时整点
    created_at       TEXT NOT NULL,          -- 提交时刻（UTC）
    submitted_text   TEXT NOT NULL,          -- 规范化后的提交原文（用于同版异文判定）
    content_hash     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_point (
    version_id   INTEGER NOT NULL REFERENCES config_version(id),
    point_id     TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('incoming', 'outgoing')),
    PRIMARY KEY (version_id, point_id)
);

-- 累计抄码 CSV 原件批次。原件按字节保存在 originals/<sha256>.csv，
-- 同一文件（同 SHA-256）重传幂等。
CREATE TABLE IF NOT EXISTS import_batch (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256         TEXT NOT NULL UNIQUE,
    original_name  TEXT NOT NULL,
    byte_size      INTEGER NOT NULL,
    row_count      INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);

-- 逐小时电量读数：同一计量点同一小时可有递增修订号
-- source=manual 为人工录入；source=derived 由两次累计抄码示数作差派生，
-- 并记录两端示数（值、来源批次、CSV 物理行号）供逐修订追溯。
CREATE TABLE IF NOT EXISTS reading (
    point_id        TEXT NOT NULL,
    hour            TEXT NOT NULL,           -- UTC 整点小时
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    energy          REAL NOT NULL CHECK (energy >= 0),  -- 非负电量
    submitted_text  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('manual', 'derived')),
    start_batch_id  INTEGER REFERENCES import_batch(id),
    start_line_no   INTEGER,
    start_cumulative_kwh REAL,
    end_batch_id    INTEGER REFERENCES import_batch(id),
    end_line_no     INTEGER,
    end_cumulative_kwh   REAL,
    PRIMARY KEY (point_id, hour, revision)
);

-- 累计抄码 CSV 原件批次。原件按字节保存在 originals/<sha256>.csv，
-- 同一文件（同 SHA-256）重传幂等。
CREATE TABLE IF NOT EXISTS import_batch (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256         TEXT NOT NULL UNIQUE,
    original_name  TEXT NOT NULL,
    byte_size      INTEGER NOT NULL,
    row_count      INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);

-- 批次内每一物理数据行的原文（含同一批次内同值重复行），保留行号供追溯。
CREATE TABLE IF NOT EXISTS import_row (
    batch_id        INTEGER NOT NULL REFERENCES import_batch(id),
    line_no         INTEGER NOT NULL,
    point_id        TEXT NOT NULL,
    hour            TEXT NOT NULL,
    cumulative_kwh  REAL NOT NULL CHECK (cumulative_kwh >= 0),
    is_first_for_key INTEGER NOT NULL CHECK (is_first_for_key IN (0, 1)),
    PRIMARY KEY (batch_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_import_row_key ON import_row(point_id, hour);

-- 库内累计示数（每点每整点唯一，值一旦落库不可改写）。
-- 同值重传保留首次来源批次/行号；异值整批拒收。
CREATE TABLE IF NOT EXISTS meter_reading (
    point_id        TEXT NOT NULL,
    hour            TEXT NOT NULL,
    cumulative_kwh  REAL NOT NULL CHECK (cumulative_kwh >= 0),
    batch_id        INTEGER NOT NULL REFERENCES import_batch(id),
    line_no         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (point_id, hour)
);
CREATE INDEX IF NOT EXISTS idx_meter_hour ON meter_reading(hour);

-- 质检日报
CREATE TABLE IF NOT EXISTS report (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date        TEXT NOT NULL,        -- YYYY-MM-DD
    tolerance          REAL NOT NULL CHECK (tolerance >= 0),
    status             TEXT NOT NULL CHECK (status IN ('complete', 'incomplete', 'anomaly')),
    config_snapshot    TEXT NOT NULL,        -- 提交时 24 小时配置快照 JSON
    reading_snapshot   TEXT NOT NULL,        -- 提交时 24 小时读数依据 JSON
    detail             TEXT NOT NULL,        -- 每小时结论 JSON
    created_at         TEXT NOT NULL,
    invalidated_reason TEXT,                   -- 非空即已失效（由配置/读数追溯变更触发）
    invalidated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_report_lookup
    ON report(report_date, tolerance, created_at);
CREATE INDEX IF NOT EXISTS idx_reading_hour ON reading(hour);
CREATE INDEX IF NOT EXISTS idx_config_effective ON config_version(effective_hour);
"""


def get_db_path() -> Path:
    return DB_PATH


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开一个连接。使用 autocommit 模式，事务由服务层显式控制。"""
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """向前兼容旧库：为已有 reading 表补齐派生来源列。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reading)")}
    additions = {
        "source": "TEXT NOT NULL DEFAULT 'manual'",
        "start_batch_id": "INTEGER REFERENCES import_batch(id)",
        "start_line_no": "INTEGER",
        "start_cumulative_kwh": "REAL",
        "end_batch_id": "INTEGER REFERENCES import_batch(id)",
        "end_line_no": "INTEGER",
        "end_cumulative_kwh": "REAL",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE reading ADD COLUMN {name} {ddl}")


def init_db(db_path: str | Path | None = None) -> None:
    conn = connect(db_path)
    try:
        # executescript 会先隐式提交再执行整段脚本，故不在此手动开事务
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()
