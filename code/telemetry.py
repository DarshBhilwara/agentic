import os
from contextlib import contextmanager


def _attribute_value(value):
    return value if isinstance(value, (str, bool, int, float)) else str(value)


try:
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

except Exception:
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

    tracer = _NoopTracer()
    task_counter = task_error_counter = inference_counter = _NoopMetric()


@contextmanager
def span(name, **attributes):
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, _attribute_value(value))
        yield current


@contextmanager
def intent_span(prompt, *, user, benchmark=None, case_id=None, model=None):
    """Record agent intent with OpenTelemetry GenAI attributes/events."""
    attributes = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": os.getenv("OTEL_AGENT_NAME", "enterprise-agent"),
        "user.id": user,
    }
    if model:
        attributes["gen_ai.request.model"] = model
    if benchmark:
        attributes["benchmark.name"] = benchmark
    if case_id:
        attributes["benchmark.case.id"] = case_id
    with span("gen_ai.invoke_agent", **attributes) as current:
        current.add_event("gen_ai.user.message", {
            "gen_ai.event.content": prompt,
            "gen_ai.event.role": "user",
        })
        yield current
