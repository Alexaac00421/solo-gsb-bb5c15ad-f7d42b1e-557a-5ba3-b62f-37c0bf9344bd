"""端到端测试：用临时 SQLite + FastAPI TestClient 验证全部业务规则。"""
from __future__ import annotations

import os
import tempfile

import pytest

# 在导入应用前指定临时数据库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["WATER_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c
    c.delete("/api/network")


# 演示管网（满足 S1/S2 互锁：两支路仅由常闭管段 P5 相连）：
#   S1 --P1(cap10,V1)--> D1(需求10, 高优先级9)
#   S2 --P2(cap6)------> J1 --P3(cap4,V3)--> D2(需求8, 优先级1)
#                            \--P6(cap6)----> D3(需求6, 优先级1)
#   P5(cap5,V5, 关闭): S1 -> S2   （打开即互锁冲突）
def valid_network():
    return {
        "sources": [
            {"id": "S1", "capacity": 10},
            {"id": "S2", "capacity": 6},
        ],
        "demands": [
            {"id": "D1", "demand": 10, "priority": 9},
            {"id": "D2", "demand": 8, "priority": 1},
            {"id": "D3", "demand": 6, "priority": 1},
        ],
        "junctions": [{"id": "J1"}],
        "pipes": [
            {"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 10},
            {"id": "P2", "from_node": "S2", "to_node": "J1", "capacity": 6},
            {"id": "P3", "from_node": "J1", "to_node": "D2", "capacity": 4},
            {"id": "P6", "from_node": "J1", "to_node": "D3", "capacity": 6},
            {"id": "P5", "from_node": "S1", "to_node": "S2", "capacity": 5},
        ],
        "valves": [
            {"id": "V1", "pipe_id": "P1", "is_open": True},
            {"id": "V3", "pipe_id": "P3", "is_open": True},
            {"id": "V5", "pipe_id": "P5", "is_open": False},
        ],
        "forbidden_pairs": [{"source_a": "S1", "source_b": "S2"}],
    }


def load(client):
    r = client.put("/api/network", json=valid_network())
    assert r.status_code == 200, r.text
    return r


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_load_and_allocation_priority(client):
    load(client)
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    alloc = {a["demand_id"]: a for a in data["allocations"]}

    # 排序：D1(优先级9) 最先；同级 D2、D3 按编号升序
    assert [a["demand_id"] for a in data["allocations"]] == ["D1", "D2", "D3"]

    # D1 由 S1 独立供水 10，足额
    assert alloc["D1"]["supplied"] == pytest.approx(10)
    assert alloc["D1"]["fulfilled"] is True
    assert alloc["D1"]["shortfall_reason_code"] is None
    assert alloc["D1"]["by_source"] == {"S1": 10.0}

    # D2 受共享管段 P3 容量 4 限制，只能得 4，欠 4
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D2"]["fulfilled"] is False
    assert alloc["D2"]["shortfall_reason_code"] == "PIPE_LIMIT"
    pids = [p["pipe_id"] for p in alloc["D2"]["constrained_pipes"]]
    assert "P3" in pids

    # D2 占 4 后，D3 分得 S2 剩余 2；S2 已空、共享干管 P2 被两点合计占满
    assert alloc["D3"]["supplied"] == pytest.approx(2)
    assert alloc["D3"]["shortfall_reason_code"] == "SOURCE_AND_PIPE_LIMIT"
    assert any(p["pipe_id"] == "P2" for p in alloc["D3"]["constrained_pipes"])

    # 水源总量守恒；S2 的 6 在 D2/D3 间不重复占用
    assert data["totals"]["supplied"] == pytest.approx(16)
    src = {s["source_id"]: s for s in data["source_utilization"]}
    assert src["S1"]["used"] == pytest.approx(10)
    assert src["S2"]["used"] == pytest.approx(6)


def test_shared_pipe_capacity_not_double_counted(client):
    load(client)
    # 关掉 V1：D1 隔离；S2 的 6 仍按 D2(先) D3(后) 分派
    r = client.post("/api/drill", json={"close_valves": ["V1"]})
    assert r.status_code == 200
    data = r.json()
    assert "V1" in data["closed_valves"]
    alloc = {a["demand_id"]: a for a in data["allocations"]}
    assert alloc["D1"]["supplied"] == 0
    assert alloc["D1"]["shortfall_reason_code"] == "ISOLATED"
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)

    # 预演不写状态：库内 V1 仍开启
    net = client.get("/api/network").json()["network"]
    v1 = next(v for v in net["valves"] if v["id"] == "V1")
    assert v1["is_open"] is True


def test_drill_close_valve_isolates_demand(client):
    load(client)
    # 关闭 V3（J1->D2）：D2 被隔离，P3 是受限管段且 V3 已关
    r = client.post("/api/drill", json={"close_valves": ["V3"]})
    assert r.status_code == 200
    alloc = {a["demand_id"]: a for a in r.json()["allocations"]}
    assert alloc["D2"]["supplied"] == 0
    assert alloc["D2"]["shortfall_reason_code"] == "ISOLATED"
    assert any(p["pipe_id"] == "P3" and "V3" in p["closed_valves"]
               for p in alloc["D2"]["constrained_pipes"])
    # 其他点不受影响
    assert alloc["D1"]["fulfilled"] is True
    assert alloc["D3"]["supplied"] == pytest.approx(6)


def test_valve_change_connecting_forbidden_sources_rejected(client):
    load(client)
    # 打开 V5 会经 P5 让 S1/S2 连通 -> 409，原状态保持关闭
    r = client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V5", "action": "open"}]})
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "forbidden_sources_connected"
    assert body["details"][0]["path"][0] == "S1"
    assert body["details"][0]["path"][-1] == "S2"

    net = client.get("/api/network").json()["network"]
    v5 = next(v for v in net["valves"] if v["id"] == "V5")
    assert v5["is_open"] is False


def test_valve_batch_atomic_all_or_nothing(client):
    load(client)
    # 同批次：合法关 V3 + 非法阀门 X9 -> 整批拒绝，V3 保持开启
    r = client.post("/api/valves/change", json={"changes": [
        {"valve_id": "V3", "action": "close"},
        {"valve_id": "X9", "action": "close"},
    ]})
    assert r.status_code == 400
    assert r.json()["error"]["details"][0]["location"] == "changes[1].valve_id"

    net = client.get("/api/network").json()["network"]
    v3 = next(v for v in net["valves"] if v["id"] == "V3")
    assert v3["is_open"] is True

    # 同批次里 V5 打开（互锁冲突）+ V3 关闭 -> 整批拒绝，V3 保持开启
    r = client.post("/api/valves/change", json={"changes": [
        {"valve_id": "V5", "action": "open"},
        {"valve_id": "V3", "action": "close"},
    ]})
    assert r.status_code == 409
    net = client.get("/api/network").json()["network"]
    assert next(v for v in net["valves"] if v["id"] == "V3")["is_open"] is True


def test_valid_valve_batch_applies(client):
    load(client)
    r = client.post("/api/valves/change", json={"changes": [
        {"valve_id": "V3", "action": "close"},
        {"valve_id": "V1", "is_open": False},
    ]})
    assert r.status_code == 200
    assert len(r.json()["applied"]) == 2
    # 恢复
    client.post("/api/valves/change", json={"changes": [
        {"valve_id": "V3", "action": "open"},
        {"valve_id": "V1", "action": "open"},
    ]})


def test_unknown_endpoint_negative_capacity_errors(client):
    bad = valid_network()
    bad["pipes"][0]["to_node"] = "GHOST"
    r = client.put("/api/network", json=bad)
    assert r.status_code == 400
    locs = [d["location"] for d in r.json()["error"]["details"]]
    assert "pipes[0].to_node" in locs

    bad = valid_network()
    bad["pipes"][1]["capacity"] = -3
    r = client.put("/api/network", json=bad)
    assert r.status_code == 422  # Pydantic 层拦截负数
    assert r.json()["error"]["details"][0]["location"].endswith("capacity")

    bad = valid_network()
    bad["sources"][0]["capacity"] = -1
    r = client.put("/api/network", json=bad)
    assert r.status_code == 422


def test_valve_on_missing_pipe_rejected(client):
    bad = valid_network()
    bad["valves"].append({"id": "VX", "pipe_id": "NOPE", "is_open": True})
    r = client.put("/api/network", json=bad)
    assert r.status_code == 400
    d = r.json()["error"]["details"][0]
    assert d["location"] == "valves[3].pipe_id"
    assert d["code"] == "unknown_pipe"


def test_initial_forbidden_connection_rejected(client):
    bad = valid_network()
    # V5 初始开启 -> S1/S2 已经连通
    bad["valves"][2]["is_open"] = True
    r = client.put("/api/network", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["details"][0]["code"] == "forbidden_connected"


def test_unknown_valve_in_drill(client):
    load(client)
    r = client.post("/api/drill", json={"close_valves": ["NO-SUCH-VALVE"]})
    assert r.status_code == 400
    d = r.json()["error"]["details"][0]
    assert d["location"] == "close_valves[0]"
    assert d["code"] == "unknown_valve"


def test_source_limit_reason(client):
    # 小水源：S1 cap 3、S2 cap 6，但 P5 关闭且去掉 D2 的分支竞争
    net = {
        "sources": [{"id": "S1", "capacity": 3}],
        "demands": [{"id": "D1", "demand": 10, "priority": 5}],
        "junctions": [],
        "pipes": [{"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 100}],
        "valves": [{"id": "V1", "pipe_id": "P1", "is_open": True}],
        "forbidden_pairs": [],
    }
    r = client.put("/api/network", json=net)
    assert r.status_code == 200, r.text
    r = client.get("/api/status")
    a = r.json()["allocations"][0]
    assert a["supplied"] == pytest.approx(3)
    assert a["shortfall_reason_code"] == "SOURCE_LIMIT"


def test_duplicate_and_extra_field(client):
    bad = valid_network()
    bad["pipes"].append(dict(bad["pipes"][0]))  # 重复编号
    r = client.put("/api/network", json=bad)
    assert r.status_code == 400
    codes = [d["code"] for d in r.json()["error"]["details"]]
    assert "duplicate_id" in codes

    bad = valid_network()
    bad["sources"][0]["bogus"] = 1
    r = client.put("/api/network", json=bad)
    assert r.status_code == 422  # extra="forbid"
