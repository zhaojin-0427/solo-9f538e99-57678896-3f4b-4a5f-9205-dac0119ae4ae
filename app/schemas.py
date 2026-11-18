"""请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, confloat

# 状态码：inside 内部点 / boundary 边界待判 / outside 流域外
PointStatus = Literal["inside", "boundary", "outside"]


class GridIn(BaseModel):
    grid_id: str = Field(..., min_length=1, max_length=128, description="网格唯一编号")
    geometry: dict[str, Any] = Field(..., description="Polygon 或 MultiPolygon 的 GeoJSON geometry")


class PublishRequest(BaseModel):
    expected_version: int | None = Field(
        None,
        ge=0,
        description="提交方所见的当前版号；首次发布传 null 或 0，之后传当前版号",
    )
    watershed: dict[str, Any] = Field(..., description="流域多边形 GeoJSON geometry")
    grids: list[GridIn] = Field(..., min_length=2, max_length=20, description="2~20 个带唯一编号的网格")


class ValidationIssue(BaseModel):
    type: Literal["overlap", "gap", "excess"]
    location: dict[str, float]
    representative_point: list[float]
    area: float
    message: str


class PublishResponse(BaseModel):
    version: int
    expected_version: int | None
    published_at: str
    grid_count: int
    status: Literal["published"] = "published"


class ErrorResponse(BaseModel):
    error: str
    detail: str
    issues: list[ValidationIssue] | None = None


class QueryPoint(BaseModel):
    point_id: str | None = Field(None, max_length=128, description="观测点编号，可选，便于结果对应")
    lon: float
    lat: float


class QueryRequest(BaseModel):
    version: int | None = Field(
        None, ge=1, description="固定查询版号；不传或传 null 表示当前版，一次查询内所有点同版"
    )
    points: list[QueryPoint] = Field(..., min_length=1, max_length=100, description="至多 100 个观测点")


class PointResult(BaseModel):
    point_id: str | None
    lon: float
    lat: float
    status: PointStatus
    grid_id: str | None = Field(None, description="内部点返回唯一网格编号；其余为 null")
    grid_ids: list[str] = Field(
        default_factory=list, description="边界点列出所有相接网格（待判）；域外为空"
    )


class QueryResponse(BaseModel):
    version: int = Field(..., description="本次查询实际使用的版号")
    is_current: bool
    results: list[PointResult]


class VersionInfo(BaseModel):
    version: int
    expected_version: int | None
    published_at: str
    grid_count: int


# ---------- 数据质量诊断 ----------

# 有限浮点数：拒绝 NaN / Infinity（allow_inf_nan=False），水位必须是有限读数
FiniteFloat = confloat(allow_inf_nan=False)
# 非负差值阈值：有限且 >= 0
NonNegativeFloat = confloat(ge=0.0, allow_inf_nan=False)


class WaterReading(BaseModel):
    grid_id: str = Field(..., min_length=1, max_length=128, description="已发布版本中的网格编号")
    water_level: FiniteFloat = Field(..., description="该格一条有限水位读数，NaN/Infinity 整批拒绝")
    review_cost: int = Field(..., gt=0, description="复核该格的成本，必须为正整数")


class DiagnoseRequest(BaseModel):
    version: int | None = Field(
        None, ge=1, description="固定诊断版号；不传或传 null 表示受理时的当前版"
    )
    threshold: NonNegativeFloat = Field(..., description="非负差值阈值，水位差严格超过阈值即冲突")
    readings: list[WaterReading] = Field(
        ..., min_length=1, description="每格一条水位与复核成本；缺格、重复格整批拒绝"
    )


class ConflictPair(BaseModel):
    grid_id_a: str
    grid_id_b: str
    level_a: float
    level_b: float
    difference: float
    cost_a: int
    cost_b: int


class DiagnoseResponse(BaseModel):
    version: int = Field(..., description="本次诊断实际固定使用的版号")
    is_current: bool = Field(..., description="该版号是否为受理时的当前版")
    threshold: float
    grid_count: int = Field(..., description="该版本网格总数（应与读数条数一致）")
    conflict_count: int
    conflicts: list[ConflictPair] = Field(..., description="全部冲突对（按网格编号排序）")
    minimum_cost: int = Field(..., description="所有可行复核方案中的最低总成本")
    solution_count: int = Field(..., description="达到最低成本的方案总数；大于 1 时结论不唯一")
    mandatory_grid_ids: list[str] = Field(
        ..., description="必选格：每一组最低成本方案都必须复核的网格"
    )
    optional_grid_ids: list[str] = Field(
        ..., description="可选格：只在部分最低成本方案中出现的网格"
    )
    review_grid_ids: list[str] = Field(
        ..., description="一组最低成本复核格（字典序最小的代表方案），不代表唯一方案"
    )
