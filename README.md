# 流域防汛网格服务

基于 **FastAPI + Shapely + SQLite** 的防汛网格整版发布、观测点落格查询与逐格水位数据质量诊断 API。

## 业务规则

### 发布（`POST /publish`）

- 提交一个流域多边形和 **2～20 个**带唯一编号的网格 GeoJSON（Polygon 或 MultiPolygon）。
- 网格之间**内部不得重叠**（共边、共点的相切允许）。
- 网格合并后须**恰好覆盖**流域：既不能有缺口（`gap`），也不能越界（`excess`）。
- 任何重叠 / 缺口 / 越界都会在响应 `issues` 中给出问题位置（包围盒、代表点、面积），**整版拒绝**，当前版不变。
- 请求携带提交方**所见版号** `expected_version`（首次发布传 `null` 或 `0`）。两个并发改版只有一个能按该版号发布，另一个收到 `409 VERSION_CONFLICT`。
- 发布成功后版本号递增；旧版网格整版保留，永不改写。

### 查询（`POST /query`）

- 一次至多 **100 个**观测点；一次查询固定使用**同一版本**（`version` 不传 = 当前版）。
- 判定结果：
  - `inside`：点在某网格内部，返回唯一 `grid_id`；
  - `boundary`：点落在网格 / 流域边界上（含角点、洞边），列出所有相接网格，`grid_id` 为 `null`，标记为**待判**；
  - `outside`：流域外，网格列表为空。
- 指定的版本不存在返回 `404 VERSION_NOT_FOUND`；尚无任何已发布版本时查当前版返回 `404 NO_PUBLISHED_VERSION`。
- 旧版数据持久化在 SQLite 中，**进程 / 容器重启后仍可查询**。

### 数据质量诊断（`POST /diagnose`）

入仓前按网格邻接关系筛出需要复核的矛盾读数：

- 入参：已发布版号 `version`（可省略，省略时取**受理时当前版**）、非负差值阈值 `threshold`、每格一条读数 `readings`（有限水位 `water_level` + 正整数复核成本 `review_cost`）。
- 读数必须与该版本网格**一一对应**：缺格、重复格、出现该版本不存在的格，均**整批拒绝**（`422 READINGS_INVALID`，响应分别列出 `missing_grid_ids` / `duplicate_grid_ids` / `unknown_grid_ids`）；`NaN` / `Infinity` 水位、非正成本、负阈值同样 422 拒绝。
- **邻接定义**：仅共享**非零长度边界（共边）**的网格算相邻；只在角点 / 点上相接、互不接触的网格不算相邻。
- 相邻两格水位差**严格超过**阈值（`difference > threshold`，等于阈值不冲突）即一条冲突；每条冲突至少复核其中一端。
- 响应给出：
  - `conflicts`：全部冲突对（两端编号、水位、差值、各自成本，按编号排序）；
  - `minimum_cost`：所有可行复核方案中的最低总成本；
  - `review_grid_ids`：**一组**最低成本复核格（字典序最小的代表方案）；
  - `solution_count`：最低成本方案总数；
  - `mandatory_grid_ids`：**必选格**，每一组最低成本方案都包含；
  - `optional_grid_ids`：**可选格**，只在部分最低成本方案中出现。
- `solution_count > 1` 时不存在唯一结论，必须结合必选 / 可选格理解，不能把代表方案当成唯一答案。
- **版本固定**：版号解析与整版网格读取在同一只读事务内完成，一次诊断始终使用同一网格版本；并发发布只新增版本、永不改写旧版，因此邻接关系不会与返回的 `version` 错配；指定旧版重查，结果逐字段一致。
- 求解器为冲突图上的**最小权顶点覆盖**精确算法（O(2^k) 子集 DP，k ≤ 20 为已发布网格上限），枚举全部最优方案后再区分必选 / 可选，不使用近似算法。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式文档（可直接提交和查询）：http://localhost:8000/docs
- SQLite 数据保存在命名卷 `watershed-data`（容器内 `/data/watershed.db`），重建容器不丢数据。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATABASE_PATH=data/watershed.db .venv/bin/uvicorn app.main:app --reload
```

## 请求示例

发布第一版：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_v1.json
```

携带所见版号发布第二版（并发时只有一个成功，另一个返回 409）：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_v2.json
```

批量查询（可传 `"version": 1` 固定查旧版，不传则查当前版）：

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d @examples/query.json
```

数据质量诊断（先发布四宫格版本，再对每格水位做诊断；不传 `version` 即当前版）：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_quads.json

curl -X POST http://localhost:8000/diagnose \
  -H 'Content-Type: application/json' \
  -d @examples/diagnose.json
```

诊断响应示例（两条互不共享端点的冲突边，各有 2 种等优选法，共 4 个最低成本方案，故无必选格）：

```json
{
  "version": 1,
  "is_current": true,
  "threshold": 1.0,
  "grid_count": 4,
  "conflict_count": 2,
  "conflicts": [
    {"grid_id_a": "BL", "grid_id_b": "BR", "level_a": 9.0, "level_b": 0.5,
     "difference": 8.5, "cost_a": 1, "cost_b": 1},
    {"grid_id_a": "TL", "grid_id_b": "TR", "level_a": 9.0, "level_b": 0.5,
     "difference": 8.5, "cost_a": 1, "cost_b": 1}
  ],
  "minimum_cost": 2,
  "solution_count": 4,
  "mandatory_grid_ids": [],
  "optional_grid_ids": ["BL", "BR", "TL", "TR"],
  "review_grid_ids": ["BL", "TL"]
}
```

错误响应（重叠 / 缺口，HTTP 422）示例：

```json
{
  "error": "COVERAGE_INVALID",
  "detail": "网格存在重叠或未恰好覆盖流域，整版拒绝（问题位置见 issues）",
  "issues": [
    {
      "type": "overlap",
      "location": {"minx": 1.0, "miny": 0.0, "maxx": 2.0, "maxy": 4.0},
      "representative_point": [1.5, 2.0],
      "area": 4.0,
      "message": "网格 L 与 X 内部重叠，重叠面积 4"
    }
  ]
}
```

## 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查与当前版号 |
| GET | `/versions` | 全部已发布版本清单 |
| POST | `/diagnose` | 逐格水位数据质量诊断：冲突对、最低成本复核方案、必选 / 可选格 |
| GET | `/docs` | Swagger UI，可在线提交 / 查询 / 诊断 |

## 设计说明

- **乐观锁**：`meta.current_version` 记录当前版。发布在单条 `BEGIN IMMEDIATE` 事务内比对 `expected_version` 与当前版，不一致即回滚并返回 409；应用层另用写锁串行化同进程并发，SQLite `busy_timeout` 处理跨进程锁等待。
- **持久化**：几何以 WKB 存入 `versions` / `grids` 表，WAL 模式；每个版本独立行组，发布新版本不修改旧版任何行。
- **诊断版本固定**：`/diagnose` 在同一只读事务内完成“解析版号 → 加载整版快照”，邻接关系与返回版号必然同属一个版本；并发发布只追加新版本行，不影响进行中的诊断，旧版重查结果稳定可复现。
- **最小权顶点覆盖**：冲突边要求至少复核一端，等价于冲突图顶点覆盖；网格上限 20，用 O(2^k) 子集 DP 求全部最优方案（实现上取对偶的最大权独立集，顶点覆盖与独立集互补），再取所有最优方案的交集 / 差集得到必选格 / 可选格，避免把单组方案误报为唯一结论。
- **容差**：面积比较使用 `AREA_TOL_REL × 流域面积`（默认 `1e-9`）吸收浮点噪音；点查询对距边界极近（`POINT_EPS_REL`，默认 `1e-9` × 外接矩形对角线）的点兜底判为待判，避免浮点抖动把边界点误判为域外。邻接判定按共享边界长度是否严格大于 0，仅共点（零维交集，长度 0）不算相邻。
- 坐标系按平面坐标处理（经纬度直接作为 x/y），面积单位与输入坐标单位一致；如需米制面积请在入库前投影。

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
```

覆盖：首版发布、版号乐观锁冲突、并发发布仅一个成功、网格数上下限、编号重复、重叠 / 缺口 / 越界定位、无效几何拒绝、失败发布不改当前版、内部 / 边界 / 角点 / 洞边 / 域外判定、100 点上限、旧版查询、版本不存在、重启后旧版仍可查；诊断侧覆盖共边 / 仅共点邻接、阈值开闭边界、缺格 / 重复格 / 未知格整批拒绝、NaN/Infinity 与非法成本拒绝、必选 / 可选格与多方案计数、无冲突空方案、旧版重查一致、并发发布下版本不错配，以及 60 组随机冲突图上 DP 与暴力枚举逐案对照、20 格完全图最坏情形。
