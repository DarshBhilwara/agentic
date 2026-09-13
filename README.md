# Agentic platform

A Kubernetes-based agent platform with separate agent and inference servers, per-user workspaces, and OpenTelemetry collection across the full execution path.

## Architecture

The components are split across manifests:

- [`manifests/platform.yaml`](manifests/platform.yaml) is the cluster-wide layer. It installs the namespace, NVIDIA device plugin, and a node telemetry DaemonSet.
- [`manifests/telemetry.yaml`](manifests/telemetry.yaml) installs the shared OpenTelemetry Collector, Phoenix, and Prometheus.
- [`manifests/agent-node.yaml`](manifests/agent-node.yaml) runs the gateway, Redis, and workers on nodes labelled `agentic.io/role=agent`.
- [`manifests/inference-node.yaml`](manifests/inference-node.yaml) runs vLLM
  on nodes labelled `agentic.io/role=inference`.

## Telemetry tiers

1. **Agent intent** - GenAI intent events, task spans, tool calls, model requests, user IDs, and benchmark case IDs.
2. **Process and hardware state** - host CPU, load, memory, disk, filesystem, network, process, and process-state metrics collected on every node.
3. **Inference engine** - vLLM request traces and native metrics.

All tiers use the in-cluster OTLP Collector as their ingestion endpoint. The Collector forwards traces to Phoenix and metrics to Prometheus, leaving shared trace/resource metadata available for a future correlation engine.

## Quick start
See [`SETUP.md`](SETUP.md) for prerequisites, credentials, deployment order, telemetry checks, `agentctl` usage, and the BFCL intent smoke test.

The normal deployment order is:

```sh
kubectl apply -f manifests/platform.yaml
kubectl apply -f manifests/telemetry.yaml
kubectl apply -f manifests/agent-node.yaml
kubectl apply -f manifests/inference-node.yaml
```

Then connect through the agent gateway:

```sh
export AGENTCTL_GATEWAY_URL="http://<agent-host>:30080"
./agentctl login <user-token>
./agentctl submit "Inspect my workspace" --watch
```
