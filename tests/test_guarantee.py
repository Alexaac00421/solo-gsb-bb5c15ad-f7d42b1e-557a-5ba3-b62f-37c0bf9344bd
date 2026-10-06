"""端到端测试：抢修最低保障供水预演 POST /api/guarantee/preview。

覆盖：
- 可行：保障量保持 + 按优先级/同级编号逐点最大化额外供水；
- “必要时重排先前流量”的字典序语义（区别于锁死路径的旧贪心）；
- 不可行：联合最大流判定、逐点保障缺口、瓶颈水源/管段/隔离设施（不出部分方案）；
- 校验：保障量超需求、未知/错类设施、重复/缺失条目（400），负数/NaN（422）；
- 过期修订号 409；预演只读（不改管网/阀位/修订号）。
使用独立临时 SQLite，不依赖其它测试模块的数据状态。
"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ.setdefault("WATER_DB_PATH", _tmp.name)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402
from app.engine import build_network, guarantee_preview  # noqa: E402

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


def _by_id(rows, key):
    return {r[key]: r for r in rows}


def _guarantee(rev, mapping):
    return {
        "revision": rev,
        "guarantees": [
            {"demand_id": did, "guarantee": amt}
            for did, amt in sorted(mapping.items())
        ],
    }


# --------------------------------------------------------------------------- #
# 引擎层：字典序最大化 + 重排语义
# --------------------------------------------------------------------------- #
def test_engine_feasible_keeps_guarantee_and_fills_by_priority():
    network, errs = build_network(valid_network())
    assert not errs
    r = guarantee_preview(network, {"D1": 0, "D2": 4, "D3": 2})
    assert r["feasible"] is True
    alloc = _by_id(r["allocations"], "demand_id")
    # 所有保障量都被保持
    assert alloc["D1"]["supplied"] >= 10 - 1e-9
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)
    # D1 最高优先级先最大化（10）；D2 同级先于 D3：D2=4（P3 限），D3=2（S2 剩余）
    assert alloc["D1"]["supplied"] == pytest.approx(10)
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)
    assert alloc["D2"]["extra_supplied"] == pytest.approx(0)
    assert alloc["D3"]["extra_supplied"] == pytest.approx(0)
    # 总量守恒，水源/管段不重复占用
    assert r["totals"]["supplied"] == pytest.approx(16)
    src = _by_id(r["source_utilization"], "source_id")
    assert src["S1"]["used"] == pytest.approx(10)
    assert src["S2"]["used"] == pytest.approx(6)
    pipe = _by_id(r["pipe_utilization"], "pipe_id")
    assert pipe["P2"]["used"] == pytest.approx(6)
    assert pipe["P3"]["used"] == pytest.approx(4)
    assert pipe["P6"]["used"] == pytest.approx(2)


def test_engine_reroutes_previous_flow_to_maximize_higher_priority():
    """重排语义：初始保障流可能占住高优先点的后续路径，最大化时必须能重排。

    DA（高优先级）有直连大管 P1(S1->DA,10) 与共享支路；
    DB（低优先级）只能经 S2->J->DB。保障各 5；最大化 DA 时应得 10，
    同时 DB 的保障 5 不被破坏（需要把初始占用从共享支路挪开）。
    """
    net = {
        "sources": [
            {"id": "S1", "capacity": 10},
            {"id": "S2", "capacity": 10},
        ],
        "demands": [
            {"id": "DA", "demand": 10, "priority": 9},
            {"id": "DB", "demand": 10, "priority": 1},
        ],
        "junctions": [{"id": "J"}],
        "pipes": [
            {"id": "P1", "from_node": "S1", "to_node": "DA", "capacity": 10},
            {"id": "P2", "from_node": "S1", "to_node": "J", "capacity": 5},
            {"id": "P5", "from_node": "S2", "to_node": "J", "capacity": 10},
            {"id": "P3", "from_node": "J", "to_node": "DA", "capacity": 5},
            {"id": "P4", "from_node": "J", "to_node": "DB", "capacity": 5},
        ],
        "valves": [],
        "forbidden_pairs": [],
    }
    network, errs = build_network(net)
    assert not errs
    r = guarantee_preview(network, {"DA": 5, "DB": 5})
    assert r["feasible"] is True
    alloc = _by_id(r["allocations"], "demand_id")
    assert alloc["DA"]["supplied"] == pytest.approx(10)
    assert alloc["DB"]["supplied"] == pytest.approx(5)  # 保障量必须保持
    assert alloc["DA"]["extra_supplied"] == pytest.approx(5)
    assert alloc["DB"]["extra_supplied"] == pytest.approx(0)
    # 容量不重复占用：总供水 15
    assert r["totals"]["supplied"] == pytest.approx(15)


def test_engine_guarantee_water_not_given_to_lower_priority_extra():
    """保障不影响低优先级抢占：无富余时低优先级只拿到保障量。"""
    network, errs = build_network(valid_network())
    assert not errs
    r = guarantee_preview(network, {"D1": 10, "D2": 4, "D3": 6})
    # S2 侧 D2+D3 保障共 10 > S2 能力 6 -> 不可行
    assert r["feasible"] is False


def test_engine_zero_guarantees_equals_status_allocation():
    network, errs = build_network(valid_network())
    assert not errs
    r = guarantee_preview(network, {"D1": 0, "D2": 0, "D3": 0})
    assert r["feasible"] is True
    alloc = _by_id(r["allocations"], "demand_id")
    assert alloc["D1"]["supplied"] == pytest.approx(10)
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)


def test_engine_infeasible_pipe_bottleneck_and_gap():
    network, errs = build_network(valid_network())
    assert not errs
    # D2 保障 5，但 P3 容量只有 4
    r = guarantee_preview(network, {"D1": 0, "D2": 5, "D3": 0})
    assert r["feasible"] is False
    alloc = _by_id(r["allocations"], "demand_id")
    assert alloc["D2"]["shortfall"] == pytest.approx(1)
    assert alloc["D2"]["deliverable"] == pytest.approx(4)
    assert alloc["D2"]["shortfall_reason_code"] == "PIPE_LIMIT"
    pids = {p["pipe_id"] for p in r["bottleneck_pipes"]}
    assert "P3" in pids
    assert r["totals"]["shortfall"] == pytest.approx(1)
    # 不可行不返回任何成功方案字段
    assert "source_utilization" not in r
    assert "pipe_utilization" not in r


def test_engine_infeasible_shared_pipe_bottleneck():
    network, errs = build_network(valid_network())
    assert not errs
    r = guarantee_preview(network, {"D1": 10, "D2": 8, "D3": 6})
    assert r["feasible"] is False
    assert r["totals"]["shortfall"] == pytest.approx(8)
    pids = {p["pipe_id"] for p in r["bottleneck_pipes"]}
    # S2 干管 P2(cap6) 是共享瓶颈；D2 另受 P3 限
    assert "P2" in pids
    alloc = _by_id(r["allocations"], "demand_id")
    assert alloc["D2"]["shortfall"] == pytest.approx(4)
    assert alloc["D3"]["shortfall"] == pytest.approx(4)


def test_engine_infeasible_source_bottleneck():
    net = {
        "sources": [{"id": "S1", "capacity": 3}],
        "demands": [{"id": "D1", "demand": 10, "priority": 5}],
        "junctions": [],
        "pipes": [{"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 100}],
        "valves": [{"id": "V1", "pipe_id": "P1", "is_open": True}],
        "forbidden_pairs": [],
    }
    network, errs = build_network(net)
    assert not errs
    r = guarantee_preview(network, {"D1": 5})
    assert r["feasible"] is False
    assert r["allocations"][0]["shortfall"] == pytest.approx(2)
    assert r["allocations"][0]["shortfall_reason_code"] == "SOURCE_LIMIT"
    assert {s["source_id"] for s in r["bottleneck_sources"]} == {"S1"}
    assert r["bottleneck_pipes"] == []


def test_engine_infeasible_isolated_demand():
    net = {
        "sources": [{"id": "S1", "capacity": 10}],
        "demands": [{"id": "D1", "demand": 10, "priority": 5}],
        "junctions": [],
        "pipes": [{"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 10}],
        "valves": [{"id": "V1", "pipe_id": "P1", "is_open": False}],
        "forbidden_pairs": [],
    }
    network, errs = build_network(net)
    assert not errs
    r = guarantee_preview(network, {"D1": 3})
    assert r["feasible"] is False
    a = r["allocations"][0]
    assert a["shortfall"] == pytest.approx(3)
    assert a["deliverable"] == pytest.approx(0)
    assert a["shortfall_reason_code"] == "ISOLATED"
    bpipe_ids = {p["pipe_id"] for p in r["bottleneck_pipes"]}
    assert "P1" in bpipe_ids
    cp = {p["pipe_id"]: p for p in r["bottleneck_pipes"]}["P1"]
    assert "V1" in cp["closed_valves"]


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
def test_requires_network_initialized(client):
    client.delete("/api/network")
    r = client.post("/api/guarantee/preview",
                    json={"revision": 0, "guarantees": []})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "network_not_initialized"
    client.put("/api/network", json=valid_network())


def test_preview_feasible(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview",
                    json=_guarantee(rev, {"D1": 0, "D2": 4, "D3": 2}))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True
    assert data["revision"] == rev
    assert len(data["guarantees"]) == 3
    alloc = _by_id(data["allocations"], "demand_id")
    assert alloc["D1"]["guarantee"] == 0.0
    assert alloc["D2"]["guarantee"] == 4.0
    assert alloc["D1"]["supplied"] == pytest.approx(10)
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)
    assert alloc["D1"]["guarantee_met"] is True
    assert "source_utilization" in data and "pipe_utilization" in data
    assert data["totals"]["guarantee"] == pytest.approx(6)


def test_preview_infeasible_no_partial_plan(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview",
                    json=_guarantee(rev, {"D1": 10, "D2": 8, "D3": 6}))
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is False
    assert data["totals"]["shortfall"] == pytest.approx(8)
    assert len(data["allocations"]) == 3
    assert data["bottleneck_pipes"]
    # 不可行不返回部分成功方案
    assert "source_utilization" not in data
    assert "pipe_utilization" not in data
    for a in data["allocations"]:
        assert "by_source" not in a


def test_guarantee_above_demand_located(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview",
                    json=_guarantee(rev, {"D1": 11, "D2": 0, "D3": 0}))
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "invalid_guarantee"
    d = body["details"][0]
    assert d["location"] == "guarantees[0].guarantee"
    assert d["code"] == "guarantee_above_demand"
    assert d["current_demand"] == pytest.approx(10)
    assert d["submitted_guarantee"] == pytest.approx(11)
    assert "allocations" not in r.json()


def test_guarantee_equal_demand_accepted(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview",
                    json=_guarantee(rev, {"D1": 10, "D2": 0, "D3": 0}))
    assert r.status_code == 200, r.text
    assert r.json()["feasible"] is True


def test_unknown_wrong_type_duplicate_missing(loaded):
    client, rev = loaded
    # 未知 + 错类（水源编号）+ 重复 D2；缺 D3
    body = {
        "revision": rev,
        "guarantees": [
            {"demand_id": "GHOST", "guarantee": 1},
            {"demand_id": "S1", "guarantee": 1},
            {"demand_id": "D1", "guarantee": 1},
            {"demand_id": "D2", "guarantee": 1},
            {"demand_id": "D2", "guarantee": 2},
        ],
    }
    r = client.post("/api/guarantee/preview", json=body)
    assert r.status_code == 400
    by = {}
    for d in r.json()["error"]["details"]:
        by.setdefault(d["location"], d["code"])
    assert by["guarantees[0].demand_id"] == "unknown_facility"
    assert by["guarantees[1].demand_id"] == "wrong_facility_type"
    assert by["guarantees[4].demand_id"] == "duplicate_facility"
    assert by["guarantees"] == "missing_entry"
    missing = [d for d in r.json()["error"]["details"]
               if d["code"] == "missing_entry"]
    assert {d["facility_id"] for d in missing} == {"D3"}


def test_empty_guarantees_missing_all(loaded):
    """清单为空但管网有用水点：全部缺失，400（不把空清单当合法）。"""
    client, rev = loaded
    r = client.post("/api/guarantee/preview",
                    json={"revision": rev, "guarantees": []})
    assert r.status_code == 400
    missing = [d for d in r.json()["error"]["details"]
               if d["code"] == "missing_entry"]
    assert {d["facility_id"] for d in missing} == {"D1", "D2", "D3"}


def test_negative_and_nan_422(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview",
                    json=_guarantee(rev, {"D1": -1, "D2": 0, "D3": 0}))
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["location"].endswith("guarantee")

    r = client.post(
        "/api/guarantee/preview",
        content=(
            '{"revision": %d, "guarantees": ['
            '{"demand_id": "D1", "guarantee": NaN}]}' % rev
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_extra_field_422(loaded):
    client, rev = loaded
    body = _guarantee(rev, {"D1": 0, "D2": 0, "D3": 0})
    body["bogus"] = 1
    r = client.post("/api/guarantee/preview", json=body)
    assert r.status_code == 422


def test_stale_revision_rejected(loaded):
    client, rev = loaded
    r = client.post("/api/guarantee/preview",
                    json=_guarantee(rev - 1, {"D1": 0, "D2": 0, "D3": 0}))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    d = err["details"][0]
    assert d["seen_revision"] == rev - 1
    assert d["current_revision"] == rev
    assert "allocations" not in r.json()


def test_revision_conflict_after_valve_change(loaded):
    client, rev = loaded
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V3", "action": "close"}]})
    try:
        r = client.post("/api/guarantee/preview",
                        json=_guarantee(rev, {"D1": 0, "D2": 0, "D3": 0}))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "revision_conflict"
    finally:
        client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V3", "action": "open"}]})


def test_preview_does_not_mutate_state(loaded):
    client, rev = loaded
    before_net = client.get("/api/network").json()
    before_status = client.get("/api/status").json()

    r = client.post("/api/guarantee/preview",
                    json=_guarantee(rev, {"D1": 0, "D2": 4, "D3": 2}))
    assert r.status_code == 200

    after_net = client.get("/api/network").json()
    after_status = client.get("/api/status").json()
    assert after_net["revision"] == rev
    assert after_net["network"] == before_net["network"]
    assert after_status["revision"] == rev
    assert after_status["allocations"] == before_status["allocations"]


def test_closed_valve_can_make_guarantee_infeasible(loaded):
    """当前阀位直接生效：关闭 V3 使 D2 保障不可行（ISOLATED）。"""
    client, rev = loaded
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V3", "action": "close"}]})
    try:
        rev2 = client.get("/api/status").json()["revision"]
        r = client.post("/api/guarantee/preview",
                        json=_guarantee(rev2, {"D1": 0, "D2": 3, "D3": 0}))
        assert r.status_code == 200
        data = r.json()
        assert data["feasible"] is False
        alloc = _by_id(data["allocations"], "demand_id")
        assert alloc["D2"]["shortfall_reason_code"] == "ISOLATED"
        assert alloc["D2"]["shortfall"] == pytest.approx(3)
    finally:
        client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V3", "action": "open"}]})
