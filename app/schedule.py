"""多时段有限储水供水调度预演（只读，不写入）。

业务规则：
1. 请求携带当前管网**修订号**、各水源的**初始储水**与**库容**，以及**非空有序时段**；
   每期必须**恰好**列出所有水源的补水量与所有用水点的需求量（不重复、不缺失、不多余）。
2. 初始储水不得超过库容；每期**先补水至库容**，超出部分计入**溢出**；
   再按现有阀位、单向管段、共享容量与用水点优先级分配（engine.simulate 同一口径）。
3. 水源本期出水上限 = min(额定上限, 可用储水)；实际出水扣减储水；
   **欠供不结转**——下一期需求以请求为准，与往期欠供无关。
4. 未知/错类设施、重复/缺失条目、初始储水超库容一次性带定位 400 拒绝；
   负数/非有限数/空时段由 Pydantic 层 422 拒绝；过期修订号 409 拒绝。
   任何失败都**不返回混合版本结果**；预演只读：不改变管网、阀位、复水计划或修订号。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from .engine import EPS, Network, simulate

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


def _check_coverage(
    network: Network,
    items: list[dict[str, Any]],
    id_field: str,
    loc_name: str,
    want_kind: str,
    errors: list[dict[str, Any]],
) -> None:
    """校验一组条目恰好覆盖某类设施：不重复、不缺失，未知/错类逐条定位。"""
    label = _TYPE_NAMES[want_kind]
    seen: set[str] = set()
    for j, item in enumerate(items):
        loc = f"{loc_name}[{j}].{id_field}"
        fid = item[id_field]
        if fid in seen:
            errors.append({
                "location": loc,
                "code": "duplicate_facility",
                "facility_id": fid,
                "message": f"条目重复：{fid} 在 {loc_name} 中出现多次",
            })
            continue
        seen.add(fid)
        kind = _facility_kind(network, fid)
        if kind is None:
            errors.append({
                "location": loc,
                "code": "unknown_facility",
                "facility_id": fid,
                "message": f"未知设施：{fid}（当前管网中不存在该{label}）",
            })
            continue
        if kind != want_kind:
            errors.append({
                "location": loc,
                "code": "wrong_facility_type",
                "facility_id": fid,
                "message": (
                    f"设施类别错误：{fid} 是{_KIND_NAMES[kind]}，"
                    f"不能作为{label}条目（请核对编号）"
                ),
            })
            continue
    want_ids = (
        network.source_ids() if want_kind == "source" else network.demand_ids()
    )
    for fid in want_ids:
        if fid not in seen:
            errors.append({
                "location": loc_name,
                "code": "missing_entry",
                "facility_id": fid,
                "message": (
                    f"缺失条目：{loc_name} 必须恰好列出所有{label}，缺少 {fid}"
                ),
            })


def validate_schedule(
    network: Network,
    sources: list[dict[str, Any]],
    periods: list[dict[str, Any]],
) -> tuple[dict[str, tuple[float, float]], list[dict[str, Any]]]:
    """基于已提交快照逐条校验调度预演参数。

    返回 (init, errors)：init 为 水源id -> (初始储水, 库容)；
    errors 非空时映射不可用于预演（调用方应整单拒绝）。
    """
    errors: list[dict[str, Any]] = []

    # ---- 各水源初始储水与库容：必须恰好覆盖所有水源 ----
    init: dict[str, tuple[float, float]] = {}
    seen: set[str] = set()
    for i, item in enumerate(sources):
        loc = f"sources[{i}]"
        sid = item["source_id"]
        if sid in seen:
            errors.append({
                "location": f"{loc}.source_id",
                "code": "duplicate_facility",
                "facility_id": sid,
                "message": f"条目重复：{sid} 在 sources 中出现多次",
            })
            continue
        seen.add(sid)
        kind = _facility_kind(network, sid)
        if kind is None:
            errors.append({
                "location": f"{loc}.source_id",
                "code": "unknown_facility",
                "facility_id": sid,
                "message": f"未知设施：{sid}（当前管网中不存在该水源）",
            })
            continue
        if kind != "source":
            errors.append({
                "location": f"{loc}.source_id",
                "code": "wrong_facility_type",
                "facility_id": sid,
                "message": (
                    f"设施类别错误：{sid} 是{_KIND_NAMES[kind]}，"
                    "不能作为水源条目（请核对编号）"
                ),
            })
            continue
        storage0 = float(item["initial_storage"])
        cap = float(item["storage_capacity"])
        # 初始储水不得超过库容（1e-9 容差，吸收浮点序列化误差）
        if storage0 > cap + EPS:
            errors.append({
                "location": f"{loc}.initial_storage",
                "code": "storage_exceeds_capacity",
                "facility_id": sid,
                "initial_storage": storage0,
                "storage_capacity": cap,
                "message": (
                    f"水源 {sid} 的初始储水 {storage0:g} 超过库容 {cap:g}；"
                    "初始储水不得超过库容"
                ),
            })
            continue
        init[sid] = (storage0, cap)
    for sid in network.source_ids():
        if sid not in seen:
            errors.append({
                "location": "sources",
                "code": "missing_entry",
                "facility_id": sid,
                "message": (
                    "缺失条目：sources 必须恰好列出所有水源的初始储水与库容，"
                    f"缺少 {sid}"
                ),
            })

    # ---- 每期：补水恰好覆盖所有水源、需求恰好覆盖所有用水点 ----
    for p, period in enumerate(periods):
        _check_coverage(
            network, period["replenishment"], "source_id",
            f"periods[{p}].replenishment", "source", errors,
        )
        _check_coverage(
            network, period["demands"], "demand_id",
            f"periods[{p}].demands", "demand", errors,
        )

    return init, errors


def preview(
    network: Network,
    revision: int,
    sources: list[dict[str, Any]],
    periods: list[dict[str, Any]],
) -> dict[str, Any]:
    """逐期滚动预演：补水至库容（超出计溢出）-> 分配 -> 出水扣减储水。

    调用方须先完成修订号校验与 validate_schedule 条目校验。
    """
    init, _errors = validate_schedule(network, sources, periods)

    source_ids = network.source_ids()
    rated = {sid: network.nodes[sid].capacity or 0.0 for sid in source_ids}
    storage_cap = {sid: cap for sid, (_s0, cap) in init.items()}
    storage = {sid: s0 for sid, (s0, _cap) in init.items()}

    period_results: list[dict[str, Any]] = []
    tot_requested = 0.0
    tot_supplied = 0.0
    tot_shortfall = 0.0
    tot_overflow = 0.0

    for p, period in enumerate(periods):
        replenishment = {
            it["source_id"]: float(it["amount"]) for it in period["replenishment"]
        }
        want = {it["demand_id"]: float(it["amount"]) for it in period["demands"]}

        # 1) 先补水至库容，超出部分计入溢出
        start_storage = dict(storage)
        overflow: dict[str, float] = {}
        for sid in source_ids:
            inflow = storage[sid] + replenishment[sid]
            cap = storage_cap[sid]
            if inflow > cap + EPS:
                overflow[sid] = round(inflow - cap, 9)
                storage[sid] = cap
            else:
                overflow[sid] = 0.0
                storage[sid] = inflow

        # 2) 本期可供上限 = min(额定上限, 可用储水)；需求替换为本期值（欠供不结转）。
        #    在内存副本上叠加，不写库、不改阀位、不改修订号。
        nodes = dict(network.nodes)
        for sid in source_ids:
            nodes[sid] = replace(
                nodes[sid], capacity=min(rated[sid], storage[sid])
            )
        for did, amount in want.items():
            nodes[did] = replace(nodes[did], demand=amount)
        period_network = Network(
            nodes=nodes,
            pipes=network.pipes,
            valves=network.valves,
            forbidden_pairs=network.forbidden_pairs,
            valves_by_pipe=network.valves_by_pipe,
        )
        result = simulate(period_network, force_closed=set())

        # 3) 实际出水扣减储水（不出现在 by_source 中的水源出水为 0）
        used = {s["source_id"]: s["used"] for s in result["source_utilization"]}
        output_rows: list[dict[str, Any]] = []
        storage_rows: list[dict[str, Any]] = []
        for sid in source_ids:
            out = round(used.get(sid, 0.0), 9)
            available = storage[sid]
            end = round(max(0.0, available - out), 9)
            output_rows.append({
                "source_id": sid,
                "rated_capacity": rated[sid],
                "storage_available": round(available, 9),
                "output_limit": round(min(rated[sid], available), 9),
                "output": out,
            })
            storage_rows.append({
                "source_id": sid,
                "start": round(start_storage[sid], 9),
                "replenishment": round(replenishment[sid], 9),
                "capacity": storage_cap[sid],
                "overflow": overflow[sid],
                "output": out,
                "end": end,
            })
            storage[sid] = end

        period_overflow = round(sum(overflow.values()), 9)
        period_results.append({
            "period": p + 1,
            "allocations": result["allocations"],
            "totals": result["totals"],
            "source_outputs": output_rows,
            "storage": storage_rows,
            "overflow": period_overflow,
        })
        tot_requested += result["totals"]["requested"]
        tot_supplied += result["totals"]["supplied"]
        tot_shortfall += result["totals"]["shortfall"]
        tot_overflow += period_overflow

    return {
        "message": "调度预演完成：逐期结果为临时计算，"
        "未改变管网、阀位、计划或修订号",
        "revision": revision,
        "sources": [
            {
                "source_id": sid,
                "rated_capacity": rated[sid],
                "initial_storage": init[sid][0],
                "storage_capacity": storage_cap[sid],
            }
            for sid in source_ids
        ],
        "periods": period_results,
        "totals": {
            "periods": len(period_results),
            "requested": round(tot_requested, 9),
            "supplied": round(tot_supplied, 9),
            "shortfall": round(tot_shortfall, 9),
            "overflow": round(tot_overflow, 9),
        },
    }
