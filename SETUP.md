# Deployment setup

This deployment has three Kubernetes layers:

| Manifest | Placement | Responsibility |
| --- | --- | --- |
| `platform.yaml` | Every cluster node | Namespace, NVIDIA device plugin, and node/process telemetry |
| `telemetry.yaml` | Telemetry-capable cluster | Shared OTLP Collector, Phoenix, and Prometheus |
| `agent-node.yaml` | Smaller agent server | API gateway, Redis, and agent workers |
| `inference-node.yaml` | GPU inference server | vLLM and the model cache |

`agentctl` connects to the agent gateway. It does not connect directly to the worker, inference server, workspace, or telemetry services.

## Prerequisites
Install NVIDIA drivers plus the NVIDIA Container Toolkit on the inference server. Ensure the agent server can export its workspace over NFS. Bootstrap the inference node with:

```sh
./setup.sh inference
```

Get the join token on the inference node:

```sh
sudo cat /var/lib/rancher/k3s/server/node-token
```

On the agent node, run the same script from the directory that should contain the agent files. This also installs K3s, but does not configure the NVIDIA runtime:

```sh
cd /path/to/agentic
export K3S_URL="https://<inference-host>:6443"
export K3S_TOKEN="<token-from-inference-node>"
/path/to/setup.sh agent
```

The inference node uses `/home/iiitd/Documents/agentic`, the agent node uses the directory from which the command was run. The setup script creates the required workspace and model-cache directories for either role.

Set kubeconfig on the machine used to administer the cluster:

```sh
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
```

Label the agent and inference nodes:

```sh
kubectl label node <agent-host> agentic.io/role=agent --overwrite
kubectl label node <inference-host> agentic.io/role=inference --overwrite
```

Before deployment, update the NFS server, NFS path, and node affinity in `manifests/agent-node.yaml` if the defaults do not match your environment.

## Credentials
Configure one API token per user. The token-to-user mapping creates isolated workspaces under `/workspace/users/<user>`:

```sh
export AGENT_API_KEYS='{"alice-token":"alice","bob-token":"bob"}'
```

After the namespace exists, store the mapping as a Kubernetes Secret:

```sh
kubectl -n agentic-ai create secret generic agent-api-keys \
  --from-literal=json="$AGENT_API_KEYS" \
  --dry-run=client -o yaml | kubectl apply -f -
```

If the model requires Hugging Face authentication, also create:

```sh
kubectl -n agentic-ai create secret generic hf-token \
  --from-literal=token="$HUGGINGFACE_HUB_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## Telemetry architecture

All application and host telemetry enters through the internal service `otel-collector:4318`:

1. Agent gateway and worker emit agent intent, tool-call, task, and inference spans.
2. `otel-node-collector` runs on every node and emits CPU, memory, disk, filesystem, network, process, and process-state metrics.
3. vLLM emits inference traces and its Prometheus metrics are scraped by the Collector.

The Collector sends traces to Phoenix and metrics to Prometheus remote write. Trace IDs and resource attributes provide the common correlation keys for a future correlation engine. The DSL and log-query engine are not part of this deployment yet.

The default externally exposed services are:

| Service | URL |
| --- | --- |
| API gateway | `http://<agent-host>:30080` |
| vLLM | `http://<inference-host>:30800` |
| Phoenix | `http://<telemetry-host>:30006` |
| Prometheus | `http://<telemetry-host>:30090` |

## Deploy

Apply the platform first because it creates the namespace. Then apply the shared telemetry stack and the two workload manifests:

```sh
kubectl apply -f manifests/platform.yaml
kubectl apply -f manifests/telemetry.yaml
kubectl apply -f manifests/agent-node.yaml
kubectl apply -f manifests/inference-node.yaml
```

Check the rollout:

```sh
kubectl -n agentic-ai get pods -o wide
kubectl -n agentic-ai get svc
kubectl -n agentic-ai logs deploy/otel-collector-gateway
kubectl -n agentic-ai logs deploy/agent-worker
kubectl -n agentic-ai logs deploy/vllm
```

## Use `agentctl`

Install the client dependency on the client machine:

```sh
python3 -m pip install requests
export AGENTCTL_GATEWAY_URL="http://<agent-host>:30080"
./agentctl login <user-token>
./agentctl submit "Inspect my workspace and summarize its contents" --watch
./agentctl list
```

The token determines the user workspace. A token mapped to `alice` uses
`/workspace/users/alice`; a token mapped to `bob` uses
`/workspace/users/bob`.

## BFCL intent smoke test

The benchmark runner records BFCL cases as intent telemetry. It is an observability runner, not the official BFCL scorer:

```sh
git clone https://github.com/EnlightenedAI/BFCL.git /tmp/BFCL
BFCL_CASES=$(find /tmp/BFCL/berkeley-function-call-leaderboard/bfcl_eval/data \
  -type f -name '*simple*.json' | head -1)
python3 benchmarks/run_bfcl_intent.py "$BFCL_CASES" --limit 10
```

Each case emits a `gen_ai.invoke_agent` span with the benchmark name and case
ID, plus a `gen_ai.user.message` intent event.
