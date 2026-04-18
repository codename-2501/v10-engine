"""
engine/observability/metrics.py
Prometheus 메트릭 — /metrics 엔드포인트.
의존: prometheus_client (pip)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    logger.warning("prometheus_client 미설치 — 메트릭 비활성화")


def _make_gauge(name, doc, labels=()):
    if not _PROMETHEUS_AVAILABLE:
        return _NoopMetric()
    return Gauge(name, doc, labelnames=list(labels))

def _make_counter(name, doc, labels=()):
    if not _PROMETHEUS_AVAILABLE:
        return _NoopMetric()
    return Counter(name, doc, labelnames=list(labels))

def _make_histogram(name, doc, labels=(), buckets=None):
    if not _PROMETHEUS_AVAILABLE:
        return _NoopMetric()
    kwargs = {"labelnames": list(labels)}
    if buckets:
        kwargs["buckets"] = buckets
    return Histogram(name, doc, **kwargs)


class _NoopMetric:
    """prometheus_client 미설치 시 no-op 대체."""
    def labels(self, **_): return self
    def inc(self, *_, **__): pass
    def dec(self, *_, **__): pass
    def set(self, *_, **__): pass
    def observe(self, *_, **__): pass
    def time(self): return _NoopContext()

class _NoopContext:
    def __enter__(self): return self
    def __exit__(self, *_): pass


# ---------------------------------------------------------------------------
# 메트릭 정의
# ---------------------------------------------------------------------------

# 활성 에이전트 수 (Phase별)
ACTIVE_AGENTS = _make_gauge(
    "platform_active_agents_total",
    "현재 실행 중인 에이전트 수",
    labels=["phase"],
)

# Circuit Breaker 상태 (0=CLOSED, 1=HALF_OPEN, 2=OPEN)
CB_STATE = _make_gauge(
    "platform_circuit_breaker_state",
    "Provider Circuit Breaker 상태",
    labels=["provider"],
)

# DB 쓰기 지연
DB_WRITE_LATENCY = _make_histogram(
    "platform_db_write_latency_seconds",
    "DatabaseAdapter 쓰기 지연",
    buckets=[0.001, 0.01, 0.05, 0.1, 0.5, 1.0],
)

# API 호출 카운터
API_CALLS = _make_counter(
    "platform_model_api_calls_total",
    "AI Provider API 호출 횟수",
    labels=["provider", "model", "status"],
)

# 토큰 사용량 히스토그램
TOKEN_USAGE = _make_histogram(
    "platform_token_usage_per_call",
    "API 호출당 토큰 사용량",
    labels=["provider", "type"],
    buckets=[100, 500, 1000, 5000, 10000, 50000, 100000],
)

# Outbox 큐 깊이
OUTBOX_QUEUE_DEPTH = _make_gauge(
    "platform_outbox_queue_depth",
    "미처리 Outbox 메시지 수",
)

# DAG 처리 횟수
DAG_ADVANCES = _make_counter(
    "platform_dag_advances_total",
    "DAGAdvancer _advance_dag 실행 횟수",
    labels=["result"],  # success|error
)


def start_metrics_server(port: int = 8001) -> None:
    """
    독립 포트로 Prometheus scrape 엔드포인트 시작.
    FastAPI /metrics 대신 사용 가능.
    """
    if not _PROMETHEUS_AVAILABLE:
        logger.warning("prometheus_client 미설치 — 메트릭 서버 시작 안 함")
        return
    start_http_server(port)
    logger.info("metrics_server_started port=%s", port)


# ---------------------------------------------------------------------------
# Stage 15: 신규 지표 15종 — "-77% 토큰" · "-75% 시간" 증명용
# ---------------------------------------------------------------------------

# 청크 실행 지연 (node_type, spec_id 별)
CHUNK_DURATION = _make_histogram(
    "v8_chunk_duration_seconds",
    "chunk 실행 시간 (LLM 호출 + 파싱)",
    labels=["node_type", "spec_id"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],
)

# Prompt 캐시 hit ratio
CACHE_HIT_RATIO = _make_gauge(
    "v8_cache_hit_ratio",
    "Prompt Cache hit ratio (cache_read / total_input)",
    labels=["layer"],  # prompt|content
)

# Coverage 누락 카운트
COVERAGE_MISSING = _make_counter(
    "v8_coverage_missing_total",
    "Coverage verify 후 누락 item 누적",
    labels=["node_id"],
)

# Coverage retry 결과
COVERAGE_RETRY = _make_counter(
    "v8_coverage_retry_total",
    "Coverage retry_missing 결과",
    labels=["result"],  # success|fail|human
)

# Advisor reject
ADVISOR_REJECT = _make_counter(
    "v8_advisor_reject_total",
    "Advisor 일관성 reject 횟수",
    labels=["reason"],
)

# Advisor circuit state
ADVISOR_CIRCUIT_STATE = _make_gauge(
    "v8_advisor_circuit_state",
    "Advisor Circuit Breaker (0=closed, 1=open)",
    labels=["engagement_id"],
)

# Content cache hit/miss
CONTENT_CACHE_HIT = _make_counter(
    "v8_content_cache_hit_total",
    "Content hash cache 히트",
)
CONTENT_CACHE_MISS = _make_counter(
    "v8_content_cache_miss_total",
    "Content hash cache 미스",
)

# API 세마포어 대기 시간
API_SEMAPHORE_WAIT = _make_histogram(
    "v8_api_semaphore_wait_seconds",
    "_api_semaphore acquire 대기 시간",
    buckets=[0.01, 0.1, 1, 5, 10, 30, 60],
)

# 계정 쿼터 remaining (추정)
ACCOUNT_QUOTA = _make_gauge(
    "v8_account_quota_remaining",
    "계정별 일일 잔여 쿼터 (토큰)",
    labels=["account"],
)

# 529 카운트
OVERLOAD_529 = _make_counter(
    "v8_529_total",
    "529/overload 발생",
    labels=["account"],
)

# task_snapshot 크기
TASK_SNAPSHOT_SIZE = _make_gauge(
    "v8_task_snapshot_size_bytes",
    "chunked 노드 task_snapshot JSON 크기",
    labels=["node_id"],
)

# Prompt 입력/출력 토큰
PROMPT_INPUT_TOKENS = _make_histogram(
    "v8_prompt_input_tokens",
    "호출당 input 토큰 (cache_read 제외)",
    labels=["spec_id"],
    buckets=[500, 2_000, 10_000, 30_000, 100_000, 244_000],
)
PROMPT_OUTPUT_TOKENS = _make_histogram(
    "v8_prompt_output_tokens",
    "호출당 output 토큰",
    labels=["spec_id"],
    buckets=[500, 2_000, 8_000, 16_000, 32_000, 64_000],
)

# Schema validation fail
SCHEMA_FAIL = _make_counter(
    "v8_schema_validation_fail_total",
    "output_schema strict 검증 실패",
    labels=["spec_id"],
)
