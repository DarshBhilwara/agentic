# One task with multiple ReAct steps

The kernel retention and inference measurement update also requires fresh kernel
collector images on both nodes, a fresh inference image, and worker/gateway images
from the updated source. The worker-only recovery rollout below is insufficient
for that update. Apply the updated collector settings while preserving live image
references; source manifests still contain placeholder `:0.1` tags.
Full retention uses `KERNEL_REDIS_MAX_EVENTS=0`, `KERNEL_REDIS_TTL=0`, and
`KERNEL_ARCHIVE_MAX_BYTES=0`; Redis memory and archive disk use grow until managed.
These settings cannot restore already trimmed records. Rebuild/rollout and runtime
validation remain operator steps; no such operations were executed here.

Run this sample yourself on **ryzen01 / agent-01** from
`/home/agentic/agentic`. This guide covers only the fresh sample and its
telemetry. No tests, sample tasks or deployment changes have been executed for
you while preparing this procedure.

## Correct execution model

Submit **one user request once**. The worker keeps executing its LangGraph loop
until the task finishes, fails or reaches its limit:

```text
One session → one submitted task → one program/run ID

step 1: model request → inspect files → tool observations
step 2: model request → make the focused repair → observations
step 3+: model request → run checks / inspect report → final answer
```

A ReAct step here means one model request and its associated tool calls and
observations. Multiple tools can belong to one step. The terminal model response
also counts as an inference step, even without tools. The actual number of steps
comes from execution; the sample does not manufacture a fixed count.

Do not send a separate user message for each work stage. That would create
separate programs. Observable model/tool choices are recorded; hidden model
reasoning is not inspected.

The existing API's `turn_id` identifies a submitted user message. It is not the
ReAct cycle counter. Use `step_index` and `request_id` to inspect successive
model/action/observation cycles within the run.

## 1. Leave the old chat and deploy the structured-tool recovery fix

If the previous interactive chat is still open, type:

```text
/exit
```

Keep the previous workspaces and run records. They are separate evidence and
will not be reused or deleted.

Push the updated files from the development checkout and pull them on the agent
host through your usual source synchronization. Keep existing local changes.
The sample launcher and exporter need `requests`; fixture preparation also needs
`git`.

```sh
cd /home/agentic/agentic
ls scripts/run_telemetry_sample.py scripts/export_telemetry_sample.py
```

Use your existing agentctl credential and working gateway configuration.
Credentials are read from `~/.agentic_token` without printing them. If
`AGENTCTL_GATEWAY_URL` is unset, the default is `http://localhost:30080`.
Do not replace a working gateway URL or print your token.

The earlier exporter fixes were client-side only. The new fix changes gateway
and worker code: updating Git alone does not update running application pods.
Build/import a fresh worker image and roll out the gateway and workers. Do not
restart Kubernetes, delete workloads/PVCs, change the model, or rebuild vLLM or
the kernel collector for this fix.

On **iiitd-desktop**, record the live application images and check that no tasks
are queued or processing. Arrange a quiet window before rolling out workers:

```sh
kubectl -n agentic-ai get deploy agent-worker api-gateway vllm -o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image,READY:.status.readyReplicas'
kubectl -n agentic-ai exec deploy/redis -- redis-cli LLEN task_queue
kubectl -n agentic-ai exec deploy/redis -- redis-cli LLEN task_processing
```

If either list is nonempty, wait for work to finish; do not clear queues or kill
other programs. Preserve the previous failed sample and its telemetry.

After the updated source has been committed/pushed/pulled, build on **ryzen01**:

```sh
cd /home/agentic/agentic
SAMPLE_WORKER_REV="$(git rev-parse --short HEAD)"
SAMPLE_WORKER_IMAGE="agentic-worker:$SAMPLE_WORKER_REV"
git archive HEAD | docker build -t "$SAMPLE_WORKER_IMAGE" -
```

Use a new commit/tag for this build so an existing image is not silently reused.
Compare the committed-source checksums with the built image before import:

```sh
git show HEAD:code/agent.py | sha256sum
git show HEAD:code/gateway.py | sha256sum
git show HEAD:code/worker.py | sha256sum
docker run --rm --entrypoint sha256sum "$SAMPLE_WORKER_IMAGE" /app/agent.py /app/gateway.py /app/worker.py
```

The three corresponding hashes must match. Then import on that same agent node:

```sh
docker save "$SAMPLE_WORKER_IMAGE" | sudo k3s ctr images import -
printf '%s\n' "$SAMPLE_WORKER_IMAGE"
```

On **iiitd-desktop**, set the variable to that exact printed image tag (replace
the placeholder), then update only the affected application deployments:

```sh
SAMPLE_WORKER_IMAGE='agentic-worker:<the-new-commit-tag>'
kubectl -n agentic-ai set image deployment/api-gateway gateway="$SAMPLE_WORKER_IMAGE"
kubectl -n agentic-ai rollout status deployment/api-gateway --timeout=180s
kubectl -n agentic-ai set image deployment/agent-worker worker="$SAMPLE_WORKER_IMAGE"
kubectl -n agentic-ai rollout status deployment/agent-worker --timeout=180s
kubectl -n agentic-ai get deploy agent-worker api-gateway vllm -o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image,READY:.status.readyReplicas'
```

Confirm both application deployments use the new image and are fully Ready,
and that vLLM still uses its original image. Do not apply source manifests with
their older `:0.1` tags. No rollout or build above has been performed for you.

## 2. Start the sample with one command

In the agent-host shell:

```sh
SAMPLE_OUTPUT="$PWD/benchmark-results/react-sample-$(date +%Y%m%d-%H%M%S)"

python3 scripts/run_telemetry_sample.py \
  --workspace-root "$PWD/benchmark-workspaces" \
  --output "$SAMPLE_OUTPUT" \
  --timeout 300 \
  --max-steps 20
```

Use the Python environment that already has `requests`; substitute
`.venv/bin/python` if needed.

The launcher:

1. Creates a fresh child workspace containing a small CSV dataset and an
   analyzer with one intentional bug: integer division truncates its average.
2. Initializes a clean Git baseline **only inside that new sample workspace**.
   This is fixture preparation, outside the captured program; the platform
   repository and existing user workspaces are not committed or modified.
3. Creates one fresh session and submits one coding task. The agent must inspect
   the files, change only the average calculation, run the standard-library tests,
   execute a North-filtered report command, and read the resulting report using
   real workspace tools. This keeps the requested code change small while
   retaining several observable ReAct steps; generation length can still vary.
4. Saves the accepted session/task/program/run IDs before waiting for completion.
5. Leaves subsequent model/tool cycles to the existing worker loop. It does not
   submit a new user request for each cycle or silently resubmit on failure.
6. Exports the terminal run's statistics and retained syscall records, then
   independently verifies the generated program on the host.

The task uses coding-only mode and explicitly opts into
`require_command_execution=true`. Only workspace tools are available. A final
answer without workspace changes is rejected; the next model request requires
a structured `write_file` call. A final answer without a successful command after
the latest write attempt (including a failed write) is rejected; the next request requires a structured
`execute_command` call. Repeated failure remains bounded by the recovery count,
step budget and deadline.

Tool-call tags or JSON printed in assistant text are never parsed and executed.
Only structured calls from the model API enter the normal tool node and receive
tool IDs/kernel scopes. Named tool requests use guided decoding supported by
[vLLM 0.8.5](https://docs.vllm.ai/en/v0.8.5/features/tool_calling.html); schema
compliance does not guarantee correct code or commands. Initial guided-schema
compilation may add latency.

This policy improves actual execution, not proof of task correctness. Independent
host verification remains required. The command guard is opt-in; normal tasks
are not forced to run commands. Existing coding tasks receive structured write
recovery when their completion is rejected for an unchanged workspace.

If the launcher reports an unknown outcome, timeout or failure, stop and keep
its output. Do not launch a replacement until the original task's state is
understood. A client timeout does not stop a backend task.
If submission acknowledgement is lost, `preparation.json` retains the session
ID for locating the accepted task without submitting another request.

## 3. Understand the two separate outcomes

| Result | Meaning |
|---|---|
| Runtime task status | What the worker recorded: Success, Failed or Timeout. |
| Sample assessment | Whether this particular execution provides the requested example: actual code work, independent functional verification and multiple tool-bearing ReAct steps. |

A runtime Success with an incorrect program or insufficient action steps is
not a validated sample. Its actual status and evidence are preserved; it is not
relabelled as runtime failure or padded with extra user messages.

The sample dataset contains six rows with total 350. Filtering North should
produce count 2, total 150 and average 75. Additional cases exercise decimals,
refunds, empty input and invalid amounts. These are validation expectations,
not claims about what an unexecuted run produced.

## 4. Inspect the saved evidence

| File under `$SAMPLE_OUTPUT` | What to inspect |
|---|---|
| `submission.json` | Accepted session, task/program and run IDs, saved before polling. |
| `result.json` | Actual runtime outcome and the separate verification/sample assessment. |
| `react_steps.json` | Actual model-step sequence, request IDs, tool calls and associated correlation information. |
| `evidence/report.json` | Program summary, model/tool timings, tokens, semantic snapshot and graph. |
| `evidence/correlation.json` | Run/step/tool identity inventory, scope counts, representative syscall pairs, heartbeat, counters, retention and warnings. |
| `evidence/kernel.jsonl` | The full retained run syscall snapshot, up to the per-run 10,000-record cap. |

Read the launcher result and actual step sequence:

```sh
python3 -m json.tool "$SAMPLE_OUTPUT/submission.json"
python3 -m json.tool "$SAMPLE_OUTPUT/result.json"
python3 -m json.tool "$SAMPLE_OUTPUT/react_steps.json"
```

Then inspect the numeric report and syscall correlation:

```sh
python3 -m json.tool "$SAMPLE_OUTPUT/evidence/report.json"
python3 -m json.tool "$SAMPLE_OUTPUT/evidence/correlation.json"
```

The helper's host-side verification is outside the captured agent task. Only
verification commands actually invoked through agent tools belong to its tool
spans and worker syscall scopes.

The export omits raw prompt/response/tool/workspace contents from reports while
retaining safe identity fields. Numeric syscall arguments and process identifiers
remain. Do not share the API token.

The semantic identity inventory also preserves controlled recovery metadata:
`tool_choice_mode`, `required_tool_name`, `recovery_reason`, and known completion
rejection reasons/counts. Use these to distinguish automatic model choices from
guard-enforced recovery. Such intervention remains visible rather than being
presented as an unassisted model choice.

## 5. What establishes correlation

- One run/program identity spans all observed ReAct steps.
- Each model step has a distinct request ID; that exact ID joins supported vLLM
  engine measurements into the step.
- Each real tool call has a call ID, name, index and status; compare these with
  semantic events and worker kernel scopes.
- A syscall entry/exit pair matches collector ID, PID, TID, entry monotonic
  timestamp and syscall ID. Retain unmatched boundaries and scope warnings.
- Inspect capture totals, retained/exported counts, trimming, changing totals,
  heartbeat freshness and loss/export counters.

Longer programs may exceed retention. Exports are asynchronous snapshots even
after terminal task status. Zero reported counters do not establish lossless
capture. Worker scope describes the active container phase, not proven causality
for every background syscall. Shared vLLM syscalls retain workload identity only.
Engine timings overlap model wall time; unsupported KV measurements remain null.

## 6. Compare end-to-end runtime with telemetry on and off

The comparison normalizes only the experiment mode flags in both regular and
init containers. Other init-container settings remain part of the configuration
hash. Earlier reports produced before this normalization fix remain historical
evidence; do not overwrite them or combine attempts across batches.

Each attempt now saves `task-NN.json` (terminal status, error, and timers) and
`verification-NN.json` (bounded command output, individual pass/fail checks, and
failure reasons). These files are saved after the captured task time. Independent
grading uses the original fixture tests rather than trusting an agent-edited
test file. Missing, invalid, and changed reports are separate checks; none is
silently repaired by grading. Runtime Success still does not establish correctness.

Kernel exports warn about nonzero cumulative collector loss counters; these are
not exact per-run loss counts. The bounded batch writer reduces per-record file
opens and Redis round trips, but deployment and hardware validation are still
required before making capture-completeness or performance claims. Do not keep
repeating batches solely to obtain five favourable outcomes.

Before either batch, install the inference readiness gate on iiitd-desktop:

```sh
kubectl -n agentic-ai patch deployment/vllm --type strategic --patch-file manifests/inference-readiness.patch.json
kubectl -n agentic-ai rollout status deployment/vllm --timeout=600s
```

The `/health` readiness probe keeps inference out of service endpoints until
the API responds successfully. Without it, rollout success can precede model
loading and tasks can fail with connection refused. Stop on rollout failure;
save fresh deployment snapshots after installing this patch, and retain the
same probe in both modes. Do not apply the base manifest's placeholder images.

The comparison runner submits the same small CSV task sequentially, once as a
warmup and five measured repetitions per mode. The primary metric is
`task_end_to_end_time_ms` from the gateway task record; it includes queue wait
and synchronous ledger finalization before terminal publication. It also records
the existing `task_completion_time_ms` and client-observed submission-to-terminal
time as secondary metrics. The client timer includes polling (up to two seconds
of quantization); the primary metric ends before final HSET transport and
asynchronous export flushing. Fixture setup and independent host checks are
outside task timing. Each successful task is independently checked;
failed or incorrect runs are retained in the output but excluded from the mean.
No failed run is silently replaced. The comparison is exploratory with five
samples and may be affected by model/server load and output variation.

This requires the updated telemetry toggle code deployed in worker, gateway,
reaper, collector, and inference images. The implementation in this checkout is
not evidence that those changes are live. The host snapshot must show the new
mode flags on every workload and the same image/spec hashes apart from those
flags. The on/off switch disables this platform's per-task instrumentation;
Kubernetes, Redis, GPU, and host background monitoring remain active in both
modes, so this measures the incremental task instrumentation cost.

On **iiitd-desktop**, use the live image references already deployed and set
only the experiment flags. Do not apply base manifests with placeholder image
tags. Start with an empty queue, enable all task telemetry, and wait for rollouts:

```sh
kubectl -n agentic-ai exec deploy/redis -- redis-cli LLEN task_queue
kubectl -n agentic-ai exec deploy/redis -- redis-cli LLEN task_processing

kubectl -n agentic-ai set env deployment/api-gateway deployment/agent-worker deployment/task-reaper AGENT_PROGRAM_TELEMETRY_ENABLED=true AGENT_SEMANTIC_TELEMETRY_ENABLED=true
kubectl -n agentic-ai set env deployment/agent-worker KERNEL_TELEMETRY_ENABLED=true
kubectl -n agentic-ai set env daemonset/kernel-telemetry KERNEL_INFERENCE_SYSCALLS_ENABLED=true
kubectl -n agentic-ai set env deployment/vllm AGENT_INFERENCE_TELEMETRY_ENABLED=true AGENT_INFERENCE_TRACING_ENABLED=true
cd /home/iiitd/Documents/agentic
kubectl -n agentic-ai patch deployment/vllm --type strategic --patch-file manifests/inference-telemetry-switch.patch.json

kubectl -n agentic-ai set env deployment/api-gateway deployment/agent-worker deployment/task-reaper AGENT_TELEMETRY_ENABLED=true
kubectl -n agentic-ai set env daemonset/kernel-telemetry KERNEL_CAPTURE_ENABLED=true

kubectl -n agentic-ai rollout status deployment/api-gateway --timeout=180s
kubectl -n agentic-ai rollout status deployment/agent-worker --timeout=180s
kubectl -n agentic-ai rollout status deployment/task-reaper --timeout=180s
kubectl -n agentic-ai rollout status deployment/vllm --timeout=180s
kubectl -n agentic-ai rollout status daemonset/kernel-telemetry --timeout=300s
kubectl -n agentic-ai get deploy,ds -o json > /tmp/telemetry-on-snapshot.json
```

Copy `/tmp/telemetry-on-snapshot.json` to the same path on **ryzen01** without exposing the
credential. There, run one on batch:

```sh
cd /home/agentic/agentic
COMPARISON_ROOT="$PWD/benchmark-results/telemetry-compare-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$COMPARISON_ROOT"

python3 scripts/run_telemetry_comparison.py run \
  --mode on \
  --deployment-snapshot /tmp/telemetry-on-snapshot.json \
  --workspace-root "$PWD/benchmark-workspaces" \
  --output "$COMPARISON_ROOT/on" \
  --repetitions 5 --warmups 1 --timeout 300 --max-steps 20
```

Wait until the batch finishes and both queues return to zero. On **iiitd-desktop**
disable all task telemetry and wait for the same rollouts, then save and copy
`/tmp/telemetry-off-snapshot.json` to the same path on ryzen01:

```sh
kubectl -n agentic-ai exec deploy/redis -- redis-cli LLEN task_queue
kubectl -n agentic-ai exec deploy/redis -- redis-cli LLEN task_processing

kubectl -n agentic-ai set env deployment/api-gateway deployment/agent-worker deployment/task-reaper AGENT_PROGRAM_TELEMETRY_ENABLED=true AGENT_SEMANTIC_TELEMETRY_ENABLED=true
kubectl -n agentic-ai set env deployment/agent-worker KERNEL_TELEMETRY_ENABLED=true
kubectl -n agentic-ai set env daemonset/kernel-telemetry KERNEL_INFERENCE_SYSCALLS_ENABLED=true
kubectl -n agentic-ai set env deployment/api-gateway deployment/agent-worker deployment/task-reaper AGENT_TELEMETRY_ENABLED=false
kubectl -n agentic-ai set env daemonset/kernel-telemetry KERNEL_CAPTURE_ENABLED=false
kubectl -n agentic-ai set env deployment/vllm AGENT_INFERENCE_TELEMETRY_ENABLED=false AGENT_INFERENCE_TRACING_ENABLED=false

kubectl -n agentic-ai rollout status deployment/api-gateway --timeout=180s
kubectl -n agentic-ai rollout status deployment/agent-worker --timeout=180s
kubectl -n agentic-ai rollout status deployment/task-reaper --timeout=180s
kubectl -n agentic-ai rollout status deployment/vllm --timeout=180s
kubectl -n agentic-ai rollout status daemonset/kernel-telemetry --timeout=300s
kubectl -n agentic-ai get deploy,ds -o json > /tmp/telemetry-off-snapshot.json
```

On **ryzen01**, run the off batch with a separate output directory:

```sh
python3 scripts/run_telemetry_comparison.py run \
  --mode off \
  --deployment-snapshot /tmp/telemetry-off-snapshot.json \
  --workspace-root "$PWD/benchmark-workspaces" \
  --output "$COMPARISON_ROOT/off" \
  --repetitions 5 --warmups 1 --timeout 300 --max-steps 20

python3 scripts/run_telemetry_comparison.py compare \
  --on "$COMPARISON_ROOT/on" \
  --off "$COMPARISON_ROOT/off" \
  --output "$COMPARISON_ROOT/comparison.json"
cat "$COMPARISON_ROOT/comparison.json"
```

The report gives each mode's mean, median, sample standard deviation, and
`(on mean - off mean) / off mean * 100`. It withholds the overhead estimate if
either side has fewer than five independently verified successful runs or the
deployment image/spec hashes differ. Keep all attempt workspaces and JSONL
records, including failures.

After both batches, wait for empty queues before restoring the usual on-mode
settings. Keep the conditional vLLM startup command installed; its environment
flag controls whether inference traces are emitted.

```sh
kubectl -n agentic-ai exec deploy/redis -- redis-cli LLEN task_queue
kubectl -n agentic-ai exec deploy/redis -- redis-cli LLEN task_processing
kubectl -n agentic-ai set env deployment/api-gateway deployment/agent-worker deployment/task-reaper AGENT_TELEMETRY_ENABLED=true AGENT_PROGRAM_TELEMETRY_ENABLED=true AGENT_SEMANTIC_TELEMETRY_ENABLED=true
kubectl -n agentic-ai set env deployment/agent-worker KERNEL_TELEMETRY_ENABLED=true
kubectl -n agentic-ai set env daemonset/kernel-telemetry KERNEL_CAPTURE_ENABLED=true KERNEL_INFERENCE_SYSCALLS_ENABLED=true
kubectl -n agentic-ai set env deployment/vllm AGENT_INFERENCE_TELEMETRY_ENABLED=true AGENT_INFERENCE_TRACING_ENABLED=true
kubectl -n agentic-ai rollout status deployment/api-gateway --timeout=180s
kubectl -n agentic-ai rollout status deployment/agent-worker --timeout=180s
kubectl -n agentic-ai rollout status deployment/task-reaper --timeout=180s
kubectl -n agentic-ai rollout status deployment/vllm --timeout=180s
kubectl -n agentic-ai rollout status daemonset/kernel-telemetry --timeout=300s
```

## Next checkpoint

Paste the launcher output, followed by `result.json` and `react_steps.json`.
We will inspect the actual cycles under that one run ID, then locate that run's
trace in Phoenix. No additional user prompts are needed to advance the agent's
internal loop.
