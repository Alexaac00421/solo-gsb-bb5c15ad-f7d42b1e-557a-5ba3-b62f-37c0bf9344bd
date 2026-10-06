"""管网模型、整批校验、连通冲突检测与分配仿真引擎。

关键业务规则：
1. 管段为单向（泵/止回）。阀门挂在管段上，管段上“所有阀门均开启”时该管段才导通。
2. 不可并联水源对：只要二者经由“开启管段”连通即冲突。连通按无向判定
   —— 检修时关阀隔离只关心物理连通，止回阀的方向不应让互锁水源被误判为隔离，
   这是偏保守（更安全）的工程判定。
3. 预演分配：用水点按优先级从高到低排序，同级按编号升序；依次求解
   “剩余水源上限 + 剩余共享管段容量”下该点可获得的最大流量（Dinic 最大流），
   被占用的水源额度与管段容量对后续点不再重复可用。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

EPS = 1e-9


# --------------------------------------------------------------------------- #
# 错误类型（带定位信息）
# --------------------------------------------------------------------------- #
@dataclass
class ValidationError:
    """录入/变更校验错误。location 给出精确定位，如 pipes[2].capacity。"""

    location: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"location": self.location, "code": self.code, "message": self.message}


# --------------------------------------------------------------------------- #
# 内存模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Node:
    id: str
    type: str  # source / demand / junction
    capacity: Optional[float] = None  # 水源供水上限
    demand: Optional[float] = None    # 用水点需求
    priority: Optional[int] = None    # 用水点优先级


@dataclass(frozen=True)
class Pipe:
    id: str
    from_node: str
    to_node: str
    capacity: float


@dataclass(frozen=True)
class Valve:
    id: str
    pipe_id: str
    is_open: bool


@dataclass
class Network:
    nodes: dict[str, Node]
    pipes: dict[str, Pipe]
    valves: dict[str, Valve]
    forbidden_pairs: list[tuple[str, str]]
    # 管段 -> 该管段上的阀门 id 列表
    valves_by_pipe: dict[str, list[str]] = field(default_factory=dict)

    def source_ids(self) -> list[str]:
        return [nid for nid, n in self.nodes.items() if n.type == "source"]

    def demand_ids(self) -> list[str]:
        return [nid for nid, n in self.nodes.items() if n.type == "demand"]

    def pipe_is_open(
        self, pipe: Pipe, force_closed: Optional[set[str]] = None
    ) -> bool:
        """管段导通条件：其上所有阀门均开启。

        force_closed 为预演时拟关闭的阀门集合（临时覆盖，不写库）。
        """
        force_closed = force_closed or set()
        for vid in self.valves_by_pipe.get(pipe.id, ()):
            if vid in force_closed or not self.valves[vid].is_open:
                return False
        return True


# --------------------------------------------------------------------------- #
# 整批录入校验
# --------------------------------------------------------------------------- #
def _err(errs: list[ValidationError], location: str, code: str, message: str) -> None:
    errs.append(ValidationError(location, code, message))


def _finite_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def build_network(data: dict) -> tuple[Optional[Network], list[ValidationError]]:
    """整批解析 + 校验录入数据。

    校验内容：编号合法/唯一、字段取值合法、端点存在且类型正确、
    阀门引用的管段存在、水源对引用合法且不重复，以及不可并联水源经
    开启管段连通的初始冲突。返回 (Network, errors)；errors 非空时 Network 为 None。
    """
    errs: list[ValidationError] = []
    root_keys = {
        "sources", "demands", "junctions", "pipes", "valves", "forbidden_pairs",
    }
    for key in data:
        if key not in root_keys:
            _err(errs, key, "unknown_field", f"未知字段：{key}")
    for key in root_keys:
        if key in data and not isinstance(data[key], list):
            _err(errs, key, "invalid_type", f"{key} 必须是数组")
    if errs:
        return None, errs

    sources = data.get("sources", [])
    demands = data.get("demands", [])
    junctions = data.get("junctions", [])
    pipes = data.get("pipes", [])
    valves = data.get("valves", [])
    pairs = data.get("forbidden_pairs", [])

    nodes: dict[str, Node] = {}

    def check_id(item: object, loc: str, seen: set[str]) -> Optional[str]:
        if not isinstance(item, dict):
            _err(errs, loc, "invalid_type", "必须是对象")
            return None
        nid = item.get("id")
        if not isinstance(nid, str) or not nid.strip():
            _err(errs, f"{loc}.id", "invalid_id", "编号必须是非空字符串")
            return None
        if nid in seen:
            _err(errs, f"{loc}.id", "duplicate_id", f"编号重复：{nid}")
            return None
        seen.add(nid)
        return nid

    def check_amount(item: dict, field_name: str, loc: str) -> Optional[float]:
        v = item.get(field_name)
        if not _finite_number(v):
            _err(errs, f"{loc}.{field_name}", "invalid_number",
                 f"{field_name} 必须是有限数值")
            return None
        if v < 0:
            _err(errs, f"{loc}.{field_name}", "negative_value",
                 f"{field_name} 不能为负数")
            return None
        return float(v)

    seen_ids: set[str] = set()

    for i, item in enumerate(sources):
        loc = f"sources[{i}]"
        nid = check_id(item, loc, seen_ids)
        cap = check_amount(item, "capacity", loc) if isinstance(item, dict) else None
        if nid is not None and cap is not None:
            nodes[nid] = Node(nid, "source", capacity=cap)

    for i, item in enumerate(demands):
        loc = f"demands[{i}]"
        nid = check_id(item, loc, seen_ids)
        if not isinstance(item, dict) or nid is None:
            continue
        need = check_amount(item, "demand", loc)
        pri = item.get("priority", 0)
        if not isinstance(pri, int) or isinstance(pri, bool):
            _err(errs, f"{loc}.priority", "invalid_priority",
                 "priority 必须是整数（数值越大优先级越高）")
            pri = None
        if need is not None and pri is not None:
            nodes[nid] = Node(nid, "demand", demand=float(need), priority=pri)

    for i, item in enumerate(junctions):
        loc = f"junctions[{i}]"
        nid = check_id(item, loc, seen_ids)
        if nid is not None:
            nodes[nid] = Node(nid, "junction")

    # 管段
    pipe_objs: dict[str, Pipe] = {}
    for i, item in enumerate(pipes):
        loc = f"pipes[{i}]"
        pid = check_id(item, loc, seen_ids)
        if not isinstance(item, dict) or pid is None:
            continue
        cap = check_amount(item, "capacity", loc)
        a, b = item.get("from_node"), item.get("to_node")
        endpoints_ok = True
        for name, ref in (("from_node", a), ("to_node", b)):
            if not isinstance(ref, str):
                _err(errs, f"{loc}.{name}", "invalid_endpoint",
                     f"{name} 必须是已存在的节点编号字符串")
                endpoints_ok = False
            elif ref not in nodes:
                _err(errs, f"{loc}.{name}", "unknown_endpoint",
                     f"未知端点：{ref}（节点不存在）")
                endpoints_ok = False
        if a == b and isinstance(a, str):
            _err(errs, loc, "self_loop", f"管段起终点不能相同：{a}")
            endpoints_ok = False
        if cap is not None and endpoints_ok:
            pipe_objs[pid] = Pipe(pid, a, b, cap)

    # 阀门
    valve_objs: dict[str, Valve] = {}
    valves_by_pipe: dict[str, list[str]] = {}
    for i, item in enumerate(valves):
        loc = f"valves[{i}]"
        vid = check_id(item, loc, seen_ids)
        if not isinstance(item, dict) or vid is None:
            continue
        pid = item.get("pipe_id")
        if not isinstance(pid, str) or pid not in pipe_objs:
            _err(errs, f"{loc}.pipe_id", "unknown_pipe",
                 f"阀门 {vid} 引用的管段不存在：{pid!r}")
            continue
        is_open = item.get("is_open", True)
        if not isinstance(is_open, bool):
            _err(errs, f"{loc}.is_open", "invalid_state", "is_open 必须是布尔值")
            continue
        valve_objs[vid] = Valve(vid, pid, is_open)
        valves_by_pipe.setdefault(pid, []).append(vid)

    # 不可并联水源对
    pair_objs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, item in enumerate(pairs):
        loc = f"forbidden_pairs[{i}]"
        if not isinstance(item, dict):
            _err(errs, loc, "invalid_type", "必须是对象")
            continue
        a, b = item.get("source_a"), item.get("source_b")
        for name, ref in (("source_a", a), ("source_b", b)):
            if not isinstance(ref, str) or ref not in nodes:
                _err(errs, f"{loc}.{name}", "unknown_endpoint",
                     f"未知端点：{ref!r}（水源不存在）")
            elif nodes[ref].type != "source":
                _err(errs, f"{loc}.{name}", "not_a_source",
                     f"节点 {ref} 不是水源，不能构成水源对")
        if isinstance(a, str) and isinstance(b, str) and a in nodes and b in nodes:
            if a == b:
                _err(errs, loc, "invalid_pair", "水源对必须是两个不同的水源")
                continue
            key = tuple(sorted((a, b)))
            if key in seen_pairs:
                _err(errs, loc, "duplicate_pair",
                     f"水源对重复：{a} / {b}")
                continue
            seen_pairs.add(key)
            pair_objs.append((a, b))

    if errs:
        return None, errs

    network = Network(nodes, pipe_objs, valve_objs, pair_objs, valves_by_pipe)

    # 初始连通冲突：录入后已有开启管段把互锁水源连通，也应拒绝整批数据
    violations = find_forbidden_connections(network)
    if violations:
        for v in violations:
            a, b, path = v
            _err(errs, "forbidden_pairs", "forbidden_connected",
                 f"水源 {a} 与 {b} 不可并联，但当前已通过开启管段连通："
                 + " -> ".join(path))
        return None, errs

    return network, errs


# --------------------------------------------------------------------------- #
# 连通性 / 冲突检测（无向遍历开启管段，BFS 返回路径用于定位）
# --------------------------------------------------------------------------- #
def _undirected_adj(
    network: Network, force_closed: Optional[set[str]] = None
) -> dict[str, list[tuple[str, str]]]:
    """构造无向邻接表：node -> [(neighbor, pipe_id)]，只含导通管段。"""
    adj: dict[str, list[tuple[str, str]]] = {nid: [] for nid in network.nodes}
    for pipe in network.pipes.values():
        if network.pipe_is_open(pipe, force_closed):
            adj[pipe.from_node].append((pipe.to_node, pipe.id))
            adj[pipe.to_node].append((pipe.from_node, pipe.id))
    return adj


def _bfs_path(
    adj: dict[str, list[tuple[str, str]]], start: str, target: str
) -> Optional[list[str]]:
    """无向 BFS，返回从 start 到 target 的节点路径（含两端）。"""
    if start == target:
        return [start]
    prev: dict[str, tuple[str, str]] = {start: ("", "")}
    q = deque([start])
    while q:
        cur = q.popleft()
        for nxt, _pid in adj.get(cur, ()):
            if nxt in prev:
                continue
            prev[nxt] = (cur, _pid)
            if nxt == target:
                path = [nxt]
                while path[-1] != start:
                    path.append(prev[path[-1]][0])
                path.reverse()
                return path
            q.append(nxt)
    return None


def find_forbidden_connections(
    network: Network, force_closed: Optional[set[str]] = None
) -> list[tuple[str, str, list[str]]]:
    """返回所有“经开启管段连通的不可并联水源对”：(a, b, 节点路径)。"""
    adj = _undirected_adj(network, force_closed)
    out: list[tuple[str, str, list[str]]] = []
    for a, b in network.forbidden_pairs:
        if a not in network.nodes or b not in network.nodes:
            continue
        path = _bfs_path(adj, a, b)
        if path is not None:
            out.append((a, b, path))
    return out


# --------------------------------------------------------------------------- #
# Dinic 最大流
# --------------------------------------------------------------------------- #
class _Edge:
    __slots__ = ("to", "rev", "cap", "tag")

    def __init__(self, to: int, rev: int, cap: float, tag: Optional[dict]):
        self.to = to
        self.rev = rev
        self.cap = cap
        # tag: {'kind': 'source'|'pipe', 'id': ...} 用于回溯占用与瓶颈
        self.tag = tag


class Dinic:
    """带标签边的 Dinic 最大流（浮点容量）。"""

    def __init__(self, n: int):
        self.n = n
        self.g: list[list[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: float, tag: Optional[dict] = None) -> None:
        # 必须先确定正向边在 g[u] 中的位置，反向边的 rev 才不会错位
        fwd_idx = len(self.g[u])
        fwd = _Edge(v, len(self.g[v]), cap, tag)
        rev = _Edge(u, fwd_idx, 0.0, None)
        self.g[u].append(fwd)
        self.g[v].append(rev)

    def bfs(self, s: int, t: int) -> list[int]:
        level = [-1] * self.n
        level[s] = 0
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > EPS and level[e.to] < 0:
                    level[e.to] = level[u] + 1
                    q.append(e.to)
        return level

    def dfs(self, u: int, t: int, pushed: float, level: list[int], it: list[int]) -> float:
        if u == t:
            return pushed
        while it[u] < len(self.g[u]):
            e = self.g[u][it[u]]
            if e.cap > EPS and level[e.to] == level[u] + 1:
                d = self.dfs(e.to, t, min(pushed, e.cap), level, it)
                if d > EPS:
                    e.cap -= d
                    self.g[e.to][e.rev].cap += d
                    return d
            it[u] += 1
        return 0.0

    def max_flow(self, s: int, t: int) -> float:
        flow = 0.0
        # 递归深度最多为节点数；预留余量，避免大管网触发递归限制
        import sys
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, self.n * 4 + 1000))
        try:
            while True:
                level = self.bfs(s, t)
                if level[t] < 0:
                    break
                it = [0] * self.n
                while True:
                    f = self.dfs(s, t, math.inf, level, it)
                    if f <= EPS:
                        break
                    flow += f
        finally:
            sys.setrecursionlimit(old_limit)
        return flow

    def reachable_from_source(self, s: int) -> set[int]:
        """最大流后沿残余容量 > 0 的边可达的点（用于求最小割）。"""
        seen = {s}
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > EPS and e.to not in seen:
                    seen.add(e.to)
                    q.append(e.to)
        return seen


# --------------------------------------------------------------------------- #
# 预演分配
# --------------------------------------------------------------------------- #
def _directed_reachability(
    network: Network,
    sources: list[str],
    force_closed: set[str],
    reverse: bool = False,
) -> set[str]:
    """沿导通管段（容量任意）的有向可达集合；reverse=True 时反向遍历。"""
    adj: dict[str, list[str]] = {nid: [] for nid in network.nodes}
    for pipe in network.pipes.values():
        if network.pipe_is_open(pipe, force_closed):
            if reverse:
                adj[pipe.to_node].append(pipe.from_node)
            else:
                adj[pipe.from_node].append(pipe.to_node)
    seen: set[str] = set()
    q: deque[str] = deque()
    for s in sources:
        if s in network.nodes and s not in seen:
            seen.add(s)
            q.append(s)
    while q:
        u = q.popleft()
        for v in adj.get(u, ()):
            if v not in seen:
                seen.add(v)
                q.append(v)
    return seen


def simulate(
    network: Network, force_closed: Optional[set[str]] = None
) -> dict:
    """执行隔离预演（不写状态）。

    返回各用水点供水量、欠供原因与受限管段，以及全局占用统计。
    """
    force_closed = set(force_closed or ())

    # 排序：优先级降序，同级按编号升序
    demands = sorted(
        (network.nodes[nid] for nid in network.demand_ids()),
        key=lambda n: (-(n.priority or 0), n.id),
    )

    # 剩余资源：水源上限、共享管段容量
    remaining_source: dict[str, float] = {
        nid: network.nodes[nid].capacity or 0.0 for nid in network.source_ids()
    }
    remaining_pipe: dict[str, float] = {
        pid: p.capacity for pid, p in network.pipes.items()
    }

    allocations: list[dict] = []
    for node in demands:
        need = node.demand or 0.0
        entry: dict = {
            "demand_id": node.id,
            "priority": node.priority or 0,
            "requested": need,
            "supplied": 0.0,
            "shortfall": need,
            "fulfilled": False,
            "shortfall_reason_code": None,
            "shortfall_reason": None,
            "constrained_pipes": [],
            "by_source": {},
        }

        if need <= EPS:
            entry.update(supplied=0.0, shortfall=0.0, fulfilled=True)
            allocations.append(entry)
            continue

        # ---- 阶段一：可达性（忽略剩余容量，只看拓扑 + 阀门） ----
        can_reach = _directed_reachability(
            network, network.source_ids(), force_closed
        )
        if node.id not in can_reach:
            entry["shortfall_reason_code"] = "ISOLATED"
            entry["shortfall_reason"] = (
                f"用水点 {node.id} 已被隔离：无任何水源经开启管段可达该点"
            )
            entry["constrained_pipes"] = _isolation_constraints(
                network, node.id, force_closed
            )
            allocations.append(entry)
            continue

        # ---- 阶段二：在剩余资源约束下求最大流 ----
        # 节点 0..N-1 对应网络节点；S = 超级源（接水源），T = 超级汇（接用水点）
        index = {nid: i for i, nid in enumerate(network.nodes)}
        super_source = len(index)
        super_sink = super_source + 1
        dinic = Dinic(super_sink + 1)
        source_edges: dict[str, _Edge] = {}
        pipe_edges: dict[str, _Edge] = {}

        for sid, cap in remaining_source.items():
            if cap > EPS:
                e_idx = len(dinic.g[super_source])
                dinic.add_edge(
                    super_source, index[sid], cap, {"kind": "source", "id": sid}
                )
                source_edges[sid] = dinic.g[super_source][e_idx]
        for pid, pipe in network.pipes.items():
            if network.pipe_is_open(pipe, force_closed):
                cap = remaining_pipe[pid]
                if cap > EPS:
                    u = index[pipe.from_node]
                    e_idx = len(dinic.g[u])
                    dinic.add_edge(
                        u, index[pipe.to_node], cap, {"kind": "pipe", "id": pid}
                    )
                    pipe_edges[pid] = dinic.g[u][e_idx]
        # 用水点 -> 超级汇，容量为需求量
        demand_u = index[node.id]
        dinic.add_edge(demand_u, super_sink, need, {"kind": "demand", "id": node.id})

        flow = dinic.max_flow(super_source, super_sink)
        supplied = min(flow, need)
        entry["supplied"] = round(supplied, 9)
        entry["shortfall"] = round(max(0.0, need - supplied), 9)
        entry["fulfilled"] = supplied + EPS >= need

        # 回溯各水源供水量：正向源边剩余容量 = 初始 - 已用
        by_source: dict[str, float] = {}
        for sid, edge in source_edges.items():
            used = remaining_source[sid] - edge.cap
            if used > EPS:
                by_source[sid] = round(used, 9)
        entry["by_source"] = by_source

        # 扣除已占用资源（不得对后续点重复占用）
        for sid, edge in source_edges.items():
            remaining_source[sid] = max(0.0, edge.cap)
        for pid, edge in pipe_edges.items():
            remaining_pipe[pid] = max(0.0, edge.cap)

        if entry["fulfilled"]:
            entry["shortfall_reason_code"] = None
            entry["shortfall_reason"] = None
        else:
            # 最小割分类：源侧割到汇侧的饱和标签边即瓶颈
            reachable = dinic.reachable_from_source(super_source)
            sat_sources = [
                sid for sid, e in source_edges.items()
                if index[sid] not in reachable and e.cap <= EPS
            ]
            # 瓶颈判定：
            #  1) 割上（一端可达、一端不可达）的饱和标签边 = 决定性瓶颈；
            #  2) 若割上没有管段瓶颈，则把“已流满”的管段边也作为受限管段
            #     （典型：水源与下游管段容量恰好相等、同时打满）。
            sat_cut_pipes: list[str] = []
            saturated_pipes: list[str] = []
            for pid, edge in pipe_edges.items():
                pipe = network.pipes[pid]
                u_in = index[pipe.from_node] in reachable
                v_in = index[pipe.to_node] in reachable
                if edge.cap <= EPS:
                    saturated_pipes.append(pid)
                    if u_in and not v_in:
                        sat_cut_pipes.append(pid)
            sat_cut_pipes.sort()
            saturated_pipes.sort()
            constrained_pids = sat_cut_pipes or saturated_pipes

            entry["constrained_pipes"] = [
                _pipe_constraint(network, pid, remaining_pipe, force_closed)
                for pid in constrained_pids
            ]

            has_source_budget = any(
                c > EPS for c in remaining_source.values()
            )
            if sat_sources and constrained_pids:
                code = "SOURCE_AND_PIPE_LIMIT"
                reason = (
                    f"用水点 {node.id} 欠供 {entry['shortfall']:.3f}："
                    f"水源 {', '.join(sorted(sat_sources))} 供水上限与共享管段 "
                    f"{', '.join(constrained_pids)} 容量同时达到瓶颈"
                )
            elif sat_sources:
                code = "SOURCE_LIMIT"
                reason = (
                    f"用水点 {node.id} 欠供 {entry['shortfall']:.3f}："
                    f"水源 {', '.join(sorted(sat_sources))} 供水上限已占满"
                )
            elif constrained_pids:
                code = "PIPE_LIMIT"
                reason = (
                    f"用水点 {node.id} 欠供 {entry['shortfall']:.3f}："
                    f"共享管段 {', '.join(constrained_pids)} 容量已占满"
                )
            elif not has_source_budget:
                code = "SOURCE_LIMIT"
                reason = (
                    f"用水点 {node.id} 欠供 {entry['shortfall']:.3f}："
                    "所有可用水源供水上限均已被高优先级用水点占用"
                )
            else:  # 容量被高优先级点占尽导致割边均为已占资源
                code = "PIPE_LIMIT"
                reason = (
                    f"用水点 {node.id} 欠供 {entry['shortfall']:.3f}："
                    "通往该点的共享管段剩余容量已被高优先级用水点占用"
                )
            entry["shortfall_reason_code"] = code
            entry["shortfall_reason"] = reason

        allocations.append(entry)

    # 管段上实际关闭的阀门（库内关闭 + 预演拟关闭）
    closed_valves = sorted(
        vid
        for vid, v in network.valves.items()
        if (not v.is_open) or vid in force_closed
    )

    return {
        "closed_valves": closed_valves,
        "simulated_closed_valves": sorted(force_closed),
        "allocations": allocations,
        "totals": {
            "requested": round(sum(a["requested"] for a in allocations), 9),
            "supplied": round(sum(a["supplied"] for a in allocations), 9),
            "shortfall": round(sum(a["shortfall"] for a in allocations), 9),
        },
        "source_utilization": [
            {
                "source_id": sid,
                "capacity": network.nodes[sid].capacity or 0.0,
                "used": round((network.nodes[sid].capacity or 0.0) - remaining_source[sid], 9),
                "remaining": round(remaining_source[sid], 9),
            }
            for sid in network.source_ids()
        ],
        "pipe_utilization": [
            {
                "pipe_id": pid,
                "capacity": pipe.capacity,
                "used": round(pipe.capacity - remaining_pipe[pid], 9),
                "remaining": round(remaining_pipe[pid], 9),
                "conducting": network.pipe_is_open(pipe, force_closed),
            }
            for pid, pipe in sorted(network.pipes.items())
        ],
    }


def _pipe_constraint(
    network: Network,
    pid: str,
    remaining_pipe: dict[str, float],
    force_closed: set[str],
) -> dict:
    pipe = network.pipes[pid]
    closed = sorted(
        vid
        for vid in network.valves_by_pipe.get(pid, ())
        if vid in force_closed or not network.valves[vid].is_open
    )
    return {
        "pipe_id": pid,
        "from_node": pipe.from_node,
        "to_node": pipe.to_node,
        "capacity": pipe.capacity,
        "remaining_capacity": round(remaining_pipe.get(pid, pipe.capacity), 9),
        "closed_valves": closed,
    }


def _isolation_constraints(
    network: Network, demand_id: str, force_closed: set[str]
) -> list[dict]:
    """隔离场景下找出“切断它”的管段：一端在水源可达区、一端能到达该点。

    这些管段因阀门关闭（预演或库内状态）而不导通，是隔离的直接位置。
    """
    src_reach = _directed_reachability(
        network, network.source_ids(), force_closed, reverse=False
    )
    can_reach_demand = _directed_reachability(
        network, [demand_id], force_closed, reverse=True
    )
    out: list[dict] = []
    for pid, pipe in network.pipes.items():
        if network.pipe_is_open(pipe, force_closed):
            continue
        if pipe.from_node in src_reach and pipe.to_node in can_reach_demand:
            closed = sorted(
                vid
                for vid in network.valves_by_pipe.get(pid, ())
                if vid in force_closed or not network.valves[vid].is_open
            )
            out.append({
                "pipe_id": pid,
                "from_node": pipe.from_node,
                "to_node": pipe.to_node,
                "capacity": pipe.capacity,
                "remaining_capacity": pipe.capacity,
                "closed_valves": closed,
            })
    return out
