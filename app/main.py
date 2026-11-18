"""FastAPI 入口：流域网格发布与观测点批量查询。"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .config import settings
from .db import Database, VersionConflict, VersionNotFound
from .diagnosis import diagnose
from .geometry import (
    GeometryError,
    build_adjacency,
    classify_point,
    parse_surface,
    validate_submission,
)
from .schemas import (
    DiagnoseRequest,
    DiagnoseResponse,
    PointResult,
    PublishRequest,
    PublishResponse,
    QueryRequest,
    QueryResponse,
    VersionInfo,
)

app = FastAPI(
    title="流域防汛网格服务",
    description=(
        "提交流域多边形与整版网格（互不重叠且恰好覆盖流域），按所见版号乐观锁发布；"
        "观测点按固定版号批量落格，旧版持久保留可查。"
    ),
    version="1.1.0",
)

db = Database(settings.database_path)


def _error(
    status: int,
    error: str,
    detail: str,
    issues: list | None = None,
    extra: dict | None = None,
) -> JSONResponse:
    content: dict[str, Any] = {"error": error, "detail": detail, "issues": issues}
    if extra:
        content.update(extra)
    return JSONResponse(status_code=status, content=content)


@app.exception_handler(GeometryError)
async def geometry_error_handler(_: Request, exc: GeometryError) -> JSONResponse:
    return _error(422, "INVALID_GEOMETRY", str(exc))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 默认处理器会原样回显输入；NaN/Infinity 无法被标准 JSON 序列化会引发二次崩溃，
    # 这里把非法值转成字符串，保证客户端始终拿到结构稳定的 422 响应。
    safe_errors: list[dict[str, Any]] = []
    for err in exc.errors():
        item = {k: (v if k != "input" else _json_safe(v)) for k, v in err.items()}
        safe_errors.append(item)
    return JSONResponse(
        status_code=422,
        content={"error": "REQUEST_VALIDATION_FAILED", "detail": "请求参数校验失败", "errors": safe_errors},
    )


def _json_safe(value: Any) -> Any:
    """把 NaN/Infinity 等无法标准 JSON 序列化的值转成可序列化表示。"""
    if isinstance(value, float):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"])
async def health() -> dict:
    return {"status": "ok", "current_version": db.current_version()}


@app.get("/versions", response_model=list[VersionInfo], tags=["版本"])
async def versions() -> list[dict]:
    return db.list_versions()


@app.post(
    "/publish",
    response_model=PublishResponse,
    tags=["发布"],
    responses={
        409: {"model": dict, "description": "版号冲突，整版拒绝，当前版不变"},
        422: {"model": dict, "description": "几何非法 / 网格重叠或未恰好覆盖流域，整版拒绝"},
    },
)
async def publish(req: PublishRequest) -> JSONResponse | PublishResponse:
    # 1) 解析流域
    watershed = parse_surface(req.watershed, "流域多边形")

    # 2) 解析网格并检查编号唯一
    grids: list[tuple[str, object]] = []
    seen_ids: set[str] = set()
    duplicates: list[str] = []
    for grid in req.grids:
        if grid.grid_id in seen_ids and grid.grid_id not in duplicates:
            duplicates.append(grid.grid_id)
        seen_ids.add(grid.grid_id)
        geom = parse_surface(grid.geometry, f"网格 {grid.grid_id}")
        grids.append((grid.grid_id, geom))
    if duplicates:
        return _error(
            422,
            "DUPLICATE_GRID_ID",
            f"网格编号必须唯一，重复编号：{', '.join(duplicates)}",
        )

    # 3) 几何整版校验：重叠 / 缺口 / 越界，任何问题整版拒绝
    issues = validate_submission(watershed, grids, settings.area_tol_rel)
    if issues:
        return _error(
            422,
            "COVERAGE_INVALID",
            "网格存在重叠或未恰好覆盖流域，整版拒绝（问题位置见 issues）",
            issues,
        )

    # 4) 乐观锁发布（首次发布 expected_version 传 null 或 0）
    expected = None if req.expected_version in (None, 0) else req.expected_version
    try:
        new_version = db.publish(expected, watershed, grids)
    except VersionConflict as exc:
        return _error(409, "VERSION_CONFLICT", str(exc))

    info = next(v for v in db.list_versions() if v["version"] == new_version)
    return PublishResponse(
        version=new_version,
        expected_version=expected,
        published_at=info["published_at"],
        grid_count=info["grid_count"],
    )


@app.post(
    "/query",
    response_model=QueryResponse,
    tags=["查询"],
    responses={404: {"model": dict, "description": "指定版本不存在或尚无已发布版本"}},
)
async def query(req: QueryRequest) -> JSONResponse | QueryResponse:
    current = db.current_version()
    if req.version is None:
        if current is None:
            return _error(404, "NO_PUBLISHED_VERSION", "尚无已发布版本，请先调用 /publish")
        version = current
    else:
        version = req.version
        if not any(v["version"] == version for v in db.list_versions()):
            return _error(404, "VERSION_NOT_FOUND", f"版本 {version} 不存在")

    watershed, grids = db.load_snapshot(version)
    diag = max(watershed.bounds[2] - watershed.bounds[0],
               watershed.bounds[3] - watershed.bounds[1])
    eps = diag * settings.point_eps_rel

    results: list[PointResult] = []
    for p in req.points:
        verdict = classify_point(p.lon, p.lat, watershed, grids, eps)
        results.append(
            PointResult(
                point_id=p.point_id,
                lon=p.lon,
                lat=p.lat,
                status=verdict["status"],
                grid_id=verdict["grid_id"],
                grid_ids=verdict["grid_ids"],
            )
        )

    return QueryResponse(version=version, is_current=(version == current), results=results)


@app.post(
    "/diagnose",
    response_model=DiagnoseResponse,
    tags=["诊断"],
    responses={
        404: {"model": dict, "description": "指定版本不存在或尚无已发布版本"},
        422: {"model": dict, "description": "读数缺格 / 重复格 / 含非有限水位或非法成本，整批拒绝"},
    },
)
async def diagnose_readings(
    req: DiagnoseRequest,
) -> JSONResponse | DiagnoseResponse:
    # 1) 版号解析与整版快照在同一只读事务内完成，一次诊断固定同一网格版本
    try:
        version, is_current, _watershed, grids = db.resolve_version_and_snapshot(
            req.version
        )
    except VersionNotFound as exc:
        code = "NO_PUBLISHED_VERSION" if req.version is None else "VERSION_NOT_FOUND"
        return _error(404, code, str(exc))

    version_grid_ids = [grid_id for grid_id, _ in grids]
    version_grid_set = set(version_grid_ids)

    # 2) 整批校验：重复格、缺格、未知格，任一问题整批拒绝（不做部分诊断）
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    for reading in req.readings:
        if reading.grid_id in seen and reading.grid_id not in duplicate_ids:
            duplicate_ids.append(reading.grid_id)
        seen.add(reading.grid_id)
    reading_ids = set(seen)
    missing_ids = sorted(version_grid_set - reading_ids)
    unknown_ids = sorted(reading_ids - version_grid_set)
    if duplicate_ids or missing_ids or unknown_ids:
        problems = []
        if duplicate_ids:
            problems.append(f"重复格：{', '.join(duplicate_ids)}")
        if missing_ids:
            problems.append(f"缺格：{', '.join(missing_ids)}")
        if unknown_ids:
            problems.append(f"不属于该版本的格：{', '.join(unknown_ids)}")
        return _error(
            422,
            "READINGS_INVALID",
            "读数必须与该版本网格一一对应；" + "；".join(problems),
            extra={
                "duplicate_grid_ids": duplicate_ids,
                "missing_grid_ids": missing_ids,
                "unknown_grid_ids": unknown_ids,
            },
        )

    # 3) 邻接关系由该版本几何计算（仅共享非零长度边界算相邻）
    adjacency = build_adjacency(grids)
    levels = {r.grid_id: r.water_level for r in req.readings}
    costs = {r.grid_id: r.review_cost for r in req.readings}

    # 4) 冲突检测 + 最小复核成本方案（枚举全部最优方案，区分必选/可选）
    result = diagnose(levels, costs, adjacency, req.threshold)

    return DiagnoseResponse(
        version=version,
        is_current=is_current,
        threshold=req.threshold,
        grid_count=len(version_grid_ids),
        conflict_count=len(result["conflicts"]),
        conflicts=result["conflicts"],
        minimum_cost=result["minimum_cost"],
        solution_count=result["solution_count"],
        mandatory_grid_ids=result["mandatory"],
        optional_grid_ids=result["optional"],
        review_grid_ids=result["representative_solution"],
    )
