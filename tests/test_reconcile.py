"""用量复核 API 的测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_and_root():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["endpoint"] == "/api/v1/reconcile"


def _reconcile(points, modulus, rate):
    return client.post(
        "/api/v1/reconcile",
        json={"points": points, "modulus": modulus, "max_rate_per_sec": rate},
    )


# ---------------------------------------------------------------------------
# 唯一定性
# ---------------------------------------------------------------------------


def test_unique_no_rollover():
    # 60s 内 100 -> 130，M=1000，R=10，cap=600；d=30，再多走一圈需 1030 > 600
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 100},
            {"ts": "2025-10-09T12:01:00Z", "reading": 130},
        ],
        1000,
        10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unique"
    assert body["total_usage"] == 30
    seg = body["segments"][0]
    assert seg["status"] == "unique"
    assert seg["usage"] == 30
    assert seg["wraps"] == 0
    assert seg["explicit_rollover"] is False
    assert seg["delta_mod"] == 30
    assert seg["from"]["utc"] == "2025-10-09T12:00:00Z"
    assert seg["to"]["unix_seconds"] == seg["from"]["unix_seconds"] + 60


def test_unique_with_explicit_rollover():
    # 60s 内 990 -> 10，M=1000，d=20；走一圈需要 1020 > cap=600
    # => 只能发生 1 次物理整圈，用量 20，唯一
    r = _reconcile(
        [
            {"ts": 1760000000, "reading": 990},
            {"ts": 1760000060, "reading": 10},
        ],
        1000,
        10,
    )
    body = r.json()
    assert body["status"] == "unique"
    assert body["total_usage"] == 20
    seg = body["segments"][0]
    assert seg["usage"] == 20
    assert seg["wraps"] == 1
    assert seg["explicit_rollover"] is True


def test_multi_segment_all_unique_total_is_sum():
    # 段1: 0 -> 100 (60s, d=100, cap=600, 再圈 1100>600) 唯一 100
    # 段2: 100 -> 50 (10s, d=950, cap=100) 无解
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 0},
            {"ts": "2025-10-09T12:01:00Z", "reading": 100},
            {"ts": "2025-10-09T12:01:10Z", "reading": 50},
        ],
        1000,
        10,
    )
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "inconclusive"
    assert body["total_usage"] is None
    assert len(body["errors"]) == 1
    assert body["errors"][0]["segment_index"] == 1
    assert body["errors"][0]["kind"] == "no_solution"
    assert body["segments"][0]["status"] == "unique"
    assert body["segments"][1]["status"] == "no_solution"
    assert body["segments"][1]["usage"] is None


def test_zero_rate_zero_delta_is_unique():
    # R=0 且读数不变：用量 0 唯一
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 5},
            {"ts": "2025-10-09T12:00:30Z", "reading": 5},
        ],
        100,
        0,
    )
    body = r.json()
    assert body["status"] == "unique"
    assert body["total_usage"] == 0


def test_same_reading_can_be_ambiguous():
    # 读数不变 (d=0)，但 R=10、Δt=100，cap=1000：可能走了 0 圈或 1 圈
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 500},
            {"ts": "2025-10-09T12:01:40Z", "reading": 500},
        ],
        1000,
        10,
    )
    body = r.json()
    assert body["status"] == "inconclusive"
    seg = body["segments"][0]
    assert seg["status"] == "ambiguous"
    assert seg["usage"] is None
    assert seg["wraps"] is None
    c = seg["candidates"]
    assert c["min_wraps"] == 0
    assert c["max_wraps"] == 1
    assert c["min_usage"] == 0
    assert c["max_usage"] == 1000


def test_ambiguous_explicit_rollover_two_choices():
    # 990 -> 10, d=20, cap=1040, M=1000：n=0(1圈,用量20) 或 n=1(2圈,用量1020)
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 990},
            {"ts": "2025-10-09T12:01:44Z", "reading": 10},
        ],
        1000,
        10,
    )
    body = r.json()
    assert body["status"] == "inconclusive"
    err = body["errors"][0]
    assert err["kind"] == "ambiguous"
    c = err["candidates"]
    assert c["min_wraps"] == 1
    assert c["max_wraps"] == 2
    assert c["min_usage"] == 20
    assert c["max_usage"] == 1020


def test_boundary_d_plus_m_equals_cap_is_ambiguous():
    # d=30, cap=1030 => (1030-30)//1000 = 1 => 多解（边界包含）
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 100},
            {"ts": "2025-10-09T12:01:43Z", "reading": 130},
        ],
        1000,
        10,
    )
    assert r.json()["segments"][0]["status"] == "ambiguous"


def test_boundary_d_equals_cap_is_unique():
    # d=600, cap=600 => nmax=0 唯一；d+M=1600>cap
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 0},
            {"ts": "2025-10-09T12:01:00Z", "reading": 600},
        ],
        1000,
        10,
    )
    seg = r.json()["segments"][0]
    assert seg["status"] == "unique"
    assert seg["usage"] == 600


def test_modulus_one():
    # M=1，读数只能为 0，d 恒为 0；R=1, Δt=2 => cap=2，nmax=2 多解
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 0},
            {"ts": "2025-10-09T12:00:02Z", "reading": 0},
        ],
        1,
        1,
    )
    seg = r.json()["segments"][0]
    assert seg["status"] == "ambiguous"
    assert seg["candidates"]["max_wraps"] == 2


# ---------------------------------------------------------------------------
# 请求校验错误（422）
# ---------------------------------------------------------------------------


def test_fractional_seconds_rejected():
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00.500Z", "reading": 0},
            {"ts": "2025-10-09T12:01:00Z", "reading": 1},
        ],
        1000,
        10,
    )
    assert r.status_code == 422
    assert "秒精度" in str(r.json()["detail"])


def test_naive_timestamp_rejected():
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00", "reading": 0},
            {"ts": "2025-10-09T12:01:00Z", "reading": 1},
        ],
        1000,
        10,
    )
    assert r.status_code == 422
    assert "时区" in str(r.json()["detail"])


def test_non_strictly_increasing_rejected():
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 0},
            {"ts": "2025-10-09T12:00:00Z", "reading": 1},
        ],
        1000,
        10,
    )
    assert r.status_code == 422
    assert "严格递增" in str(r.json()["detail"])

    r = _reconcile(
        [
            {"ts": "2025-10-09T12:01:00Z", "reading": 0},
            {"ts": "2025-10-09T12:00:00Z", "reading": 1},
        ],
        1000,
        10,
    )
    assert r.status_code == 422


def test_reading_out_of_range_rejected():
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": 1000},
            {"ts": "2025-10-09T12:01:00Z", "reading": 0},
        ],
        1000,
        10,
    )
    assert r.status_code == 422
    assert "模数范围" in str(r.json()["detail"])


def test_float_inputs_rejected():
    # 严格模式：float 不能冒充 int
    r = _reconcile(
        [
            {"ts": 1760000000.5, "reading": 0},
            {"ts": 1760000060, "reading": 1},
        ],
        1000,
        10,
    )
    assert r.status_code == 422

    r = client.post(
        "/api/v1/reconcile",
        json={
            "points": [
                {"ts": "2025-10-09T12:00:00Z", "reading": 0},
                {"ts": "2025-10-09T12:01:00Z", "reading": 1},
            ],
            "modulus": 1000.0,
            "max_rate_per_sec": 10,
        },
    )
    assert r.status_code == 422


def test_bool_reading_rejected():
    r = _reconcile(
        [
            {"ts": "2025-10-09T12:00:00Z", "reading": True},
            {"ts": "2025-10-09T12:01:00Z", "reading": 1},
        ],
        1000,
        10,
    )
    assert r.status_code == 422


def test_too_few_points_rejected():
    r = _reconcile(
        [{"ts": "2025-10-09T12:00:00Z", "reading": 0}],
        1000,
        10,
    )
    assert r.status_code == 422


def test_bad_modulus_and_rate_rejected():
    r = client.post(
        "/api/v1/reconcile",
        json={
            "points": [
                {"ts": "2025-10-09T12:00:00Z", "reading": 0},
                {"ts": "2025-10-09T12:01:00Z", "reading": 1},
            ],
            "modulus": 0,
            "max_rate_per_sec": 10,
        },
    )
    assert r.status_code == 422

    r = client.post(
        "/api/v1/reconcile",
        json={
            "points": [
                {"ts": "2025-10-09T12:00:00Z", "reading": 0},
                {"ts": "2025-10-09T12:01:00Z", "reading": 1},
            ],
            "modulus": 1000,
            "max_rate_per_sec": -1,
        },
    )
    assert r.status_code == 422


def test_timezone_offset_normalized_to_utc():
    # +01:00 的 13:00 等于 UTC 12:00，与第二个点相差 60 秒
    r = _reconcile(
        [
            {"ts": "2025-10-09T13:00:00+01:00", "reading": 100},
            {"ts": "2025-10-09T12:01:00Z", "reading": 130},
        ],
        1000,
        10,
    )
    assert r.status_code == 200
    seg = r.json()["segments"][0]
    assert seg["duration_seconds"] == 60
    assert seg["from"]["utc"] == "2025-10-09T12:00:00Z"


def test_extra_field_rejected():
    r = client.post(
        "/api/v1/reconcile",
        json={
            "points": [
                {"ts": "2025-10-09T12:00:00Z", "reading": 0, "x": 1},
                {"ts": "2025-10-09T12:01:00Z", "reading": 1},
            ],
            "modulus": 1000,
            "max_rate_per_sec": 10,
        },
    )
    assert r.status_code == 422


def test_unix_integer_timestamps_supported():
    r = _reconcile(
        [
            {"ts": 1760000000, "reading": 0},
            {"ts": 1760000060, "reading": 30},
        ],
        1000,
        10,
    )
    assert r.status_code == 200
    assert r.json()["total_usage"] == 30
