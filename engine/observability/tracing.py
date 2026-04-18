"""
OpenTelemetry 분산 추적 — optional.
OTEL_EXPORTER_OTLP_ENDPOINT 환경변수 없으면 no-op.
"""

import os
from contextlib import asynccontextmanager

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


def setup_tracing(service_name: str = "ax-factory-v9") -> None:
    """
    OpenTelemetry endpoint 환경변수 있을 때만 활성화.
    없으면 no-op. ImportError 처리로 optional 의존성.
    """
    if not _OTEL_AVAILABLE:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        _otel_trace.set_tracer_provider(provider)
    except Exception:
        pass


@asynccontextmanager
async def llm_span(model: str, node_id: str = ""):
    """LLM 호출 span. OTEL 미활성 시 그냥 통과."""
    if not _OTEL_AVAILABLE:
        yield
        return

    try:
        tracer = _otel_trace.get_tracer("ax-factory")
        with tracer.start_as_current_span("llm.call") as span:
            span.set_attribute("llm.model", model)
            if node_id:
                span.set_attribute("node.id", node_id)
            yield span
    except Exception:
        yield
