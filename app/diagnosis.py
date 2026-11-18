"""水位数据质量诊断：冲突检测与最小复核成本方案求解。

业务含义：
- 输入固定某一网格版本的每格水位与正整数复核成本；
- 仅共享非零长度边界（共边）的网格才算相邻；
- 相邻两格的水位差严格超过阈值即为冲突，每条冲突至少复核其中一端；
- 因此“需要复核的网格集合”就是冲突图上的顶点覆盖，目标是求总复核成本
  最低的一组（最小权顶点覆盖），并在所有最优方案中区分必选格与可选格。

求解方式：已发布版本网格数上限为 20，冲突图顶点数 k ≤ 20，可直接用
O(2^k) 子集 DP 精确枚举，不依赖近似算法，也不会把“一组方案”误当成唯一结论。
为控制内存，DP 改写为对偶的最大权独立集问题（顶点覆盖的补集是独立集，
且总成本 = 全部网格成本之和 − 独立集权值，最大权独立集与最小权覆盖一一对应）。
"""
from __future__ import annotations

from array import array
from collections.abc import Mapping, Sequence

# 独立集位标 bytearray 最大 2^20 ≈ 1MB；成本表 uint64 最大 8MB。
# 成本超过 uint64 时自动退回 Python 整数列表（牺牲部分内存换正确性）。
_UINT64_MAX = (1 << 64) - 1


def detect_conflicts(
    levels: Mapping[str, float],
    costs: Mapping[str, int],
    adjacency: Mapping[str, set[str]],
    threshold: float,
) -> list[dict]:
    """按邻接关系找出水位差严格超过阈值的冲突对。

    返回按 (grid_id_a, grid_id_b) 字典序排序的冲突清单，每条带上两端水位、
    差值与各自复核成本，保证输出确定。
    """
    conflicts: list[dict] = []
    for grid_id, neighbors in adjacency.items():
        for other in neighbors:
            if grid_id >= other:
                continue
            diff = abs(levels[grid_id] - levels[other])
            if diff > threshold:
                a, b = sorted((grid_id, other))
                conflicts.append(
                    {
                        "grid_id_a": a,
                        "grid_id_b": b,
                        "level_a": levels[a],
                        "level_b": levels[b],
                        "difference": diff,
                        "cost_a": costs[a],
                        "cost_b": costs[b],
                    }
                )
    conflicts.sort(key=lambda c: (c["grid_id_a"], c["grid_id_b"]))
    return conflicts


def _build_conflict_graph(
    conflict_vertices: Sequence[str],
    conflicts: Sequence[dict],
) -> list[int]:
    """把冲突对压缩到 0..k-1 的位掩码邻接表。"""
    index = {grid_id: i for i, grid_id in enumerate(conflict_vertices)}
    neighbors = [0] * len(conflict_vertices)
    for edge in conflicts:
        ia = index[edge["grid_id_a"]]
        ib = index[edge["grid_id_b"]]
        neighbors[ia] |= 1 << ib
        neighbors[ib] |= 1 << ia
    return neighbors


def min_weight_vertex_cover(
    conflict_vertices: Sequence[str],
    conflicts: Sequence[dict],
    costs: Mapping[str, int],
) -> dict:
    """求冲突图的全部最小权顶点覆盖方案。

    返回：
    - minimum_cost：任一最优方案的总复核成本；
    - solution_count：最优方案数量（至少 1；无冲突时为 1，即空方案）；
    - mandatory：所有最优方案都包含的格（必选格）；
    - optional：仅部分最优方案包含的格（可选格）；
    - representative_solution：字典序最小的一组最优复核格，便于直接落地。

    未参与任何冲突的格（孤立点）不必复核，不会出现在任何方案中。
    """
    k = len(conflict_vertices)

    # 无冲突：唯一最优方案是空集，总成本 0
    if k == 0:
        return {
            "minimum_cost": 0,
            "solution_count": 1,
            "mandatory": [],
            "optional": [],
            "representative_solution": [],
        }

    neighbors = _build_conflict_graph(conflict_vertices, conflicts)
    weights = [costs[gid] for gid in conflict_vertices]
    total_weight = sum(weights)
    size = 1 << k

    # 第一遍子集 DP：标记每个顶点子集是否为独立集。
    # independent[mask] 为 1 表示 mask 内任意两点之间都没有冲突边。
    independent = bytearray(size)
    independent[0] = 1
    for mask in range(1, size):
        bit = mask & -mask  # 最低位顶点
        idx = bit.bit_length() - 1
        rest = mask ^ bit
        independent[mask] = (
            independent[rest] and (neighbors[idx] & rest == 0)
        )

    # 第二遍子集 DP：在全部独立集中求最大权独立集。
    # 最小权覆盖成本 = 总成本 − 最大独立集权值；两者方案一一对应（取补集）。
    use_uint64 = total_weight <= _UINT64_MAX
    if use_uint64:
        best = array("Q", [0]) * size
    else:  # pragma: no cover - 成本为超大整数时的正确性兜底
        best = [0] * size
    for mask in range(1, size):
        bit = mask & -mask
        idx = bit.bit_length() - 1
        rest = mask ^ bit
        without = best[rest]
        with_v = weights[idx] + best[rest & ~neighbors[idx]]
        best[mask] = with_v if with_v > without else without

    max_independent_weight = best[size - 1]
    minimum_cost = total_weight - max_independent_weight

    # 回溯枚举全部最大权独立集，再取补集得到全部最小权顶点覆盖。
    # 按“从低位到高位优先不取顶点”的顺序枚举，第一个覆盖即字典序最小方案。
    solutions: list[tuple[str, ...]] = []
    representative: tuple[str, ...] | None = None
    mandatory_mask = (1 << k) - 1  # 与所有方案交集累积，最后剩下必选顶点
    full = (1 << k) - 1

    def enumerate_independent(mask: int, chosen: int, weight: int) -> None:
        nonlocal representative, mandatory_mask
        if weight + best[mask] < max_independent_weight:
            return  # 上界剪枝：剩余部分无论怎么选都达不到最优
        if mask == 0:
            if weight != max_independent_weight:
                return
            cover_mask = full ^ chosen
            cover = tuple(
                conflict_vertices[i]
                for i in range(k)
                if cover_mask & (1 << i)
            )
            if representative is None:
                representative = cover
            mandatory_mask &= cover_mask
            solutions.append(cover)
            return
        bit = mask & -mask
        idx = bit.bit_length() - 1
        rest = mask ^ bit
        # 先尝试不取该顶点（字典序较小的覆盖优先产生）
        enumerate_independent(rest, chosen, weight)
        # 再尝试取该顶点（必须与已选顶点不相邻）
        if neighbors[idx] & chosen == 0:
            enumerate_independent(
                rest & ~neighbors[idx],
                chosen | bit,
                weight + weights[idx],
            )

    enumerate_independent(full, 0, 0)

    mandatory = sorted(
        conflict_vertices[i]
        for i in range(k)
        if mandatory_mask & (1 << i)
    )
    optional_set = {
        gid for cover in solutions for gid in cover
    } - set(mandatory)
    return {
        "minimum_cost": minimum_cost,
        "solution_count": len(solutions),
        "mandatory": mandatory,
        "optional": sorted(optional_set),
        "representative_solution": list(representative or []),
    }


def diagnose(
    levels: Mapping[str, float],
    costs: Mapping[str, int],
    adjacency: Mapping[str, set[str]],
    threshold: float,
) -> dict:
    """组合冲突检测与最小权覆盖求解，产出一次诊断的完整结论。"""
    conflicts = detect_conflicts(levels, costs, adjacency, threshold)
    conflict_vertices = sorted(
        {c["grid_id_a"] for c in conflicts}
        | {c["grid_id_b"] for c in conflicts}
    )
    cover = min_weight_vertex_cover(conflict_vertices, conflicts, costs)
    return {"conflicts": conflicts, **cover}
