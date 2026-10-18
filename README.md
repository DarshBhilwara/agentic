# Agentic platform

A Kubernetes-based autonomous-agent platform with per-user workspaces, a
LangGraph worker runtime, and an execution ledger backed by OpenTelemetry and
Redis. It uses one Kubernetes cluster with separate agent and inference nodes.
Telemetry combines program measurements, semantic execution events, and an
eBPF syscall layer. See [the overall plan](TELEMETRY_PLAN.md) for scope and
the [telemetry console guide](GUI.md) for the configuration and reporting GUI.

The gateway serves the console at `/dashboard/`. It offers collection drafts,
manifest previews, device/run inspection, and reports. It supports a labelled
demo mode when no platform is running; it never applies Kubernetes changes.

The current experiment is **program-level agent telemetry on SWE-bench Pro**:
one dataset instance attempt is one program, with inference-step counts,
submission-to-completion time, queue wait, tool timings/statuses, context size,
and inference-node timing/cache measurements. The agent runs on the agent node;
vLLM runs on the inference node. See the [telemetry contract and metric
definitions](TELEMETRY.md), and
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
- [`manifests/telemetry.yaml`](manifests/telemetry.yaml) installs the shared OpenTelemetry Collector, Phoenix, Prometheus, and persistent Loki log storage.
- [`manifests/kernel-telemetry.yaml`](manifests/kernel-telemetry.yaml) installs
  the eBPF syscall collector on the agent and inference nodes.
- [`manifests/agent-node.yaml`](manifests/agent-node.yaml) runs the gateway, Redis, and workers on nodes labelled `agentic.io/role=agent`.
- [`manifests/inference-node.yaml`](manifests/inference-node.yaml) runs vLLM
  on nodes labelled `agentic.io/role=inference`.

The combined cluster provides a shared Redis ledger and exact request-ID joins
for inference-engine measurements. Kernel collection runs separately on both
workload nodes; see [kernel setup and validation](kernel/README.md).

### Agent orchestration

Each queued task executes as a LangGraph state graph:

```text
START -> model -> tools -> model -> ... -> END
                 ^       |
                 +-------+  (when the model requests tools)
```

The graph state contains the conversation, current step number, pending tool
calls, route, and final result. The model node invokes the configured
OpenAI-compatible vLLM endpoint; its conditional edge routes to the tools node
when there are tool calls and to `END` when there is a final answer. Tool
results are appended to state before the next model node runs. The worker's
Redis queue remains responsible for distributing independent tasks across
replicas.

`delegate_to_agent` is intentionally an asynchronous handoff rather than a
hard-coded graph edge: it enqueues a separate child task, preserving the root
run and parent agent-run identities. The parent can poll that child through
`check_delegated_task`. This keeps dynamic delegation available without
turning the system into a fixed workflow DAG.

Graph state currently lasts for one queued task. Redis retains the session
conversation, task record, execution ledger, and final result; it does not yet
store resumable LangGraph checkpoints. If a worker is lost, the deadline
reaper records a timeout rather than replaying tools with side effects.

## Unified multi-agent telemetry

The platform does not require a predefined workflow. Agents can dynamically delegate independent work to child agents; each task, agent invocation, handoff, model call, tool call, artifact, and evaluation case is written to an append-only execution ledger.

The canonical hierarchy is:

```text
session -> task/run -> agent invocation -> model call | tool call | handoff | artifact
```

Every event carries durable correlation identities including `run.id`, `session.id`, `task.id`, `agent.id`, `agent.run.id`, and, for child agents, `agent.parent.run.id`. The same contract is exposed through `POST /telemetry/events` for other runtimes. The native worker uses LangGraph; external runtimes such as CrewAI or custom agents can write their own events through this API.

The complete event vocabulary and external ingestion contract live in [`TELEMETRY.md`](TELEMETRY.md).

Telemetry combines:

1. **Execution ledger** - Redis-backed durable events, bounded to 10,000 events per run and retained for 30 days by default.
2. **OpenTelemetry** - correlated traces, metrics, and OTLP log intake for agent and infrastructure signals.
3. **Process and hardware state** - host CPU, load, memory, disk, filesystem, network, process, and process-state metrics.
4. **Inference engine** - vLLM request traces and native metrics.
5. **Kernel syscalls** - eBPF entry/exit records, separated from the bounded
   semantic ledger and correlated with execution scopes where known.

The research deployment sets `AGENT_TELEMETRY_CONTENT_CAPTURE=full`, preserving raw prompt, response, tool, and event content without redaction or truncation. Use it only on access-controlled, non-production infrastructure because telemetry may contain secrets.

Inspect any submitted run through the gateway:

```sh
./agentctl telemetry <run-id>
curl -H "X-API-Key: <token>" http://<agent-host>:30080/telemetry/runs/<run-id>
```

## Quick start: existing single cluster
See [`SETUP.md`](SETUP.md) for prerequisites, credentials, deployment order, telemetry checks, `agentctl` usage, and the BFCL intent smoke test.

The normal deployment order is:

```sh
kubectl apply -f build/manifests/platform.yaml
kubectl apply -f build/manifests/console-access.yaml
kubectl apply -f build/manifests/telemetry.yaml
kubectl apply -f build/manifests/kernel-telemetry.yaml
kubectl apply -f build/manifests/agent-node.yaml
kubectl apply -f build/manifests/inference-node.yaml
```

Then connect through the agent gateway:

```sh
export AGENTCTL_GATEWAY_URL="http://<agent-host>:30080"
./agentctl login <user-token>
./agentctl /home/agentic/agentic/<project>
```

Telemetry captures each queued task as a LangGraph graph, including graph
start/end/error, model/tools node transitions, nested inference and tool spans,
and all run/task/agent/root identities. Dynamic delegation creates child task
graphs linked by W3C trace context, parent IDs, root run ID, and handoff ID;
it is not a predeclared static graph topology.
