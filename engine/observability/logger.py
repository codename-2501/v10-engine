"""
engine/observability/logger.py
구조화 로깅 — structlog JSON 출력.
correlation_id: contextvars 기반 요청 추적.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

import structlog

# 요청별 correlation_id (asyncio task 단위 격리)
_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def set_correlation_id(cid: str | None = None) -> str:
    """새 correlation_id 설정 (없으면 UUID 생성)."""
    cid = cid or str(uuid.uuid4())
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id.get()


def _add_correlation_id(logger, method_name, event_dict):
    """structlog 프로세서: correlation_id 자동 주입."""
    cid = _correlation_id.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def _add_trace_id(logger, method_name, event_dict):
    """structlog 프로세서: OpenTelemetry trace_id 자동 주입."""
    try:
        from opentelemetry import trace as _otel_trace
        span = _otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
    except Exception:
        pass
    return event_dict


def configure_logging(env: str = "production") -> None:
    """
    앱 시작 시 한 번 호출.
    production: JSON 출력
    development: 컬러 콘솔 출력
    """
    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _add_correlation_id,
        _add_trace_id,
    ]

    if env == "production":
        processors = shared_processors + [structlog.processors.JSONRenderer()]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True)
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # stdlib logging 기본 설정
    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """structlog 로거 반환."""
    return structlog.get_logger(name)
