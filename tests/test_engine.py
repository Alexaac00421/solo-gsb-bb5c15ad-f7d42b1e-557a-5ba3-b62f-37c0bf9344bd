"""引擎级测试：Dinic 最大流与贪心分派数值正确性。"""
from __future__ import annotations

from app.engine import Dinic, build_network, simulate


def test_dinic_classic_network():
    # S->A(3), S->B(5), A->T(4), B->T(2), A->B(2)
    # 瓶颈在 B->T(2)：A->B 的流量无处可去，最大流 = 3+2 = 5
    d = Dinic(4)
    S, A, B, T = 0, 1, 2, 3
    d.add_edge(S, A, 3)
    d.add_edge(S, B, 5)
    d.add_edge(A, T, 4)
    d.add_edge(B, T, 2)
    d.add_edge(A, B, 2)
    assert abs(d.max_flow(S, T) - 5) < 1e-9


def test_dinic_reroute_through_residual():
    # 标准可增广网络：算法需走残余边重路由，最大流 5
    # S->A(3), S->B(2), A->B(2), A->T(1), B->T(3)
    d = Dinic(4)
    S, A, B, T = 0, 1, 2, 3
    d.add_edge(S, A, 3)
    d.add_edge(S, B, 2)
    d.add_edge(A, B, 2)
    d.add_edge(A, T, 1)
    d.add_edge(B, T, 3)
    assert abs(d.max_flow(S, T) - 4) < 1e-9


def test_dinic_two_sources_merge():
    # 两源汇流：S1->J(3), S2->J(5), J->T(6) => 最大流 6
    d = Dinic(5)
    S1, S2, J, T, SUP = 0, 1, 2, 3, 4
    d.add_edge(SUP, S1, 3)
    d.add_edge(SUP, S2, 5)
    d.add_edge(S1, J, 3)
    d.add_edge(S2, J, 5)
    d.add_edge(J, T, 6)
    assert abs(d.max_flow(SUP, T) - 6) < 1e-9


def test_priority_allocation_shared_pipe():
    """两个高/低优先级点共享一根 cap=5 的管段，先到先占，不重复计算。"""
    data = {
        "sources": [{"id": "S", "capacity": 100}],
        "demands": [
            {"id": "HI", "demand": 8, "priority": 10},
            {"id": "LO", "demand": 8, "priority": 1},
        ],
        "junctions": [{"id": "J"}],
        "pipes": [
            {"id": "P0", "from_node": "S", "to_node": "J", "capacity": 100},
            {"id": "P1", "from_node": "J", "to_node": "HI", "capacity": 5},
            {"id": "P2", "from_node": "J", "to_node": "LO", "capacity": 5},
        ],
        "valves": [],
        "forbidden_pairs": [],
    }
    network, errs = build_network(data)
    assert errs == []
    result = simulate(network)
    a = {x["demand_id"]: x for x in result["allocations"]}
    assert a["HI"]["supplied"] == 5.0
    assert a["HI"]["shortfall_reason_code"] == "PIPE_LIMIT"
    assert a["LO"]["supplied"] == 5.0
    # 两个点各走各的支管；总管 P0 用了 10，未超过 100
    pu = {p["pipe_id"]: p for p in result["pipe_utilization"]}
    assert pu["P0"]["used"] == 10.0
    assert pu["P1"]["used"] == 5.0
    assert pu["P2"]["used"] == 5.0


def test_shared_trunk_consumed_once():
    """干管 cap=6，两个点共需 10：高优先级点先占 6，低优先级点得 0。"""
    data = {
        "sources": [{"id": "S", "capacity": 100}],
        "demands": [
            {"id": "HI", "demand": 6, "priority": 10},
            {"id": "LO", "demand": 4, "priority": 1},
        ],
        "junctions": [{"id": "J"}],
        "pipes": [
            {"id": "TRUNK", "from_node": "S", "to_node": "J", "capacity": 6},
            {"id": "P1", "from_node": "J", "to_node": "HI", "capacity": 100},
            {"id": "P2", "from_node": "J", "to_node": "LO", "capacity": 100},
        ],
        "valves": [],
        "forbidden_pairs": [],
    }
    network, errs = build_network(data)
    assert errs == []
    result = simulate(network)
    a = {x["demand_id"]: x for x in result["allocations"]}
    assert a["HI"]["supplied"] == 6.0
    assert a["HI"]["fulfilled"] is True
    assert a["LO"]["supplied"] == 0.0
    assert a["LO"]["fulfilled"] is False
    pu = {p["pipe_id"]: p for p in result["pipe_utilization"]}
    assert pu["TRUNK"]["used"] == 6.0
    assert pu["TRUNK"]["remaining"] == 0.0


def test_two_sources_both_binding():
    """水源与管段同时打满：SOURCE_AND_PIPE_LIMIT。"""
    data = {
        "sources": [{"id": "S", "capacity": 5}],
        "demands": [{"id": "D", "demand": 9, "priority": 1}],
        "junctions": [],
        "pipes": [{"id": "P", "from_node": "S", "to_node": "D", "capacity": 5}],
        "valves": [],
        "forbidden_pairs": [],
    }
    network, errs = build_network(data)
    assert errs == []
    result = simulate(network)
    a = result["allocations"][0]
    assert a["supplied"] == 5.0
    assert a["shortfall"] == 4.0
    # 水源 cap 与管段 cap 相等同时打满
    assert a["shortfall_reason_code"] == "SOURCE_AND_PIPE_LIMIT"
    assert a["constrained_pipes"][0]["pipe_id"] == "P"
