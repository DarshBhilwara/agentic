# Deployment setup

This deployment has three Kubernetes layers:

| Manifest | Placement | Responsibility |
| --- | --- | --- |
| `platform.yaml` | Every cluster node | Namespace, NVIDIA device plugin, and node/process telemetry |
| `telemetry.yaml` | Telemetry-capable cluster | Shared OTLP Collector, Phoenix, and Prometheus |
| `agent-node.yaml` | Smaller agent server | API gateway, Redis, and agent workers |
| `inference-node.yaml` | GPU inference server | vLLM and the model cache |

`agentctl` connects to the agent gateway. Its current directory is sent as the agent workspace; the backend queues and executes the work there. The backend `/workspace/users` volume remains available for backend-owned artifacts.

## Prerequisites
Install NVIDIA drivers plus the NVIDIA Container Toolkit on the inference server.
The two-node configuration uses local agent-node workspace storage; NFS is not
required. For an existing cluster, skip bootstrapping and use the build/render
steps below. To bootstrap a new inference node:

```sh
./setup.sh inference
```

Get the join token on the inference node:

```sh
sudo cat /var/lib/rancher/k3s/server/node-token
```

On the agent node, place this repository at `/home/agentic/agentic`. The setup
script uses that path and installs K3s, but does not configure the NVIDIA runtime:

```sh
cd /home/agentic/agentic
export K3S_URL="https://<inference-host>:6443"
export K3S_TOKEN="<token-from-inference-node>"
./setup.sh agent
```

The bootstrap script creates the directory anchors. Render deployment paths for
your actual nodes using the separate command below.

Set kubeconfig on the machine used to administer the cluster:

```sh
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
```

Label the agent and inference nodes:

```sh
kubectl label node <agent-host> agentic.io/role=agent --overwrite
kubectl label node <inference-host> agentic.io/role=inference --overwrite
```

The manifests assume one node of each role and K3s's `local-path` storage class.
Existing PVCs/PVs require a deliberate migration if their storage definitions
differ; applying a manifest does not migrate existing ephemeral Redis data.

## Build and render for the existing two-node cluster

Build both images from `/home/agentic/agentic`, then make the worker image available
on the agent node and the vLLM image on the inference node. These are custom
images: the stock vLLM image alone cannot provide the per-request adapter.

```sh
docker build -t agentic-worker:0.1 .
docker build -t agentic-vllm:0.1 -f inference/Dockerfile .
```

Push to your registry and update the image names, or save/import images into
K3s on the corresponding nodes (`docker save ... -o image.tar`, then
`sudo k3s ctr images import image.tar`). Run the benchmark client on the agent
host. Its checkouts must lie under the agent project root mounted into workers.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -r benchmarks/requirements.txt
.venv/bin/python scripts/render_manifests.py \
  --agent-root /home/agentic/agentic \
  --inference-cache /home/iiitd/Documents/agentic/model-cache
```

This writes `build/manifests/` without accessing a cluster. Ensure the agent root
exists on the agent host. Review rendered paths and image names before applying.
The agent-node source manifest already targets `/home/agentic/agentic`. Rendering
is still required to replace the inference-node model-cache placeholder.

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

All application and host telemetry enters through the internal service `otel-collector:4318`, whose collector deployment is pinned to the node labelled `agentic.io/role=inference`:

1. Agent gateway and worker emit agent intent, tool-call, task, inference, artifact, and handoff spans.
2. `otel-node-collector` runs on every node and emits CPU, memory, disk, filesystem, network, process, and process-state metrics.
3. vLLM emits inference traces and its Prometheus metrics are scraped by the Collector.

The Collector sends traces to Phoenix and metrics to Prometheus remote write. In parallel, the gateway and workers maintain a Redis-backed execution ledger. This is the durable, framework-neutral record for dynamic multi-agent work; it is not a workflow graph. Every run is keyed by `run.id` and carries `user.id`, `session.id`, `task.id`, `agent.id`, `agent.run.id`, and optional `agent.parent.run.id`.

The native agent exposes `delegate_to_agent` and `check_delegated_task` as dynamic tools. A delegated agent becomes a queued child task with its own agent/run identity and a shared root-run identity. Framework integrations can create or query runs through `POST /telemetry/events` without using these tools.

The default ledger policy retains 30 days and keeps the most recent 10,000 events per run. It redacts likely credential values before persistence. Configure it on gateway and worker pods with:

```text
AGENT_TELEMETRY_CONTENT_CAPTURE=off|redacted|full
AGENT_TELEMETRY_RETENTION_SECONDS=2592000
AGENT_TELEMETRY_MAX_EVENTS_PER_RUN=10000
```

Use `full` only for trusted, access-controlled evaluation environments.

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
kubectl apply -f build/manifests/platform.yaml
kubectl apply -f build/manifests/telemetry.yaml
kubectl apply -f build/manifests/agent-node.yaml
kubectl apply -f build/manifests/inference-node.yaml
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
./agentctl /home/agentic/agentic/<project>
./agentctl list
```

Each `agentctl submit` prints a run ID. Inspect its causal ledger with:

```sh
./agentctl telemetry <run-id>
./agentctl telemetry --follow <run-id>
```

The gateway also exposes `POST /telemetry/runs`, `GET /telemetry/runs`, `GET /telemetry/runs/{run_id}`, and authenticated `POST /telemetry/events` for external runtimes. Create the run once with its framework, agent, session, and parent-run identifiers, then emit events with `type` and `run_id`; the gateway enforces run ownership and assigns the authenticated `user.id`.

The token determines the user workspace. A token mapped to `alice` uses
`/workspace/users/alice`; a token mapped to `bob` uses
`/workspace/users/bob`.

## BFCL intent smoke test

For the primary program-level benchmark workflow, use the SWE-bench Pro section
below. BFCL remains a small tool-calling smoke test.

The benchmark runner records BFCL cases as intent telemetry. It is an observability runner, not the official BFCL scorer:

```sh
git clone https://github.com/EnlightenedAI/BFCL.git /tmp/BFCL
BFCL_CASES=$(find /tmp/BFCL/berkeley-function-call-leaderboard/bfcl_eval/data \
  -type f -name '*simple*.json' | head -1)
python3 benchmarks/run_bfcl_intent.py "$BFCL_CASES" --limit 10
```

Each case emits a `gen_ai.invoke_agent` span with the benchmark name and case ID, plus a durable run ID printed alongside the result. Use that run ID to compare model/tool behavior and inspect a failing evaluation case.

Each model inference also emits an `agent.intent.decision` event and matching span attributes. The decision is deliberately structured and bounded:

```text
agent.intent.decision       use_tool | respond_directly
agent.intent.tool_required  true | false
agent.intent.action         current_information_retrieval, workspace_file_read,
                            workspace_file_write, workspace_command_execution,
                            workspace_directory_inspection, probability_calculation,
                            vector_calculation, physics_calculation, direct_response
agent.intent.reason_code    retrieve_current_information, read_workspace_file,
                            write_workspace_file, execute_workspace_command,
                            inspect_workspace_directory, calculate_*,
                            no_tool_call_selected
agent.intent.tool_name      primary selected tool, if any
agent.intent.tool_names      JSON list of all selected tools
agent.intent.tool_actions    JSON list aligned with tool_names
agent.intent.tool_count      number of selected tools
```

For example, a search call is recorded as `decision=use_tool`, `action=current_information_retrieval`, and `reason_code=retrieve_current_information`; a direct answer is recorded as `decision=respond_directly`, `action=direct_response`, and `reason_code=no_tool_call_selected`. These fields describe the model's observable next action and do not store hidden chain-of-thought.

## SWE-bench Pro program traces

Run on the **agent host**, after deploying both node workloads. Use a new output
directory for each experiment and a workspace root inside the configured shared
agent project root. Start with one instance, then increase `--limit` and
`--concurrency`; two worker replicas process two programs at a time. Increasing
client concurrency above worker count produces measurable task queue wait.

```sh
export AGENTCTL_GATEWAY_URL=http://<agent-host>:30080
export AGENT_API_KEY=<your-token>
.venv/bin/python benchmarks/run_swe_bench_pro.py \
  --revision <dataset-commit-or-tag> --config default \
  --limit 1 --concurrency 2 --max-steps 100 --task-timeout 1800 \
  --workspace-root /home/agentic/agentic/benchmark-workspaces \
  --output /home/agentic/agentic/benchmark-results/swe-pro-experiment-001
```

Use the [official dataset](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)
and choose a revision explicitly. The runner resolves tags to a commit SHA and
records it. Config `default` follows the current public dataset and is also the
available config for the `v1.0` revision. Alternatively pass `--dataset-jsonl cases.jsonl`;
the runner records the file's SHA-256. Select specific cases with repeatable
`--instance-id`; `--limit 0` runs all selected cases.

Each instance gets a fresh repository checkout at `base_commit`, a fresh agent
session, and a unique program/run ID. Only the problem statement, requirements,
and interfaces enter the prompt. No dataset reference patch or hidden test
metadata is passed to the agent. Checkout time is reported as `setup_time_ms`,
separate from program execution time. Coding-only mode disables delegation,
search, and calculator tools for these runs. It also rejects a terminal model
answer when the Git workspace is unchanged, gives the model one explicit retry,
and fails the task if the retry still produces no change.

Outputs:

- `programs.jsonl`: one program summary per instance attempt, suitable for analysis.
- `<case-hash>/submission.json`: program/run identity saved immediately for recovery.
- `<case-hash>/summary.json`, `steps.json`, `events.jsonl`, `run.json`: structured measurements and traces.
- `<case-hash>/model.patch`, `prediction.json`: generated patch, including new files.
- `predictions.json`: combined `{instance_id, patch, prefix}` predictions for the upstream v1 evaluator format.

Inspect a live program via `GET /telemetry/programs/<program-id>` or
`./agentctl telemetry --follow <run-id>`. Phoenix visualizes the same correlated
program/step/inference/tool spans; summaries in Redis are the source for numeric
per-program exports. Events are paginated; the runner exports the retained tail
in full. Polling/network errors are `runner_error`, not invented task outcomes.

This runner uses separate checkouts in the shared worker environment. The
worker image includes Python and Node.js/npm for basic repository inspection
and testing. It does
not provision the official per-instance Docker images, install each repository's
language dependencies, or run the hidden grading harness. Extend the worker
image with the dependencies needed for your chosen repositories. This mode
supports telemetry experiments and patch generation; it is not a sandboxed,
leaderboard-equivalent evaluation. `evaluation_status=NotEvaluated` and
`resolved=null` stay explicit, even when `task_status=Success`.

Grade patches separately with the matching version of the
[official harness](https://github.com/scaleapi/SWE-bench_Pro-os); V2 uses its
Harbor/locked-protocol workflow, so the legacy prediction JSON is not a claim
of V2 harness compatibility. Dataset preparation and grading are outside the
measured program execution boundary.

## Local verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q code inference benchmarks scripts
```

Tests use fake Redis, a mocked model, and in-process gateway requests. They do
not need the cluster or a GPU. On hardware, verify that the vLLM startup log
reports `Agentic per-request telemetry hooks installed`, run a small program,
then check `engine_measured_steps == inference_steps` and non-null engine fields
in its summary. Retain a run with failures/timeouts to inspect partial traces.
