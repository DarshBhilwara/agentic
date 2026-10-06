import os
import logging
import re
import hashlib
from datetime import datetime, timezone
from contextlib import contextmanager


def _attribute_value(value):
    return value if isinstance(value, (str, bool, int, float)) else str(value)


CONTENT_CAPTURE = os.getenv("AGENT_TELEMETRY_CONTENT_CAPTURE", "redacted").lower()
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|password|secret|token)\s*([:=]|\s)\s*(?:bearer\s+)?[^\s,;]+"
)


def redact_text(value, *, limit=4096):
    text = _SECRET_PATTERN.sub(r"\1\2[REDACTED]", str(value))
    return text[:limit] + ("... [truncated]" if len(text) > limit else "")


def safe_content(value, *, limit=4096):
    """Return content according to the configured capture policy.

    full is useful only in a trusted evaluation environment.  Production
    defaults to redacted capture; off keeps a stable content fingerprint.
    """
    if value is None:
        return None
    text = str(value)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    if CONTENT_CAPTURE in ("off", "none", "metadata"):
        return {"sha256": digest, "length": len(text), "capture": "off"}
    text = redact_text(text, limit=limit)
    if CONTENT_CAPTURE == "full":
        return {"text": text, "sha256": digest, "length": len(str(value)), "capture": "full"}
    return {"text": text, "sha256": digest, "length": len(str(value)), "capture": "redacted"}


def safe_payload(value):
    """Apply capture policy recursively to an untrusted event payload."""
    if isinstance(value, str):
        return safe_content(value)
    if isinstance(value, dict):
        return {str(key): safe_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_payload(item) for item in value[:100]]
    return value


try:
    from opentelemetry import context as otel_context, propagate
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({
        SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", "agent-worker"),
        SERVICE_VERSION: os.getenv("OTEL_SERVICE_VERSION", "0.1"),
        "deployment.environment": os.getenv("ENVIRONMENT", "agentic"),
    })

    tracer_provider = TracerProvider(resource=resource)
    trace_endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if trace_endpoint:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=trace_endpoint))
        )
    trace.set_tracer_provider(tracer_provider)
    tracer = trace.get_tracer("agentic", "0.1")

    metrics_endpoint = os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    if metrics_endpoint:
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=metrics_endpoint),
            export_interval_millis=int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "10000")),
        )
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    meter = metrics.get_meter("agentic", "0.1")
    task_counter = meter.create_counter("agent.tasks.total", description="Agent tasks processed")
    task_error_counter = meter.create_counter("agent.task.errors.total", description="Agent task failures")
    inference_counter = meter.create_counter("agent.inference.requests.total", description="Inference requests")
    event_counter = meter.create_counter("agent.telemetry.events.total", description="Execution ledger events")
    handoff_counter = meter.create_counter("agent.handoffs.total", description="Agent handoffs")
    program_duration = meter.create_histogram("agent.program.duration", unit="ms")
    program_queue = meter.create_histogram("agent.program.queue.duration", unit="ms")
    program_steps = meter.create_histogram("agent.program.inference.steps", unit="{step}")

except Exception:
    logging.exception("OpenTelemetry initialization failed; telemetry is disabled")
    class _NoopSpan:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def set_attribute(self, *_args): return None
        def record_exception(self, *_args): return None
        def add_event(self, *_args, **_kwargs): return None

    class _NoopTracer:
        def start_as_current_span(self, *_args, **_kwargs): return _NoopSpan()

    class _NoopMetric:
        def add(self, *_args, **_kwargs): return None
        def record(self, *_args, **_kwargs): return None

    tracer = _NoopTracer()
    task_counter = task_error_counter = inference_counter = event_counter = handoff_counter = _NoopMetric()
    program_duration = program_queue = program_steps = _NoopMetric()
    otel_context = propagate = None


@contextmanager
def span(name, *, context=None, **attributes):
    kwargs = {"context": context} if context is not None else {}
    with tracer.start_as_current_span(name, **kwargs) as current:
        for key, value in attributes.items():
            current.set_attribute(key, _attribute_value(value))
        yield current


def inject_context():
    carrier = {}
    if propagate is not None:
        propagate.inject(carrier)
    return carrier


def trace_identity():
    """Expose trace correlation without coupling the ledger to an exporter."""
    if "trace" not in globals():
        return {}
    context = trace.get_current_span().get_span_context()
    if not context or not context.is_valid:
        return {}
    return {"trace.id": format(context.trace_id, "032x"), "span.id": format(context.span_id, "016x")}


@contextmanager
def parent_context(carrier):
    if propagate is None:
        yield None
        return
    context = propagate.extract(carrier or {})
    token = otel_context.attach(context)
    try:
        yield context
    finally:
        otel_context.detach(token)


@contextmanager
def intent_span(prompt, *, user, agent_id, session_id, turn_id, turn_number,
                benchmark=None, case_id=None, model=None, run_id=None, agent_run_id=None,
                parent_agent_run_id=None):
    """Record agent intent with OpenTelemetry GenAI attributes/events."""
    attributes = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": os.getenv("OTEL_AGENT_NAME", "enterprise-agent"),
        "agent.id": agent_id,
        "agent.run.id": agent_run_id or turn_id,
        "agent.parent.run.id": parent_agent_run_id or "",
        "run.id": run_id or turn_id,
        "agent.session.id": session_id,
        "gen_ai.conversation.id": session_id,
        "agent.turn.id": turn_id,
        "agent.turn.number": turn_number,
        "user.id": user,
        "intent.started_at": datetime.now(timezone.utc).isoformat(),
    }
    if model:
        attributes["gen_ai.request.model"] = model
    if benchmark:
        attributes["benchmark.name"] = benchmark
    if case_id:
        attributes["benchmark.case.id"] = case_id
    with span("gen_ai.invoke_agent", **attributes) as current:
        current.add_event("gen_ai.user.message", {
            "gen_ai.event.content": str(safe_content(prompt)),
            "gen_ai.event.role": "user",
        })
        try:
            yield current
        finally:
            current.set_attribute("intent.completed_at", datetime.now(timezone.utc).isoformat())
