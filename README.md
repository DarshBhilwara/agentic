# Agentic platform

A Kubernetes-based autonomous-agent platform with separate agent and inference servers, per-user workspaces, and a framework-neutral execution ledger backed by OpenTelemetry and Redis.

The current experiment is **program-level agent telemetry on SWE-bench Pro**:
one dataset instance attempt is one program, with inference-step counts,
submission-to-completion time, queue wait, tool timings/statuses, context size,
and inference-node timing/cache measurements. The agent runs on the agent node;
vLLM runs on the inference node. See the [collected telemetry inventory](COLLECTED_TELEMETRY.md),
[metric definitions](TELEMETRY.md), and
[benchmark instructions](SETUP.md#swe-bench-pro-program-traces).

`benchmarks/run_swe_bench_pro.py` exports `programs.jsonl`, per-program steps and
events, and generated patches. Benchmark test correctness is recorded separately
from agent execution status; this runner does not claim an official benchmark score.

The agent node repository and workspace root is `/home/agentic/agentic`. Benchmark
checkouts default to `/home/agentic/agentic/benchmark-workspaces` and results
default to `/home/agentic/agentic/benchmark-results`.

## Architecture

The components are split across manifests:

- [`manifests/platform.yaml`](manifests/platform.yaml) is the cluster-wide layer. It installs the namespace, NVIDIA device plugin, and a node telemetry DaemonSet.
- [`manifests/telemetry.yaml`](manifests/telemetry.yaml) installs the shared OpenTelemetry Collector, Phoenix, and Prometheus.
- [`manifests/agent-node.yaml`](manifests/agent-node.yaml) runs the gateway, Redis, and workers on nodes labelled `agentic.io/role=agent`.
- [`manifests/inference-node.yaml`](manifests/inference-node.yaml) runs vLLM
  on nodes labelled `agentic.io/role=inference`.

## Unified multi-agent telemetry

The platform does not require a predefined workflow. Agents can dynamically delegate independent work to child agents; each task, agent invocation, handoff, model call, tool call, artifact, and evaluation case is written to an append-only execution ledger.

The canonical hierarchy is:

```text
session -> task/run -> agent invocation -> model call | tool call | handoff | artifact
```

Every event carries durable correlation identities including `run.id`, `session.id`, `task.id`, `agent.id`, `agent.run.id`, and, for child agents, `agent.parent.run.id`. The same contract is exposed through `POST /telemetry/events` for other runtimes; LangGraph, CrewAI, or a custom coding agent are adapters, not the control plane.

The complete event vocabulary and external ingestion contract live in [`TELEMETRY.md`](TELEMETRY.md).

Telemetry combines:

1. **Execution ledger** - Redis-backed durable events, bounded to 10,000 events per run and retained for 30 days by default.
2. **OpenTelemetry** - correlated traces, metrics, and OTLP log intake for agent and infrastructure signals.
3. **Process and hardware state** - host CPU, load, memory, disk, filesystem, network, process, and process-state metrics.
4. **Inference engine** - vLLM request traces and native metrics.

Content capture defaults to `redacted`. Set `AGENT_TELEMETRY_CONTENT_CAPTURE=off` to retain only hashes and lengths, or `full` only in a trusted evaluation environment.

Inspect any submitted run through the gateway:

```sh
./agentctl telemetry <run-id>
curl -H "X-API-Key: <token>" http://<agent-host>:30080/telemetry/runs/<run-id>
```

## Quick start
See [`SETUP.md`](SETUP.md) for prerequisites, credentials, deployment order, telemetry checks, `agentctl` usage, and the BFCL intent smoke test.

The normal deployment order is:

```sh
kubectl apply -f build/manifests/platform.yaml
kubectl apply -f build/manifests/telemetry.yaml
kubectl apply -f build/manifests/agent-node.yaml
kubectl apply -f build/manifests/inference-node.yaml
```

Then connect through the agent gateway:

```sh
export AGENTCTL_GATEWAY_URL="http://<agent-host>:30080"
./agentctl login <user-token>
./agentctl /home/agentic/agentic/<project>
```
