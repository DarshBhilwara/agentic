import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

from collection_policy import program_capture_enabled, semantic_capture_enabled
from program_telemetry import collect_engine_metrics


def test_global_switch_disables_program_and_semantic_capture(monkeypatch):
    monkeypatch.setenv("AGENT_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("AGENT_PROGRAM_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("AGENT_SEMANTIC_TELEMETRY_ENABLED", "true")
    assert not program_capture_enabled()
    assert not semantic_capture_enabled()


def test_engine_join_does_not_wait_when_program_capture_is_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_TELEMETRY_ENABLED", "false")

    class Redis:
        def get(self, _key):
            raise AssertionError("disabled telemetry must not query Redis")

    record = NS(engine_metrics_status="unavailable")
    collect_engine_metrics(Redis(), "request", record)
    assert record.engine_metrics_status == "unavailable"


def test_global_switch_disables_worker_kernel_scope_registration(monkeypatch):
    from kernel_telemetry import KernelTelemetry

    monkeypatch.setenv("AGENT_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("KERNEL_TELEMETRY_ENABLED", "true")
    assert KernelTelemetry().required is False


def test_disabled_otel_uses_noops_without_importing_otel_packages():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, AGENT_TELEMETRY_ENABLED="false",
               PYTHONPATH=str(root / "code"))
    code = (
        "import sys, telemetry; telemetry.export_ledger_event({}); "
        "telemetry.set_error_status(None, 'disabled'); "
        "assert telemetry.inject_context() == {}; "
        "assert not any(name == 'opentelemetry' or name.startswith('opentelemetry.') "
        "for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=env,
                   cwd=root)


def test_inference_plugin_can_be_disabled_before_vllm_import():
    env = dict(os.environ, AGENT_INFERENCE_TELEMETRY_ENABLED="false")
    code = (
        "import sys, agentic_vllm; agentic_vllm.register(); "
        "assert 'vllm' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=env,
                   cwd=Path(__file__).resolve().parents[1] / "inference")


def test_inference_trace_flags_are_conditional_in_manifest():
    import yaml

    path = Path(__file__).resolve().parents[1] / "manifests" / "inference-node.yaml"
    deployment = next(item for item in yaml.safe_load_all(path.read_text())
                      if item and item.get("kind") == "Deployment" and
                      item.get("metadata", {}).get("name") == "vllm")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    command = container["args"][0]
    assert "AGENT_INFERENCE_TRACING_ENABLED" in command
    assert "--otlp-traces-endpoint" in command
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["AGENT_INFERENCE_TRACING_ENABLED"] == "true"
    assert env["AGENT_INFERENCE_TELEMETRY_ENABLED"] == "true"
