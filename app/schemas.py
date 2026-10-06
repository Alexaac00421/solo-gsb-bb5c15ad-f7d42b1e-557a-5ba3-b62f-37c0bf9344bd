"""HTTP API 的请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 多余字段直接报错，防止录入错位


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("必须是有限数值，不能是 NaN 或无穷大")
    return v


class SourceIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="水源编号")
    capacity: float = Field(ge=0, description="供水上限（非负）")

    @field_validator("capacity")
    @classmethod
    def _cap_finite(cls, v: float) -> float:
        return _finite(v)


class DemandIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="用水点编号")
    demand: float = Field(ge=0, description="需求量（非负）")
    priority: int = Field(default=0, description="优先级，数值越大越优先")

    @field_validator("demand")
    @classmethod
    def _demand_finite(cls, v: float) -> float:
        return _finite(v)


class JunctionIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="连接点编号")


class PipeIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="管段编号")
    from_node: str = Field(min_length=1, description="起点节点编号")
    to_node: str = Field(min_length=1, description="终点节点编号（管段单向）")
    capacity: float = Field(ge=0, description="管段容量（非负）")

    @field_validator("capacity")
    @classmethod
    def _cap_finite(cls, v: float) -> float:
        return _finite(v)


class ValveIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="阀门编号")
    pipe_id: str = Field(min_length=1, description="所在管段编号")
    is_open: bool = Field(default=True, description="true=开启，false=关闭")


class ForbiddenPairIn(StrictModel):
    source_a: str = Field(min_length=1)
    source_b: str = Field(min_length=1)


class NetworkLoad(StrictModel):
    """整批录入：一次性覆盖式写入全部设施（原子事务）。"""

    sources: list[SourceIn] = Field(default_factory=list)
    demands: list[DemandIn] = Field(default_factory=list)
    junctions: list[JunctionIn] = Field(default_factory=list)
    pipes: list[PipeIn] = Field(default_factory=list)
    valves: list[ValveIn] = Field(default_factory=list)
    forbidden_pairs: list[ForbiddenPairIn] = Field(default_factory=list)


class ValveChangeOp(StrictModel):
    """单只阀门的变更。action 与 is_open 二选一。"""

    valve_id: str = Field(min_length=1)
    action: Optional[Literal["open", "close"]] = None
    is_open: Optional[bool] = None


class ValveChangeBatch(StrictModel):
    changes: list[ValveChangeOp] = Field(min_length=1)


class DrillRequest(StrictModel):
    """隔离预演：拟关闭的阀门编号列表（不写入状态）。"""

    close_valves: list[str] = Field(default_factory=list)


class DeratedCapacityIn(StrictModel):
    """降额预演中的单个设施临时容量上限（水源或管段）。

    有限非负由本层保证；“不得超过当前额定容量”“设施必须存在且类别正确”
    需要结合已提交管网快照，在服务层逐条定位校验。
    """

    facility_id: str = Field(min_length=1, max_length=64, description="设施编号（水源或管段）")
    capacity: float = Field(
        ge=0, description="抢修期间的临时容量上限（有限非负，且不超过当前额定容量）"
    )

    @field_validator("capacity")
    @classmethod
    def _cap_finite(cls, v: float) -> float:
        return _finite(v)


class DeratingPreviewRequest(StrictModel):
    """抢修降额影响预演：携带所见修订号与水源/管段临时容量上限（只读，不写入）。"""

    revision: int = Field(
        ge=0, description="客户端当前所见管网基准修订号（过期则整单拒绝）"
    )
    sources: list[DeratedCapacityIn] = Field(default_factory=list)
    pipes: list[DeratedCapacityIn] = Field(default_factory=list)


class ScheduleSourceIn(StrictModel):
    """调度预演中某水源的初始储水与库容。

    有限非负由本层保证；“初始储水不得超过库容”“必须恰好覆盖所有水源”
    需要结合已提交管网快照，在服务层逐条定位校验。
    """

    source_id: str = Field(min_length=1, max_length=64, description="水源编号")
    initial_storage: float = Field(
        ge=0, description="期初储水（有限非负，且不得超过库容）"
    )
    storage_capacity: float = Field(ge=0, description="库容（有限非负）")

    @field_validator("initial_storage", "storage_capacity")
    @classmethod
    def _storage_finite(cls, v: float) -> float:
        return _finite(v)


class ScheduleReplenishIn(StrictModel):
    """某水源在某一期的补水量。"""

    source_id: str = Field(min_length=1, max_length=64, description="水源编号")
    amount: float = Field(ge=0, description="补水量（有限非负）")

    @field_validator("amount")
    @classmethod
    def _amount_finite(cls, v: float) -> float:
        return _finite(v)


class ScheduleDemandIn(StrictModel):
    """某用水点在某一期的需求量。"""

    demand_id: str = Field(min_length=1, max_length=64, description="用水点编号")
    amount: float = Field(ge=0, description="需求量（有限非负）")

    @field_validator("amount")
    @classmethod
    def _amount_finite(cls, v: float) -> float:
        return _finite(v)


class SchedulePeriodIn(StrictModel):
    """一个等长时段：恰好列出所有水源的补水量与所有用水点的需求量。

    缺省为空数组；“恰好覆盖、不重复、不错类”由服务层结合管网快照校验。
    """

    replenishment: list[ScheduleReplenishIn] = Field(default_factory=list)
    demands: list[ScheduleDemandIn] = Field(default_factory=list)


class SchedulePreviewRequest(StrictModel):
    """多时段有限储水调度预演：携带所见修订号、各水源初始储水与库容、非空有序时段（只读，不写入）。"""

    revision: int = Field(
        ge=0, description="客户端当前所见管网基准修订号（过期则整单拒绝）"
    )
    sources: list[ScheduleSourceIn] = Field(
        default_factory=list,
        description="各水源初始储水与库容（须恰好覆盖所有水源）",
    )
    periods: list[SchedulePeriodIn] = Field(
        min_length=1,
        description="非空有序时段；每期恰好列出全部水源补水与全部用水点需求",
    )


class RestoreStepIn(StrictModel):
    """复水计划中的单步：对某只阀门执行开/关。

    action 不用 Literal：非法动作需与未知阀门、重复阀门一起在服务层逐条定位报错。
    """

    valve_id: str = Field(min_length=1, description="阀门编号")
    action: str = Field(min_length=1, description="动作：open/close")


class RestorePlanRequest(StrictModel):
    """复水计划：非空、有序（数组顺序即操作顺序）、阀门编号不重复的步骤清单。"""

    steps: list[RestoreStepIn] = Field(min_length=1)


class PlanExecuteRequest(StrictModel):
    """执行计划下一步：必须携带客户端所见基准修订号；step 可选（1 基）用于幂等/防跳步。"""

    revision: int = Field(ge=0, description="客户端所见的当前基准修订号")
    step: Optional[int] = Field(
        default=None, ge=1, description="拟执行的步骤位置（1 基）；缺省为计划的下一步"
    )
