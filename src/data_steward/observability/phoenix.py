from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from data_steward.config import get_settings

_CONFIGURED = False
_PROVIDER = None


def configure_phoenix(settings=None) -> bool:
    """Send traces to the Arize AX space in .env, with Phoenix Cloud as a fallback."""
    global _CONFIGURED, _PROVIDER
    if _CONFIGURED:
        return True
    settings = settings or get_settings()
    if not settings.tracing_enabled:
        return False
    try:
        if settings.arize_space_id and settings.arize_api_key:
            _PROVIDER = _register_arize_ax(settings)
            _CONFIGURED = True
            return True
        if settings.phoenix_endpoint or settings.phoenix_api_key:
            from phoenix.otel import register

            _PROVIDER = register(
                project_name=settings.tracing_project,
                endpoint=settings.phoenix_endpoint,
                api_key=settings.phoenix_api_key,
                auto_instrument=True,
                batch=False,
            )
            _CONFIGURED = True
            return True
    except Exception:
        _CONFIGURED = False
        _PROVIDER = None
        return False
    return False


def _register_arize_ax(settings):
    from arize.otel import register

    kwargs: dict = {
        "space_id": settings.arize_space_id,
        "api_key": settings.arize_api_key,
        "project_name": settings.tracing_project,
        "batch": False,
        "auto_instrument": True,
        "verbose": False,
    }
    endpoint = (settings.arize_collector_endpoint or "").rstrip("/")
    if endpoint:
        if endpoint in {"https://otlp.arize.com", "http://otlp.arize.com"}:
            endpoint = "https://otlp.arize.com/v1"
        kwargs["endpoint"] = endpoint
    return register(**kwargs)


def _instrument(tracer_provider) -> None:
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
    except Exception:
        pass
    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
    except Exception:
        pass


def current_trace_id() -> str | None:
    context = _current_span_context()
    if context is None:
        return None
    return format(context.trace_id, "032x")


def current_span_id() -> str | None:
    context = _current_span_context()
    if context is None:
        return None
    return format(context.span_id, "016x")


def _current_span_context():
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        context = span.get_span_context()
        if not context or not context.is_valid:
            return None
        return context
    except Exception:
        return None


def record_eval(name: str, *, label: str, score: float, explanation: str = "") -> None:
    """Attach an eval to the current span so Arize AX can show it on the trace."""
    try:
        from opentelemetry import trace
    except Exception:
        return
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context or not context.is_valid:
        return
    safe = "".join(char if char.isalnum() or char == "_" else "_" for char in name)
    span.set_attribute(f"eval.{safe}.label", label)
    span.set_attribute(f"eval.{safe}.score", float(score))
    if explanation:
        span.set_attribute(f"eval.{safe}.explanation", explanation[:2000])


def flush_traces(timeout_millis: int = 10_000) -> None:
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            flush(timeout_millis)
    except Exception:
        return


def publish_span_evaluations(
    rows: list[dict[str, Any]],
    settings=None,
) -> str:
    """Best-effort: copy span evals into Arize AX's Evaluations column.

    Requires the optional ``arize`` SDK. Span attributes are already on the
    traces from ``record_eval`` even if this publish step is skipped.
    """
    settings = settings or get_settings()
    if not rows or not settings.arize_space_id or not settings.arize_api_key:
        return "skipped_no_arize"
    try:
        from arize import ArizeClient
        import pandas as pd
    except Exception:
        return "logged_spans"
    try:
        flush_traces()
        client = ArizeClient(api_key=settings.arize_api_key)
        frame = pd.DataFrame(rows)
        client.spans.update_evaluations(
            space_id=settings.arize_space_id,
            project_name=settings.tracing_project,
            dataframe=frame,
        )
        return "published"
    except Exception:
        return "logged_spans"


@contextmanager
def trace_incident(incident_id: str, **attributes: str):
    from opentelemetry import trace

    settings = get_settings()
    tracer = trace.get_tracer("data_steward")
    with tracer.start_as_current_span("data_steward.investigation") as span:
        span.set_attribute("incident.id", incident_id)
        span.set_attribute("openinference.project.name", settings.tracing_project)
        if settings.arize_space_id:
            span.set_attribute("arize.space_id", settings.arize_space_id)
            span.set_attribute("arize.project.name", settings.tracing_project)
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, str(value))
        yield span


def phoenix_trace_url(trace_id: str | None) -> str | None:
    if not trace_id:
        return None
    settings = get_settings()
    if settings.arize_space_id and settings.arize_api_key:
        return f"https://app.arize.com/projects/{settings.tracing_project}"
    base = (settings.phoenix_client_url or "").rstrip("/")
    if not base:
        return trace_id
    return f"{base}/projects/{settings.tracing_project}/traces/{trace_id}"
