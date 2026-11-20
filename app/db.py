"""SQLite 持久化层。

设计要点:
- 路网按版本注册,版本一旦创建即不可变;修订路网 = 注册新版本。
- 分析结果(匹配路径 + 缺口)按分析时的路网版本落库,
  因此路网修订后,旧分析仍可按原版本查询。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS networks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id  INTEGER NOT NULL REFERENCES networks(id),
    segment_id  TEXT NOT NULL,
    start_lat   REAL NOT NULL,
    start_lon   REAL NOT NULL,
    end_lat     REAL NOT NULL,
    end_lon     REAL NOT NULL,
    length_m    REAL NOT NULL,
    UNIQUE (network_id, segment_id)
);

CREATE TABLE IF NOT EXISTS trajectories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id   INTEGER NOT NULL REFERENCES trajectories(id),
    seq             INTEGER NOT NULL,
    ts              TEXT NOT NULL,   -- UTC ISO8601
    lat             REAL NOT NULL,
    lon             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id    INTEGER NOT NULL REFERENCES trajectories(id),
    network_id       INTEGER NOT NULL REFERENCES networks(id),
    created_at       TEXT NOT NULL,
    total_points     INTEGER NOT NULL,
    matched_points   INTEGER NOT NULL,
    total_distance_m REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    segment_id  TEXT NOT NULL,
    enter_time  TEXT NOT NULL,
    leave_time  TEXT NOT NULL,
    distance_m  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    reason      TEXT NOT NULL,  -- out_of_range | disconnected
    start_seq   INTEGER,        -- 缺口起止采样序号(旧分析为 NULL)
    end_seq     INTEGER
);

-- 按采样序号保存的核算依据:每个轨迹采样点匹配到的累计里程,
-- 未匹配点记录缺口原因。对账只依赖这份依据,不重跑地图匹配。
CREATE TABLE IF NOT EXISTS analysis_samples (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id          INTEGER NOT NULL REFERENCES analyses(id),
    seq                  INTEGER NOT NULL,
    matched              INTEGER NOT NULL,  -- 0/1
    cumulative_matched_m REAL,               -- 未匹配时为 NULL
    gap_reason           TEXT,               -- out_of_range | disconnected
    UNIQUE (analysis_id, seq)
);

-- 车载里程表读数:每条轨迹维护一组,整组补交/更正(revision 递增)。
CREATE TABLE IF NOT EXISTS odometer_groups (
    trajectory_id INTEGER PRIMARY KEY REFERENCES trajectories(id),
    revision      INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS odometer_readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id INTEGER NOT NULL REFERENCES trajectories(id),
    seq           INTEGER NOT NULL,      -- 原轨迹采样序号
    odometer_m    REAL NOT NULL CHECK (odometer_m >= 0),
    UNIQUE (trajectory_id, seq)
);

-- 保存的对账结果:新分析落库时或读数整组更新时在同一事务内生成。
CREATE TABLE IF NOT EXISTS reconciliations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id           INTEGER NOT NULL UNIQUE REFERENCES analyses(id),
    status                TEXT NOT NULL,   -- ok | pending_readings
    readings_revision     INTEGER,
    computed_at           TEXT NOT NULL,
    segment_count         INTEGER NOT NULL DEFAULT 0,
    checked_count         INTEGER NOT NULL DEFAULT 0,
    unverifiable_count    INTEGER NOT NULL DEFAULT 0,
    odometer_delta_m      REAL,
    matched_distance_m    REAL,
    difference_m          REAL
);

CREATE TABLE IF NOT EXISTS reconciliation_segments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    reconciliation_id   INTEGER NOT NULL REFERENCES reconciliations(id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    start_seq           INTEGER NOT NULL,
    end_seq             INTEGER NOT NULL,
    status              TEXT NOT NULL,   -- ok | unverifiable
    reason              TEXT,            -- match_gap | reading_regression
    reasons             TEXT,            -- JSON 数组,可能同时存在多个原因
    gap_reasons         TEXT,            -- JSON 数组,缺口的具体原因
    odometer_delta_m    REAL,
    matched_distance_m  REAL,
    difference_m        REAL
);

CREATE INDEX IF NOT EXISTS idx_analysis_samples_analysis
    ON analysis_samples(analysis_id);
CREATE INDEX IF NOT EXISTS idx_analyses_trajectory
    ON analyses(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_odometer_readings_trajectory
    ON odometer_readings(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_segments_reconciliation
    ON reconciliation_segments(reconciliation_id);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧库补列。旧分析的缺口没有采样序号,对应列为 NULL。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_gaps)")}
    if "start_seq" not in cols:
        conn.execute("ALTER TABLE analysis_gaps ADD COLUMN start_seq INTEGER")
    if "end_seq" not in cols:
        conn.execute("ALTER TABLE analysis_gaps ADD COLUMN end_seq INTEGER")


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
