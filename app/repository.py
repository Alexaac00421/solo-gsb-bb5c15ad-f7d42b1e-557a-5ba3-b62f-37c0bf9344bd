"""数据库与内存模型之间的转换及事务写入。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .engine import Network, Node, Pipe, Valve


def load_network(conn: sqlite3.Connection) -> Optional[Network]:
    node_rows = conn.execute("SELECT * FROM nodes").fetchall()
    if not node_rows:
        return None
    nodes: dict[str, Node] = {}
    for r in node_rows:
        nodes[r["id"]] = Node(
            id=r["id"],
            type=r["type"],
            capacity=r["capacity"],
            demand=r["demand"],
            priority=r["priority"],
        )
    pipes = {
        r["id"]: Pipe(r["id"], r["from_node"], r["to_node"], r["capacity"])
        for r in conn.execute("SELECT * FROM pipes").fetchall()
    }
    valves = {
        r["id"]: Valve(r["id"], r["pipe_id"], bool(r["is_open"]))
        for r in conn.execute("SELECT * FROM valves").fetchall()
    }
    valves_by_pipe: dict[str, list[str]] = {}
    for vid, v in valves.items():
        valves_by_pipe.setdefault(v.pipe_id, []).append(vid)
    for vids in valves_by_pipe.values():
        vids.sort()
    pairs = [
        (r["source_a"], r["source_b"])
        for r in conn.execute("SELECT source_a, source_b FROM pairs").fetchall()
    ]
    return Network(nodes, pipes, valves, pairs, valves_by_pipe)


def replace_all(conn: sqlite3.Connection, network: Network) -> None:
    """在调用方事务内覆盖式写入全部设施。"""
    cur = conn.cursor()
    cur.execute("DELETE FROM pairs")
    cur.execute("DELETE FROM valves")
    cur.execute("DELETE FROM pipes")
    cur.execute("DELETE FROM nodes")
    cur.executemany(
        "INSERT INTO nodes (id, type, capacity, demand, priority) VALUES (?,?,?,?,?)",
        [
            (n.id, n.type, n.capacity, n.demand, n.priority)
            for n in network.nodes.values()
        ],
    )
    cur.executemany(
        "INSERT INTO pipes (id, from_node, to_node, capacity) VALUES (?,?,?,?)",
        [(p.id, p.from_node, p.to_node, p.capacity) for p in network.pipes.values()],
    )
    cur.executemany(
        "INSERT INTO valves (id, pipe_id, is_open) VALUES (?,?,?)",
        [(v.id, v.pipe_id, 1 if v.is_open else 0) for v in network.valves.values()],
    )
    cur.executemany(
        "INSERT INTO pairs (source_a, source_b) VALUES (?,?)",
        network.forbidden_pairs,
    )


def serialize_network(network: Network) -> dict:
    return {
        "sources": [
            {"id": n.id, "capacity": n.capacity}
            for n in network.nodes.values()
            if n.type == "source"
        ],
        "demands": [
            {"id": n.id, "demand": n.demand, "priority": n.priority}
            for n in network.nodes.values()
            if n.type == "demand"
        ],
        "junctions": [
            {"id": n.id}
            for n in network.nodes.values()
            if n.type == "junction"
        ],
        "pipes": [
            {"id": p.id, "from_node": p.from_node, "to_node": p.to_node,
             "capacity": p.capacity}
            for p in network.pipes.values()
        ],
        "valves": [
            {"id": v.id, "pipe_id": v.pipe_id, "is_open": v.is_open}
            for v in network.valves.values()
        ],
        "forbidden_pairs": [
            {"source_a": a, "source_b": b} for a, b in network.forbidden_pairs
        ],
    }


# --------------------------------------------------------------------------- #
# 复水计划持久化
# --------------------------------------------------------------------------- #
def invalidate_active_plans(
    conn: sqlite3.Connection, except_id: Optional[str] = None
) -> int:
    """在调用方事务内使所有未完（active）计划失效。

    except_id 给定时，该计划保留 active（用于计划自身执行）。
    返回被失效的计划数。
    """
    if except_id is None:
        cur = conn.execute(
            "UPDATE plans SET status='invalidated', "
            "updated_at=datetime('now') WHERE status='active'"
        )
    else:
        cur = conn.execute(
            "UPDATE plans SET status='invalidated', "
            "updated_at=datetime('now') WHERE status='active' AND id <> ?",
            (except_id,),
        )
    return cur.rowcount


def save_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    steps: list[dict[str, Any]],
    base_revision: int,
) -> None:
    """在调用方事务内保存不可变计划及其步骤。

    steps: [{'valve_id','action','target_is_open'}, ...]，按顺序写入。
    """
    conn.execute(
        "INSERT INTO plans (id, status, base_revision, total_steps, next_step) "
        "VALUES (?, 'active', ?, ?, 0)",
        (plan_id, base_revision, len(steps)),
    )
    conn.executemany(
        "INSERT INTO plan_steps "
        "(plan_id, step_index, position, valve_id, action, target_is_open) "
        "VALUES (?,?,?,?,?,?)",
        [
            (plan_id, i, i + 1, s["valve_id"], s["action"],
             1 if s["target_is_open"] else 0)
            for i, s in enumerate(steps)
        ],
    )


def get_plan(conn: sqlite3.Connection, plan_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        return None
    plan = dict(row)
    plan["steps"] = [
        dict(r)
        for r in conn.execute(
            "SELECT step_index, position, valve_id, action, target_is_open "
            "FROM plan_steps WHERE plan_id = ? ORDER BY step_index",
            (plan_id,),
        ).fetchall()
    ]
    return plan


def list_plans(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, status, base_revision, total_steps, next_step, "
            "created_at, updated_at FROM plans ORDER BY created_at, id"
        ).fetchall()
    ]


def save_step_result(
    conn: sqlite3.Connection,
    plan_id: str,
    step_index: int,
    revision: int,
    result: dict,
) -> None:
    """在调用方事务内持久化某步执行结果，并推进计划进度。"""
    conn.execute(
        "INSERT INTO plan_results (plan_id, step_index, revision, result_json) "
        "VALUES (?,?,?,?)",
        (plan_id, step_index, revision, json.dumps(result, ensure_ascii=False)),
    )
    conn.execute(
        "UPDATE plans SET next_step = ?, "
        "status = CASE WHEN ? >= total_steps THEN 'completed' ELSE status END, "
        "updated_at = datetime('now') WHERE id = ?",
        (step_index + 1, step_index + 1, plan_id),
    )


def get_step_result(
    conn: sqlite3.Connection, plan_id: str, step_index: int
) -> Optional[dict]:
    row = conn.execute(
        "SELECT result_json FROM plan_results "
        "WHERE plan_id = ? AND step_index = ?",
        (plan_id, step_index),
    ).fetchone()
    return json.loads(row["result_json"]) if row is not None else None
