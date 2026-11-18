"""数据质量诊断 API（/diagnose）端到端测试。

覆盖：
- 邻接判定：共边相邻、仅共点不相邻；
- 冲突阈值边界（差值 == 阈值不冲突，超过才冲突）；
- 缺格 / 重复格 / 未知格整批拒绝，NaN/Infinity、非正成本、负阈值拒绝；
- 最小复核成本方案、必选格 / 可选格区分、多方案计数；
- 无冲突空方案、版本不存在 / 尚无版本 404；
- 旧版重查结果一致、并发发布不使版号与邻接错配；
- 子集 DP 与暴力枚举在随机冲突图上完全一致。
"""
from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.config as config
    import app.db as dbmod
    import app.main as main
    importlib.reload(config)
    importlib.reload(dbmod)
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, main


def poly(coords):
    return {"type": "Polygon", "coordinates": [coords]}


# 4×4 流域，2×2 四宫格：BL 邻接 TL/BR，BR 邻接 BL/TR，对角仅共点
WATERSHED = poly([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
TL = poly([(0, 2), (2, 2), (2, 4), (0, 4), (0, 2)])
TR = poly([(2, 2), (4, 2), (4, 4), (2, 4), (2, 2)])
BL = poly([(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)])
BR = poly([(2, 0), (4, 0), (4, 2), (2, 2), (2, 0)])
QUAD_GRIDS = [
    {"grid_id": "BL", "geometry": BL},
    {"grid_id": "BR", "geometry": BR},
    {"grid_id": "TL", "geometry": TL},
    {"grid_id": "TR", "geometry": TR},
]
GRID_IDS = ["BL", "BR", "TL", "TR"]


def publish_quads(c, expected=None):
    r = c.post(
        "/publish",
        json={
            "expected_version": expected,
            "watershed": WATERSHED,
            "grids": QUAD_GRIDS,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["version"]


def readings(levels, costs=None):
    costs = costs or {}
    return [
        {
            "grid_id": gid,
            "water_level": levels[gid],
            "review_cost": costs.get(gid, 1),
        }
        for gid in levels
    ]


def diagnose(c, levels, threshold=1.0, version=None, costs=None):
    body: dict = {"threshold": threshold, "readings": readings(levels, costs)}
    if version is not None:
        body["version"] = version
    return c.post("/diagnose", json=body)


def conflict_pairs(payload):
    return sorted((c["grid_id_a"], c["grid_id_b"]) for c in payload["conflicts"])


# ---------- 基础流程：邻接、冲突、最小复核方案 ----------

def test_no_conflict_when_all_below_threshold(client):
    c, _ = client
    publish_quads(c)
    r = diagnose(c, {"BL": 1.0, "BR": 1.5, "TL": 0.8, "TR": 1.2}, threshold=1.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["is_current"] is True
    assert body["conflict_count"] == 0 and body["conflicts"] == []
    assert body["minimum_cost"] == 0
    assert body["solution_count"] == 1
    assert body["mandatory_grid_ids"] == []
    assert body["optional_grid_ids"] == []
    assert body["review_grid_ids"] == []


def test_basic_conflict_and_cheapest_endpoint(client):
    c, _ = client
    publish_quads(c)
    # 四宫格共边关系：BL-BR、BL-TL、BR-TR、TL-TR。
    # 仅 BL-BR 水位差 3 > 阈值 1；BL-TL 差 1 恰好不超阈值，
    # BR-TR 差 1 恰好不超阈值，TL-TR 差 2 但 TL-TR 同为高侧……
    # 取水位 BL=5、BR=2、TL=4、TR=3、阈值 1：只有 BL-BR（差 3）超阈值。
    levels = {"BL": 5.0, "BR": 2.0, "TL": 4.0, "TR": 3.0}
    r = diagnose(
        c,
        levels,
        threshold=1.0,
        costs={"BL": 10, "BR": 2, "TL": 7, "TR": 7},
    )
    body = r.json()
    assert body["conflict_count"] == 1
    assert conflict_pairs(body) == [("BL", "BR")]
    pair = body["conflicts"][0]
    assert pair["difference"] == pytest.approx(3.0)
    assert pair["cost_a"] == 10 and pair["cost_b"] == 2
    assert body["minimum_cost"] == 2
    assert body["solution_count"] == 1
    assert body["mandatory_grid_ids"] == ["BR"]
    assert body["optional_grid_ids"] == []
    assert body["review_grid_ids"] == ["BR"]


def test_threshold_is_exclusive(client):
    c, _ = client
    publish_quads(c)
    # 三档水位只让 BL-BR 一条边产生差值：BL=2, BR=3, TL=2, TR=3
    # 此时 BL-TL 差 0、BR-TR 差 0、TL-TR 差 1
    # 差值恰好等于阈值 -> 不冲突（两条边都不冲突）
    r_eq = diagnose(c, {"BL": 2.0, "BR": 3.0, "TL": 2.0, "TR": 3.0}, threshold=1.0)
    assert r_eq.json()["conflict_count"] == 0
    # 超过阈值（哪怕极微小）-> BL-BR 与 TL-TR 同时冲突
    r_over = diagnose(
        c, {"BL": 2.0, "BR": 3.0000000001, "TL": 2.0, "TR": 3.0000000001},
        threshold=1.0,
    )
    assert conflict_pairs(r_over.json()) == [("BL", "BR"), ("TL", "TR")]
    # 阈值 0：任何不相等的相邻读数都冲突（共 4 条共边，对角仍不算）
    r_zero = diagnose(
        c, {"BL": 2.0, "BR": 2.5, "TL": 2.0, "TR": 2.5}, threshold=0
    )
    assert conflict_pairs(r_zero.json()) == [("BL", "BR"), ("TL", "TR")]


def test_point_touch_is_not_adjacency(client):
    c, _ = client
    publish_quads(c)
    # TL 与 BR 仅在角点 (2,2) 相接：两者水位差再大也不冲突。
    # 取 BL=0、BR=0、TL=10、TR=10：共边对 BL-TL 与 BR-TR 差 10 冲突，
    # TL-TR 水位相同不冲突；若角点被误判为相邻，BR-TL 也会出现冲突。
    r = diagnose(c, {"BL": 0.0, "BR": 0.0, "TL": 10.0, "TR": 10.0}, threshold=1.0)
    assert r.status_code == 200
    assert conflict_pairs(r.json()) == [("BL", "TL"), ("BR", "TR")]
    # 阈值降到 0 结论相同（角点相接永不相邻）；对角 BL-TR、BR-TL 永不出现
    r2 = diagnose(c, {"BL": 0.0, "BR": 0.0, "TL": 10.0, "TR": 10.0}, threshold=0)
    pairs = conflict_pairs(r2.json())
    assert pairs == [("BL", "TL"), ("BR", "TR")]
    assert ("BL", "TR") not in pairs and ("BR", "TL") not in pairs


def test_shared_vertex_makes_mandatory_grid(client):
    c, _ = client
    publish_quads(c)
    # 两条冲突边 BL-BR、BL-TL 共享 BL：复核 BL 一次覆盖两条，成本最低且唯一
    r = diagnose(
        c,
        {"BL": 9.0, "BR": 0.0, "TL": 0.0, "TR": 0.0},
        threshold=1.0,
        costs={"BL": 3, "BR": 5, "TL": 5, "TR": 5},
    )
    body = r.json()
    assert conflict_pairs(body) == [("BL", "BR"), ("BL", "TL")]
    assert body["minimum_cost"] == 3
    assert body["solution_count"] == 1
    assert body["mandatory_grid_ids"] == ["BL"]
    assert body["review_grid_ids"] == ["BL"]


def test_multiple_optimal_solutions_split_mandatory_and_optional(client):
    c, _ = client
    publish_quads(c)
    # 两条互不共享端点的冲突边 BL-BR、TL-TR，各边两端等成本：
    # 每条边 2 种选法，共 2×2=4 个最低成本方案，没有必选格
    r = diagnose(
        c,
        {"BL": 9.0, "BR": 0.0, "TL": 9.0, "TR": 0.0},
        threshold=1.0,
        costs={gid: 1 for gid in GRID_IDS},
    )
    body = r.json()
    assert conflict_pairs(body) == [("BL", "BR"), ("TL", "TR")]
    assert body["minimum_cost"] == 2
    assert body["solution_count"] == 4
    assert body["mandatory_grid_ids"] == []
    assert body["optional_grid_ids"] == ["BL", "BR", "TL", "TR"]
    # 代表方案只是其中之一，不能被误当成唯一结论
    assert set(body["review_grid_ids"]) in (
        {"BL", "TL"}, {"BL", "TR"}, {"BR", "TL"}, {"BR", "TR"},
    )
    assert len(body["review_grid_ids"]) == 2


def test_cheaper_choice_can_force_mandatory(client):
    c, _ = client
    publish_quads(c)
    # BL-BR 与 TL-TR 两条独立冲突边；BL、TL 明显更便宜 -> 最优方案唯一
    r = diagnose(
        c,
        {"BL": 9.0, "BR": 0.0, "TL": 9.0, "TR": 0.0},
        threshold=1.0,
        costs={"BL": 1, "BR": 10, "TL": 1, "TR": 10},
    )
    body = r.json()
    assert body["minimum_cost"] == 2
    assert body["solution_count"] == 1
    assert body["mandatory_grid_ids"] == ["BL", "TL"]
    assert body["optional_grid_ids"] == []
    assert body["review_grid_ids"] == ["BL", "TL"]


# ---------- 整批拒绝：缺格 / 重复 / 未知 / 非法值 ----------

def test_missing_grid_rejected_whole_batch(client):
    c, _ = client
    publish_quads(c)
    r = diagnose(c, {"BL": 1.0, "BR": 1.0, "TL": 1.0})  # 缺 TR
    assert r.status_code == 422
    payload = r.json()
    assert payload["error"] == "READINGS_INVALID"
    assert payload["missing_grid_ids"] == ["TR"]
    assert payload["duplicate_grid_ids"] == []


def test_duplicate_grid_rejected_whole_batch(client):
    c, _ = client
    publish_quads(c)
    body = {
        "threshold": 1.0,
        "readings": [
            {"grid_id": "BL", "water_level": 1.0, "review_cost": 1},
            {"grid_id": "BR", "water_level": 1.0, "review_cost": 1},
            {"grid_id": "TL", "water_level": 1.0, "review_cost": 1},
            {"grid_id": "TR", "water_level": 1.0, "review_cost": 1},
            {"grid_id": "BL", "water_level": 2.0, "review_cost": 1},
        ],
    }
    r = c.post("/diagnose", json=body)
    assert r.status_code == 422
    payload = r.json()
    assert payload["error"] == "READINGS_INVALID"
    assert payload["duplicate_grid_ids"] == ["BL"]


def test_unknown_grid_rejected(client):
    c, _ = client
    publish_quads(c)
    r = diagnose(
        c,
        {"BL": 1.0, "BR": 1.0, "TL": 1.0, "TR": 1.0, "XX": 1.0},
    )
    assert r.status_code == 422
    assert r.json()["unknown_grid_ids"] == ["XX"]


def test_non_finite_level_and_bad_cost_and_negative_threshold(client):
    c, _ = client
    publish_quads(c)
    headers = {"Content-Type": "application/json"}
    # NaN / Infinity 不是有限水位：绕过客户端 JSON 限制直接发原始 JSON 文本，
    # 服务端 Pydantic（allow_inf_nan=False）必须整批 422 拒绝
    for token in ("NaN", "Infinity", "-Infinity"):
        raw = (
            '{"threshold": 1.0, "readings": ['
            + ",".join(
                '{"grid_id": "%s", "water_level": %s, "review_cost": 1}'
                % (gid, token if gid == "BL" else "1.0")
                for gid in GRID_IDS
            )
            + "]}"
        )
        r = c.post("/diagnose", content=raw, headers=headers)
        assert r.status_code == 422, f"应拒绝非有限水位 {token}"
    # 非正整数成本
    body = {
        "threshold": 1.0,
        "readings": [
            {"grid_id": gid, "water_level": 1.0, "review_cost": 0 if gid == "BL" else 1}
            for gid in GRID_IDS
        ],
    }
    assert c.post("/diagnose", json=body).status_code == 422
    # 负阈值
    body = {"threshold": -1.0, "readings": readings({gid: 1.0 for gid in GRID_IDS})}
    assert c.post("/diagnose", json=body).status_code == 422


# ---------- 版本固定与并发 ----------

def test_version_not_found_and_no_published_version(client):
    c, _ = client
    body = {"threshold": 1.0, "readings": [{"grid_id": "X", "water_level": 1.0, "review_cost": 1}]}
    # 尚无任何版本
    r = c.post("/diagnose", json=body)
    assert r.status_code == 404
    assert r.json()["error"] == "NO_PUBLISHED_VERSION"
    publish_quads(c)
    # 指定不存在的版本
    r = c.post("/diagnose", json={"version": 99, **body})
    assert r.status_code == 404
    assert r.json()["error"] == "VERSION_NOT_FOUND"


def test_old_version_recheck_is_deterministic_and_consistent(client):
    c, _ = client
    v1 = publish_quads(c)
    # 第 2 版：左右两个 2×4 网格，编号与邻接关系完全不同
    left = poly([(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)])
    right = poly([(2, 0), (4, 0), (4, 4), (2, 4), (2, 0)])
    v2 = c.post(
        "/publish",
        json={
            "expected_version": 1,
            "watershed": WATERSHED,
            "grids": [
                {"grid_id": "L", "geometry": left},
                {"grid_id": "R", "geometry": right},
            ],
        },
    ).json()["version"]
    assert (v1, v2) == (1, 2)

    old_body = {
        "version": 1,
        "threshold": 1.0,
        "readings": readings({"BL": 9.0, "BR": 0.0, "TL": 9.0, "TR": 0.0}),
    }
    first = c.post("/diagnose", json=old_body).json()
    second = c.post("/diagnose", json=old_body).json()
    # 旧版重查结果逐字段一致
    assert first == second
    assert first["version"] == 1 and first["is_current"] is False
    assert conflict_pairs(first) == [("BL", "BR"), ("TL", "TR")]

    # 当前版（v2）同样的读数键不适用；用 v2 编号诊断，邻接只有 L-R
    cur = diagnose(c, {"L": 9.0, "R": 0.0}, threshold=1.0)
    cur_body = cur.json()
    assert cur_body["version"] == 2 and cur_body["is_current"] is True
    assert conflict_pairs(cur_body) == [("L", "R")]


def test_concurrent_publish_cannot_mix_up_diagnosed_version(client):
    c, main = client
    publish_quads(c)
    captured: list[dict] = []
    barrier = threading.Barrier(2)

    def diagnose_worker():
        barrier.wait()
        # 固定旧版 1 诊断；期间另一线程发布第 2 版
        r = c.post(
            "/diagnose",
            json={
                "version": 1,
                "threshold": 1.0,
                "readings": readings(
                    {"BL": 9.0, "BR": 0.0, "TL": 9.0, "TR": 0.0}
                ),
            },
        )
        captured.append(r.json())

    def publish_worker():
        barrier.wait()
        left = poly([(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)])
        right = poly([(2, 0), (4, 0), (4, 4), (2, 4), (2, 0)])
        c.post(
            "/publish",
            json={
                "expected_version": 1,
                "watershed": WATERSHED,
                "grids": [
                    {"grid_id": "L", "geometry": left},
                    {"grid_id": "R", "geometry": right},
                ],
            },
        )

    t1 = threading.Thread(target=diagnose_worker)
    t2 = threading.Thread(target=publish_worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    body = captured[0]
    # 返回版号与邻接关系必须同属版本 1，不出现 v2 的 L/R
    assert body["version"] == 1
    assert set(conflict_pairs(body)) == {("BL", "BR"), ("TL", "TR")}
    assert main.db.current_version() == 2


def test_current_version_snapshot_is_coherent_when_published_alongside(client):
    c, _ = client
    publish_quads(c)
    # 不传 version：受理时解析为当前版，响应回显实际版号与 is_current
    r = diagnose(c, {gid: 1.0 for gid in GRID_IDS}, threshold=1.0)
    body = r.json()
    assert body["version"] == 1 and body["is_current"] is True
    assert body["grid_count"] == 4 and body["threshold"] == 1.0


# ---------- 求解器：路径图与随机图暴力对照 ----------

def test_path_graph_vertex_cover_cases(client):
    c, _ = client
    # 自定义 1×3 流域，三个相邻方格 A-B-C，冲突边 A-B、B-C
    watershed = poly([(0, 0), (3, 0), (3, 1), (0, 1), (0, 0)])
    a = poly([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    b = poly([(1, 0), (2, 0), (2, 1), (1, 1), (1, 0)])
    cc = poly([(2, 0), (3, 0), (3, 1), (2, 1), (2, 0)])
    c.post(
        "/publish",
        json={
            "expected_version": None,
            "watershed": watershed,
            "grids": [
                {"grid_id": "A", "geometry": a},
                {"grid_id": "B", "geometry": b},
                {"grid_id": "C", "geometry": cc},
            ],
        },
    )

    # 情形 1：B 复核成本低于 A+C -> 唯一方案 {B}，B 必选
    r = diagnose(
        c, {"A": 9.0, "B": 0.0, "C": 9.0},
        threshold=1.0, costs={"A": 5, "B": 3, "C": 5},
    ).json()
    assert r["minimum_cost"] == 3 and r["solution_count"] == 1
    assert r["mandatory_grid_ids"] == ["B"] and r["review_grid_ids"] == ["B"]

    # 情形 2：B 更贵 -> 唯一方案 {A, C}
    r = diagnose(
        c, {"A": 9.0, "B": 0.0, "C": 9.0},
        threshold=1.0, costs={"A": 2, "B": 10, "C": 2},
    ).json()
    assert r["minimum_cost"] == 4 and r["solution_count"] == 1
    assert r["mandatory_grid_ids"] == ["A", "C"]

    # 情形 3：cost(B)=2、cost(A)=cost(C)=1 -> {B} 与 {A,C} 同为成本 2，
    # 两个最优方案并存，没有必选格
    r = diagnose(
        c, {"A": 9.0, "B": 0.0, "C": 9.0},
        threshold=1.0, costs={"A": 1, "B": 2, "C": 1},
    ).json()
    assert r["minimum_cost"] == 2 and r["solution_count"] == 2
    assert r["mandatory_grid_ids"] == []
    assert r["optional_grid_ids"] == ["A", "B", "C"]
    assert set(r["review_grid_ids"]) in ({"B"}, {"A", "C"})


def test_solver_matches_brute_force_on_random_graphs():
    """随机冲突图（含孤立点、等权、不等权）上 DP 结果与 2^k 暴力枚举一致。"""
    import random

    from app.diagnosis import min_weight_vertex_cover

    rng = random.Random(20261118)
    for trial in range(60):
        k = rng.randint(1, 12)
        ids = [f"G{i}" for i in range(k)]
        costs = {gid: rng.randint(1, 9) for gid in ids}
        edges = []
        for i in range(k):
            for j in range(i + 1, k):
                if rng.random() < 0.35:
                    edges.append(
                        {
                            "grid_id_a": ids[i],
                            "grid_id_b": ids[j],
                            "level_a": 9.0,
                            "level_b": 0.0,
                            "difference": 9.0,
                            "cost_a": costs[ids[i]],
                            "cost_b": costs[ids[j]],
                        }
                    )
        used = sorted({g for e in edges for g in (e["grid_id_a"], e["grid_id_b"])})
        result = min_weight_vertex_cover(used, edges, costs)

        edge_sets = [(e["grid_id_a"], e["grid_id_b"]) for e in edges]
        brute_covers = []
        for mask in range(1 << k):
            chosen = {ids[i] for i in range(k) if mask & (1 << i)}
            if all(u in chosen or v in chosen for u, v in edge_sets):
                brute_covers.append((sum(costs[g] for g in chosen), tuple(sorted(chosen))))
        min_cost = min(c for c, _ in brute_covers)
        optimal = sorted(s for c, s in brute_covers if c == min_cost)

        assert result["minimum_cost"] == min_cost
        assert result["solution_count"] == len(optimal)
        assert tuple(result["representative_solution"]) == optimal[0]
        mandatory = sorted(set.intersection(*(set(s) for s in optimal)))
        optional = sorted(set.union(*(set(s) for s in optimal)) - set(mandatory))
        assert result["mandatory"] == mandatory
        assert result["optional"] == optional


def test_solver_handles_largest_graph_and_isolated_vertices():
    """20 格上限下最坏情形（完全图 K20）可在限时间内完成，且结论正确。"""
    import time

    from app.diagnosis import min_weight_vertex_cover

    ids = [f"G{i:02d}" for i in range(20)]
    costs = {gid: 1 for gid in ids}
    edges = [
        {
            "grid_id_a": ids[i],
            "grid_id_b": ids[j],
            "level_a": 9.0,
            "level_b": 0.0,
            "difference": 9.0,
            "cost_a": 1,
            "cost_b": 1,
        }
        for i in range(20)
        for j in range(i + 1, 20)
    ]
    start = time.monotonic()
    result = min_weight_vertex_cover(ids, edges, costs)
    elapsed = time.monotonic() - start
    # K20 的最小覆盖是任意 19 个顶点，共 20 个等优方案，无必选格
    assert result["minimum_cost"] == 19
    assert result["solution_count"] == 20
    assert result["mandatory"] == []
    assert result["optional"] == ids
    assert len(result["representative_solution"]) == 19
    assert elapsed < 10.0
