# Standalone agents cluster

This cluster is independent of the existing inference cluster. It runs only
the agent namespace, workspace storage, Redis, workers, gateway, and reaper;
vLLM remains external and no GPU workloads are installed.

This deployment is configured for full telemetry capture for research. Worker,
gateway, and reaper pods set `AGENT_TELEMETRY_CONTENT_CAPTURE=full`, so raw
prompts, model responses, tool arguments/results, event payloads, and string
attributes are retained in the Redis ledger without redaction or truncation.
OTLP span events retain the exact model requests/responses and tool
arguments/results; generic gateway event payloads remain ledger-only. Deploy only on
trusted, access-controlled non-production infrastructure; telemetry may
contain secrets and private source code.
Full mode also records each model step's exact `messages` request and response,
and tool arguments/results, as correlated OpenTelemetry span events as well as
ledger events.

Bootstrap a fresh host (not a node of the existing cluster) with
`sudo ./setup.sh agents-server`. Build/import the agent image on that host
(or change `agentic-worker:0.1` in the manifest to an image in your registry).
The image installs `requirements.txt`, including LangGraph, and each worker
executes a task with the same LangGraph graph as the single-cluster deployment:

```sh
docker build -t agentic-worker:0.1 .
docker save agentic-worker:0.1 -o /tmp/agentic-worker.tar
sudo k3s ctr images import /tmp/agentic-worker.tar
```

Create an explicit API-key mapping before exposing the gateway. The deployment
will not start without it:

```sh
export AGENT_API_KEYS='{"alice-token":"alice"}'
sudo kubectl create namespace agentic-ai --dry-run=client -o yaml | sudo kubectl apply -f -
sudo kubectl -n agentic-ai create secret generic agent-api-keys \
  --from-literal=json="$AGENT_API_KEYS" --dry-run=client -o yaml | sudo kubectl apply -f -
```

Then render and apply the manifest:

```sh
python3 agents-cluster/render_manifests.py \
  --vllm-url https://vllm.example/v1 \
  --phoenix-endpoint phoenix.example:4317 \
  --prometheus-endpoint https://prometheus.example/api/v1/write
sudo kubectl apply -f build/agents-cluster/agents.yaml
```

Configure `OTEL_PHOENIX_ENDPOINT` and `OTEL_PROMETHEUS_ENDPOINT` in the
rendered `agent-inference` ConfigMap with endpoints reachable from this cluster.
The included Collector forwards worker, gateway, and reaper traces, metrics,
and application logs. Logs are retained by the persistent Loki deployment;
Phoenix and Prometheus may remain in a separate observability cluster.
Loki is exposed internally as `loki-svc`; use `kubectl -n agentic-ai port-forward
service/loki-svc 3100:3100` for local access.

Each queued task is represented by one LangGraph graph. Graph lifecycle spans
and ledger events cover model and tools node transitions, while model and tool
spans remain nested beneath their node. Dynamic delegated work is represented
as child task graphs carrying parent task/run, root run, handoff, and W3C trace
context across Redis; there is no fixed static subgraph inventory.

Phoenix receives OpenInference-compatible `AGENT`, `CHAIN`, `LLM`, and `TOOL`
span kinds. LLM spans include prompt, completion, and total token-count
attributes after a response is available; model content is retained in events.
Tool input/output attributes follow the capture policy and retain raw values in
full research mode. The external vLLM endpoint is not an agent
node, so its engine queue/prefill/decode/KV telemetry is out of scope unless
that service provides an equivalent telemetry adapter. Missing values are
unknown rather than zero, including timings lost when the reaper finalizes a
worker-lost task (`timing_complete=false`). Ledger events are durable records,
not one-to-one Phoenix spans.

To change inference, rerun the renderer with the new `--vllm-url` and apply
again. All workers read the endpoint from the `agent-inference` ConfigMap, which
is also projected at `/etc/agentic-inference/url`. You can update it in place
with `kubectl -n agentic-ai edit configmap agent-inference`; kubelet projection
may take up to about two minutes to reach running pods.
