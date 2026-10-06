"""复水计划端到端测试：逐步预演、不可变保存、修订号执行/回放/失效。"""
from __future__ import annotations

import os
import tempfile

import pytest

# 独立运行本文件时（不经过 test_api）确保有数据库文件
os.environ.setdefault(
    "WATER_DB_PATH",
    tempfile.NamedTemporaryFile(suffix=".db", delete=False).name,
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402

init_db()


# 与 test_api 同一进程/同一数据库：每个用例先 PUT 覆盖管网（同时使旧计划失效）。
def network():
    return {
        "sources": [
            {"id": "S1", "capacity": 10},
            {"id": "S2", "capacity": 6},
        ],
        "demands": [
            {"id": "D1", "demand": 10, "priority": 9},
            {"id": "D2", "demand": 8, "priority": 1},
        ],
        "junctions": [{"id": "J1"}],
        "pipes": [
            {"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 10},
            {"id": "P2", "from_node": "S2", "to_node": "J1", "capacity": 6},
            {"id": "P3", "from_node": "J1", "to_node": "D2", "capacity": 4},
            {"id": "P5", "from_node": "S1", "to_node": "S2", "capacity": 5},
        ],
        "valves": [
            {"id": "V1", "pipe_id": "P1", "is_open": True},
            {"id": "V3", "pipe_id": "P3", "is_open": True},
            {"id": "V5", "pipe_id": "P5", "is_open": False},
        ],
        "forbidden_pairs": [{"source_a": "S1", "source_b": "S2"}],
    }


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def setup_isolated(client):
    """录入管网后关闭 V1/V3（模拟抢修隔离态），返回当前修订号。"""
    r = client.put("/api/network", json=network())
    assert r.status_code == 200, r.text
    rev = r.json()["revision"]
    r = client.post("/api/valves/change", json={"changes": [
        {"valve_id": "V1", "action": "close"},
        {"valve_id": "V3", "action": "close"},
    ]})
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def test_revision_starts_and_bumps(client):
    r = client.put("/api/network", json=network())
    rev1 = r.json()["revision"]
    assert isinstance(rev1, int) and rev1 >= 1
    assert client.get("/api/network").json()["revision"] == rev1
    assert client.get("/api/status").json()["revision"] == rev1
    r = client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V1", "action": "close"}]})
    assert r.json()["revision"] == rev1 + 1
    # 恢复
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V1", "action": "open"}]})


def test_plan_preview_validation_errors(client):
    setup_isolated(client)
    # 空清单 -> 422（min_length）
    r = client.post("/api/restore/plans", json={"steps": []})
    assert r.status_code == 422

    # 未知阀门 / 非法动作 / 重复阀门 -> 400 带定位
    before = len(client.get("/api/restore/plans").json()["plans"])
    r = client.post("/api/restore/plans", json={"steps": [
        {"valve_id": "NOPE", "action": "open"},
        {"valve_id": "V3", "action": "detonate"},
        {"valve_id": "V3", "action": "open"},
    ]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_restore_plan"
    locs = [(d["location"], d["code"]) for d in r.json()["error"]["details"]]
    assert ("steps[0].valve_id", "unknown_valve") in locs
    assert ("steps[1].action", "invalid_action") in locs
    assert ("steps[2].valve_id", "duplicate_valve") in locs

    # 校验失败不保存任何计划
    after = len(client.get("/api/restore/plans").json()["plans"])
    assert after == before


def test_plan_preview_conflict_not_saved(client):
    setup_isolated(client)
    before = len(client.get("/api/restore/plans").json()["plans"])
    # 第 1 步打开 V3 安全；第 2 步打开 V5（互锁水源 S1/S2 经 P5 连通）-> 首个冲突
    r = client.post("/api/restore/plans", json={"steps": [
        {"valve_id": "V3", "action": "open"},
        {"valve_id": "V5", "action": "open"},
    ]})
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "forbidden_sources_connected"
    d = body["details"][0]
    assert d["position"] == 2
    assert d["location"] == "steps[1]"
    assert d["source_a"] == "S1" and d["source_b"] == "S2"
    assert d["path"][0] == "S1" and d["path"][-1] == "S2"

    # 预演不写状态：V3 仍为关闭
    net = client.get("/api/network").json()["network"]
    states = {v["id"]: v["is_open"] for v in net["valves"]}
    assert states["V3"] is False and states["V5"] is False

    # 冲突计划不得被保存
    after = len(client.get("/api/restore/plans").json()["plans"])
    assert after == before


def test_full_plan_execute_replay_and_completion(client):
    rev = setup_isolated(client)
    r = client.post("/api/restore/plans", json={"steps": [
        {"valve_id": "V3", "action": "open"},
        {"valve_id": "V1", "action": "open"},
    ]})
    assert r.status_code == 200, r.text
    saved = r.json()
    plan_id = saved["plan_id"]
    assert saved["status"] == "active"
    assert saved["base_revision"] == rev
    assert saved["total_steps"] == 2 and saved["next_step"] == 1

    # 每步快照含阀位与沿用现有口径的供水/欠供
    s1 = saved["steps"][0]
    assert s1["position"] == 1 and s1["is_open"] is True
    v3_state = next(v for v in s1["valves"] if v["valve_id"] == "V3")
    assert v3_state["is_open"] is True
    alloc = {a["demand_id"]: a for a in s1["allocations"]}
    # V3 打开后 D2 由 S2 供 4（P3 容量限制）；V1 未开 D1 仍隔离
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D1"]["shortfall_reason_code"] == "ISOLATED"

    # ---- 执行第 1 步 ----
    r = client.post(f"/api/restore/plans/{plan_id}/execute",
                    json={"revision": rev})
    assert r.status_code == 200, r.text
    ex1 = r.json()
    assert ex1["position"] == 1 and ex1["next_step"] == 2
    assert ex1["status"] == "active"
    rev1 = ex1["revision"]
    assert rev1 == rev + 1
    assert ex1["step"]["valve_id"] == "V3"
    assert ex1["idempotent_replay"] is False

    # 库内阀位已变：V3 开
    states = {v["id"]: v["is_open"]
              for v in client.get("/api/network").json()["network"]["valves"]}
    assert states["V3"] is True and states["V1"] is False

    # ---- 过期修订号执行下一步 -> 409，状态不变 ----
    r = client.post(f"/api/restore/plans/{plan_id}/execute",
                    json={"revision": rev})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "revision_conflict"
    assert r.json()["error"]["details"][0]["current_revision"] == rev1

    # ---- 跳步 -> 409，状态不变 ----
    r = client.post(f"/api/restore/plans/{plan_id}/execute",
                    json={"revision": rev1, "step": 5})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "step_out_of_order"

    # ---- 重复提交已完成的第 1 步：回放原结果，修订号不变 ----
    r = client.post(f"/api/restore/plans/{plan_id}/execute",
                    json={"revision": rev1, "step": 1})
    assert r.status_code == 200
    replay = r.json()
    assert replay["idempotent_replay"] is True
    assert replay["position"] == 1
    assert replay["revision"] == rev1  # 仍是执行时的修订号，不再次 +1
    assert client.get("/api/network").json()["revision"] == rev1

    # ---- 正确修订号执行第 2 步 -> 完成 ----
    r = client.post(f"/api/restore/plans/{plan_id}/execute",
                    json={"revision": rev1})
    assert r.status_code == 200
    ex2 = r.json()
    assert ex2["position"] == 2 and ex2["next_step"] is None
    assert ex2["status"] == "completed"
    rev2 = ex2["revision"]
    assert rev2 == rev1 + 1
    alloc = {a["demand_id"]: a for a in ex2["step"]["allocations"]}
    assert alloc["D1"]["fulfilled"] is True

    # 完成后无 step 再执行 -> 409 plan_already_completed
    r = client.post(f"/api/restore/plans/{plan_id}/execute",
                    json={"revision": rev2})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_already_completed"

    # 已完成步骤仍可回放
    r = client.post(f"/api/restore/plans/{plan_id}/execute",
                    json={"revision": rev2, "step": 2})
    assert r.status_code == 200
    assert r.json()["idempotent_replay"] is True
    assert r.json()["revision"] == rev2


def test_plan_invalidated_by_valve_change_and_reload(client):
    rev = setup_isolated(client)
    r = client.post("/api/restore/plans", json={"steps": [
        {"valve_id": "V3", "action": "open"},
    ]})
    plan_id = r.json()["plan_id"]

    # 阀门变更使未完计划失效，修订号推进
    r = client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V1", "action": "open"}]})
    assert r.status_code == 200
    rev2 = r.json()["revision"]

    detail = client.get(f"/api/restore/plans/{plan_id}").json()
    assert detail["status"] == "invalidated"

    r = client.post(f"/api/restore/plans/{plan_id}/execute",
                    json={"revision": rev2})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_invalidated"

    # 清空管网也使计划失效
    r = client.post("/api/restore/plans", json={"steps": [
        {"valve_id": "V3", "action": "close"},
    ]})
    assert r.status_code == 200  # V1 当前开启，关 V3 安全
    pid2 = r.json()["plan_id"]
    client.delete("/api/network")
    detail = client.get(f"/api/restore/plans/{pid2}").json()
    assert detail["status"] == "invalidated"


def test_plan_steps_are_immutable_snapshot(client):
    rev = setup_isolated(client)
    r = client.post("/api/restore/plans", json={"steps": [
        {"valve_id": "V3", "action": "open"},
    ]})
    plan_id = r.json()["plan_id"]
    detail = client.get(f"/api/restore/plans/{plan_id}").json()
    assert [s["valve_id"] for s in detail["steps"]] == ["V3"]
    assert detail["steps"][0]["action"] == "open"
    assert detail["base_revision"] == rev

    # 未知计划 -> 404
    r = client.post("/api/restore/plans/PLAN-NOPE/execute",
                    json={"revision": rev})
    assert r.status_code == 404
