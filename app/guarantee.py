"""抢修最低保障供水预演（只读，不写入）。

业务规则：
1. 请求携带当前管网**修订号**与**恰好覆盖所有用水点**的保障量清单；
   保障量为有限非负数（Pydantic 层保证），同一用水点不得重复，
   且不得超过该点**现有需求**。
2. **保障可行性判定**：在现有阀位、单向管段、水源上限与共享管段容量下，
   把每个用水点的保障量作为必须同时满足的最低供水，用最大流判定能否
   **同时**满足全部保障量。不可行时返回各点保障缺口与瓶颈设施
   （最小割上打满的水源/管段），**不返回任何部分分配方案**。
3. **额外供水最大化**：可行时在保持全部保障量的前提下，按现有优先级
   （数值越大越优先）及同级编号升序逐点把供水提升至需求——在残余网络上
   继续增广，允许经管段反向边**重排先前流量的路径**；汇边（用水点→超级汇）
   的反向边不在分层增广路径上，任何点的既有供水只增不减，
   单点总供水不超过其需求。
4. 未知/错类设施、重复/缺失条目、保障量超需求一次性带定位 400 拒绝；
   负数/非有限数由 Pydantic 层 422 拒绝；过期修订号 409 拒绝。
   任何失败都**不返回混合版本结果**；预演只读：不改变管网、阀位、
   复水计划或修订号。
"""
from __future__ import annotations

from typing import Any, Optional

from .engine import EPS, Dinic, Network

_TYPE_NAMES = {"source": "水源", "demand": "用水点", "junction": "连接点"}
_KIND_NAMES = {**_TYPE_NAMES, "pipe": "管段", "valve": "阀门"}


def _facility_kind(network: Network, fid: str) -> Optional[str]:
    """设施类别：source/demand/junction/pipe/valve；不存在返回 None。"""
    node = network.nodes.get(fid)
    if node is not None:
        return node.type
    if fid in network.pipes:
        return "pipe"
    if fid in network.valves:
        return "valve"
    return None


def validate_guarantees(
    network: Network, items: list[dict[str, Any]]
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """基于已提交快照逐条校验保障量条目。

    返回 (guarantees, errors)：guarantees 为 用水点id -> 保障量；
    errors 非空时映射不可用于预演（调用方应整单拒绝）。
    """
    errors: list[dict[str, Any]] = []
    guarantees: dict[str, float] = {}
    seen: set[str] = set()
    for i, item in enumerate(items):
        loc = f"guarantees[{i}]"
        did = item["demand_id"]
        amount = float(item["amount"])

        if did in seen:
            errors.append({
                "location": f"{loc}.demand_id",
                "code": "duplicate_facility",
                "facility_id": did,
                "message": f"条目重复：{did} 在 guarantees 中出现多次",
            })
            continue
        seen.add(did)

        kind = _facility_kind(network, did)
        if kind is None:
            errors.append({
                "location": f"{loc}.demand_id",
                "code": "unknown_facility",
                "facility_id": did,
                "message": f"未知设施：{did}（当前管网中不存在该用水点）",
            })
            continue
        if kind != "demand":
            errors.append({
                "location": f"{loc}.demand_id",
                "code": "wrong_facility_type",
                "facility_id": did,
                "message": (
                    f"设施类别错误：{did} 是{_KIND_NAMES[kind]}，"
                    "不能作为用水点条目（请核对编号）"
                ),
            })
            continue

        need = network.nodes[did].demand or 0.0
        # 保障量不得超过该点现有需求（1e-9 容差，吸收浮点序列化误差）
        if amount > need + EPS:
            errors.append({
                "location": f"{loc}.amount",
                "code": "guarantee_exceeds_demand",
                "facility_id": did,
                "demand": need,
                "submitted_guarantee": amount,
                "message": (
                    f"用水点 {did} 的保障量 {amount:g} 超过其现有需求 "
                    f"{need:g}；保障量不得大于需求"
                ),
            })
            continue
        guarantees[did] = amount

    for did in network.demand_ids():
        if did not in seen:
            errors.append({
                "location": "guarantees",
                "code": "missing_entry",
                "facility_id": did,
                "message": (
                    "缺失条目：guarantees 必须恰好列出所有用水点的保障量，"
                    f"缺少 {did}"
                ),
            })
    return guarantees, errors


def preview(
    network: Network,
    revision: int,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """两阶段保障预演：先判定全部保障量能否同时满足，再逐点最大化额外供水。

    调用方须先完成修订号校验与 validate_guarantees 条目校验。
    """
    guarantees, _errors = validate_guarantees(network, items)

    # 处理顺序：优先级降序，同级按编号升序（与 engine.simulate 同口径）
    demands = sorted(
        (network.nodes[did] for did in network.demand_ids()),
        key=lambda n: (-(n.priority or 0), n.id),
    )

    # 流网络：超级源 -> 水源（上限）-> 单向导通管段（容量）-> 用水点 -> 超级汇。
    # 汇边初始容量 0：保障量与额外供水都通过“提额 + 残余增广”逐点汇入，
    # 使每点的 by_source 可按增广归属精确记账（源边用量只增不减）。
    index = {nid: i for i, nid in enumerate(network.nodes)}
    super_source = len(index)
    super_sink = super_source + 1
    dinic = Dinic(super_sink + 1)

    source_edges: dict[str, Any] = {}
    for sid in network.source_ids():
        cap = network.nodes[sid].capacity or 0.0
        if cap > EPS:
            e_idx = len(dinic.g[super_source])
            dinic.add_edge(
                super_source, index[sid], cap, {"kind": "source", "id": sid}
            )
            source_edges[sid] = dinic.g[super_source][e_idx]
    pipe_edges: dict[str, Any] = {}
    for pid, pipe in network.pipes.items():
        if network.pipe_is_open(pipe) and pipe.capacity > EPS:
            u = index[pipe.from_node]
            e_idx = len(dinic.g[u])
            dinic.add_edge(
                u, index[pipe.to_node], pipe.capacity, {"kind": "pipe", "id": pid}
            )
            pipe_edges[pid] = dinic.g[u][e_idx]
    demand_edges: dict[str, Any] = {}
    demand_cap: dict[str, float] = {}
    for node in demands:
        u = index[node.id]
        e_idx = len(dinic.g[u])
        dinic.add_edge(u, super_sink, 0.0, {"kind": "demand", "id": node.id})
        demand_edges[node.id] = dinic.g[u][e_idx]
        demand_cap[node.id] = 0.0

    by_source: dict[str, dict[str, float]] = {n.id: {} for n in demands}

    def augment_to(node: Any, target: float) -> float:
        """把该点汇边容量提升到 target 并在残余图上继续增广。

        返回本次新增流量。管段反向边允许重排先前流量的路径；
        汇边反向边从超级汇出发、不在分层增广路径上，
        因此任何点的既有供水只增不减（保障量必被保持）。
        """
        edge = demand_edges[node.id]
        add = target - demand_cap[node.id]
        if add > EPS:
            edge.cap += add
            demand_cap[node.id] = target
        before = {sid: e.cap for sid, e in source_edges.items()}
        gained = dinic.max_flow(super_source, super_sink)
        if gained > EPS:
            for sid, e in source_edges.items():
                delta = before[sid] - e.cap
                if delta > EPS:
                    acc = by_source[node.id]
                    acc[sid] = round(acc.get(sid, 0.0) + delta, 9)
        return gained

    def flow_of(did: str) -> float:
        return demand_cap[did] - demand_edges[did].cap

    # ---- 阶段一：逐点汇入保障量，判定全部保障量能否同时满足 ----
    for node in demands:
        augment_to(node, guarantees[node.id])

    shortfalls: list[dict[str, Any]] = []
    total_guaranteed = 0.0
    total_suppliable = 0.0
    for node in demands:
        g = guarantees[node.id]
        attained = flow_of(node.id)
        total_guaranteed += g
        total_suppliable += attained
        shortfalls.append({
            "demand_id": node.id,
            "priority": node.priority or 0,
            "guaranteed": round(g, 9),
            "suppliable": round(attained, 9),
            "shortfall": round(max(0.0, g - attained), 9),
        })

    if any(s["shortfall"] > EPS for s in shortfalls):
        # 不可行：最小割上打满的水源/管段即瓶颈设施；不返回任何部分分配方案
        reachable = dinic.reachable_from_source(super_source)
        bottlenecks: list[dict[str, Any]] = []
        for sid in network.source_ids():
            e = source_edges.get(sid)
            if e is None:
                continue  # 上限为 0 的水源不在流网络中，不属于“打满”的割边
            if index[sid] not in reachable and e.cap <= EPS:
                cap = network.nodes[sid].capacity or 0.0
                bottlenecks.append({
                    "kind": "source",
                    "facility_id": sid,
                    "capacity": cap,
                    "used": round(cap - e.cap, 9),
                    "remaining": round(e.cap, 9),
                })
        for pid, pipe in sorted(network.pipes.items()):
            e = pipe_edges.get(pid)
            if e is None:
                continue  # 不导通或容量为 0 的管段不在流网络中
            if (
                index[pipe.from_node] in reachable
                and index[pipe.to_node] not in reachable
                and e.cap <= EPS
            ):
                bottlenecks.append({
                    "kind": "pipe",
                    "facility_id": pid,
                    "from_node": pipe.from_node,
                    "to_node": pipe.to_node,
                    "capacity": pipe.capacity,
                    "used": round(pipe.capacity - e.cap, 9),
                    "remaining": round(e.cap, 9),
                    "closed_valves": sorted(
                        vid
                        for vid in network.valves_by_pipe.get(pid, ())
                        if not network.valves[vid].is_open
                    ),
                })
        return {
            "message": "保障预演完成：当前阀位与容量下全部保障量无法同时满足，"
            "未给出任何部分分配方案；本次结果为临时计算，"
            "未改变管网、阀位、计划或修订号",
            "revision": revision,
            "feasible": False,
            "guarantee_shortfalls": shortfalls,
            "totals": {
                "guaranteed": round(total_guaranteed, 9),
                "suppliable": round(total_suppliable, 9),
                "shortfall": round(
                    max(0.0, total_guaranteed - total_suppliable), 9
                ),
            },
            "bottleneck_facilities": bottlenecks,
        }

    # ---- 阶段二：保持全部保障量，按优先级逐点最大化额外供水 ----
    for node in demands:
        augment_to(node, node.demand or 0.0)

    allocations: list[dict[str, Any]] = []
    for node in demands:
        need = node.demand or 0.0
        g = guarantees[node.id]
        supplied = round(flow_of(node.id), 9)
        allocations.append({
            "demand_id": node.id,
            "priority": node.priority or 0,
            "requested": need,
            "guaranteed": round(g, 9),
            "supplied": supplied,
            "extra": round(max(0.0, supplied - g), 9),
            "shortfall": round(max(0.0, need - supplied), 9),
            "fulfilled": supplied + EPS >= need,
            "by_source": dict(by_source[node.id]),
        })

    source_utilization = []
    for sid in network.source_ids():
        cap = network.nodes[sid].capacity or 0.0
        e = source_edges.get(sid)
        remaining = e.cap if e is not None else 0.0
        source_utilization.append({
            "source_id": sid,
            "capacity": cap,
            "used": round(cap - remaining, 9),
            "remaining": round(remaining, 9),
        })
    pipe_utilization = []
    for pid, pipe in sorted(network.pipes.items()):
        conducting = network.pipe_is_open(pipe)
        e = pipe_edges.get(pid)
        if e is not None:
            remaining = e.cap
        else:
            # 不在流网络中：不导通管段容量未被占用；导通但容量为 0 则剩余 0
            remaining = pipe.capacity if not conducting else 0.0
        pipe_utilization.append({
            "pipe_id": pid,
            "capacity": pipe.capacity,
            "used": round(pipe.capacity - remaining, 9),
            "remaining": round(remaining, 9),
            "conducting": conducting,
        })

    return {
        "message": "保障预演完成：全部保障量可同时满足，"
        "额外供水已按优先级逐点最大化；本次结果为临时计算，"
        "未改变管网、阀位、计划或修订号",
        "revision": revision,
        "feasible": True,
        "allocations": allocations,
        "totals": {
            "requested": round(sum(a["requested"] for a in allocations), 9),
            "guaranteed": round(sum(a["guaranteed"] for a in allocations), 9),
            "supplied": round(sum(a["supplied"] for a in allocations), 9),
            "extra": round(sum(a["extra"] for a in allocations), 9),
            "shortfall": round(sum(a["shortfall"] for a in allocations), 9),
        },
        "source_utilization": source_utilization,
        "pipe_utilization": pipe_utilization,
    }
