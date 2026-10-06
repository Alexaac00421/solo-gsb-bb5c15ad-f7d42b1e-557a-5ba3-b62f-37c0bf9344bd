"""端到端测试：抢修降额影响预演 POST /api/derating/preview。

使用独立临时 SQLite，不依赖其它测试模块的数据状态。
"""
from __future__ import annotations

import os
import tempfile

import pytest

# 独立运行本文件时（不经过 test_api）确保有数据库文件；与其它模块同进程时共用
os.environ.setdefault(
    "WATER_DB_PATH",
    tempfile.NamedTemporaryFile(suffix=".db", delete=False).name,
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402

init_db()


# 演示管网（同 tests/test_api.py）：
#   S1 --P1(cap10,V1)--> D1(需求10, 优先级9)
#   S2 --P2(cap6)------> J1 --P3(cap4,V3)--> D2(需求8, 优先级1)
#                            \--P6(cap6)----> D3(需求6, 优先级1)
#   P5(cap5,V5, 关闭): S1 -> S2
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


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def loaded(client):
    r = client.put("/api/network", json=valid_network())
    assert r.status_code == 200, r.text
    revision = r.json()["revision"]
    yield client, revision


def test_requires_network_initialized(client):
    client.delete("/api/network")
    r = client.post("/api/derating/preview", json={"revision": 0})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "network_not_initialized"
    # 重新录入供后续用例
    client.put("/api/network", json=valid_network())


def test_no_derating_equals_baseline(loaded):
    client, rev = loaded
    r = client.post("/api/derating/preview", json={"revision": rev})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["revision"] == rev
    assert data["derating"]["no_derating"] is True

    status = client.get("/api/status").json()
    assert data["baseline"]["allocations"] == status["allocations"]
    assert data["derated"]["allocations"] == data["baseline"]["allocations"]
    assert data["derated"]["totals"] == data["baseline"]["totals"]
    assert all(c["delta"] == 0 and c["affected"] is False
               for c in data["demand_changes"])
    assert data["affected_demands"] == []
    assert data["derated_utilization"] == []
    assert data["totals_change"]["delta_supplied"] == 0


def test_source_derating_impacts_lower_priority(loaded):
    # S2 临时降额 6 -> 3：D1 不受影响；D2 先占 4 中的 3，D3 降为 0
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [{"facility_id": "S2", "capacity": 3}],
    })
    assert r.status_code == 200, r.text
    data = r.json()

    base = {a["demand_id"]: a for a in data["baseline"]["allocations"]}
    der = {a["demand_id"]: a for a in data["derated"]["allocations"]}
    assert base["D1"]["supplied"] == pytest.approx(10)
    assert der["D1"]["supplied"] == pytest.approx(10)
    assert base["D2"]["supplied"] == pytest.approx(4)
    assert der["D2"]["supplied"] == pytest.approx(3)
    assert der["D3"]["supplied"] == pytest.approx(0)

    changes = {c["demand_id"]: c for c in data["demand_changes"]}
    assert changes["D1"]["affected"] is False
    assert changes["D2"]["delta"] == pytest.approx(-1)
    assert changes["D3"]["delta"] == pytest.approx(-2)
    assert {c["demand_id"] for c in data["affected_demands"]} == {"D2", "D3"}
    assert data["totals_change"]["delta_supplied"] == pytest.approx(-3)

    # 降额设施的容量使用情况：S2 以临时容量 3 计、已用满
    util = data["derated_utilization"]
    assert len(util) == 1
    u = util[0]
    assert u["kind"] == "source" and u["facility_id"] == "S2"
    assert u["rated_capacity"] == 6
    assert u["temporary_capacity"] == 3
    assert u["used"] == pytest.approx(3)
    assert u["remaining"] == 0
    assert u["saturated"] is True


def test_pipe_derating_impacts_supply(loaded):
    # 共享干管 P2 6 -> 2：D2 只得 2，D3 得 0
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "pipes": [{"facility_id": "P2", "capacity": 2}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    der = {a["demand_id"]: a for a in data["derated"]["allocations"]}
    assert der["D2"]["supplied"] == pytest.approx(2)
    assert der["D3"]["supplied"] == pytest.approx(0)
    util = data["derated_utilization"][0]
    assert util["kind"] == "pipe" and util["facility_id"] == "P2"
    assert util["rated_capacity"] == 6 and util["temporary_capacity"] == 2
    assert util["used"] == pytest.approx(2) and util["saturated"] is True
    assert util["from_node"] == "S2" and util["to_node"] == "J1"


def test_zero_capacity_pipe_cuts_branch(loaded):
    # P3 临时上限 0（合法的有限非负值）：D2 经 P3 完全无流量
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "pipes": [{"facility_id": "P3", "capacity": 0}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    der = {a["demand_id"]: a for a in data["derated"]["allocations"]}
    assert der["D2"]["supplied"] == 0
    # D3 独占 S2 的 6，足额
    assert der["D3"]["supplied"] == pytest.approx(6)


def test_stale_revision_rejected(loaded):
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev - 1,
        "sources": [{"facility_id": "S2", "capacity": 3}],
    })
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    d = err["details"][0]
    assert d["location"] == "revision"
    assert d["seen_revision"] == rev - 1 and d["current_revision"] == rev
    # 过期修订号不返回任何分配结果
    assert "baseline" not in r.json() and "derated" not in r.json()


def test_revision_advances_after_valve_change(loaded):
    # 旧快照修订号在阀位变更后过期
    client, rev = loaded
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V3", "action": "close"}]})
    try:
        r = client.post("/api/derating/preview", json={
            "revision": rev,
            "sources": [{"facility_id": "S2", "capacity": 3}],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "revision_conflict"
    finally:
        client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V3", "action": "open"}]})


def test_unknown_facility_located(loaded):
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [{"facility_id": "GHOST", "capacity": 1}],
    })
    assert r.status_code == 400
    d = r.json()["error"]["details"][0]
    assert d["location"] == "sources[0].facility_id"
    assert d["code"] == "unknown_facility"


def test_wrong_facility_type_located(loaded):
    client, rev = loaded
    # 管段编号放到 sources；水源编号放到 pipes
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [{"facility_id": "P1", "capacity": 1}],
        "pipes": [{"facility_id": "S1", "capacity": 1}],
    })
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    locs = {d["location"]: d["code"] for d in details}
    assert locs["sources[0].facility_id"] == "wrong_facility_type"
    assert locs["pipes[0].facility_id"] == "wrong_facility_type"


def test_duplicate_entries_located(loaded):
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [
            {"facility_id": "S2", "capacity": 3},
            {"facility_id": "S2", "capacity": 2},
        ],
    })
    assert r.status_code == 400
    d = r.json()["error"]["details"][0]
    assert d["location"] == "sources[1].facility_id"
    assert d["code"] == "duplicate_facility"


def test_capacity_above_rated_located(loaded):
    client, rev = loaded
    # 一次性收集多个错误：水源越额 + 管段越额
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [{"facility_id": "S2", "capacity": 6.5}],
        "pipes": [{"facility_id": "P3", "capacity": 4.1}],
    })
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "invalid_derating"
    details = {d["location"]: d for d in body["details"]}
    assert details["sources[0].capacity"]["code"] == "capacity_above_rated"
    assert details["sources[0].capacity"]["rated_capacity"] == 6
    assert details["pipes[0].capacity"]["code"] == "capacity_above_rated"
    assert "baseline" not in r.json()


def test_capacity_equal_to_rated_accepted(loaded):
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [{"facility_id": "S2", "capacity": 6}],
        "pipes": [{"facility_id": "P3", "capacity": 4}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    # 上限等于额定值：结果与现状一致
    assert data["derated"]["allocations"] == data["baseline"]["allocations"]
    assert all(not c["affected"] for c in data["demand_changes"])


def test_negative_and_nan_capacity_422(loaded):
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [{"facility_id": "S2", "capacity": -1}],
    })
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["location"].endswith("capacity")

    # NaN/Infinity 不是合法 JSON 数值：直接以原始请求体发送，由服务端 422 拒绝
    r = client.post(
        "/api/derating/preview",
        content=(
            '{"revision": %d, "pipes": [{"facility_id": "P3", "capacity": NaN}]}'
            % rev
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_missing_revision_422(loaded):
    client, _rev = loaded
    r = client.post("/api/derating/preview", json={
        "sources": [{"facility_id": "S2", "capacity": 3}],
    })
    assert r.status_code == 422


def test_preview_does_not_mutate_state(loaded):
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [{"facility_id": "S2", "capacity": 0}],
        "pipes": [{"facility_id": "P2", "capacity": 0}],
    })
    assert r.status_code == 200
    # 管网容量、阀位、修订号均不变；再查 /api/status 仍是现状结果
    net = client.get("/api/network").json()
    assert net["revision"] == rev
    s2 = next(s for s in net["network"]["sources"] if s["id"] == "S2")
    assert s2["capacity"] == 6
    p2 = next(p for p in net["network"]["pipes"] if p["id"] == "P2")
    assert p2["capacity"] == 6

    status = client.get("/api/status").json()
    alloc = {a["demand_id"]: a for a in status["allocations"]}
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)


def test_demand_node_rejected_as_facility(loaded):
    # 用水点既不是水源也不是管段：编号存在但类别不可降额
    client, rev = loaded
    r = client.post("/api/derating/preview", json={
        "revision": rev,
        "sources": [{"facility_id": "D1", "capacity": 1}],
    })
    assert r.status_code == 400
    d = r.json()["error"]["details"][0]
    assert d["code"] == "wrong_facility_type"
