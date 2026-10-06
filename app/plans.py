"""复水计划：逐步开关阀预演、不可变计划保存与带修订号的逐步执行。

业务规则：
1. 计划步骤必须非空、有序（数组顺序即操作顺序）且阀门编号不重复；
   未知阀门 / 非法动作要带 `steps[i].*` 定位报错。
2. 预演以**当前管网与当前阀位**为起点，内存中逐步叠加开/关动作，**不改实际状态**；
   每步按现有“无向开启管段”规则检查不可并联水源，首个冲突立即返回
   （步骤位置、连通的水源对与节点路径），且不保存计划。
3. 全部步骤安全才保存**不可变计划**，返回每步阀位与沿用现有口径的
   各用水点供水 / 欠供结果及基准修订号。
4. 执行只接受“计划的下一步 + 所见修订号”：同一事务内复核修订号与安全性、
   更新阀位与进度并把修订号 +1；重复提交已完成步骤原样回放；过期或跳步拒绝且不改状态。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from .engine import Network, Valve, find_forbidden_connections, simulate


def validate_steps(
    network: Network, steps: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """条目级校验：未知阀门、阀门编号重复。返回 (规范化步骤, 错误列表)。"""
    errors: list[dict[str, str]] = []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, step in enumerate(steps):
        loc = f"steps[{i}]"
        vid = step.get("valve_id")
        action = step.get("action")

        # 阀门编号检查（未知/空）；重复判定只依赖编号，与动作是否合法无关
        valid_valve = isinstance(vid, str) and vid in network.valves
        if not isinstance(vid, str) or not vid:
            errors.append({
                "location": f"{loc}.valve_id",
                "code": "invalid_valve",
                "message": "阀门编号必须是非空字符串",
            })
            continue
        if not valid_valve:
            errors.append({
                "location": f"{loc}.valve_id",
                "code": "unknown_valve",
                "message": f"非法/未知阀门编号：{vid}",
            })
            seen.add(vid)
            continue
        if vid in seen:
            errors.append({
                "location": f"{loc}.valve_id",
                "code": "duplicate_valve",
                "message": f"阀门 {vid} 在步骤清单中重复出现",
            })
            continue
        seen.add(vid)

        if action not in ("open", "close"):
            errors.append({
                "location": f"{loc}.action",
                "code": "invalid_action",
                "message": f"非法动作：{action!r}（只允许 open/close）",
            })
            continue

        normalized.append({
            "valve_id": vid,
            "action": action,
            "target_is_open": action == "open",
        })
    return normalized, errors


def _valve_snapshot(valves: dict[str, Valve]) -> list[dict[str, Any]]:
    """当前全部阀门状态（按编号排序），与既有接口口径一致。"""
    return [
        {"valve_id": v.id, "pipe_id": v.pipe_id, "is_open": v.is_open}
        for _, v in sorted(valves.items())
    ]


def step_snapshot(
    network_after: Network, step: dict[str, Any], position: int
) -> dict[str, Any]:
    """单步动作后的阀位与供水分派（沿用 /api/status 的口径）。"""
    result = simulate(network_after, force_closed=set())
    return {
        "position": position,
        "valve_id": step["valve_id"],
        "action": step["action"],
        "is_open": step["target_is_open"],
        "closed_valves": result["closed_valves"],
        "valves": _valve_snapshot(network_after.valves),
        "allocations": result["allocations"],
        "totals": result["totals"],
        "source_utilization": result["source_utilization"],
        "pipe_utilization": result["pipe_utilization"],
    }


def preview_plan(
    network: Network, normalized_steps: list[dict[str, Any]]
) -> tuple[Optional[list[dict[str, Any]]], Optional[dict[str, Any]]]:
    """从当前阀位出发逐步预演（不写状态）。

    返回 (steps_snapshot, None) 表示全部安全；
    或 (None, conflict)，conflict 为首个不可并联水源冲突的定位信息。
    """
    tentative_valves = dict(network.valves)
    snapshots: list[dict[str, Any]] = []

    for i, step in enumerate(normalized_steps):
        vid = step["valve_id"]
        target = step["target_is_open"]
        tentative_valves[vid] = replace(tentative_valves[vid], is_open=target)

        tentative = Network(
            network.nodes,
            network.pipes,
            tentative_valves,
            network.forbidden_pairs,
            network.valves_by_pipe,
        )

        # 每步按无向开启管段检查互锁水源，首个冲突立即返回
        violations = find_forbidden_connections(tentative)
        if violations:
            a, b, path = violations[0]
            conflict = {
                "location": f"steps[{i}]",
                "code": "forbidden_connected",
                "position": i + 1,
                "valve_id": vid,
                "action": step["action"],
                "source_a": a,
                "source_b": b,
                "path": path,
                "message": (
                    f"第 {i + 1} 步对阀门 {vid} 执行 {step['action']} 后，"
                    f"不可并联水源 {a} 与 {b} 将经开启管段连通："
                    + " -> ".join(path)
                ),
            }
            return None, conflict

        snapshots.append(step_snapshot(tentative, step, i + 1))

    return snapshots, None
