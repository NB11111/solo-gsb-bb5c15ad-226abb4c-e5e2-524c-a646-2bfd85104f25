# 累计水表读数用量复核 API

基于 **Python 3.11 + FastAPI** 的容器化服务。对带模数（回绕）的累计水表/计数器读数序列进行用量复核：逐段判定在「非负增量」与「每秒最大计数增量」约束下，整圈回绕次数是否**唯一**；唯一时返回分段用量与总用量；**无解或多解时返回全部出错区间及原因，绝不猜测圈数**。

---

## 1. 复核原理

设相邻两个采样点时间戳为 `t1 < t2`（秒），读数为 `v1、v2`，计数器模数为 `M`（读数范围 `[0, M-1]`），每秒最大计数增量为 `R`。

由于计数器回绕，真实用量只能写为：

```
d = (v2 - v1) mod M          # 模意义读数差，0 <= d < M
C = d + n * M，  n = 0,1,2,… # n：未在端点读数中体现的整圈数
```

- 若 `v2 < v1`，端点读数发生了一次**显式回绕**，物理整圈数 `wraps = n + 1`；否则 `wraps = n`。
- 速率约束：分段总增量不得超过 `cap = R × (t2 - t1)`。

判定规则（每段独立）：

| 条件 | 结论 | 说明 |
|---|---|---|
| `d > cap` | **无解** `no_solution` | 即使零整圈，最小用量 `d` 已超速率上限；再多走几圈只会更大 |
| `nmax = (cap - d) // M = 0` | **唯一** `unique` | 只能 `n=0`，分段用量 `C=d`，物理整圈数由端点是否显式回绕确定（0 或 1） |
| `nmax >= 1` | **多解** `ambiguous` | `n=0..nmax` 均可行，端点数据无法区分，返回可行圈数/用量区间，不猜测 |

> 重要：速率上限约束的是分段**总**增量。匀速分配始终存在（各秒增量 `floor(C/Δt)` 或 `ceil`），因此 `C <= cap` 既是必要条件也是充分条件。
>
> 存在出错段时，各段仍会全部计算并返回，但 `total_usage` 为 `null`——不把猜测值计入总量。

---

## 2. 启动命令（容器一键启动）

前置条件：已安装 Docker 与 Docker Compose 插件（Docker Desktop / Docker Engine 20.10+ 自带）。

```bash
# 1. 克隆/进入项目目录
cd water-meter-reconciliation

# 2. （可选）自定义宿主机端口
cp .env.example .env
#   编辑 .env，例如 HOST_PORT=8080

# 3. 构建镜像并后台启动（自动安装 requirements.txt 中的全部依赖）
docker compose up -d --build

# 查看状态 / 日志
docker compose ps
docker compose logs -f

# 停止
docker compose down
```

也可不使用 Compose：

```bash
docker build -t water-meter-reconciliation:1.0.0 .
docker run -d --name water-meter-reconciliation -p 8000:8000 \
  water-meter-reconciliation:1.0.0
```

## 3. 必要配置

| 配置 | 位置 | 默认值 | 说明 |
|---|---|---|---|
| `HOST_PORT` | `.env` / 环境变量 | `8000` | 宿主机映射端口；被占用时改为其他端口（如 `8080:8000`） |
| 容器内监听端口 | `Dockerfile` / `docker-compose.yml` | `8000` | uvicorn 监听 `0.0.0.0:8000`，一般无需修改 |
| `requirements.txt` | 项目根 | 固定版本 | 构建镜像时自动安装：fastapi 0.115.6、uvicorn[standard] 0.34.0、pydantic 2.10.4 |

服务无数据库、无外部依赖，唯一的配置就是端口。

## 4. API 地址与端口

启动后（默认宿主机端口 `8000`）：

| 地址 | 用途 |
|---|---|
| http://localhost:8000/ | 服务信息 |
| http://localhost:8000/health | 健康检查（容器 HEALTHCHECK 也调用此端点） |
| http://localhost:8000/docs | Swagger UI（可在线发请求） |
| http://localhost:8000/redoc | ReDoc 文档 |
| http://localhost:8000/openapi.json | OpenAPI 规范 |
| `POST http://localhost:8000/api/v1/reconcile` | **用量复核接口** |

---

## 5. 接口说明

### 请求体 `POST /api/v1/reconcile`

```json
{
  "points": [
    {"ts": "2025-10-09T12:00:00Z", "reading": 100},
    {"ts": "2025-10-09T12:01:00Z", "reading": 130}
  ],
  "modulus": 1000,
  "max_rate_per_sec": 10
}
```

| 字段 | 类型 | 约束 |
|---|---|---|
| `points` | 数组 | **至少 2 个点**，按时间顺序排列 |
| `points[].ts` | int 或 string | **秒精度 UTC 时间戳**：Unix 整数秒（`1760000000`），或带显式时区的 ISO 8601（`2025-10-09T12:00:00Z` / `2025-10-09T13:00:00+01:00`，会归一化到 UTC）；**禁止小数秒**（如 `.500`）、禁止无时区的朴素时间 |
| `points[].reading` | int | 模数范围内的非负整数 `[0, modulus-1]` |
| `modulus` | int | 计数器模数 `M >= 1` |
| `max_rate_per_sec` | int | 每秒最大计数增量 `R >= 0` |

额外校验：时间戳必须**严格递增**（相等或倒退均拒绝）；所有字段严格类型检查（`float`、`bool`、多余字段一律 `422`）。

### 响应

**HTTP 200（业务结果，`status` 区分）**

- `status = "unique"`：所有分段唯一，`total_usage` 为各分段用量之和；
- `status = "inconclusive"`：存在无解/多解段，`total_usage = null`，`errors[]` 列出**全部**出错区间（下标、两端点、时长、原因、多解时的可行圈数/用量区间）。

**HTTP 422**：请求本身不合法（类型、取值范围、时间戳格式、非严格递增、读数超模数范围等），`detail` 给出逐字段原因。

`segments[]` 每段包含：`duration_seconds`、`delta_mod`（即 d）、`explicit_rollover`（端点是否显式回绕）、`usage`、`wraps`（物理整圈数）、`candidates`（多解时 `min_wraps/max_wraps/min_usage/max_usage`）、`status`、`reason`（中文判定说明）。

### 示例 1：唯一（无回绕）

`M=1000, R=10`，60 秒内 `100 → 130`：`d=30 <= cap=600`，再多走一圈需 `1030 > 600`。

```bash
curl -s -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"ts": "2025-10-09T12:00:00Z", "reading": 100},
      {"ts": "2025-10-09T12:01:00Z", "reading": 130}
    ],
    "modulus": 1000,
    "max_rate_per_sec": 10
  }'
```

```json
{
  "status": "unique",
  "modulus": 1000,
  "max_rate_per_sec": 10,
  "total_usage": 30,
  "errors": [],
  "segments": [
    {
      "index": 0,
      "from": {"index": 0, "utc": "2025-10-09T12:00:00Z", "unix_seconds": 1760011200, "reading": 100},
      "to":   {"index": 1, "utc": "2025-10-09T12:01:00Z", "unix_seconds": 1760011260, "reading": 130},
      "duration_seconds": 60,
      "delta_mod": 30,
      "explicit_rollover": false,
      "usage": 30,
      "wraps": 0,
      "candidates": null,
      "status": "unique",
      "reason": "唯一解：分段用量为 30（物理整圈 0 圈）。速率上限 10*60=600 不允许再多走一圈（30+1000=1030>600）。"
    }
  ],
  "points": [ "...与 segment 端点一致..." ]
}
```

### 示例 2：唯一（恰好回绕 1 圈）

60 秒内 `990 → 10`：`d=(10-990) mod 1000 = 20`，显式回绕；`20+1000=1020 > cap=600`，只能是 1 圈、用量 20。

```bash
curl -s -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"ts": "2025-10-09T12:00:00Z", "reading": 990},
      {"ts": "2025-10-09T12:01:00Z", "reading": 10}
    ],
    "modulus": 1000,
    "max_rate_per_sec": 10
  }'
# -> status "unique"，usage 20，wraps 1，total_usage 20
```

### 示例 3：多解（读数相同也可能走了整圈）

100 秒内 `500 → 500`，`R=10`，`cap=1000 >= 0+1000`：走 0 圈（用量 0）或 1 圈（用量 1000）都合法 → **不猜测**。

```bash
curl -s -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"ts": "2025-10-09T12:00:00Z", "reading": 500},
      {"ts": "2025-10-09T12:01:40Z", "reading": 500}
    ],
    "modulus": 1000,
    "max_rate_per_sec": 10
  }'
```

```json
{
  "status": "inconclusive",
  "total_usage": null,
  "errors": [
    {
      "segment_index": 0,
      "kind": "ambiguous",
      "duration_seconds": 100,
      "delta_mod": 0,
      "candidates": {"min_wraps": 0, "max_wraps": 1, "min_usage": 0, "max_usage": 1000},
      "reason": "多解：可行的未显式整圈数 n=0..1（共 2 种）……端点数据无法区分，不猜测圈数。"
    }
  ]
}
```

### 示例 4：无解

10 秒内 `100 → 50`，`M=1000, R=10`：`d=950 > cap=100`，任何非负圈数都超速率。

```bash
curl -s -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"ts": "2025-10-09T12:00:00Z", "reading": 100},
      {"ts": "2025-10-09T12:00:10Z", "reading": 50}
    ],
    "modulus": 1000,
    "max_rate_per_sec": 10
  }'
# -> status "inconclusive"，errors[0].kind "no_solution"，total_usage null
```

### 示例 5：请求不合法（422）

时间戳非严格递增 / 读数超出模数范围 / 小数秒等：

```json
{
  "detail": [
    {"loc": ["body", "points", 1, "ts"], "msg": "时间戳必须严格递增：……", "type": "value_error"}
  ]
}
```

---

## 6. 本地开发与测试（可选，不依赖 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000
pytest -q
```

测试覆盖：唯一/多解/无解、显式回绕、边界（`d == cap` 唯一、`d+M == cap` 多解）、模数 1、速率 0、整数秒与带偏移 ISO 时间戳、小数秒/朴素时间/非递增/超范围/类型错误等校验。

## 7. 项目结构

```
.
├── app/
│   ├── __init__.py
│   └── main.py            # FastAPI 应用：模型、时间戳解析、复核核心、接口
├── tests/
│   └── test_reconcile.py  # 单元/接口测试
├── Dockerfile             # 容器镜像（python:3.11-slim，非 root，自带 HEALTHCHECK）
├── docker-compose.yml     # 一键编排
├── .env.example           # 端口配置样例
├── requirements.txt       # 运行依赖（固定版本）
├── requirements-dev.txt   # 测试依赖
├── pytest.ini
├── .dockerignore
└── README.md
```
