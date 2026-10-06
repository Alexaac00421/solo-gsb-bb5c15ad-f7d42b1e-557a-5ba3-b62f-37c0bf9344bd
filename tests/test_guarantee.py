"""端到端测试：抢修最低保障供水预演 POST /api/guarantee/preview。

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


# 需要“重排先前流量”才能同时满足全部保障的管网：
#   S1(cap6) --P1(cap6)--> J --P3(cap6)--> D1(需求6, 优先级9)
#   S2(cap6) --P2(cap6)--> J
#   S1       --P4(cap6)--> D2(需求6, 优先级1)
# D1 高优先级先取流时会占满 S1（经 P1），D2 的唯一来源 P4 也在 S1 上；
# 只有把 D1 的流重排到 S2（经 P2），D2 才能经 P4 获水。
def reroute_network():
    return {
        "sources": [
            {"id": "S1", "capacity": 6},
            {"id": "S2", "capacity": 6},
        ],
        "demands": [
            {"id": "D1", "demand": 6, "priority": 9},
            {"id": "D2", "demand": 6, "priority": 1},
        ],
        "junctions": [{"id": "J"}],
        "pipes": [
            {"id": "P1", "from_node": "S1", "to_node": "J", "capacity": 6},
            {"id": "P2", "from_node": "S2", "to_node": "J", "capacity": 6},
            {"id": "P3", "from_node": "J", "to_node": "D1", "capacity": 6},
            {"id": "P4", "from_node": "S1", "to_node": "D2", "capacity": 6},
        ],
        "valves": [],
        "forbidden_pairs": [],
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


def guarantees(**kwargs):
    return [{"demand_id": did, "amount": amt} for did, amt in kwargs.items()]


def _by_id(rows, key="demand_id"):
    return {r[key]: r for r in rows}


def test_requires_network_initialized(client):
    client.delete("/api/network")
    r = client.post("/api/guarantee/preview", json={"revision": 0, "guarantees": []})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "network_not_initialized"
    # 重新录入供后续用例
    client.put("/api/network", json=valid_network())


def test_feasible_guarantees_then_extra_by_priority(loaded):
    """保障可行：先保全部保障量，再按优先级逐点最大化额外供水。"""
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=4, D2=2, D3=1),
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True
    assert data["revision"] == rev

    alloc = _by_id(data["allocations"])
    # D1 优先级最高：保障 4 + 额外 6 = 需求 10，足额
    assert alloc["D1"]["guaranteed"] == pytest.approx(4)
    assert alloc["D1"]["supplied"] == pytest.approx(10)
    assert alloc["D1"]["extra"] == pytest.approx(6)
    assert alloc["D1"]["fulfilled"] is True
    assert alloc["D1"]["by_source"] == {"S1": 10.0}
    # D2：保障 2，额外受共享管段 P3(cap4) 限制，总供水 4
    assert alloc["D2"]["guaranteed"] == pytest.approx(2)
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D2"]["extra"] == pytest.approx(2)
    assert alloc["D2"]["shortfall"] == pytest.approx(4)
    # D3：保障 1，S2 剩余 2（6 - D2 占 4），总供水 2
    assert alloc["D3"]["guaranteed"] == pytest.approx(1)
    assert alloc["D3"]["supplied"] == pytest.approx(2)
    assert alloc["D3"]["shortfall"] == pytest.approx(4)

    totals = data["totals"]
    assert totals["requested"] == pytest.approx(24)
    assert totals["guaranteed"] == pytest.approx(7)
    assert totals["supplied"] == pytest.approx(16)
    assert totals["extra"] == pytest.approx(9)
    assert totals["shortfall"] == pytest.approx(8)

    src = _by_id(data["source_utilization"], "source_id")
    assert src["S1"]["used"] == pytest.approx(10)
    assert src["S2"]["used"] == pytest.approx(6)
    pipes = _by_id(data["pipe_utilization"], "pipe_id")
    assert pipes["P3"]["used"] == pytest.approx(4)
    assert pipes["P2"]["used"] == pytest.approx(6)
    assert pipes["P5"]["conducting"] is False
    assert pipes["P5"]["used"] == pytest.approx(0)


def test_zero_guarantees_match_status_allocation(loaded):
    """全部保障量为 0：等价于现状分派（同 /api/status 口径）。"""
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=0, D2=0, D3=0),
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True
    status = client.get("/api/status").json()
    got = {a["demand_id"]: a["supplied"] for a in data["allocations"]}
    want = {a["demand_id"]: a["supplied"] for a in status["allocations"]}
    assert got == want


def test_guarantee_equal_demand_leaves_no_extra(loaded):
    """保障量 = 需求：全部足额，无额外可求。"""
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=10, D2=4, D3=2),
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True
    alloc = _by_id(data["allocations"])
    assert alloc["D1"]["supplied"] == pytest.approx(10)
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)
    assert all(a["extra"] == pytest.approx(0) for a in alloc.values())
    assert data["totals"]["supplied"] == pytest.approx(16)


def test_infeasible_returns_shortfalls_and_bottlenecks(loaded):
    """保障总量超过可供能力：返回缺口与瓶颈设施，不返回部分分配方案。"""
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=10, D2=8, D3=6),
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is False
    # 不返回部分成功方案
    assert "allocations" not in data
    assert "source_utilization" not in data
    assert "pipe_utilization" not in data

    sf = _by_id(data["guarantee_shortfalls"])
    # D1 可足额保障（S1->P1 独享 10）；D2 保障 8 但 P3 仅 4；D3 保障 6 但 S2 仅剩 2
    assert sf["D1"]["shortfall"] == pytest.approx(0)
    assert sf["D2"]["guaranteed"] == pytest.approx(8)
    assert sf["D2"]["suppliable"] == pytest.approx(4)
    assert sf["D2"]["shortfall"] == pytest.approx(4)
    assert sf["D3"]["suppliable"] == pytest.approx(2)
    assert sf["D3"]["shortfall"] == pytest.approx(4)
    assert data["totals"]["guaranteed"] == pytest.approx(24)
    assert data["totals"]["suppliable"] == pytest.approx(16)
    assert data["totals"]["shortfall"] == pytest.approx(8)

    # 瓶颈：最小割上打满的水源 S1、S2（二者上限均已占满）
    bn = {b["facility_id"]: b for b in data["bottleneck_facilities"]}
    assert bn["S1"]["kind"] == "source"
    assert bn["S1"]["used"] == pytest.approx(10)
    assert bn["S2"]["used"] == pytest.approx(6)


def test_infeasible_pipe_bottleneck_located(client):
    """水源充足、共享干管不足：瓶颈定位为管段。"""
    net = valid_network()
    net["sources"][1]["capacity"] = 20  # S2 水源充足
    r = client.put("/api/network", json=net)
    assert r.status_code == 200, r.text
    rev = r.json()["revision"]
    try:
        r = client.post("/api/guarantee/preview", json={
            "revision": rev,
            "guarantees": guarantees(D1=10, D2=8, D3=6),
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["feasible"] is False
        sf = _by_id(data["guarantee_shortfalls"])
        # P2(cap6) 是 D2/D3 的唯一来水：D2 得 4（P3 限），D3 得 2
        assert sf["D2"]["suppliable"] == pytest.approx(4)
        assert sf["D3"]["suppliable"] == pytest.approx(2)
        bn = {b["facility_id"]: b for b in data["bottleneck_facilities"]}
        assert "P2" in bn
        assert bn["P2"]["kind"] == "pipe"
        assert bn["P2"]["used"] == pytest.approx(6)
        assert bn["P2"]["from_node"] == "S2"
        assert bn["P2"]["to_node"] == "J1"
    finally:
        client.put("/api/network", json=valid_network())


def test_reroute_previous_flow_for_guarantee(client):
    """必要时重排先前流量：D1 先占 S1，D2 的保障迫使 D1 改由 S2 供水。"""
    r = client.put("/api/network", json=reroute_network())
    assert r.status_code == 200, r.text
    rev = r.json()["revision"]
    try:
        r = client.post("/api/guarantee/preview", json={
            "revision": rev,
            "guarantees": guarantees(D1=6, D2=6),
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["feasible"] is True
        alloc = _by_id(data["allocations"])
        assert alloc["D1"]["supplied"] == pytest.approx(6)
        assert alloc["D2"]["supplied"] == pytest.approx(6)
        # 两水源均被占满：D1 的 6 与 D2 的 6 合计 12 = S1(6) + S2(6)
        src = _by_id(data["source_utilization"], "source_id")
        assert src["S1"]["used"] == pytest.approx(6)
        assert src["S2"]["used"] == pytest.approx(6)
        by_all = alloc["D1"]["by_source"]
        by_all.update(alloc["D2"]["by_source"])
        assert sum(by_all.values()) == pytest.approx(12)
    finally:
        client.put("/api/network", json=valid_network())


def test_closed_valve_makes_guarantee_infeasible(loaded):
    """现有阀位直接作用于判定：V3 关闭后 D2 的保障不可达。"""
    client, rev = loaded
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V3", "action": "close"}]})
    try:
        rev = client.get("/api/status").json()["revision"]
        r = client.post("/api/guarantee/preview", json={
            "revision": rev,
            "guarantees": guarantees(D1=4, D2=1, D3=1),
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["feasible"] is False
        sf = _by_id(data["guarantee_shortfalls"])
        assert sf["D2"]["suppliable"] == pytest.approx(0)
        assert sf["D2"]["shortfall"] == pytest.approx(1)
    finally:
        client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V3", "action": "open"}]})


def test_guarantee_exceeds_demand_located(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=10.5, D2=8, D3=6),
    })
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "invalid_guarantee"
    d = body["details"][0]
    assert d["location"] == "guarantees[0].amount"
    assert d["code"] == "guarantee_exceeds_demand"
    assert d["demand"] == pytest.approx(10)
    assert d["submitted_guarantee"] == pytest.approx(10.5)
    assert "allocations" not in r.json()


def test_unknown_and_wrong_type_located(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": [
            {"demand_id": "GHOST", "amount": 1},
            {"demand_id": "S1", "amount": 1},
            {"demand_id": "P1", "amount": 1},
            {"demand_id": "D1", "amount": 1},
            {"demand_id": "D2", "amount": 1},
            {"demand_id": "D3", "amount": 1},
        ],
    })
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    locs = {d["location"]: d["code"] for d in details}
    assert locs["guarantees[0].demand_id"] == "unknown_facility"
    assert locs["guarantees[1].demand_id"] == "wrong_facility_type"
    assert locs["guarantees[2].demand_id"] == "wrong_facility_type"


def test_duplicate_and_missing_entries_located(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": [
            {"demand_id": "D1", "amount": 1},
            {"demand_id": "D2", "amount": 1},
            {"demand_id": "D2", "amount": 2},
        ],
    })
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    locs = {d["location"]: d["code"] for d in details}
    assert locs["guarantees[2].demand_id"] == "duplicate_facility"
    assert locs["guarantees"] == "missing_entry"
    missing = [d for d in details if d["code"] == "missing_entry"]
    assert {d["facility_id"] for d in missing} == {"D3"}
    assert "allocations" not in r.json()


def test_empty_guarantees_rejected_when_demands_exist(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev, "guarantees": [],
    })
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    assert all(d["code"] == "missing_entry" for d in details)
    assert {d["facility_id"] for d in details} == {"D1", "D2", "D3"}


def test_negative_and_nan_422(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=-1, D2=0, D3=0),
    })
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["location"].endswith("amount")

    # NaN/Infinity 不是合法 JSON 数值：直接以原始请求体发送，由服务端 422 拒绝
    r = client.post(
        "/api/guarantee/preview",
        content=(
            '{"revision": %d, "guarantees": ['
            '{"demand_id": "D1", "amount": NaN},'
            '{"demand_id": "D2", "amount": 0},'
            '{"demand_id": "D3", "amount": 0}]}' % rev
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_extra_field_422(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=0, D2=0, D3=0),
        "bogus": 1,
    })
    assert r.status_code == 422


def test_missing_revision_422(loaded):
    client, _rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "guarantees": guarantees(D1=0, D2=0, D3=0),
    })
    assert r.status_code == 422


def test_stale_revision_rejected(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview", json={
        "revision": rev - 1,
        "guarantees": guarantees(D1=0, D2=0, D3=0),
    })
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    d = err["details"][0]
    assert d["location"] == "revision"
    assert d["seen_revision"] == rev - 1
    assert d["current_revision"] == rev
    # 过期修订号不返回任何分配结果
    assert "allocations" not in r.json() and "feasible" not in r.json()


def test_revision_advances_after_valve_change(loaded):
    client, rev = loaded
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V3", "action": "close"}]})
    try:
        r = client.post("/api/guarantee/preview", json={
            "revision": rev,
            "guarantees": guarantees(D1=0, D2=0, D3=0),
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "revision_conflict"
    finally:
        client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V3", "action": "open"}]})


def test_preview_does_not_mutate_state(loaded):
    client, rev = loaded
    before_net = client.get("/api/network").json()
    before_status = client.get("/api/status").json()

    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=10, D2=8, D3=6),  # 不可行
    })
    assert r.status_code == 200
    assert r.json()["feasible"] is False
    r = client.post("/api/guarantee/preview", json={
        "revision": rev,
        "guarantees": guarantees(D1=4, D2=2, D3=1),  # 可行
    })
    assert r.status_code == 200
    assert r.json()["feasible"] is True

    after_net = client.get("/api/network").json()
    after_status = client.get("/api/status").json()
    # 修订号、管网、阀位、分派结果均不变
    assert after_net["revision"] == rev
    assert after_net["network"] == before_net["network"]
    assert after_status["allocations"] == before_status["allocations"]
    assert after_status["revision"] == rev
