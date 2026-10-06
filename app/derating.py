"""抢修降额影响预演：以同一已提交管网快照计算现状与降额两种情景。

业务规则：
1. 操作者携带当前管网**修订号**与若干水源、管段的**临时容量上限**。
   同一设施在同一类别中不得重复；上限必须是有限非负数（Pydantic 层保证），
   且不得超过该设施在“所提交修订号快照”中的当前额定容量。
2. 未知设施、错误类别（把管段当水源/水源当管段/阀门等不可降额设施）、
   重复条目、越额上限一次性带 `sources[i].*` / `pipes[i].*` 定位拒绝；
   修订号过期单独以 409 revision_conflict 拒绝。任何失败都**不返回混合版本结果**。
3. 现状情景与降额情景由**同一快照**分别计算，沿用现有阀位、单向流量、
   用水点优先级与共享容量规则（engine.simulate，不叠加任何拟关阀门）。
4. 降额只作用于本次预演：在内存中替换水源/管段容量，**不改变**管网、阀位、
   计划或修订号。无降额项时降额情景与现状完全一致。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from .engine import EPS, Network, simulate


def validate_derating(
    network: Network, sources: list[dict[str, Any]], pipes: list[dict[str, Any]]
) -> tuple[dict[str, float], dict[str, float], list[dict[str, str]]]:
    """基于已提交快照逐条校验降额条目。

    返回 (source_caps, pipe_caps, errors)：前两者为 设施id -> 临时容量；
    errors 非空时容量映射不可用于预演（调用方应整单拒绝）。
    """
    errors: list[dict[str, str]] = []
    source_caps: dict[str, float] = {}
    pipe_caps: dict[str, float] = {}

    def check(
        items: list[dict[str, Any]],
        loc_name: str,
        want_kind: str,
        caps: dict[str, float],
    ) -> None:
        type_names = {"source": "水源", "demand": "用水点", "junction": "连接点"}
        seen: set[str] = set()
        for i, item in enumerate(items):
            loc = f"{loc_name}[{i}]"
            fid = item["facility_id"]
            cap = float(item["capacity"])

            # 同一设施（同一类别内）不得重复
            if fid in seen:
                errors.append({
                    "location": f"{loc}.facility_id",
                    "code": "duplicate_facility",
                    "facility_id": fid,
                    "message": f"设施 {fid} 在 {loc_name} 中重复出现",
                })
                continue
            seen.add(fid)

            node = network.nodes.get(fid)
            pipe = network.pipes.get(fid)

            # 编号在管网中完全不存在（水源、管段、阀门均无此编号）
            if node is None and pipe is None:
                errors.append({
                    "location": f"{loc}.facility_id",
                    "code": "unknown_facility",
                    "facility_id": fid,
                    "message": f"未知设施：{fid}（当前管网中不存在该水源或管段）",
                })
                continue

            if want_kind == "source":
                # 类别错误：编号存在但不是水源（管段编号、用水点、连接点）
                if node is None or node.type != "source":
                    actual = "管段" if pipe is not None else type_names.get(node.type, node.type)
                    errors.append({
                        "location": f"{loc}.facility_id",
                        "code": "wrong_facility_type",
                        "facility_id": fid,
                        "message": (
                            f"设施类别错误：{fid} 是{actual}，"
                            "不能作为水源降额（请改放到 pipes 或核对编号）"
                        ),
                    })
                    continue
                rated = node.capacity or 0.0
            else:
                # 类别错误：编号存在但不是管段（水源、用水点、连接点）
                if pipe is None:
                    errors.append({
                        "location": f"{loc}.facility_id",
                        "code": "wrong_facility_type",
                        "facility_id": fid,
                        "message": (
                            f"设施类别错误：{fid} 是{type_names.get(node.type, node.type)}，"
                            "不能作为管段降额（请改放到 sources 或核对编号）"
                        ),
                    })
                    continue
                rated = pipe.capacity

            # 临时上限不得超过当前额定容量（1e-9 容差，吸收浮点序列化误差）
            if cap > rated + EPS:
                errors.append({
                    "location": f"{loc}.capacity",
                    "code": "capacity_above_rated",
                    "facility_id": fid,
                    "rated_capacity": rated,
                    "submitted_capacity": cap,
                    "message": (
                        f"设施 {fid} 的临时容量上限 {cap:g} 超过当前额定容量 "
                        f"{rated:g}；降额上限不得大于额定容量"
                    ),
                })
                continue

            caps[fid] = cap

    check(sources, "sources", "source", source_caps)
    check(pipes, "pipes", "pipe", pipe_caps)
    return source_caps, pipe_caps, errors


def apply_derating(
    network: Network,
    source_caps: dict[str, float],
    pipe_caps: dict[str, float],
) -> Network:
    """在内存副本上覆盖水源/管段容量（不写库、不改阀位、不改修订号）。"""
    nodes = dict(network.nodes)
    for sid, cap in source_caps.items():
        old = nodes[sid]
        nodes[sid] = replace(old, capacity=float(cap))
    pipes = dict(network.pipes)
    for pid, cap in pipe_caps.items():
        old = pipes[pid]
        pipes[pid] = replace(old, capacity=float(cap))
    return Network(
        nodes=nodes,
        pipes=pipes,
        valves=network.valves,
        forbidden_pairs=network.forbidden_pairs,
        valves_by_pipe=network.valves_by_pipe,
    )


def _alloc_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["demand_id"]: a for a in result["allocations"]}


def _demand_changes(
    baseline: dict[str, Any], derated: dict[str, Any]
) -> list[dict[str, Any]]:
    """逐用水点对比现状与降额情景的获水变化（按现状分配顺序输出）。"""
    derated_map = _alloc_map(derated)
    changes: list[dict[str, Any]] = []
    for cur in baseline["allocations"]:
        did = cur["demand_id"]
        new = derated_map[did]
        delta = round(new["supplied"] - cur["supplied"], 9)
        changes.append({
            "demand_id": did,
            "priority": cur["priority"],
            "requested": cur["requested"],
            "supplied_before": cur["supplied"],
            "supplied_after": new["supplied"],
            "delta": delta,
            "affected": abs(delta) > EPS,
            "shortfall_before": cur["shortfall"],
            "shortfall_after": new["shortfall"],
            "fulfilled_before": cur["fulfilled"],
            "fulfilled_after": new["fulfilled"],
            "reason_before": cur["shortfall_reason_code"],
            "reason_after": new["shortfall_reason_code"],
        })
    return changes


def _derated_utilization(
    network: Network,
    derated: dict[str, Any],
    source_caps: dict[str, float],
    pipe_caps: dict[str, float],
) -> list[dict[str, Any]]:
    """降额设施在降额情景中的容量使用情况（按提交顺序：先水源后管段）。"""
    src_used = {s["source_id"]: s for s in derated["source_utilization"]}
    pipe_used = {p["pipe_id"]: p for p in derated["pipe_utilization"]}
    out: list[dict[str, Any]] = []
    for sid, cap in source_caps.items():
        rated = network.nodes[sid].capacity or 0.0
        used = src_used[sid]["used"]
        out.append({
            "kind": "source",
            "facility_id": sid,
            "rated_capacity": rated,
            "temporary_capacity": round(float(cap), 9),
            "used": used,
            "remaining": round(max(0.0, cap - used), 9),
            "saturated": cap <= EPS or used + EPS >= cap,
        })
    for pid, cap in pipe_caps.items():
        pipe = network.pipes[pid]
        row = pipe_used[pid]
        out.append({
            "kind": "pipe",
            "facility_id": pid,
            "rated_capacity": pipe.capacity,
            "temporary_capacity": round(float(cap), 9),
            "used": row["used"],
            "remaining": round(max(0.0, cap - row["used"]), 9),
            "saturated": cap <= EPS or row["used"] + EPS >= cap,
            "conducting": row["conducting"],
            "from_node": pipe.from_node,
            "to_node": pipe.to_node,
        })
    return out


def _facility_echo(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"facility_id": it["facility_id"], "capacity": float(it["capacity"])}
            for it in items]


def preview(
    network: Network,
    revision: int,
    sources: list[dict[str, Any]],
    pipes: list[dict[str, Any]],
) -> dict[str, Any]:
    """以同一快照计算现状与降额两种情景并生成对比结果。

    调用方须先完成修订号校验与 validate_derating 条目校验。
    """
    source_caps, pipe_caps, _errors = validate_derating(network, sources, pipes)

    # 两种情景均由同一内存快照出发；阀位/单向/优先级/共享容量规则完全沿用
    baseline = simulate(network, force_closed=set())

    derated_network = apply_derating(network, source_caps, pipe_caps)
    derated = simulate(derated_network, force_closed=set())

    changes = _demand_changes(baseline, derated)
    affected = [c for c in changes if c["affected"]]
    utilization = _derated_utilization(
        network, derated, source_caps, pipe_caps
    )

    no_derating = not source_caps and not pipe_caps
    message = (
        "降额预演完成：临时容量仅作用于本次计算，"
        "未改变管网、阀位、计划或修订号；本次未提交降额项，降额情景与现状一致"
        if no_derating else
        "降额预演完成：临时容量仅作用于本次计算，"
        "未改变管网、阀位、计划或修订号"
    )

    return {
        "message": message,
        "revision": revision,
        "derating": {
            "sources": _facility_echo(sources),
            "pipes": _facility_echo(pipes),
            "no_derating": no_derating,
        },
        "baseline": baseline,
        "derated": derated,
        "demand_changes": changes,
        "affected_demands": affected,
        "derated_utilization": utilization,
        "totals_change": {
            "supplied_before": baseline["totals"]["supplied"],
            "supplied_after": derated["totals"]["supplied"],
            "delta_supplied": round(
                derated["totals"]["supplied"] - baseline["totals"]["supplied"], 9
            ),
            "shortfall_before": baseline["totals"]["shortfall"],
            "shortfall_after": derated["totals"]["shortfall"],
            "delta_shortfall": round(
                derated["totals"]["shortfall"] - baseline["totals"]["shortfall"], 9
            ),
        },
    }
