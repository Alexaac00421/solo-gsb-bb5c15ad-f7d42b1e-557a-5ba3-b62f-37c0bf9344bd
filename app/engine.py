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
# 下界流变换中 T->S 回流边使用的“无限”容量：远大于全管网容量之和即可
INF_FLOW = 1e15


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

    def sink_side(self, t: int) -> set[int]:
        """最大流后“最靠近汇”的最小割汇侧集合。

        从汇 t 出发，仅沿**原始正向边**的反向方向（即存在一条到当前点、
        且其残余容量仍 >0 的正向边）遍历。注意不能走 add_edge 自动添加的
        残余反向边（其 tag 为 None）——那是推送路径的撤销通道，不是物理管段。
        取到的汇侧割会把瓶颈尽量推到下游设施（水源与管段同时饱和时，
        能定位到真正卡住输送的共享管段）。
        """
        # 反向邻接：pred[v] = [(u, edge), ...] 仅统计原始正向边
        pred: list[list[tuple[int, "_Edge"]]] = [[] for _ in range(self.n)]
        for u in range(self.n):
            for e in self.g[u]:
                if e.tag is not None:
                    pred[e.to].append((u, e))
        seen = {t}
        q = deque([t])
        while q:
            v = q.popleft()
            for u, e in pred[v]:
                if e.cap > EPS and u not in seen:
                    seen.add(u)
                    q.append(u)
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


# --------------------------------------------------------------------------- #
# 最低保障供水预演（带流量下界的最大流 + 保保障的逐点字典序最大化）
# --------------------------------------------------------------------------- #
@dataclass
class _LBRef:
    """下界流网络中一条带标签边的运行期引用（用于回溯实际流量/是否割边）。"""

    cap_edge: _Edge          # Dinic 中容量为 (upper-lower) 的正向边
    u: int                   # 正向边起点（物理节点或超汇）
    v: int                   # 正向边终点（物理节点或超源）
    lower: float             # 流量下界
    span: float              # upper - lower
    tag: dict                # {'kind': 'source'|'pipe'|'demand', 'id': ...}

    @property
    def flow(self) -> float:
        """实际流量 = 下界 + 已推送的增量（正向残余 = span - 已用）。"""
        return self.lower + (self.span - self.cap_edge.cap)

    @property
    def saturated(self) -> bool:
        return self.flow + EPS >= self.lower + self.span


def _guarantee_index(network: Network) -> dict[str, int]:
    index = {nid: i for i, nid in enumerate(network.nodes)}
    index["__S__"] = len(index)
    index["__T__"] = index["__S__"] + 1
    index["__SS__"] = index["__S__"] + 2
    index["__TT__"] = index["__S__"] + 3
    return index


def _add_lb_edge(
    dinic: Dinic,
    u: int,
    v: int,
    lower: float,
    upper: float,
    tag: dict,
    balance: list[float],
) -> _LBRef:
    """添加一条下界流量边 u -> v（lower <= f <= upper）。

    按标准下界流变换：添加容量 upper-lower 的变换边；节点平衡取
    balance[x] = Σ(下界入边) − Σ(下界出边)。balance>0 的点由超级源
    SS 补入，balance<0 的点排向超级汇 TT（见 _feasible_circulation）。
    """
    span = max(0.0, upper - lower)
    e_idx = len(dinic.g[u])
    dinic.add_edge(u, v, span, tag)
    balance[v] += lower
    balance[u] -= lower
    return _LBRef(dinic.g[u][e_idx], u, v, lower, span, tag)


def _feasible_circulation(
    dinic: Dinic, balance: list[float], ss: int, tt: int
) -> bool:
    """在已加入 T->S 无限回流边的变换网络上求可行环流。

    balance[u] > 0（下界净流入）由 SS->u 补入；
    balance[u] < 0（下界净流出）经 u->TT 排出。所有 SS 出边打满才可行。
    """
    required = 0.0
    for u, b in enumerate(balance):
        if b > EPS:
            dinic.add_edge(ss, u, b)
            required += b
        elif b < -EPS:
            dinic.add_edge(u, tt, -b)
    flow = dinic.max_flow(ss, tt)
    return flow + EPS >= required


def _guarantee_base_graph(
    network: Network,
    index: dict[str, int],
    demand_lower: dict[str, float],
    demand_upper: Optional[dict[str, float]] = None,
    add_return: bool = True,
) -> tuple[Dinic, list[float], dict[str, _LBRef], dict[str, _LBRef],
           dict[str, _LBRef], Optional[_Edge]]:
    """构造保障预演的下界流网络。

    - 水源边 S超源->水源：下界 0，上界=水源供水上限；
    - 导通管段边：下界 0，上界=管段容量（当前阀位，不叠加拟关阀门）；
    - 用水点边 用水点->T超汇：下界=保障量，上界由 demand_upper 给出
      （缺省为该点现有需求）。

    add_return=True 时加入 T->S 无限回流边（下界可行环流需要）；
    阶段一的普通 s-t 最大流必须置 False——否则流满后回流边的反向残余
    （S->T）会让超源可达超汇，污染最小割的源侧集合。
    """
    s = index["__S__"]
    t = index["__T__"]
    tt = index["__TT__"]
    dinic = Dinic(tt + 1)
    balance = [0.0] * (tt + 1)

    source_refs: dict[str, _LBRef] = {}
    pipe_refs: dict[str, _LBRef] = {}
    demand_refs: dict[str, _LBRef] = {}

    for sid in network.source_ids():
        cap = network.nodes[sid].capacity or 0.0
        ref = _add_lb_edge(
            dinic, s, index[sid], 0.0, cap,
            {"kind": "source", "id": sid}, balance,
        )
        source_refs[sid] = ref

    for pid, pipe in network.pipes.items():
        if network.pipe_is_open(pipe, None):
            ref = _add_lb_edge(
                dinic, index[pipe.from_node], index[pipe.to_node],
                0.0, pipe.capacity, {"kind": "pipe", "id": pid}, balance,
            )
            pipe_refs[pid] = ref

    for node in (network.nodes[nid] for nid in network.demand_ids()):
        lower = demand_lower.get(node.id, 0.0)
        upper = (
            node.demand or 0.0 if demand_upper is None
            else demand_upper[node.id]
        )
        ref = _add_lb_edge(
            dinic, index[node.id], t, lower, upper,
            {"kind": "demand", "id": node.id}, balance,
        )
        demand_refs[node.id] = ref

    ret_edge: Optional[_Edge] = None
    if add_return:
        # 无限容量回流边 T -> S，把超源/超汇接入环流
        ret_idx = len(dinic.g[t])
        dinic.add_edge(t, s, INF_FLOW, {"kind": "return"})
        ret_edge = dinic.g[t][ret_idx]
    return dinic, balance, source_refs, pipe_refs, demand_refs, ret_edge


def _ordered_demands(network: Network) -> list[Node]:
    """优先级降序、同级按编号升序（与 simulate 同一口径）。"""
    return sorted(
        (network.nodes[nid] for nid in network.demand_ids()),
        key=lambda n: (-(n.priority or 0), n.id),
    )


def _zero_reflow_edge(dinic: Dinic, edge: _Edge) -> None:
    """可行环流求出后把 T->S 回流边及其反向边容量清零，断开短路。"""
    rev = dinic.g[edge.to][edge.rev]
    edge.cap = 0.0
    rev.cap = 0.0


def _decompose_by_source(
    network: Network,
    index: dict[str, int],
    source_refs: dict[str, _LBRef],
    pipe_refs: dict[str, _LBRef],
    demand_refs: dict[str, _LBRef],
) -> dict[str, dict[str, float]]:
    """把最终流分解为 用水点 -> {水源: 水量}。

    先沿管段流量做环消除（环流不服务任何用水点，可直接扣除），
    再用带剩余量标记的边从水源逐条 DFS 至用水点。
    """
    # 有向流图：仅管段参与（超源/超汇边的归属已由源边、需求边直接确定）
    adj: dict[str, list[dict]] = {nid: [] for nid in network.nodes}
    for pid, ref in pipe_refs.items():
        f = max(0.0, ref.flow)
        if f > EPS:
            pipe = network.pipes[pid]
            adj[pipe.from_node].append(
                {"to": pipe.to_node, "pid": pid, "f": f}
            )

    def _cancel_cycles() -> None:
        """反复用 DFS 找出有向环并按最小边流量扣减，直至无环（环流不供水）。"""
        color: dict[str, int] = {}
        stack: list[str] = []
        edge_stack: list[dict] = []

        def dfs(u: str) -> None:
            color[u] = 1
            stack.append(u)
            for e in adj[u]:
                if e["f"] <= EPS:
                    continue
                v = e["to"]
                if color.get(v, 0) == 0:
                    edge_stack.append(e)
                    dfs(v)
                    edge_stack.pop()
                elif color.get(v, 0) == 1:
                    # 回边：stack 中 v..u + 边 e 构成环
                    i0 = stack.index(v)
                    cycle_edges = edge_stack[i0:] + [e]
                    cut = min(ce["f"] for ce in cycle_edges)
                    for ce in cycle_edges:
                        ce["f"] -= cut

            stack.pop()
            color[u] = 2

        for nid in network.nodes:
            if color.get(nid, 0) == 0:
                dfs(nid)

    _cancel_cycles()

    by_source: dict[str, dict[str, float]] = {
        did: {} for did in network.demand_ids()
    }
    # 水源可用量（超源源边的实际流量）
    src_avail = {sid: max(0.0, ref.flow) for sid, ref in source_refs.items()}
    # 用水点尚待归因的入流量（需求边实际流量）
    dem_need = {did: max(0.0, ref.flow) for did, ref in demand_refs.items()}

    def send(u: str, amount: float, origin: str) -> float:
        """从 u 沿带流量的边向下推送 amount 水量，返回实际推送量。"""
        if amount <= EPS:
            return 0.0
        node = network.nodes.get(u)
        if node is not None and node.type == "demand":
            take = min(amount, dem_need.get(u, 0.0))
            if take > EPS:
                dem_need[u] -= take
                prev = by_source[u].get(origin, 0.0)
                by_source[u][origin] = prev + take
            return take
        pushed = 0.0
        for e in adj.get(u, ()):
            if amount - pushed <= EPS:
                break
            if e["f"] <= EPS:
                continue
            d = send(e["to"], min(amount - pushed, e["f"]), origin)
            if d > EPS:
                e["f"] -= d
                pushed += d
        return pushed

    for sid in sorted(src_avail):
        remaining = src_avail[sid]
        while remaining > EPS:
            got = send(sid, remaining, sid)
            if got <= EPS:
                break
            remaining -= got

    # 汇总清洗（极小浮点误差归零）；并让每点归因合计与该点需求边实际
    # 流量严格一致（把分解残差按比例并入占比最大的水源）。
    cleaned: dict[str, dict[str, float]] = {}
    for did, m in by_source.items():
        rows = {s: max(0.0, v) for s, v in m.items() if v > EPS}
        if rows:
            total = sum(rows.values())
            target = max(0.0, demand_refs[did].flow)
            if abs(total - target) <= 1e-6 and target > EPS:
                biggest = max(rows, key=lambda k: rows[k])
                rows[biggest] += target - total
        cleaned[did] = {
            s: round(v, 9) for s, v in sorted(rows.items()) if v > EPS
        }
    return cleaned


def guarantee_preview(
    network: Network, guarantees: dict[str, float]
) -> dict:
    """最低保障供水预演主算法（只读，不写入状态）。

    第一步：在“当前阀位、单向管段、水源上限、共享管段容量”下做一次
    **联合最大流**（每个用水点到超汇的上界=保障量），判定全部保障量
    能否同时满足。不能时只返回缺口与瓶颈设施（不出部分成功方案）。

    第二步：可行时按优先级降序、同级编号升序逐点最大化——第 i 步把
    已处理点的总供水固定为下界/上界（重排路径但总量只增不减），
    未处理点固定为保障量，当前点上界放到需求，用带下界最大流求其
    最大可获得水量。
    """
    index = _guarantee_index(network)
    s = index["__S__"]
    t = index["__T__"]
    ss = index["__SS__"]
    tt = index["__TT__"]
    ordered = _ordered_demands(network)
    demands_need = {n.id: (n.demand or 0.0) for n in ordered}

    # 可达性（忽略容量，只看当前阀位与单向拓扑）
    can_reach = _directed_reachability(
        network, network.source_ids(), set(), reverse=False
    )
    isolated = {
        n.id for n in ordered
        if (demands_need[n.id] > EPS or guarantees.get(n.id, 0.0) > EPS)
        and n.id not in can_reach
    }

    # ---------------------------------------------------------------- #
    # 阶段一：保障量联合可行性（每条需求边上界=保障量的联合最大流）
    # ---------------------------------------------------------------- #
    g0 = _guarantee_base_graph(
        network, index, {},
        demand_upper={did: guarantees.get(did, 0.0) for did in demands_need},
        add_return=False,
    )
    dinic0, _bal0, sref0, pref0, dref0, _ret0 = g0

    total_guarantee = sum(guarantees.get(n.id, 0.0) for n in ordered)
    total_flow0 = dinic0.max_flow(s, t)
    feasible = total_flow0 + EPS >= total_guarantee

    if not feasible:
        return _infeasible_result(
            network, index, ordered, guarantees, demands_need, isolated,
            dinic0, sref0, pref0, dref0,
        )

    # ---------------------------------------------------------------- #
    # 阶段二：保保障的逐点字典序最大化（下界流；允许重排先前路径）
    # ---------------------------------------------------------------- #
    locked: dict[str, float] = {}   # 已处理点最终总供水
    if not ordered:
        # 无用水点：联合可行性必为真（总保障 0），直接返回零结果
        return _feasible_result(
            network, index, ordered, guarantees, demands_need,
            dinic0, sref0, pref0, dref0,
        )
    last: Optional[tuple] = None
    for node in ordered:
        lower: dict[str, float] = {}
        upper: dict[str, float] = {}
        for other in ordered:
            oid = other.id
            if oid in locked:
                # 已处理点：总量固定（下界=上界），路径可重排
                lower[oid] = locked[oid]
                upper[oid] = locked[oid]
            elif oid == node.id:
                # 当前点：保障量为下界，需求为上界
                g = guarantees.get(oid, 0.0)
                lower[oid] = g
                upper[oid] = demands_need[oid]
            else:
                # 未处理点：先固定为保障量
                g = guarantees.get(oid, 0.0)
                lower[oid] = g
                upper[oid] = g
        dinic, balance, sref, pref, dref, ret_edge = _guarantee_base_graph(
            network, index, lower, demand_upper=upper
        )
        ok = _feasible_circulation(dinic, balance, ss, tt)
        # 已在阶段一判定全局可行，且每步约束都不超出可行域，理论上必可行
        if not ok:  # pragma: no cover - 防御性分支
            return _infeasible_result(
                network, index, ordered, guarantees, demands_need, isolated,
                dinic0, sref0, pref0, dref0, total_flow0,
            )
        _zero_reflow_edge(dinic, ret_edge)
        dinic.max_flow(s, t)  # 在不破坏任何下界的前提下把当前点推到最大
        locked[node.id] = min(
            max(0.0, dref[node.id].flow), demands_need[node.id]
        )
        last = (dinic, sref, pref, dref)

    assert last is not None
    dinic_f, sref_f, pref_f, dref_f = last
    return _feasible_result(
        network, index, ordered, guarantees, demands_need,
        dinic_f, sref_f, pref_f, dref_f,
    )


def _bottleneck_pipe_row(
    network: Network, pref0: dict[str, "_LBRef"], pid: str
) -> dict:
    """构造一条瓶颈管段的统一字段（容量/已用/剩余/导通/关闭阀门）。"""
    pipe = network.pipes[pid]
    ref = pref0.get(pid)
    used = max(0.0, ref.flow) if ref is not None else 0.0
    closed = sorted(
        vid
        for vid in network.valves_by_pipe.get(pid, ())
        if not network.valves[vid].is_open
    )
    return {
        "pipe_id": pid,
        "from_node": pipe.from_node,
        "to_node": pipe.to_node,
        "capacity": pipe.capacity,
        "used": round(used, 9),
        "remaining": round(max(0.0, pipe.capacity - used), 9),
        "remaining_capacity": round(max(0.0, pipe.capacity - used), 9),
        "conducting": network.pipe_is_open(pipe, None),
        "closed_valves": closed,
    }


def _bottleneck_pipe_rows(
    network: Network,
    index: dict[str, int],
    pref0: dict[str, "_LBRef"],
    sink_side_idx: set[int],
) -> list[dict]:
    """最靠近汇的最小割上（非汇侧 -> 汇侧）的饱和管段即决定性共享瓶颈。"""
    rows: list[dict] = []
    for pid, ref in pref0.items():
        pipe = network.pipes[pid]
        if (
            index[pipe.from_node] not in sink_side_idx
            and index[pipe.to_node] in sink_side_idx
            and ref.saturated
        ):
            rows.append(_bottleneck_pipe_row(network, pref0, pid))
    rows.sort(key=lambda r: r["pipe_id"])
    return rows


def _infeasible_result(
    network: Network,
    index: dict[str, int],
    ordered: list[Node],
    guarantees: dict[str, float],
    demands_need: dict[str, float],
    isolated: set[str],
    dinic0: Dinic,
    sref0: dict[str, _LBRef],
    pref0: dict[str, _LBRef],
    dref0: dict[str, _LBRef],
) -> dict:
    """不可行：只返回保障缺口与瓶颈设施，不返回部分成功方案。"""
    t = index["__T__"]
    # 取最靠近汇的最小割：割边是真正卡住下游输送的饱和设施
    sink_side_idx = dinic0.sink_side(t)

    # 各点保障量下联合最大流能交付的水量（用于量化缺口，非供水方案）
    delivered = {did: max(0.0, dref0[did].flow) for did in demands_need}

    # 全局瓶颈：最小割（非汇侧 -> 汇侧）上的饱和水源边/管段边
    cut_pipe_rows = _bottleneck_pipe_rows(
        network, index, pref0, sink_side_idx
    )
    cut_pipes = [r["pipe_id"] for r in cut_pipe_rows]
    cut_sources = sorted(
        sid for sid, ref in sref0.items()
        if index[sid] in sink_side_idx and ref.saturated
    )
    sink_side_idx_set = {
        nid for nid, i in index.items()
        if i in sink_side_idx and not nid.startswith("__")
    }

    # 沿“物理网络中仍有残余容量的导通管段”反向计算可达性：
    # 从某用水点逆流而上，能推回到哪些（饱和）瓶颈水源边/管段边的头端。
    # 这给出真正卡住该点的上游设施——水源边已饱和、但它后面的管段仍有
    # 残余容量时（如小水源+大管），可正确判为 SOURCE_LIMIT 而非 PIPE_LIMIT。
    residual_pred: dict[str, list[str]] = {nid: [] for nid in network.nodes}
    for pid, ref in pref0.items():
        if ref.cap_edge.cap > EPS:  # 该管段仍有残余容量
            pipe = network.pipes[pid]
            residual_pred[pipe.to_node].append(pipe.from_node)

    def residual_reachable(start: str) -> set[str]:
        seen = {start}
        q = deque([start])
        while q:
            u = q.popleft()
            for v in residual_pred.get(u, ()):
                if v not in seen:
                    seen.add(v)
                    q.append(v)
        return seen

    # 在汇侧（sink side）区域内沿所有导通管段（含饱和管段）正向传播。
    # 饱和割管段 u->v 把汇侧区域与上游隔开；从其头端 v 顺导通管段能到达的
    # 缺口点都受它制约——即使中途还要穿过其它饱和管段（例如干管 P2 饱和、
    # 支管 P3 也饱和时，D2 同时受 P2 与 P3 制约）。
    conducting_fwd: dict[str, list[str]] = {nid: [] for nid in network.nodes}
    for pid, pipe in network.pipes.items():
        if network.pipe_is_open(pipe, None):
            conducting_fwd[pipe.from_node].append(pipe.to_node)

    def forward_reachable_in_sinkside(start: str) -> set[str]:
        if start not in sink_side_idx_set:
            return set()
        seen = {start}
        q = deque([start])
        while q:
            u = q.popleft()
            for v in conducting_fwd.get(u, ()):
                if v in sink_side_idx_set and v not in seen:
                    seen.add(v)
                    q.append(v)
        return seen

    # 汇侧区域内每条饱和管段（全局割管段或点自身的饱和入流管）制约的用水点
    saturated_pipes_in_sinkside = sorted(
        pid for pid, ref in pref0.items()
        if ref.saturated and network.pipes[pid].to_node in sink_side_idx_set
    )
    pipe_affected: dict[str, set[str]] = {}
    for pid in saturated_pipes_in_sinkside:
        head = network.pipes[pid].to_node
        reachable = forward_reachable_in_sinkside(head)
        pipe_affected[pid] = {
            nid for nid in reachable
            if network.nodes[nid].type == "demand"
        }

    rows: list[dict] = []
    for node in ordered:
        g = round(guarantees.get(node.id, 0.0), 9)
        need = demands_need[node.id]
        got = round(min(delivered.get(node.id, 0.0), g), 9)
        gap = round(max(0.0, g - delivered.get(node.id, 0.0)), 9)
        row = {
            "demand_id": node.id,
            "priority": node.priority or 0,
            "demand": need,
            "guarantee": g,
            "deliverable": got,
            "shortfall": gap,
            "guarantee_met": gap <= EPS,
        }
        if node.id in isolated:
            row["shortfall_reason_code"] = "ISOLATED"
            row["shortfall_reason"] = (
                f"用水点 {node.id} 的保障量无法满足：该点已被隔离，"
                "无任何水源经开启管段可达"
            )
            row["constrained_pipes"] = _isolation_constraints(
                network, node.id, set()
            )
        elif gap > EPS:
            reach = residual_reachable(node.id)
            src_bottlenecks = sorted(
                sid for sid in cut_sources if sid in reach
            )
            # 汇侧区域内制约该点的饱和管段（全局割管段 + 该点的饱和入流管）
            pipe_bottlenecks = sorted(
                pid for pid, affected in pipe_affected.items()
                if node.id in affected
            )
            # 回退：该点自身就在割上且需求边饱和（无残余通路可定位）
            if not src_bottlenecks and not pipe_bottlenecks:
                if index[node.id] in sink_side_idx and dref0[node.id].saturated:
                    src_bottlenecks = list(cut_sources)
                    pipe_bottlenecks = list(cut_pipes)
            if src_bottlenecks and pipe_bottlenecks:
                code = "SOURCE_AND_PIPE_LIMIT"
                reason = (
                    f"用水点 {node.id} 保障缺口 {gap:.3f}：水源 "
                    f"{', '.join(src_bottlenecks)} 上限与共享管段 "
                    f"{', '.join(pipe_bottlenecks)} 容量同时达到瓶颈"
                )
            elif src_bottlenecks:
                code = "SOURCE_LIMIT"
                reason = (
                    f"用水点 {node.id} 保障缺口 {gap:.3f}：水源 "
                    f"{', '.join(src_bottlenecks)} 供水上限达到瓶颈"
                )
            elif pipe_bottlenecks:
                code = "PIPE_LIMIT"
                reason = (
                    f"用水点 {node.id} 保障缺口 {gap:.3f}：共享管段 "
                    f"{', '.join(pipe_bottlenecks)} 容量达到瓶颈"
                )
            else:
                code = "PIPE_LIMIT"
                reason = (
                    f"用水点 {node.id} 保障缺口 {gap:.3f}："
                    "现有阀位下单向/共享管段容量不足以同时输送全部保障量"
                )
            row["shortfall_reason_code"] = code
            row["shortfall_reason"] = reason
            remaining_pipe = {
                pid: max(0.0, network.pipes[pid].capacity - pref0[pid].flow)
                for pid in pref0
            }
            row["constrained_pipes"] = [
                _pipe_constraint(network, pid, remaining_pipe, set())
                for pid in pipe_bottlenecks
            ]
        else:
            row["shortfall_reason_code"] = None
            row["shortfall_reason"] = None
            row["constrained_pipes"] = []
        rows.append(row)

    # 全局瓶颈设施：最小割上的饱和水源边与饱和管段边
    bottleneck_sources = [
        {
            "source_id": sid,
            "capacity": network.nodes[sid].capacity or 0.0,
            "used": round(max(0.0, ref.flow), 9),
            "remaining": round(
                max(0.0, (network.nodes[sid].capacity or 0.0) - ref.flow), 9
            ),
            "saturated": ref.saturated,
        }
        for sid, ref in sorted(sref0.items())
        if index[sid] in sink_side_idx and ref.saturated
    ]
    # 缺口点（用于判定某饱和管段是否真的卡住了某个有缺口的用水点）
    gap_ids = {row["demand_id"] for row in rows if row["shortfall"] > EPS}

    bottleneck_pipes = list(cut_pipe_rows)
    # 汇侧区域内还可能存在“割内”饱和支管（如干管 P2 饱和、支管 P3 也饱和）；
    # 只要它制约到某个有缺口的用水点，也作为瓶颈设施返回。
    cut_pipe_ids = {r["pipe_id"] for r in cut_pipe_rows}
    for pid in saturated_pipes_in_sinkside:
        if pid in cut_pipe_ids:
            continue
        if pipe_affected.get(pid, set()) & gap_ids:
            bottleneck_pipes.append(
                _bottleneck_pipe_row(network, pref0, pid)
            )
    # 隔离点的关闭管段同样是保障不可行的直接设施位置
    iso_pipes: list[dict] = []
    seen_pids = {r["pipe_id"] for r in bottleneck_pipes}
    for did in sorted(isolated):
        for r in _isolation_constraints(network, did, set()):
            if r["pipe_id"] not in seen_pids:
                seen_pids.add(r["pipe_id"])
                r = dict(r)
                r["used"] = 0.0
                r["remaining"] = r["remaining_capacity"]
                r["conducting"] = False
                iso_pipes.append(r)
    bottleneck_pipes.extend(iso_pipes)
    bottleneck_pipes.sort(key=lambda r: r["pipe_id"])

    total_guarantee = sum(guarantees.get(n.id, 0.0) for n in ordered)
    total_flow0 = sum(delivered.values())
    total_gap = round(max(0.0, total_guarantee - total_flow0), 9)
    return {
        "feasible": False,
        "revision": None,  # 由路由层填入
        "message": (
            "保障预演：全部保障量无法在当前阀位、单向管段、水源上限与共享管段容量下"
            "同时满足；以下仅给出保障缺口与瓶颈设施，未返回部分成功方案，"
            "且未改变管网、阀位、计划或修订号"
        ),
        "guarantees": [
            {"demand_id": n.id, "guarantee": round(guarantees.get(n.id, 0.0), 9)}
            for n in ordered
        ],
        "allocations": rows,
        "totals": {
            "guarantee": round(total_guarantee, 9),
            "deliverable": round(min(total_flow0, total_guarantee), 9),
            "shortfall": total_gap,
        },
        "bottleneck_sources": bottleneck_sources,
        "bottleneck_pipes": bottleneck_pipes,
    }


def _feasible_result(
    network: Network,
    index: dict[str, int],
    ordered: list[Node],
    guarantees: dict[str, float],
    demands_need: dict[str, float],
    dinic_f: Dinic,
    sref_f: dict[str, _LBRef],
    pref_f: dict[str, _LBRef],
    dref_f: dict[str, _LBRef],
) -> dict:
    """可行：逐点字典序最大化后的完整保障/供水/欠供与占用。"""
    by_source = _decompose_by_source(
        network, index, sref_f, pref_f, dref_f
    )

    rows: list[dict] = []
    for node in ordered:
        g = round(guarantees.get(node.id, 0.0), 9)
        need = demands_need[node.id]
        supplied = round(min(max(0.0, dref_f[node.id].flow), need), 9)
        shortfall = round(max(0.0, need - supplied), 9)
        rows.append({
            "demand_id": node.id,
            "priority": node.priority or 0,
            "requested": need,
            "guarantee": g,
            "supplied": supplied,
            "extra_supplied": round(max(0.0, supplied - g), 9),
            "shortfall": shortfall,
            "fulfilled": supplied + EPS >= need,
            "guarantee_met": supplied + EPS >= g,
            "by_source": by_source.get(node.id, {}),
        })

    source_utilization = [
        {
            "source_id": sid,
            "capacity": network.nodes[sid].capacity or 0.0,
            "used": round(min(
                max(0.0, sref_f[sid].flow),
                network.nodes[sid].capacity or 0.0,
            ), 9),
            "remaining": round(max(
                0.0,
                (network.nodes[sid].capacity or 0.0) - sref_f[sid].flow,
            ), 9),
        }
        for sid in network.source_ids()
    ]
    pipe_utilization = [
        {
            "pipe_id": pid,
            "from_node": pipe.from_node,
            "to_node": pipe.to_node,
            "capacity": pipe.capacity,
            "used": round(min(max(0.0, pref_f[pid].flow), pipe.capacity), 9)
            if pid in pref_f else 0.0,
            "remaining": round(max(
                0.0,
                pipe.capacity - (pref_f[pid].flow if pid in pref_f else 0.0),
            ), 9),
            "conducting": network.pipe_is_open(pipe, None),
        }
        for pid, pipe in sorted(network.pipes.items())
    ]

    total_guarantee = round(sum(guarantees.get(n.id, 0.0) for n in ordered), 9)
    return {
        "feasible": True,
        "revision": None,  # 由路由层填入
        "message": (
            "保障预演：全部保障量可同时满足；已在保持所有保障量的前提下，"
            "按优先级降序、同级编号升序逐点最大化额外供水（允许重排先前流量路径），"
            "本次结果为临时计算，未改变管网、阀位、计划或修订号"
        ),
        "guarantees": [
            {"demand_id": n.id, "guarantee": round(guarantees.get(n.id, 0.0), 9)}
            for n in ordered
        ],
        "allocations": rows,
        "totals": {
            "requested": round(sum(demands_need.values()), 9),
            "guarantee": total_guarantee,
            "supplied": round(sum(r["supplied"] for r in rows), 9),
            "extra_supplied": round(sum(r["extra_supplied"] for r in rows), 9),
            "shortfall": round(sum(r["shortfall"] for r in rows), 9),
        },
        "source_utilization": source_utilization,
        "pipe_utilization": pipe_utilization,
    }
