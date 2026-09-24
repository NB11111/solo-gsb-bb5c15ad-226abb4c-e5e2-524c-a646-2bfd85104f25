"""累计水表读数用量复核 API。

核心复核规则
-------------
对任意两个相邻采样点（时间戳 t1 < t2，读数 v1、v2，计数器模数 M，
每秒最大计数增量 R），真实累计用量只能写为：

    C = d + n * M，   d = (v2 - v1) mod M，   n >= 0

其中 d 为模意义上的读数差（0 <= d < M）。物理意义：n 是"未在端点
读数中体现的整圈数"；若 v2 < v1，读数本身还跨过了一次显式回绕
（物理整圈数 w = n + 1）。

速率上限给出每秒最大增量 R，故必须满足：

    C <= R * (t2 - t1)

因此：
  * d > R * Δt             -> 无解（即使零整圈，用量也超过速率上限）
  * nmax = (R*Δt - d) // M
      - nmax == 0          -> n 唯一为 0，分段用量唯一为 d
      - nmax >= 1          -> 多解（0..nmax 均可成立），不猜测圈数

每个相邻区间相互独立，逐段判定；只要存在无解或多解区间，总用量
不予计算（返回 null），并报告全部出错区间及原因。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

# ---------------------------------------------------------------------------
# 模型配置：严格模式——拒绝 float 冒充 int（含 bool 冒充 int）、多余字段等
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ReadingPoint(StrictModel):
    """一个采样点：秒精度 UTC 时间戳 + 该时刻的读数。"""

    ts: StrictInt | StrictStr = Field(
        ...,
        description=(
            "秒精度 UTC 时间戳。接受 Unix 纪元整数秒（如 1760000000），"
            "或带时区的 ISO 8601 字符串（如 2025-10-09T12:00:00Z、"
            "2025-10-09T13:00:00+01:00）；不允许毫秒/微秒小数秒。"
        ),
        examples=[1760000000, "2025-10-09T12:00:00Z"],
    )
    reading: int = Field(
        ...,
        ge=0,
        description="水表累计读数，必须是 [0, modulus-1] 范围内的整数。",
    )


class ReconcileRequest(StrictModel):
    """用量复核请求。"""

    points: list[ReadingPoint] = Field(
        ...,
        min_length=2,
        description="按时间顺序排列的读数点，至少 2 个；时间戳必须严格递增。",
    )
    modulus: int = Field(
        ...,
        ge=1,
        description="计数器模数 M（读数回到 0 前的量程），>= 1 的整数。",
    )
    max_rate_per_sec: int = Field(
        ...,
        ge=0,
        description="每秒最大计数增量 R，非负整数。",
    )


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class PointOut(BaseModel):
    index: int = Field(..., description="采样点序号（从 0 开始）。")
    utc: str = Field(..., description="RFC 3339 UTC 时间戳。")
    unix_seconds: int = Field(..., description="Unix 纪元整数秒。")
    reading: int = Field(..., description="该点读数。")


class CandidateRange(BaseModel):
    min_wraps: int = Field(..., description="可能的物理整圈数下限。")
    max_wraps: int = Field(..., description="可能的物理整圈数上限。")
    min_usage: int = Field(..., description="对应的可能分段用量下限。")
    max_usage: int = Field(..., description="对应的可能分段用量上限。")


class SegmentOut(BaseModel):
    # 允许用字段名 from_ 构造，同时仍以别名 "from" 输出 JSON
    model_config = ConfigDict(populate_by_name=True)

    index: int = Field(..., description="分段序号（从 0 开始，共 len(points)-1 段）。")
    from_: PointOut = Field(..., alias="from")
    to: PointOut
    duration_seconds: int = Field(..., description="分段时长 Δt（秒）。")
    delta_mod: int = Field(
        ...,
        description="模意义读数差 d = (v2 - v1) mod M。",
    )
    explicit_rollover: bool = Field(
        ...,
        description="端点读数是否发生显式回绕（v2 < v1）。",
    )
    usage: int | None = Field(
        None,
        description=(
            "唯一定性时的分段真实用量；无解/多解时为 null，绝不猜测圈数。"
        ),
    )
    wraps: int | None = Field(
        None,
        description="唯一定性时的物理整圈数；无解/多解时为 null。",
    )
    candidates: CandidateRange | None = Field(
        None,
        description="多解时的可行整圈数与用量区间；唯一/无解时为 null。",
    )
    status: Literal["unique", "ambiguous", "no_solution"]
    reason: str = Field(..., description="判定说明。")


class SegmentError(BaseModel):
    # 允许用字段名 from_ 构造，同时仍以别名 "from" 输出 JSON
    model_config = ConfigDict(populate_by_name=True)

    segment_index: int
    kind: Literal["no_solution", "ambiguous"]
    from_: PointOut = Field(..., alias="from")
    to: PointOut
    duration_seconds: int
    delta_mod: int
    reason: str
    candidates: CandidateRange | None = None


class ReconcileResponse(BaseModel):
    status: Literal["unique", "inconclusive"] = Field(
        ...,
        description=(
            "unique：所有分段用量唯一，total_usage 有效；"
            "inconclusive：存在无解或多解分段，total_usage 为 null。"
        ),
    )
    modulus: int
    max_rate_per_sec: int
    points: list[PointOut]
    segments: list[SegmentOut]
    total_usage: int | None = Field(
        None,
        description="全部唯一时的总用量（各分段用量之和），否则为 null。"
    )
    errors: list[SegmentError] = Field(
        default_factory=list,
        description="全部出错区间（无解或多解），含原因；唯一定性时为空。",
    )


# ---------------------------------------------------------------------------
# 时间戳解析
# ---------------------------------------------------------------------------

# Unix 整数秒可表示的上限（UTC 9999-12-31T23:59:59Z）
_MAX_UNIX_SECONDS = 253402300799
_FRACTIONAL_SECOND_RE = re.compile(r"\.\d+")


def parse_unix_seconds(value: int | str, field_label: str) -> int:
    """将请求中的时间戳解析为 Unix 纪元整数秒。

    接受：
      * int：Unix 纪元秒（不允许 bool，已由 Pydantic strict 模式拒绝）；
      * str：带显式时区的 ISO 8601，且必须为秒精度（无小数秒）。
    """
    if isinstance(value, bool):  # 防御性检查（strict 模式通常已拦截）
        raise ValueError(f"{field_label} 必须是整数秒或 ISO 8601 字符串，不能是布尔值")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field_label} 不支持负的 Unix 时间戳：{value}")
        if value > _MAX_UNIX_SECONDS:
            raise ValueError(f"{field_label} 超出支持范围（晚于 9999-12-31T23:59:59Z）")
        return value

    s = value.strip()
    if _FRACTIONAL_SECOND_RE.search(s):
        raise ValueError(
            f"{field_label} 必须为秒精度时间戳，不允许小数秒：{value!r}"
        )
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field_label} 不是合法的 ISO 8601 时间戳：{value!r}"
        ) from exc
    if dt.tzinfo is None:
        raise ValueError(
            f"{field_label} 必须带显式时区（UTC 请使用 Z 后缀）：{value!r}"
        )
    if dt.microsecond != 0:
        raise ValueError(
            f"{field_label} 必须为秒精度时间戳，不允许亚秒部分：{value!r}"
        )
    seconds = int(dt.astimezone(timezone.utc).timestamp())
    if seconds < 0:
        raise ValueError(f"{field_label} 早于 1970-01-01T00:00:00Z：{value!r}")
    return seconds


def format_unix_seconds(seconds: int) -> str:
    """格式化为 RFC 3339 UTC 字符串，如 2025-10-09T12:00:00Z。"""
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 复核核心
# ---------------------------------------------------------------------------


def analyze_segment(
    *,
    t1: int,
    t2: int,
    v1: int,
    v2: int,
    modulus: int,
    max_rate: int,
) -> dict:
    """判定单个相邻读数区间。

    返回字段：duration_seconds / delta_mod / explicit_rollover /
    status / reason / usage / wraps / candidates。
    """
    duration = t2 - t1
    d = (v2 - v1) % modulus
    explicit_rollover = v2 < v1
    cap = max_rate * duration

    result: dict = {
        "duration_seconds": duration,
        "delta_mod": d,
        "explicit_rollover": explicit_rollover,
        "usage": None,
        "wraps": None,
        "candidates": None,
    }

    if d > cap:
        result["status"] = "no_solution"
        result["reason"] = (
            f"无解：模意义最小用量 d={d}（读数 {v1}->{v2}，模数 {modulus}），"
            f"已超过速率上限允许的最大增量 {max_rate}*{duration}={cap}；"
            f"任何整圈数只会使用量更大，不猜测圈数。"
        )
        return result

    n_max = (cap - d) // modulus

    if n_max == 0:
        # n 唯一为 0；物理整圈数仅由端点是否显式回绕决定
        wraps = 1 if explicit_rollover else 0
        result["status"] = "unique"
        result["usage"] = d
        result["wraps"] = wraps
        result["reason"] = (
            f"唯一解：分段用量为 {d}（物理整圈 {wraps} 圈）。"
            f"速率上限 {max_rate}*{duration}={cap} 不允许再多走一圈 "
            f"（{d}+{modulus}={d + modulus}>{cap}）。"
        )
        return result

    # n_max >= 1：n=0..n_max 全部可行，存在多种整圈可能
    min_wraps = 1 if explicit_rollover else 0
    max_wraps = n_max + (1 if explicit_rollover else 0)
    result["status"] = "ambiguous"
    result["candidates"] = CandidateRange(
        min_wraps=min_wraps,
        max_wraps=max_wraps,
        min_usage=d,
        max_usage=d + n_max * modulus,
    )
    result["reason"] = (
        f"多解：可行的未显式整圈数 n=0..{n_max}（共 {n_max + 1} 种），"
        f"对应物理整圈 {min_wraps}..{max_wraps} 圈、分段用量 "
        f"{d}..{d + n_max * modulus}，均满足 0<=C<={cap} 的速率约束，"
        f"端点数据无法区分，不猜测圈数。"
    )
    return result


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(
    title="累计水表读数用量复核 API",
    description=(
        "对带模数的累计水表/计数器读数序列进行用量复核：逐段判定"
        "“非负增量 + 每秒最大计数增量”约束下整圈回绕次数是否唯一；"
        "唯一时返回分段及总用量，无解或多解时报告全部出错区间与原因，"
        "绝不猜测圈数。"
    ),
    version="1.0.0",
)


@app.get("/", tags=["meta"])
def root() -> dict:
    """服务根路径：基本信息与文档入口。"""
    return {
        "service": "water-meter-usage-reconciliation",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
        "endpoint": "/api/v1/reconcile",
    }


@app.get("/health", tags=["meta"])
def health() -> dict:
    """存活探针。"""
    return {"status": "ok"}


@app.post(
    "/api/v1/reconcile",
    response_model=ReconcileResponse,
    tags=["reconciliation"],
    summary="复核累计水表读数序列的分段与总用量",
)
def reconcile(req: ReconcileRequest) -> ReconcileResponse:
    modulus = req.modulus
    max_rate = req.max_rate_per_sec
    raw_points = req.points

    # ---- 1. 解析时间戳；记录每点解析错误（不阻断后续读数校验） ----
    timestamps: list[int | None] = []
    validation_errors: list[dict] = []
    for i, p in enumerate(raw_points):
        try:
            timestamps.append(parse_unix_seconds(p.ts, f"points[{i}].ts"))
        except ValueError as exc:
            timestamps.append(None)
            validation_errors.append({"loc": ("body", "points", i, "ts"), "msg": str(exc)})

    # ---- 2. 时间戳严格递增（仅对成功解析的相邻点检查） ----
    for i in range(1, len(raw_points)):
        t_prev, t_cur = timestamps[i - 1], timestamps[i]
        if t_prev is None or t_cur is None:
            continue
        if t_cur == t_prev:
            validation_errors.append(
                {
                    "loc": ("body", "points", i, "ts"),
                    "msg": (
                        f"时间戳必须严格递增：points[{i - 1}] 与 points[{i}] "
                        f"时间戳相同（{format_unix_seconds(t_cur)}）。"
                    ),
                }
            )
        elif t_cur < t_prev:
            validation_errors.append(
                {
                    "loc": ("body", "points", i, "ts"),
                    "msg": (
                        f"时间戳必须严格递增：points[{i}]（{format_unix_seconds(t_cur)}）"
                        f"早于 points[{i - 1}]（{format_unix_seconds(t_prev)}）。"
                    ),
                }
            )

    # ---- 3. 读数必须在模数范围内 ----
    for i, p in enumerate(raw_points):
        if p.reading >= modulus:
            validation_errors.append(
                {
                    "loc": ("body", "points", i, "reading"),
                    "msg": (
                        f"读数 {p.reading} 超出模数范围：必须为 [0, {modulus - 1}] "
                        f"内的整数（模数 M={modulus}）。"
                    ),
                }
            )

    if validation_errors:
        # 汇总为标准的 422 请求校验错误
        raise HTTPException(
            status_code=422,
            detail=[{"loc": list(e["loc"]), "msg": e["msg"], "type": "value_error"} for e in validation_errors],
        )

    # 经过上面的校验，此处时间戳均为合法整数秒
    ts = [int(t) for t in timestamps]

    points_out = [
        PointOut(
            index=i,
            utc=format_unix_seconds(ts[i]),
            unix_seconds=ts[i],
            reading=p.reading,
        )
        for i, p in enumerate(raw_points)
    ]

    # ---- 4. 逐段复核（各段相互独立） ----
    segments: list[SegmentOut] = []
    errors: list[SegmentError] = []

    for i in range(len(raw_points) - 1):
        analysis = analyze_segment(
            t1=ts[i],
            t2=ts[i + 1],
            v1=raw_points[i].reading,
            v2=raw_points[i + 1].reading,
            modulus=modulus,
            max_rate=max_rate,
        )
        seg = SegmentOut(
            index=i,
            from_=points_out[i],
            to=points_out[i + 1],
            duration_seconds=analysis["duration_seconds"],
            delta_mod=analysis["delta_mod"],
            explicit_rollover=analysis["explicit_rollover"],
            usage=analysis["usage"],
            wraps=analysis["wraps"],
            candidates=analysis["candidates"],
            status=analysis["status"],
            reason=analysis["reason"],
        )
        segments.append(seg)

        if analysis["status"] != "unique":
            errors.append(
                SegmentError(
                    segment_index=i,
                    kind=analysis["status"],
                    from_=points_out[i],
                    to=points_out[i + 1],
                    duration_seconds=analysis["duration_seconds"],
                    delta_mod=analysis["delta_mod"],
                    reason=analysis["reason"],
                    candidates=analysis["candidates"],
                )
            )

    # ---- 5. 汇总 ----
    if errors:
        return ReconcileResponse(
            status="inconclusive",
            modulus=modulus,
            max_rate_per_sec=max_rate,
            points=points_out,
            segments=segments,
            total_usage=None,
            errors=errors,
        )

    total_usage = sum(int(s.usage) for s in segments)
    return ReconcileResponse(
        status="unique",
        modulus=modulus,
        max_rate_per_sec=max_rate,
        points=points_out,
        segments=segments,
        total_usage=total_usage,
        errors=[],
    )
