"""抢修最低保障供水预演：条目校验 + 引擎调用（只读，不写入）。

业务规则：
1. 请求携带当前管网**修订号**与每个用水点的**有限非负保障量**；
   条目必须**恰好**覆盖所有用水点（不重复、不缺失、不多余），
   且每个保障量**不得超过该点现有需求**。
2. 未知/错类设施（把水源、管段、连接点编号放进 guarantees）、
   重复/缺失条目、保障量超需求一次性带 `guarantees[i].*` 定位 400 拒绝；
   负数/非有限数由 Pydantic 层 422 拒绝；过期修订号 409 拒绝。
   任何失败都**整单拒绝、不返回部分方案、不返回混合版本结果**。
3. 校验通过后调用 engine.guarantee_preview：
   - 先在当前阀位、单向管段、水源上限与共享管段容量下做**联合最大流**，
     判定全部保障量能否同时满足；不可行时只返回保障缺口与瓶颈设施；
   - 可行时在保持所有保障量的前提下，按优先级降序、同级编号升序
     逐点最大化额外供水（允许重排先前流量路径），单点总供水不超过需求。
4. 预演只读：不改变管网、阀位、复水计划或修订号。
"""
from __future__ import annotations

from typing import Any

from .engine import EPS, Network, guarantee_preview
from .schedule import _facility_kind, _KIND_NAMES


def validate_guarantees(
    network: Network, items: list[dict[str, Any]]
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """基于已提交快照逐条校验保障量条目。

    返回 (guarantee_map, errors)：guarantee_map 为 用水点id -> 保障量；
    errors 非空时映射不可用于预演（调用方应整单拒绝）。
    """
    errors: list[dict[str, Any]] = []
    guarantees: dict[str, float] = {}
    seen: set[str] = set()

    for i, item in enumerate(items):
        loc = f"guarantees[{i}]"
        did = item["demand_id"]
        amount = float(item["guarantee"])

        # 同一用水点不得重复
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
                    "不能作为用水点保障条目（请核对编号）"
                ),
            })
            continue

        demand = network.nodes[did].demand or 0.0
        # 保障量不得超过现有需求（1e-9 容差，吸收浮点序列化误差）
        if amount > demand + EPS:
            errors.append({
                "location": f"{loc}.guarantee",
                "code": "guarantee_above_demand",
                "facility_id": did,
                "current_demand": demand,
                "submitted_guarantee": amount,
                "message": (
                    f"用水点 {did} 的保障量 {amount:g} 超过其现有需求 "
                    f"{demand:g}；保障量不得超过现有需求"
                ),
            })
            continue

        guarantees[did] = amount

    # 缺失条目：必须恰好列出所有用水点
    for want_id in network.demand_ids():
        if want_id not in seen:
            errors.append({
                "location": "guarantees",
                "code": "missing_entry",
                "facility_id": want_id,
                "message": (
                    "缺失条目：guarantees 必须恰好列出所有用水点的保障量，"
                    f"缺少 {want_id}"
                ),
            })

    return guarantees, errors


def preview(
    network: Network,
    revision: int,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """校验通过后执行保障预演并回填修订号。

    调用方须先完成修订号校验与 validate_guarantees 条目校验。
    """
    guarantees, _errors = validate_guarantees(network, items)
    result = guarantee_preview(network, guarantees)
    result["revision"] = revision
    return result
