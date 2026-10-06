"""端到端测试：多时段有限储水调度预演 POST /api/schedule/preview。

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


def base_request(rev):
    """两期合法请求：期1 补水（S2 超库容溢出 1），期2 不补水、吃储水。"""
    return {
        "revision": rev,
        "sources": [
            {"source_id": "S1", "initial_storage": 8, "storage_capacity": 20},
            {"source_id": "S2", "initial_storage": 5, "storage_capacity": 10},
        ],
        "periods": [
            {
                "replenishment": [
                    {"source_id": "S1", "amount": 5},
                    {"source_id": "S2", "amount": 6},
                ],
                "demands": [
                    {"demand_id": "D1", "amount": 10},
                    {"demand_id": "D2", "amount": 8},
                    {"demand_id": "D3", "amount": 6},
                ],
            },
            {
                "replenishment": [
                    {"source_id": "S1", "amount": 0},
                    {"source_id": "S2", "amount": 0},
                ],
                "demands": [
                    {"demand_id": "D1", "amount": 10},
                    {"demand_id": "D2", "amount": 8},
                    {"demand_id": "D3", "amount": 6},
                ],
            },
        ],
    }


def _by_id(rows, key):
    return {r[key]: r for r in rows}


def test_requires_network_initialized(client):
    client.delete("/api/network")
    r = client.post("/api/schedule/preview", json={
        "revision": 0,
        "periods": [{"replenishment": [], "demands": []}],
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "network_not_initialized"
    # 重新录入供后续用例
    client.put("/api/network", json=valid_network())


def test_two_period_rollover(loaded):
    client, rev = loaded
    r = client.post("/api/schedule/preview", json=base_request(rev))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["revision"] == rev
    assert len(data["periods"]) == 2

    # ---- 期1：S1 补水后 13（额定限 10）；S2 5+6=11 超库容 10，溢出 1 ----
    p1 = data["periods"][0]
    assert p1["period"] == 1
    alloc = _by_id(p1["allocations"], "demand_id")
    assert alloc["D1"]["requested"] == pytest.approx(10)
    assert alloc["D1"]["supplied"] == pytest.approx(10)
    assert alloc["D1"]["fulfilled"] is True
    assert alloc["D2"]["supplied"] == pytest.approx(4)   # 共享管段 P3 限
    assert alloc["D2"]["shortfall"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)   # S2 剩余 2
    assert p1["totals"] == {"requested": 24.0, "supplied": 16.0, "shortfall": 8.0}

    outs = _by_id(p1["source_outputs"], "source_id")
    # S1 储水 13 充足，受额定上限 10 约束
    assert outs["S1"]["rated_capacity"] == pytest.approx(10)
    assert outs["S1"]["storage_available"] == pytest.approx(13)
    assert outs["S1"]["output_limit"] == pytest.approx(10)
    assert outs["S1"]["output"] == pytest.approx(10)
    # S2 补水后满库 10，受额定上限 6 约束
    assert outs["S2"]["output_limit"] == pytest.approx(6)
    assert outs["S2"]["output"] == pytest.approx(6)

    stor = _by_id(p1["storage"], "source_id")
    assert stor["S1"] == {
        "source_id": "S1", "start": 8.0, "replenishment": 5.0,
        "capacity": 20.0, "overflow": 0.0, "output": 10.0, "end": 3.0,
    }
    assert stor["S2"] == {
        "source_id": "S2", "start": 5.0, "replenishment": 6.0,
        "capacity": 10.0, "overflow": 1.0, "output": 6.0, "end": 4.0,
    }
    assert p1["overflow"] == pytest.approx(1)

    # ---- 期2：不补水，吃储水；S1 仅剩 3、S2 仅剩 4（储水约束生效） ----
    p2 = data["periods"][1]
    assert p2["period"] == 2
    alloc = _by_id(p2["allocations"], "demand_id")
    assert alloc["D1"]["supplied"] == pytest.approx(3)   # S1 储水耗尽
    assert alloc["D1"]["shortfall"] == pytest.approx(7)
    assert alloc["D2"]["supplied"] == pytest.approx(4)   # S2 储水 4
    assert alloc["D3"]["supplied"] == pytest.approx(0)
    assert p2["totals"] == {"requested": 24.0, "supplied": 7.0, "shortfall": 17.0}

    outs = _by_id(p2["source_outputs"], "source_id")
    assert outs["S1"]["storage_available"] == pytest.approx(3)
    assert outs["S1"]["output_limit"] == pytest.approx(3)  # min(额定10, 储水3)
    assert outs["S1"]["output"] == pytest.approx(3)
    assert outs["S2"]["output_limit"] == pytest.approx(4)
    assert outs["S2"]["output"] == pytest.approx(4)

    stor = _by_id(p2["storage"], "source_id")
    assert stor["S1"]["start"] == pytest.approx(3)
    assert stor["S1"]["end"] == pytest.approx(0)
    assert stor["S2"]["start"] == pytest.approx(4)
    assert stor["S2"]["end"] == pytest.approx(0)

    # ---- 全程汇总 ----
    totals = data["totals"]
    assert totals["periods"] == 2
    assert totals["requested"] == pytest.approx(48)
    assert totals["supplied"] == pytest.approx(23)
    assert totals["shortfall"] == pytest.approx(25)
    assert totals["overflow"] == pytest.approx(1)


def test_shortfall_not_carried_over(loaded):
    """欠供不结转：期1 欠供 8，期2 需求仍为请求值 24（而非 24+8）。"""
    client, rev = loaded
    r = client.post("/api/schedule/preview", json=base_request(rev))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["periods"][0]["totals"]["shortfall"] == pytest.approx(8)
    assert data["periods"][1]["totals"]["requested"] == pytest.approx(24)


def test_zero_demand_period_keeps_storage(loaded):
    """某期全部需求为 0：无出水，储水只受补水影响。"""
    client, rev = loaded
    req = base_request(rev)
    req["periods"] = [{
        "replenishment": [
            {"source_id": "S1", "amount": 1},
            {"source_id": "S2", "amount": 1},
        ],
        "demands": [
            {"demand_id": "D1", "amount": 0},
            {"demand_id": "D2", "amount": 0},
            {"demand_id": "D3", "amount": 0},
        ],
    }]
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 200, r.text
    p = r.json()["periods"][0]
    assert p["totals"] == {"requested": 0.0, "supplied": 0.0, "shortfall": 0.0}
    assert all(a["fulfilled"] for a in p["allocations"])
    stor = _by_id(p["storage"], "source_id")
    assert stor["S1"]["end"] == pytest.approx(9)
    assert stor["S2"]["end"] == pytest.approx(6)
    assert all(o["output"] == 0 for o in p["source_outputs"])


def test_initial_storage_equal_capacity_accepted(loaded):
    client, rev = loaded
    req = base_request(rev)
    req["sources"][0] = {
        "source_id": "S1", "initial_storage": 20, "storage_capacity": 20,
    }
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 200, r.text


def test_storage_exceeds_capacity_located(loaded):
    client, rev = loaded
    req = base_request(rev)
    req["sources"][0] = {
        "source_id": "S1", "initial_storage": 21, "storage_capacity": 20,
    }
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "invalid_schedule"
    d = body["details"][0]
    assert d["location"] == "sources[0].initial_storage"
    assert d["code"] == "storage_exceeds_capacity"
    assert d["initial_storage"] == pytest.approx(21)
    assert d["storage_capacity"] == pytest.approx(20)
    assert "periods" not in r.json()


def test_unknown_and_wrong_type_located(loaded):
    client, rev = loaded
    req = base_request(rev)
    # sources：未知编号 + 错类（用水点）；期1 补水错类（管段）；期1 需求错类（水源）
    req["sources"] = [
        {"source_id": "GHOST", "initial_storage": 1, "storage_capacity": 5},
        {"source_id": "D1", "initial_storage": 1, "storage_capacity": 5},
        {"source_id": "S1", "initial_storage": 1, "storage_capacity": 5},
        {"source_id": "S2", "initial_storage": 1, "storage_capacity": 5},
    ]
    req["periods"][0]["replenishment"][0] = {"source_id": "P1", "amount": 1}
    req["periods"][0]["demands"][0] = {"demand_id": "S1", "amount": 1}
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    locs = {d["location"]: d["code"] for d in details}
    assert locs["sources[0].source_id"] == "unknown_facility"
    assert locs["sources[1].source_id"] == "wrong_facility_type"
    assert locs["periods[0].replenishment[0].source_id"] == "wrong_facility_type"
    assert locs["periods[0].demands[0].demand_id"] == "wrong_facility_type"


def test_duplicate_and_missing_entries_located(loaded):
    client, rev = loaded
    req = base_request(rev)
    # sources 缺 S2；期1 补水 S2 重复；期1 需求缺 D3；期2 需求 D2 重复
    req["sources"] = req["sources"][:1]
    req["periods"][0]["replenishment"].append({"source_id": "S2", "amount": 1})
    req["periods"][0]["demands"] = req["periods"][0]["demands"][:2]
    req["periods"][1]["demands"].append({"demand_id": "D2", "amount": 1})
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    by_loc = {}
    for d in details:
        by_loc.setdefault(d["location"], d["code"])
    assert by_loc["sources"] == "missing_entry"
    assert by_loc["periods[0].replenishment[2].source_id"] == "duplicate_facility"
    assert by_loc["periods[0].demands"] == "missing_entry"
    assert by_loc["periods[1].demands[3].demand_id"] == "duplicate_facility"
    # 缺失定位带具体设施编号
    missing = [d for d in details if d["code"] == "missing_entry"]
    assert {d["facility_id"] for d in missing} == {"S2", "D3"}


def test_missing_whole_period_lists_located(loaded):
    """某期整列缺失（字段缺省为空数组）：所有水源/用水点报缺失。"""
    client, rev = loaded
    req = base_request(rev)
    req["periods"][1] = {}  # replenishment/demands 均缺省
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    missing = [d for d in details if d["code"] == "missing_entry"]
    assert {d["location"] for d in missing} == {
        "periods[1].replenishment", "periods[1].demands",
    }
    assert {d["facility_id"] for d in missing} == {"S1", "S2", "D1", "D2", "D3"}


def test_negative_and_nan_422(loaded):
    client, rev = loaded
    req = base_request(rev)
    req["sources"][0]["initial_storage"] = -1
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["location"].endswith("initial_storage")

    req = base_request(rev)
    req["periods"][0]["demands"][0]["amount"] = -0.5
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 422

    # NaN/Infinity 不是合法 JSON 数值：直接以原始请求体发送，由服务端 422 拒绝
    r = client.post(
        "/api/schedule/preview",
        content=(
            '{"revision": %d, "sources": [], "periods": ['
            '{"replenishment": [{"source_id": "S1", "amount": NaN}], '
            '"demands": []}]}' % rev
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_empty_periods_422(loaded):
    client, rev = loaded
    req = base_request(rev)
    req["periods"] = []
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_extra_field_422(loaded):
    client, rev = loaded
    req = base_request(rev)
    req["bogus"] = 1
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 422


def test_stale_revision_rejected(loaded):
    client, rev = loaded
    req = base_request(rev)
    req["revision"] = rev - 1
    r = client.post("/api/schedule/preview", json=req)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    d = err["details"][0]
    assert d["location"] == "revision"
    assert d["seen_revision"] == rev - 1
    assert d["current_revision"] == rev
    # 过期修订号不返回任何分配结果
    assert "periods" not in r.json() and "totals" not in r.json()


def test_revision_advances_after_valve_change(loaded):
    client, rev = loaded
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V3", "action": "close"}]})
    try:
        req = base_request(rev)
        r = client.post("/api/schedule/preview", json=req)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "revision_conflict"
    finally:
        client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V3", "action": "open"}]})


def test_preview_does_not_mutate_state(loaded):
    client, rev = loaded
    before_net = client.get("/api/network").json()
    before_status = client.get("/api/status").json()

    r = client.post("/api/schedule/preview", json=base_request(rev))
    assert r.status_code == 200, r.text

    after_net = client.get("/api/network").json()
    after_status = client.get("/api/status").json()
    # 修订号、管网、阀位、分派结果均不变
    assert after_net["revision"] == rev
    assert after_net["network"] == before_net["network"]
    assert after_status["allocations"] == before_status["allocations"]
    assert after_status["revision"] == rev


def test_closed_valve_isolation_carries_into_schedule(loaded):
    """现有阀位直接作用于每期分配：V3 关闭时 D2 每期都被隔离。"""
    client, rev = loaded
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V3", "action": "close"}]})
    try:
        rev = client.get("/api/status").json()["revision"]
        req = base_request(rev)
        r = client.post("/api/schedule/preview", json=req)
        assert r.status_code == 200, r.text
        for p in r.json()["periods"]:
            alloc = _by_id(p["allocations"], "demand_id")
            assert alloc["D2"]["supplied"] == 0
            assert alloc["D2"]["shortfall_reason_code"] == "ISOLATED"
    finally:
        client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V3", "action": "open"}]})
