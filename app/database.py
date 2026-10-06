"""SQLite 持久化层。

每张表对应一类设施/状态：
- nodes      水源 / 用水点 / 连接点（节点）
- pipes      单向管段（容量、起终点）
- valves     阀门（位于某管段上，状态 open / closed）
- pairs      不可并联的水源对
- meta       键值元信息：revision 为管网/阀位基准修订号（乐观并发）
- plans      复水计划（不可变步骤；status 随执行推进 / 被变更失效）
- plan_steps 计划步骤（有序、阀门不重复；含逐步预演快照）
- plan_results 已执行步骤的事务结果（供重复提交回放原结果）
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "water.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL CHECK (type IN ('source', 'demand', 'junction')),
    capacity     REAL,   -- 水源：供水上限
    demand       REAL,   -- 用水点：需求量
    priority     INTEGER -- 用水点：优先级（数值越大越优先）
);

CREATE TABLE IF NOT EXISTS pipes (
    id        TEXT PRIMARY KEY,
    from_node TEXT NOT NULL,
    to_node   TEXT NOT NULL,
    capacity  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS valves (
    id       TEXT PRIMARY KEY,
    pipe_id  TEXT NOT NULL,
    is_open  INTEGER NOT NULL CHECK (is_open IN (0, 1))
);

CREATE TABLE IF NOT EXISTS pairs (
    source_a  TEXT NOT NULL,
    source_b  TEXT NOT NULL,
    PRIMARY KEY (source_a, source_b)
);

-- 键值元信息（revision 为当前管网/阀位基准修订号，单调递增）
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
);

-- 复水计划：步骤一经保存不可变；仅 status/progress/result 随执行推进
CREATE TABLE IF NOT EXISTS plans (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL CHECK (status IN ('active','completed','invalidated')),
    base_revision INTEGER NOT NULL,  -- 创建计划时所见的基准修订号
    total_steps   INTEGER NOT NULL,
    next_step     INTEGER NOT NULL DEFAULT 0,  -- 首个未完成步骤（0 基）
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plan_steps (
    plan_id        TEXT NOT NULL,
    step_index     INTEGER NOT NULL,   -- 0 基
    position       INTEGER NOT NULL,   -- 1 基（对外的“步骤位置”）
    valve_id       TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('open','close')),
    target_is_open INTEGER NOT NULL CHECK (target_is_open IN (0, 1)),
    PRIMARY KEY (plan_id, step_index)
);

-- 已完成步骤的事务执行结果（JSON），重复提交时原样回放
CREATE TABLE IF NOT EXISTS plan_results (
    plan_id     TEXT NOT NULL,
    step_index  INTEGER NOT NULL,
    revision    INTEGER NOT NULL,      -- 该步执行后的修订号
    result_json TEXT NOT NULL,
    PRIMARY KEY (plan_id, step_index)
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """在给定连接上幂等建表（含旧库升级）并播种 revision=0。"""
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', 0)"
    )


def db_path() -> str:
    return os.environ.get("WATER_DB_PATH", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    """打开一个带外键约束的连接。

    check_same_thread=False：FastAPI 的依赖生成在事件循环线程建连、
    端点函数在线程池中使用；连接本身每请求独占，且写操作在应用层加锁，安全。
    """
    path = path or db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接（普通生成器，配合 Depends 的 yield 语义）。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: str | None = None) -> None:
    """初始化数据库表结构（幂等，兼容已有旧库）。容器启动时执行一次。"""
    with connect(path) as conn:
        init_schema(conn)
        conn.commit()


def get_revision(conn: sqlite3.Connection) -> int:
    """读取当前管网/阀位基准修订号。"""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'revision'"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def bump_revision(conn: sqlite3.Connection) -> int:
    """在调用方事务内把基准修订号 +1，返回新值。"""
    conn.execute(
        "UPDATE meta SET value = value + 1 WHERE key = 'revision'"
    )
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('revision', 1)"
        )
    return get_revision(conn)


def clear_all(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM pairs")
    conn.execute("DELETE FROM valves")
    conn.execute("DELETE FROM pipes")
    conn.execute("DELETE FROM nodes")


def is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
