"""Authenticated, read-only console API and offline configuration preview."""
import csv
import io
import json
import os
import posixpath
import time
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response


DEFAULTS = {
    "program_enabled": True, "semantic_enabled": True, "kernel_enabled": True,
    "device_discovery_enabled": False,
    "inference_syscalls": True, "host_metrics": True, "traces_enabled": True,
    "metrics_enabled": True, "logs_enabled": True, "content_capture": "full",
    "retention_days": 30, "max_events_per_run": 10000, "kernel_retention_days": 30,
    "archive_mb": 64, "archive_files": 5, "discovery_seconds": 5,
    "worker_replicas": 2, "agent_root": "/home/agentic/agentic",
    "inference_cache": "/home/iiitd/Documents/agentic/model-cache", "model_name": "qwen",
    "vllm_url": "http://vllm-service:8000/v1",
}
INT_BOUNDS = {"retention_days": (1, 3650), "max_events_per_run": (1, 1_000_000),
              "kernel_retention_days": (1, 3650), "archive_mb": (1, 1_048_576),
              "archive_files": (1, 10000), "discovery_seconds": (1, 3600),
              "worker_replicas": (1, 100)}
BOOLS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}


def validate_config(body):
    if not isinstance(body, dict):
        raise HTTPException(400, "Configuration must be an object")
    unknown = set(body) - set(DEFAULTS)
    if unknown:
        raise HTTPException(400, f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    result = dict(DEFAULTS)
    result.update(body)
    for key in BOOLS:
        if type(result[key]) is not bool:
            raise HTTPException(400, f"{key} must be a boolean")
    for key, (low, high) in INT_BOUNDS.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise HTTPException(400, f"{key} must be an integer from {low} to {high}")
    if not isinstance(result["content_capture"], str) or result["content_capture"] not in {"full", "redacted", "off"}:
        raise HTTPException(400, "content_capture must be full, redacted, or off")
    for key in ("agent_root", "inference_cache"):
        value = result[key]
        if not isinstance(value, str) or not value.startswith("/") or posixpath.normpath(value) != value or "\x00" in value:
            raise HTTPException(400, f"{key} must be an absolute normalized path")
    for key in ("model_name",):
        if not isinstance(result[key], str) or not result[key].strip() or len(result[key]) > 128:
            raise HTTPException(400, f"{key} must be a non-empty string of at most 128 characters")
    from urllib.parse import urlsplit
    parsed = urlsplit(result["vllm_url"] if isinstance(result["vllm_url"], str) else "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(400, "vllm_url must be an http(s) URL without credentials, query, or fragment")
    return result


def _manifest_path(name):
    root = os.path.join(os.path.dirname(__file__), "..", "manifests")
    if not os.path.isdir(root):
        root = "/app/manifests"
    return os.path.join(root, name)


def _source_yaml(name):
    path = _manifest_path(name)
    with open(path, encoding="utf-8") as source:
        return list(yaml.safe_load_all(source))


def render_files(config):
    """Render bounded config artifacts from repository manifests; no I/O to configured URLs."""
    agent = _source_yaml("agent-node.yaml")
    inference = _source_yaml("inference-node.yaml")
    platform = _source_yaml("platform.yaml")
    telemetry = _source_yaml("telemetry.yaml")
    kernel = _source_yaml("kernel-telemetry.yaml")
    replacements = {"/home/agentic/agentic": config["agent_root"].rstrip("/"),
                    "__AGENT_BASE_DIR__": config["agent_root"].rstrip("/"),
                    "__INFERENCE_CACHE_DIR__": config["inference_cache"]}
    def replace(value):
        if isinstance(value, str):
            for old, new in replacements.items(): value = value.replace(old, new)
            return value
        if isinstance(value, list): return [replace(item) for item in value]
        if isinstance(value, dict): return {key: replace(item) for key, item in value.items()}
        return value
    agent, inference, platform = replace(agent), replace(inference), replace(platform)
    worker = next(d for d in agent if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "agent-worker")
    gateway = next(d for d in agent if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "api-gateway")
    gateway["spec"]["template"]["spec"]["serviceAccountName"] = "agentic-console"
    worker["spec"]["replicas"] = config["worker_replicas"]
    values = {"AGENT_MODEL_NAME": config["model_name"], "AGENT_VLLM_URL": config["vllm_url"],
              "AGENT_WORKSPACE_HOST_ROOT": config["agent_root"],
              "AGENT_TELEMETRY_CONTENT_CAPTURE": config["content_capture"],
              "AGENT_TELEMETRY_RETENTION_SECONDS": str(config["retention_days"] * 86400),
              "AGENT_TELEMETRY_MAX_EVENTS_PER_RUN": str(config["max_events_per_run"]),
              "AGENT_PROGRAM_TELEMETRY_ENABLED": str(config["program_enabled"]).lower(),
              "AGENT_SEMANTIC_TELEMETRY_ENABLED": str(config["semantic_enabled"]).lower(),
              "KERNEL_TELEMETRY_ENABLED": str(config["kernel_enabled"]).lower()}
    for doc in agent:
        if not doc or doc.get("kind") != "Deployment": continue
        for c in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            if c.get("name") not in {"worker", "gateway", "reaper"}: continue
            env_list = c.setdefault("env", []); env = {item["name"]: item for item in env_list}
            for name, value in values.items():
                if name not in env:
                    env[name] = {"name": name}; env_list.append(env[name])
                env[name]["value"] = value
            for signal, active in (("TRACES", config["traces_enabled"]), ("METRICS", config["metrics_enabled"]), ("LOGS", config["logs_enabled"])):
                endpoint = f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT"
                if not active and endpoint in env: env_list.remove(env[endpoint])
    gateway_container = gateway["spec"]["template"]["spec"]["containers"][0]
    gateway_env = {item["name"]: item for item in gateway_container.setdefault("env", [])}
    if "CONSOLE_K8S_DISCOVERY" not in gateway_env:
        gateway_env["CONSOLE_K8S_DISCOVERY"] = {"name": "CONSOLE_K8S_DISCOVERY"}
        gateway_container["env"].append(gateway_env["CONSOLE_K8S_DISCOVERY"])
    gateway_env["CONSOLE_K8S_DISCOVERY"]["value"] = str(config["device_discovery_enabled"]).lower()
    gateway_config = next(d for d in telemetry if d and d.get("metadata", {}).get("name") == "otel-collector-gateway-config")
    node_config = next(d for d in telemetry if d and d.get("metadata", {}).get("name") == "otel-collector-node-config")
    gateway_yaml = yaml.safe_load(gateway_config["data"]["config.yaml"])
    node_yaml = yaml.safe_load(node_config["data"]["config.yaml"])
    pipelines = gateway_yaml.get("service", {}).get("pipelines", {})
    for signal, enabled in (("traces", config["traces_enabled"]), ("metrics", config["metrics_enabled"]), ("logs", config["logs_enabled"])):
        if not enabled and len(pipelines) > 1:
            pipelines.pop(signal, None)
    if not any((config["traces_enabled"], config["metrics_enabled"], config["logs_enabled"])):
        gateway_collector = next((d for d in telemetry if d and d.get("kind") == "Deployment" and d.get("metadata", {}).get("name") == "otel-collector-gateway"), None)
        if gateway_collector: gateway_collector["spec"]["replicas"] = 0
    node_pipeline = node_yaml.get("service", {}).get("pipelines", {}).get("metrics", {})
    if not config["host_metrics"] or not config["metrics_enabled"]:
        node_yaml.get("receivers", {}).pop("hostmetrics", None)
        node_pipeline["receivers"] = [r for r in node_pipeline.get("receivers", []) if r != "hostmetrics"]
    gateway_config["data"]["config.yaml"] = yaml.safe_dump(gateway_yaml, sort_keys=False)
    node_config["data"]["config.yaml"] = yaml.safe_dump(node_yaml, sort_keys=False)
    if not config["kernel_enabled"]:
        # Leave the privileged collector available but explicitly disabled; rollout is required.
        for doc in kernel:
            if doc and doc.get("kind") == "DaemonSet":
                c = doc["spec"]["template"]["spec"]["containers"][0]
                c.setdefault("env", []).append({"name": "KERNEL_CAPTURE_ENABLED", "value": "false"})
    for doc in kernel:
        if doc and doc.get("kind") == "DaemonSet":
            c = doc["spec"]["template"]["spec"]["containers"][0]
            options = {"KERNEL_REDIS_TTL": str(config["kernel_retention_days"] * 86400),
                       "KERNEL_ARCHIVE_MAX_BYTES": str(config["archive_mb"] * 1024 * 1024),
                       "KERNEL_ARCHIVE_FILES": str(config["archive_files"]),
                       "KERNEL_DISCOVERY_INTERVAL_SECONDS": str(config["discovery_seconds"]),
                       "KERNEL_CAPTURE_ENABLED": str(config["kernel_enabled"]).lower(),
                       "KERNEL_INFERENCE_SYSCALLS_ENABLED": str(config["kernel_enabled"] and config["inference_syscalls"]).lower()}
            env_list = c.setdefault("env", [])
            existing = {item["name"]: item for item in env_list}
            for name, value in options.items():
                if name not in existing:
                    existing[name] = {"name": name}
                    env_list.append(existing[name])
                existing[name]["value"] = value
    for doc in inference:
        if doc and doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == "vllm":
            if not config["inference_syscalls"] or not config["kernel_enabled"]:
                doc["spec"]["template"]["metadata"].get("labels", {}).pop("telemetry.agentic.io/syscalls", None)
            for c in doc["spec"]["template"]["spec"].get("containers", []):
                env_list = c.get("env", [])
                if c.get("name") == "vllm":
                    model = next((e for e in env_list if e.get("name") == "INFERENCE_MODEL_NAME"), None)
                    if model: model["value"] = config["model_name"]
                disabled = {"TRACES": not config["traces_enabled"], "METRICS": not config["metrics_enabled"], "LOGS": not config["logs_enabled"]}
                c["env"] = [e for e in env_list if not any(disabled[s] and e.get("name") == f"OTEL_EXPORTER_OTLP_{s}_ENDPOINT" for s in disabled)]
                if c.get("name") == "vllm" and not config["traces_enabled"]:
                    c["args"] = [a.replace(' --otlp-traces-endpoint "$OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" --collect-detailed-traces all', "") if isinstance(a, str) else a for a in c.get("args", [])]
    if not config["metrics_enabled"]:
        platform = [doc for doc in platform if not (doc and doc.get("kind") == "DaemonSet" and doc.get("metadata", {}).get("name") == "otel-node-collector")]
    access_path = _manifest_path("console-access.yaml")
    with open(access_path, encoding="utf-8") as source:
        console_access = yaml.safe_dump_all(yaml.safe_load_all(source), sort_keys=False)
    rendered = [{"name": "platform.yaml", "content": yaml.safe_dump_all(platform, sort_keys=False)},
                {"name": "console-access.yaml", "content": console_access},
                {"name": "telemetry.yaml", "content": yaml.safe_dump_all(telemetry, sort_keys=False)},
                {"name": "kernel-telemetry.yaml", "content": yaml.safe_dump_all(kernel, sort_keys=False)},
                {"name": "agent-node.yaml", "content": yaml.safe_dump_all(agent, sort_keys=False)},
                {"name": "inference-node.yaml", "content": yaml.safe_dump_all(inference, sort_keys=False)},
                {"name": "settings.json", "content": json.dumps(config, indent=2, sort_keys=True) + "\n"}]
    return rendered


SUMMARY_FIELDS = {"task_status", "task_completion_time_ms", "task_wait_time_ms", "inference_steps", "completed_steps",
                  "tool_calls", "tool_failures", "total_llm_inference_time_ms", "total_tool_time_ms", "total_tool_ttf_ms",
                  "total_tool_wait_time_ms", "total_reasoning_time_ms", "total_prefill_time_ms", "total_decode_time_ms",
                  "total_inference_queue_time_ms", "total_prompt_tokens", "total_completion_tokens", "context_tokens",
                  "peak_context_tokens", "kv_recomputed_tokens", "kv_retained_tokens", "kv_evicted_tokens", "kv_restored_tokens"}
STEP_FIELDS = {"step_index", "step_status", "step_time_ms", "llm_inference_time_ms", "step_t_reasoning_ms",
               "step_t_acting_ms", "prefill_time_ms", "decode_time_ms", "inference_queue_time_ms", "context_tokens",
               "prompt_tokens", "completion_tokens", "engine_metrics_status"}
TOOL_FIELDS = {"tool_status", "tool_ttf_ms", "tool_latency_ms", "tool_name", "tool_index"}
EVENT_ATTRIBUTE_FIELDS = {"program_id", "step_index", "tool_name", "tool.name", "tool_status", "tool_latency_ms",
                         "tool_ttf_ms", "task_status", "status", "graph.node", "node", "previous_node", "action"}


def _safe_summary(summary):
    if not isinstance(summary, dict): return {}
    return {k: v for k, v in summary.items() if k in SUMMARY_FIELDS and (v is None or isinstance(v, (str, int, float, bool)))}


def _safe_steps(steps):
    output = []
    remaining_tools = 500
    for step in steps[:1000]:
        item = {k: v for k, v in step.items() if k in STEP_FIELDS and (v is None or isinstance(v, (str, int, float, bool)))}
        selected = step.get("tools", [])[:remaining_tools]
        item["tools"] = [{k: v for k, v in tool.items() if k in TOOL_FIELDS and (v is None or isinstance(v, (str, int, float, bool)))} for tool in selected]
        remaining_tools -= len(selected)
        output.append(item)
        if remaining_tools <= 0: break
    return output


def _safe_graph(graph):
    if not isinstance(graph, dict): return None
    result = {}
    for key in ("nodes", "action_path"):
        values = graph.get(key, [])
        if isinstance(values, list): result[key] = [str(value)[:128] for value in values[:1000]]
    edges = graph.get("edges", [])
    if isinstance(edges, list): result["edges"] = [[str(part)[:128] for part in edge[:2]] for edge in edges[:2000] if isinstance(edge, list)]
    if isinstance(graph.get("signature"), str): result["signature"] = graph["signature"][:128]
    return result


def _safe_events(events):
    output = []
    for event in events[:1000]:
        attrs = event.get("attributes") or {}
        output.append({k: event[k] for k in ("id", "timestamp", "type", "severity", "run.id") if k in event})
        if attrs:
            output[-1]["attributes"] = {k: v for k, v in attrs.items() if k in EVENT_ATTRIBUTE_FIELDS and isinstance(v, (str, int, float, bool))}
    return output


def _kernel_snapshot(redis_client, run_id):
    key = f"kernel:run:{run_id}"
    records = [json.loads(v) for v in redis_client.lrange(key, 0, 499)]
    raw = redis_client.hgetall(f"{key}:status")
    status = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else str(v)) for k, v in raw.items()}
    total, retained = int(status.get("events", 0) or 0), redis_client.llen(key)
    node, node_health = status.get("node"), {}
    if node:
        raw_health = redis_client.hgetall(f"kernel:node:{node}:status")
        node_health = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else str(v)) for k, v in raw_health.items()}
        heartbeat = float(node_health.get("heartbeat_unix", 0) or 0)
        age = max(0, time.time() - heartbeat) if heartbeat else None
        node_health["heartbeat_age_seconds"] = age
        node_health["heartbeat_ok"] = age is not None and age <= 20
    return {"records": records, "total": total, "retained": retained, "truncated": total > len(records) or retained > len(records),
            "status": status, "node_health": node_health}


def _csv_cell(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value if value is not None else "")
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _md_cell(value):
    return str(value if value is not None else "").replace("|", "\\|").replace("`", "\\`").replace("\r", " ").replace("\n", " ")[:256]


def attach_console(app, verify_key, redis_getter, ledger_getter):
    router = APIRouter(prefix="/console", dependencies=[Depends(verify_key)])

    @router.get("/config")
    def get_config():
        return {"config": dict(DEFAULTS), "source": "repository", "runtime_applied": False}

    @router.post("/config/preview")
    def preview_config(body: dict):
        config = validate_config(body)
        warnings = ["Preview only: apply the rendered manifests and roll out affected workloads to change runtime settings."]
        if not config["kernel_enabled"]:
            warnings.append("The collector DaemonSet is rendered disabled; existing pods require a rollout before capture stops.")
        if not config["metrics_enabled"]:
            warnings.append("The node metrics DaemonSet is omitted; remove any existing DaemonSet explicitly because applying manifests does not delete omitted objects.")
        if not any(config[key] for key in ("traces_enabled", "metrics_enabled", "logs_enabled")):
            warnings.append("The telemetry gateway is scaled to zero; existing gateway pods stop after the deployment update.")
        return {"config": config, "files": render_files(config), "warnings": warnings, "requires_rollout": True}

    @router.get("/runs")
    def runs(limit: int = 100, user: str = Depends(verify_key)):
        if not 1 <= limit <= 100:
            raise HTTPException(400, "limit must be 1..100")
        try:
            return {"source": "redis", "runs": ledger_getter().list_runs(user=user, limit=limit), "warnings": []}
        except Exception:
            return {"source": "unavailable", "runs": [], "warnings": ["Run store is unavailable."]}

    @router.get("/runs/{run_id}")
    def run_detail(run_id: str, user: str = Depends(verify_key)):
        ledger, redis_client = ledger_getter(), redis_getter()
        try:
            run = ledger.run(run_id)
            if not run or run.get("user.id") != user:
                raise HTTPException(404, "Run not found")
            events = ledger.events(run_id, offset=0, limit=1000)
            kernel = _kernel_snapshot(redis_client, run_id)
            safe_run = {k: v for k, v in run.items() if k != "workspace.id"}
            return {"run": safe_run, "summary": run.get("summary"), "steps": ledger.steps(run_id),
                    "events": events, "procedural_graph": ledger.graph(run_id), "kernel": kernel,
                    "snapshot": {"events_truncated": len(events) >= 1000}}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Run store is unavailable")

    @router.post("/reports")
    def report(body: dict, user: str = Depends(verify_key)):
        ids, fmt = body.get("run_ids"), body.get("format", "json")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
            raise HTTPException(400, "run_ids must contain 1..20 unique run IDs")
        if fmt not in {"json", "markdown", "csv"}:
            raise HTTPException(400, "format must be json, markdown, or csv")
        ledger, redis_client = ledger_getter(), redis_getter()
        snapshots = []
        try:
            for run_id in ids:
                run = ledger.run(run_id)
                if not run or run.get("user.id") != user:
                    raise HTTPException(404, "One or more runs not found")
                ev = ledger.events(run_id, offset=0, limit=1000)
                kernel = _kernel_snapshot(redis_client, run_id)
                full_steps = ledger.steps(run_id)
                safe_run = {k: run[k] for k in ("id", "status", "started_at", "completed_at", "schema_version") if k in run}
                safe_steps = _safe_steps(full_steps)
                snapshots.append({"run": safe_run, "summary": _safe_summary(run.get("summary")), "steps": safe_steps,
                    "events": _safe_events(ev), "procedural_graph": _safe_graph(ledger.graph(run_id)), "kernel": kernel,
                    "snapshot": {"captured_at": datetime.now(timezone.utc).isoformat(),
                                 "events_truncated": len(ev) >= 1000, "steps_truncated": len(full_steps) > len(safe_steps),
                                 "kernel_truncated": kernel["truncated"]}})
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Run store is unavailable")
        if fmt == "json":
            content, media = json.dumps({"runs": snapshots}, default=str, indent=2), "application/json"
        elif fmt == "markdown":
            lines = ["# Agent telemetry report", "", f"Generated: {datetime.now(timezone.utc).isoformat()}", ""]
            for item in snapshots:
                lines += [f"## Run {_md_cell(item['run']['id'])}", "", f"- Status: {_md_cell(item['run'].get('status', 'unknown'))}"]
                lines += [f"- {_md_cell(key)}: {_md_cell(value)}" for key, value in item["summary"].items() if isinstance(value, (int, float, str))]
                lines += [f"- Syscalls retained: {len(item['kernel']['records'])} / {item['kernel']['total']}",
                          f"- Capture gaps/status: `{json.dumps(item['kernel']['status'], sort_keys=True)}`", "",
                          "### Steps", "", "| Step | Status | Duration ms | Tool calls |", "|---:|---|---:|---:|"]
                for step in item["steps"]:
                    lines.append(f"| {_md_cell(step.get('step_index', ''))} | {_md_cell(step.get('step_status', ''))} | {_md_cell(step.get('step_time_ms', ''))} | {len(step.get('tools', []))} |")
                lines += ["", "### Tools", "", "| Tool | Status | Duration ms |", "|---|---|---:|"]
                for step in item["steps"]:
                    for tool in step.get("tools", []):
                        lines.append(f"| {_md_cell(tool.get('tool_name', ''))} | {_md_cell(tool.get('tool_status', ''))} | {_md_cell(tool.get('tool_latency_ms', ''))} |")
                lines += ["", "### Syscalls", "", "| Timestamp | PID | TID | Syscall ID | Event | Return | Duration ns |", "|---|---:|---:|---:|---|---:|---:|"]
                for record in item["kernel"]["records"]:
                    lines.append(f"| {_md_cell(record.get('timestamp', ''))} | {_md_cell(record.get('pid', ''))} | {_md_cell(record.get('tid', ''))} | {_md_cell(record.get('syscall_id', ''))} | {_md_cell(record.get('type', ''))} | {_md_cell(record.get('ret', ''))} | {_md_cell(record.get('duration_ns', ''))} |")
                lines.append("")
            content, media = "\n".join(lines), "text/markdown"
        else:
            out = io.StringIO(); writer = csv.writer(out); writer.writerow(["run_id", "status", "record_type", "step_index", "step_status", "step_time_ms", "tool_name", "tool_status", "tool_latency_ms", "syscall_timestamp", "pid", "tid", "syscall_id", "syscall_event", "return_value", "duration_ns", "args", "syscall_total", "syscall_retained", "capture_status"])
            for item in snapshots:
                rows = item["steps"] or [{}]
                for step in rows:
                    tools = step.get("tools", []) or [{}]
                    for tool in tools:
                        row = [item["run"]["id"], item["run"].get("status", "unknown"), "step", step.get("step_index", ""), step.get("step_status", ""), step.get("step_time_ms", ""), tool.get("tool_name", ""), tool.get("tool_status", ""), tool.get("tool_latency_ms", ""), "", "", "", "", "", "", "", "", item["kernel"]["total"], len(item["kernel"]["records"]), json.dumps(item["kernel"]["status"], sort_keys=True)]
                        writer.writerow([_csv_cell(value) for value in row])
                for record in item["kernel"]["records"]:
                    row = [item["run"]["id"], item["run"].get("status", "unknown"), "syscall", "", "", "", "", "", "", record.get("timestamp", ""), record.get("pid", ""), record.get("tid", ""), record.get("syscall_id", ""), record.get("type", ""), record.get("ret", ""), record.get("duration_ns", ""), json.dumps(record.get("args"), separators=(",", ":")), item["kernel"]["total"], len(item["kernel"]["records"]), json.dumps(item["kernel"]["status"], sort_keys=True)]
                    writer.writerow([_csv_cell(value) for value in row])
            content, media = out.getvalue(), "text/csv"
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="telemetry-report.{fmt}"'})

    @router.get("/devices")
    def devices():
        result = {"source": "unavailable", "observed_at": datetime.now(timezone.utc).isoformat(), "nodes": [], "pods": [], "collectors": [], "warnings": []}
        if os.getenv("CONSOLE_K8S_DISCOVERY", "false").lower() not in {"true", "1", "yes"}:
            result["warnings"].append("Kubernetes discovery is disabled; enable CONSOLE_K8S_DISCOVERY to query the in-cluster API.")
        else:
            host = os.getenv("KUBERNETES_SERVICE_HOST")
            port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
            token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
            ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
            try:
                with open(token_path, encoding="utf-8") as stream: token = stream.read().strip()
                with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", encoding="utf-8") as stream: namespace = stream.read().strip()
                import ssl
                context = ssl.create_default_context(cafile=ca_path)
                def get(path):
                    req = Request(f"https://{host}:{port}{path}", headers={"Authorization": f"Bearer {token}"})
                    with urlopen(req, timeout=2, context=context) as response: return json.load(response)
                node_data = get("/api/v1/nodes")
                result["nodes"] = [{"name": n["metadata"]["name"], "ready": next((c.get("status") == "True" for c in n.get("status", {}).get("conditions", []) if c.get("type") == "Ready"), None),
                                     "capacity": n.get("status", {}).get("capacity", {}), "allocatable": n.get("status", {}).get("allocatable", {}), "labels": {k:v for k,v in n.get("metadata",{}).get("labels",{}).items() if k.startswith(("agentic.io/", "nvidia.com/"))}}
                                    for n in node_data.get("items", [])]
                result["source"] = "kubernetes"
                pod_data = get(f"/api/v1/namespaces/{namespace}/pods")
                result["pods"] = [{"name": p["metadata"]["name"], "node": p.get("spec",{}).get("nodeName"), "phase": p.get("status",{}).get("phase"),
                                    "containers": [{"name": c.get("name"), "ready": c.get("ready"), "restart_count": c.get("restartCount", 0), "state": next(iter(c.get("state",{})), "unknown")} for c in p.get("status",{}).get("containerStatuses", [])]}
                                   for p in pod_data.get("items", [])]
            except Exception as exc:
                if result["nodes"]:
                    result["source"] = "partial"
                result["warnings"].append(f"Kubernetes discovery unavailable or incomplete ({type(exc).__name__}); no live status is inferred.")
        try:
            client = redis_getter()
            if hasattr(client, "scan"):
                cursor = 0
                pages = 0
                while pages < 4 and len(result["collectors"]) < 256:
                    cursor, keys = client.scan(cursor=cursor, match="kernel:node:*:status", count=100)
                    pages += 1
                    for raw_key in keys:
                        if len(result["collectors"]) >= 256: break
                        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                        node = key.split(":")[2]
                        raw = client.hgetall(key)
                        status = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else str(v)) for k, v in raw.items()}
                        heartbeat = float(status.get("heartbeat_unix", 0) or 0)
                        age = max(0, time.time() - heartbeat) if heartbeat else None
                        result["collectors"].append({"node": node, "heartbeat_unix": status.get("heartbeat_unix"),
                            "status": status.get("status", "active"), "capture_enabled": status.get("capture_enabled", "true").lower() == "true",
                            "heartbeat_age_seconds": age, "heartbeat_ok": age is not None and age <= 20,
                            "events": status.get("events", "0"), "perf_lost": status.get("perf_lost", "0"), "map_overflow": status.get("map_overflow", "0")})
                    if cursor == 0: break
                if cursor != 0 or len(result["collectors"]) >= 256:
                    result["warnings"].append("Collector discovery capped at 4 Redis scan pages or 256 nodes.")
            else:
                for index, raw_key in enumerate(client.scan_iter(match="kernel:node:*:status", count=100)):
                    if index >= 256:
                        result["warnings"].append("Collector list capped at 256 nodes.")
                        break
                    key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                    node = key.split(":")[2]
                    raw = client.hgetall(key)
                    status = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else str(v)) for k, v in raw.items()}
                    heartbeat = float(status.get("heartbeat_unix", 0) or 0)
                    age = max(0, time.time() - heartbeat) if heartbeat else None
                    result["collectors"].append({"node": node, "heartbeat_unix": status.get("heartbeat_unix"),
                        "status": status.get("status", "active"), "capture_enabled": status.get("capture_enabled", "true").lower() == "true",
                        "heartbeat_age_seconds": age, "heartbeat_ok": age is not None and age <= 20,
                        "events": status.get("events", "0"), "perf_lost": status.get("perf_lost", "0"), "map_overflow": status.get("map_overflow", "0")})
        except Exception:
            result["warnings"].append("Collector heartbeat store is unavailable.")
        return result

    app.include_router(router)
