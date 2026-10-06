"""FastAPI 入口：供水管网隔离预演 + 复水计划 HTTP API。

接口一览（前缀 /api）：
  GET    /health                       健康检查
  PUT    /api/network                  整批录入/覆盖管网（原子事务；使未完计划失效，revision+1）
  GET    /api/network                  查询当前管网（含基准修订号 revision）
  DELETE /api/network                  清空管网（使未完计划失效，revision+1）
  POST   /api/valves/change            阀门整批变更（原子生效；使未完计划失效，revision+1）
  POST   /api/drill                    关阀隔离预演（只读，不写入）
  GET    /api/status                   当前真实阀位下的分派结果（含 revision）
  POST   /api/derating/preview         抢修降额影响预演（只读；同快照对比现状/降额两种情景）
  POST   /api/schedule/preview         多时段有限储水调度预演（只读；逐期补水/分配/扣减储水）
  POST   /api/restore/plans            复水逐步预演；全部安全才保存不可变计划
  GET    /api/restore/plans            计划列表
  GET    /api/restore/plans/{plan_id}  计划详情（步骤与进度）
  POST   /api/restore/plans/{plan_id}/execute  执行计划的下一步（乐观修订号 + 同事务）
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from . import database
from .database import bump_revision, get_conn, get_revision
from .engine import (
    Network,
    build_network,
    find_forbidden_connections,
    simulate,
)
from . import plans as plans_svc
from . import derating as derating_svc
from . import schedule as schedule_svc
from .repository import (
    get_plan,
    get_step_result,
    invalidate_active_plans,
    list_plans,
    load_network,
    replace_all,
    save_plan,
    save_step_result,
    serialize_network,
)
from .schemas import (
    DeratingPreviewRequest,
    DrillRequest,
    NetworkLoad,
    PlanExecuteRequest,
    RestorePlanRequest,
    SchedulePreviewRequest,
    ValveChangeBatch,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库表（幂等）
    database.init_db()
    yield


app = FastAPI(
    title="供水管网隔离预演服务",
    version="1.0.0",
    description="物业抢修关阀隔离预演：水源上限、单向管段、阀门状态、"
    "用水点优先级分配与不可并联水源互锁校验。",
    lifespan=lifespan,
)

# 写操作串行化，保证“整批校验 + 原子生效”在并发下不被穿插
_write_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 统一错误响应
# --------------------------------------------------------------------------- #
def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """类型层校验失败（如负数、缺字段、非法枚举）：422 + 精确定位。"""
    details = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        details.append({
            "location": loc or "(body)",
            "code": err.get("type", "invalid"),
            "message": err.get("msg", "参数非法"),
        })
    return error_response(422, "validation_error", "请求参数校验失败", details)


def _require_network(conn) -> Network | JSONResponse:
    network = load_network(conn)
    if network is None:
        return error_response(
            409, "network_not_initialized",
            "尚未录入管网数据，请先调用 PUT /api/network",
        )
    return network  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 基础接口
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok", "service": "water-isolation-drill"}


@app.get("/")
def root():
    return {
        "service": "供水管网隔离预演服务",
        "docs": "/docs",
        "endpoints": [
            "PUT /api/network", "GET /api/network", "DELETE /api/network",
            "POST /api/valves/change", "POST /api/drill", "GET /api/status",
            "POST /api/derating/preview", "POST /api/schedule/preview",
            "POST /api/restore/plans", "GET /api/restore/plans",
            "GET /api/restore/plans/{plan_id}",
            "POST /api/restore/plans/{plan_id}/execute",
        ],
    }


# --------------------------------------------------------------------------- #
# 管网录入
# --------------------------------------------------------------------------- #
@app.put("/api/network")
def load_net(payload: NetworkLoad, conn=Depends(get_conn)):
    """整批覆盖式录入。任一设施校验失败则整批回滚，库内原数据保持不变。

    成功后基准修订号 +1，所有未完复水计划失效（原接口字段保持兼容）。
    """
    data = payload.model_dump()
    with _write_lock:
        network, errs = build_network(data)
        if errs:
            return error_response(
                400, "invalid_network",
                "管网数据校验失败，整批录入已拒绝（未写入任何数据）",
                [e.to_dict() for e in errs],
            )
        try:
            with conn:  # 自动提交/回滚
                replace_all(conn, network)
                invalidate_active_plans(conn)
                new_revision = bump_revision(conn)
        except Exception as exc:  # pragma: no cover - 兜底
            conn.rollback()
            return error_response(500, "persist_failed", f"写入失败：{exc}")
    return {
        "message": "管网已整批录入",
        "revision": new_revision,
        "counts": {
            "sources": len(data["sources"]),
            "demands": len(data["demands"]),
            "junctions": len(data["junctions"]),
            "pipes": len(data["pipes"]),
            "valves": len(data["valves"]),
            "forbidden_pairs": len(data["forbidden_pairs"]),
        },
        "network": serialize_network(network),
    }


@app.get("/api/network")
def get_network(conn=Depends(get_conn)):
    network = load_network(conn)
    if network is None:
        return {"initialized": False, "network": None, "revision": None}
    return {
        "initialized": True,
        "revision": get_revision(conn),
        "network": serialize_network(network),
    }


@app.delete("/api/network")
def clear_network(conn=Depends(get_conn)):
    with _write_lock:
        with conn:
            database.clear_all(conn)
            invalidate_active_plans(conn)
            new_revision = bump_revision(conn)
    return {"message": "管网数据已清空", "revision": new_revision}


# --------------------------------------------------------------------------- #
# 阀门整批变更
# --------------------------------------------------------------------------- #
@app.post("/api/valves/change")
def change_valves(batch: ValveChangeBatch, conn=Depends(get_conn)):
    network = load_network(conn)
    if network is None:
        return error_response(
            409, "network_not_initialized",
            "尚未录入管网数据，请先调用 PUT /api/network",
        )

    # 1) 条目级定位校验
    details: list[dict] = []
    wanted: dict[str, bool] = {}
    for i, op in enumerate(batch.changes):
        loc = f"changes[{i}]"
        if op.action is None and op.is_open is None:
            details.append({
                "location": f"{loc}.action",
                "code": "missing_field",
                "message": "必须提供 action(open/close) 或 is_open 之一",
            })
            continue
        if op.action is not None and op.is_open is not None:
            details.append({
                "location": f"{loc}.action",
                "code": "conflicting_fields",
                "message": "action 与 is_open 不能同时提供",
            })
            continue
        target_state = op.is_open if op.is_open is not None else (op.action == "open")
        if op.valve_id not in network.valves:
            details.append({
                "location": f"{loc}.valve_id",
                "code": "unknown_valve",
                "message": f"非法/未知阀门编号：{op.valve_id}",
            })
            continue
        if op.valve_id in wanted:
            details.append({
                "location": f"{loc}.valve_id",
                "code": "duplicate_valve",
                "message": f"阀门 {op.valve_id} 在同一批次中出现多次",
            })
            continue
        wanted[op.valve_id] = target_state
    if details:
        return error_response(
            400, "invalid_valve_change", "阀门变更批次校验失败，未做任何变更", details
        )

    # 2) 暂存新状态并整批校验互锁水源连通性（暂存不写库）
    tentative_valves = {
        vid: replace(v, is_open=wanted.get(vid, v.is_open))
        for vid, v in network.valves.items()
    }
    tentative = Network(
        network.nodes, network.pipes, tentative_valves,
        network.forbidden_pairs, network.valves_by_pipe,
    )
    violations = find_forbidden_connections(tentative)
    if violations:
        details = [
            {
                "location": "forbidden_pairs",
                "code": "forbidden_connected",
                "message": f"水源 {a} 与 {b} 不可并联，但变更后将经开启管段连通："
                           + " -> ".join(path),
                "source_a": a,
                "source_b": b,
                "path": path,
            }
            for a, b, path in violations
        ]
        return error_response(
            409,
            "forbidden_sources_connected",
            "阀门变更被拒绝：将使不可并联水源经开启管段连通，原阀门状态保持不变",
            details,
        )

    # 3) 校验通过：事务内原子生效；阀位变化使未完复水计划失效，修订号 +1
    affected: list[dict] = []
    with _write_lock:
        with conn:
            for vid, target in wanted.items():
                old = network.valves[vid].is_open
                conn.execute(
                    "UPDATE valves SET is_open = ? WHERE id = ?",
                    (1 if target else 0, vid),
                )
                affected.append({
                    "valve_id": vid,
                    "pipe_id": network.valves[vid].pipe_id,
                    "previous_is_open": old,
                    "current_is_open": target,
                    "changed": old != target,
                })
            invalidate_active_plans(conn)
            new_revision = bump_revision(conn)
    return {
        "message": "阀门变更已整批生效",
        "revision": new_revision,
        "applied": affected,
        "closed_valves": sorted(
            vid for vid, v in tentative_valves.items() if not v.is_open
        ),
    }


# --------------------------------------------------------------------------- #
# 关阀隔离预演（只读，不写入状态）
# --------------------------------------------------------------------------- #
@app.post("/api/drill")
def drill(payload: DrillRequest, conn=Depends(get_conn)):
    network = load_network(conn)
    if network is None:
        return error_response(
            409, "network_not_initialized",
            "尚未录入管网数据，请先调用 PUT /api/network",
        )

    close_list = payload.close_valves
    details: list[dict] = []
    seen: set[str] = set()
    for i, vid in enumerate(close_list):
        if not isinstance(vid, str) or not vid:
            details.append({
                "location": f"close_valves[{i}]",
                "code": "invalid_valve",
                "message": "阀门编号必须是非空字符串",
            })
        elif vid not in network.valves:
            details.append({
                "location": f"close_valves[{i}]",
                "code": "unknown_valve",
                "message": f"非法/未知阀门编号：{vid}",
            })
        elif vid in seen:
            details.append({
                "location": f"close_valves[{i}]",
                "code": "duplicate_valve",
                "message": f"阀门 {vid} 重复出现",
            })
        seen.add(vid)
    if details:
        return error_response(400, "invalid_drill", "预演参数校验失败", details)

    result = simulate(network, force_closed=set(close_list))
    result["message"] = "预演完成：本次结果为临时计算，未写入任何阀门状态"
    return result


@app.get("/api/status")
def status(conn=Depends(get_conn)):
    """当前库内真实阀门状态下的供水分派结果。"""
    network = load_network(conn)
    if network is None:
        return error_response(
            409, "network_not_initialized",
            "尚未录入管网数据，请先调用 PUT /api/network",
        )
    result = simulate(network, force_closed=set())
    result["revision"] = get_revision(conn)
    return result


# --------------------------------------------------------------------------- #
# 抢修降额影响预演（只读，不写入状态/阀位/计划/修订号）
# --------------------------------------------------------------------------- #
@app.post("/api/derating/preview")
def derating_preview(payload: DeratingPreviewRequest, conn=Depends(get_conn)):
    """以同一已提交管网快照分别计算现状与降额情景。

    - 必须携带当前所见修订号；过期整单 409 拒绝，不返回任何分配结果；
    - 未知设施、错误类别、重复条目、上限越额一次性带定位 400 拒绝；
    - 两种情景沿用同一阀位、单向流量、用水点优先级与共享容量规则；
    - 降额仅作用于本次内存预演，不改变管网、阀位、计划或修订号；
    - 无降额项时，降额情景与现状完全一致。
    """
    # 读取 + 两次仿真在同一把写锁内完成，保证“同一已提交快照”，
    # 不会与录入/阀变/复水执行等写事务穿插而产生混合版本结果。
    with _write_lock:
        network = load_network(conn)
        if network is None:
            return error_response(
                409, "network_not_initialized",
                "尚未录入管网数据，请先调用 PUT /api/network",
            )
        current_revision = get_revision(conn)
        if payload.revision != current_revision:
            return error_response(
                409, "revision_conflict",
                f"修订号已过期：所见 {payload.revision}，当前 {current_revision}；"
                "请重新查询当前管网后重试，本次未做任何预演",
                [{
                    "location": "revision",
                    "code": "stale_revision",
                    "seen_revision": payload.revision,
                    "current_revision": current_revision,
                }],
            )

        source_items = payload.model_dump()["sources"]
        pipe_items = payload.model_dump()["pipes"]
        source_caps, pipe_caps, errs = derating_svc.validate_derating(
            network, source_items, pipe_items
        )
        if errs:
            return error_response(
                400, "invalid_derating",
                "降额预演参数校验失败，整单已拒绝（未做任何预演、未改变状态）",
                errs,
            )

        return derating_svc.preview(
            network, current_revision, source_items, pipe_items
        )


# --------------------------------------------------------------------------- #
# 多时段有限储水调度预演（只读，不写入状态/阀位/计划/修订号）
# --------------------------------------------------------------------------- #
@app.post("/api/schedule/preview")
def schedule_preview(payload: SchedulePreviewRequest, conn=Depends(get_conn)):
    """估算连续等长时段内有限储水的供水情况。

    - 必须携带当前所见修订号；过期整单 409 拒绝，不返回任何分配结果；
    - `sources` 须恰好覆盖所有水源的初始储水与库容（初始储水不得超过库容）；
      每期须恰好列出所有水源的补水量与所有用水点的需求量；
    - 未知/错类设施、重复/缺失条目一次性带定位 400 拒绝；
    - 每期先补水至库容（超出计入溢出），再按现有阀位、单向管段、共享容量
      与用水点优先级分配；水源本期出水不超过额定上限与可用储水，
      实际出水扣减储水，欠供不结转；
    - 预演仅作用于内存：不改变管网、阀位、计划或修订号。
    """
    # 读取与逐期仿真在同一把写锁内完成，保证“同一已提交快照”，
    # 不会与录入/阀变/复水执行等写事务穿插而产生混合版本结果。
    with _write_lock:
        network = load_network(conn)
        if network is None:
            return error_response(
                409, "network_not_initialized",
                "尚未录入管网数据，请先调用 PUT /api/network",
            )
        current_revision = get_revision(conn)
        if payload.revision != current_revision:
            return error_response(
                409, "revision_conflict",
                f"修订号已过期：所见 {payload.revision}，当前 {current_revision}；"
                "请重新查询当前管网后重试，本次未做任何预演",
                [{
                    "location": "revision",
                    "code": "stale_revision",
                    "seen_revision": payload.revision,
                    "current_revision": current_revision,
                }],
            )

        data = payload.model_dump()
        _init, errs = schedule_svc.validate_schedule(
            network, data["sources"], data["periods"]
        )
        if errs:
            return error_response(
                400, "invalid_schedule",
                "调度预演参数校验失败，整单已拒绝（未做任何预演、未改变状态）",
                errs,
            )

        return schedule_svc.preview(
            network, current_revision, data["sources"], data["periods"]
        )


# --------------------------------------------------------------------------- #
# 复水计划：逐步预演 -> 保存不可变计划 -> 带修订号逐步执行
# --------------------------------------------------------------------------- #
def _serialize_plan(plan: dict) -> dict:
    completed = plan["next_step"] >= plan["total_steps"]
    return {
        "plan_id": plan["id"],
        "status": plan["status"],
        "base_revision": plan["base_revision"],
        "total_steps": plan["total_steps"],
        # 已全部完成时没有“下一步”，返回 null（与执行响应口径一致）
        "next_step": None if completed else plan["next_step"] + 1,
        "completed_steps": plan["next_step"],
        "created_at": plan["created_at"],
        "updated_at": plan["updated_at"],
        "steps": [
            {
                "position": s["position"],
                "valve_id": s["valve_id"],
                "action": s["action"],
                "is_open": bool(s["target_is_open"]),
            }
            for s in plan["steps"]
        ],
    }


@app.post("/api/restore/plans")
def create_restore_plan(payload: RestorePlanRequest, conn=Depends(get_conn)):
    """复水逐步预演：按当前管网与阀位逐步叠加开关动作（不写状态）。

    每步检查不可并联水源；首个冲突返回步骤位置/水源对/路径且不保存。
    全部安全才保存不可变计划，返回每步阀位、供水分派与基准修订号。
    """
    raw_steps = payload.model_dump()["steps"]
    plan_id = "PLAN-" + uuid.uuid4().hex[:16]
    with _write_lock:
        # 预演与保存放在同一把写锁内：避免“预演后、保存前”被其它写操作穿插，
        # 确保计划基于本次读取到的管网/阀位与修订号
        network = load_network(conn)
        if network is None:
            return error_response(
                409, "network_not_initialized",
                "尚未录入管网数据，请先调用 PUT /api/network",
            )
        normalized, errs = plans_svc.validate_steps(network, raw_steps)
        if errs:
            return error_response(
                400, "invalid_restore_plan",
                "复水步骤校验失败，计划未保存（未写入任何状态）",
                errs,
            )

        snapshots, conflict = plans_svc.preview_plan(network, normalized)
        if conflict is not None:
            # 预演冲突：不改任何实际状态、不保存计划
            return error_response(
                409,
                "forbidden_sources_connected",
                "复水预演被拒绝：某一步将使不可并联水源经开启管段连通，"
                "计划未保存（未写入任何阀门状态）",
                [conflict],
            )

        base_revision = get_revision(conn)
        with conn:
            save_plan(conn, plan_id, normalized, base_revision)

    return {
        "message": "复水计划已保存（步骤不可变）；请携带修订号逐步执行",
        "plan_id": plan_id,
        "status": "active",
        "base_revision": base_revision,
        "revision": base_revision,
        "total_steps": len(normalized),
        "next_step": 1,
        "steps": snapshots,
    }


@app.get("/api/restore/plans")
def list_restore_plans(conn=Depends(get_conn)):
    plans = list_plans(conn)
    return {
        "revision": get_revision(conn),
        "plans": [
            {
                "plan_id": p["id"],
                "status": p["status"],
                "base_revision": p["base_revision"],
                "total_steps": p["total_steps"],
                "next_step": (
                    None if p["next_step"] >= p["total_steps"]
                    else p["next_step"] + 1
                ),
                "completed_steps": p["next_step"],
                "created_at": p["created_at"],
                "updated_at": p["updated_at"],
            }
            for p in plans
        ],
    }


@app.get("/api/restore/plans/{plan_id}")
def get_restore_plan(plan_id: str, conn=Depends(get_conn)):
    plan = get_plan(conn, plan_id)
    if plan is None:
        return error_response(
            404, "plan_not_found", f"复水计划不存在：{plan_id}"
        )
    body = _serialize_plan(plan)
    body["revision"] = get_revision(conn)
    return body


@app.post("/api/restore/plans/{plan_id}/execute")
def execute_restore_step(
    plan_id: str, payload: PlanExecuteRequest, conn=Depends(get_conn)
):
    """执行计划的下一步。

    - 只接受“计划的下一步”与客户端“所见修订号”；
    - 重复提交已完成步骤：原样回放该步结果（幂等，不改状态）；
    - 过期修订号或跳步：409 拒绝，不改变任何状态；
    - 正常执行：同一事务内复核修订与安全性、更新阀位与进度、revision+1。
    """
    # 全部读取与判定都在写锁 + 同一事务内完成，避免并发穿插造成的误判
    with _write_lock:
        with conn:
            network = load_network(conn)
            if network is None:
                return error_response(
                    409, "network_not_initialized",
                    "尚未录入管网数据，请先调用 PUT /api/network",
                )
            plan = get_plan(conn, plan_id)
            if plan is None:
                return error_response(
                    404, "plan_not_found", f"复水计划不存在：{plan_id}"
                )

            total = plan["total_steps"]
            next_step = plan["next_step"]  # 0 基：首个未完成步骤

            # 1) 重复提交已完成步骤：回放原结果（幂等，不写不改修订号）
            if (
                payload.step is not None
                and 1 <= payload.step <= next_step
                and payload.step <= total
            ):
                original = get_step_result(conn, plan_id, payload.step - 1)
                if original is not None:
                    original["idempotent_replay"] = True
                    return original

            # 2) 已完成 / 已失效计划
            if plan["status"] == "completed":
                return error_response(
                    409, "plan_already_completed",
                    f"计划 {plan_id} 已全部完成（共 {total} 步）；"
                    "重复执行请携带已完成的 step 位置",
                )
            if plan["status"] == "invalidated":
                return error_response(
                    409, "plan_invalidated",
                    f"计划 {plan_id} 已因管网录入/清空或阀门变更而失效，"
                    "请基于当前修订号重新编排",
                    [{"location": "plan", "code": "plan_invalidated",
                      "base_revision": plan["base_revision"],
                      "current_revision": get_revision(conn)}],
                )

            # 3) 跳步：只接受下一步
            requested_idx = next_step if payload.step is None else payload.step - 1
            if payload.step is not None and payload.step != next_step + 1:
                return error_response(
                    409, "step_out_of_order",
                    f"必须按顺序执行：计划下一步为第 {next_step + 1} 步，"
                    f"收到第 {payload.step} 步，已拒绝且未改变状态",
                    [{
                        "location": "step",
                        "code": "unexpected_step",
                        "expected_step": next_step + 1,
                        "received_step": payload.step,
                    }],
                )

            # 4) 过期修订号：拒绝且不改变状态
            current_revision = get_revision(conn)
            if payload.revision != current_revision:
                return error_response(
                    409, "revision_conflict",
                    f"修订号已过期：所见 {payload.revision}，当前 {current_revision}；"
                    "请重新查询当前阀位/分派并重试，本次未改变状态",
                    [{
                        "location": "revision",
                        "code": "stale_revision",
                        "seen_revision": payload.revision,
                        "current_revision": current_revision,
                    }],
                )

            # 事务内重新确认计划仍 active 且仍为下一步
            if plan["status"] != "active" or plan["next_step"] != requested_idx:
                return error_response(
                    409, "plan_invalidated",
                    f"计划 {plan_id} 状态已变化，请重新查询后重试",
                )

            step_row = plan["steps"][requested_idx]
            vid = step_row["valve_id"]
            target = bool(step_row["target_is_open"])

            # 5) 同事务安全性复核：应用本步后互锁水源不得经开启管段连通
            tentative_valves = {
                v_id: replace(v, is_open=target if v_id == vid else v.is_open)
                for v_id, v in network.valves.items()
            }
            tentative = Network(
                network.nodes, network.pipes, tentative_valves,
                network.forbidden_pairs, network.valves_by_pipe,
            )
            violations = find_forbidden_connections(tentative)
            if violations:
                a, b, path = violations[0]
                return error_response(
                    409, "forbidden_sources_connected",
                    "执行被拒绝：该步将使不可并联水源经开启管段连通，"
                    "阀位与进度均未改变",
                    [{
                        "location": f"steps[{requested_idx}]",
                        "code": "forbidden_connected",
                        "position": requested_idx + 1,
                        "valve_id": vid,
                        "source_a": a,
                        "source_b": b,
                        "path": path,
                        "message": f"水源 {a} 与 {b} 将连通：" + " -> ".join(path),
                    }],
                )

            # 6) 全部通过：更新阀位、进度、修订号；其余未完计划随之失效
            conn.execute(
                "UPDATE valves SET is_open = ? WHERE id = ?",
                (1 if target else 0, vid),
            )
            invalidate_active_plans(conn, except_id=plan_id)
            new_revision = bump_revision(conn)

            # 提交后的分派结果（基于新阀位）
            after_network = load_network(conn)
            step_obj = {
                "valve_id": vid,
                "action": step_row["action"],
                "target_is_open": target,
            }
            snap = plans_svc.step_snapshot(after_network, step_obj, requested_idx + 1)
            completed = requested_idx + 1 >= total
            result = {
                "message": "步骤已执行并提交",
                "plan_id": plan_id,
                "status": "completed" if completed else "active",
                "position": requested_idx + 1,
                "total_steps": total,
                "next_step": None if completed else requested_idx + 2,
                "seen_revision": payload.revision,
                "revision": new_revision,
                "step": snap,
                "idempotent_replay": False,
            }
            save_step_result(conn, plan_id, requested_idx, new_revision, result)
            return result
