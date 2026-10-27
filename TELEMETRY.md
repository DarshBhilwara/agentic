# Telemetry Contract

## Implemented telemetry at a glance

The manifests and runtime code configure these telemetry layers. This inventory describes implementation paths; it does not assert that a particular cluster deployment is healthy or currently exporting data.

| Layer | Measures or records | Storage / destination | How to inspect |
| --- | --- | --- | --- |
| Program | Task status/timing, inference steps, tools, token use, benchmark identity | Redis run summaries and steps; OTLP | `/telemetry/programs/{program_id}`, `/telemetry/runs/{run_id}`, Phoenix, Prometheus |
| Semantic execution | Agent decisions, LangGraph nodes, handoffs, artifacts, procedural graphs | Redis event ledger and graph; OTLP | Run APIs, Phoenix, `/dashboard/` |
| Inference engine | Request timing, tokens, queue wait, batch-attributed model-forward time; native vLLM metrics | Request-ID join into Redis steps; Prometheus scrape | Run/program APIs, Prometheus |
| Host infrastructure | Host CPU, load, memory, network, disk, filesystem, process metrics | OTLP metrics to Prometheus remote write | Prometheus |
| Application logs | Python application log records when OTLP logging is configured | OTLP to Loki | Loki; see port-forward command below |
| Kernel syscalls | Raw syscall entry/exit events for selected cgroups | Per-node JSONL archives; run-attributed Redis records | Authenticated kernel run endpoint; node archive |
| Console/reports | Read-only device observations, run views, configuration previews and bounded snapshots | Gateway APIs; downloadable JSON/Markdown/CSV | `/dashboard/`, authenticated `/console` API |

```text
gateway -> Redis task queue/ledger -> worker/LangGraph agent -> vLLM
               |                        |                  |
               +--> authenticated APIs +---- OTLP --------+
                                             Collector
                                      Phoenix / Prometheus / Loki
otel-node-collector DaemonSet -> host metrics -> Collector
kernel-telemetry DaemonSet -> eBPF syscall events -> node archives + Redis
```

## Shared execution model and identity

- A queued task runs as a LangGraph graph: `START -> model -> tools -> model -> END`.
- The model routes to tools only when it returns tool calls. Current tool calls execute sequentially.
- Delegation queues a child task; it is dynamically linked to its parent by IDs and trace context, not a fixed graph edge.
- Every run has `run.id`. These identities are attached when known:

  ```text
  user.id, session.id, task.id, run.id, root.run.id
  agent.id, agent.run.id, agent.parent.run.id, handoff.id
  trace.id, span.id
  ```

- `agent.id` is the logical agent identity; `agent.run.id` identifies one invocation.
- A child has its own task/run IDs, a parent agent-run ID, and the same root run ID as its initiating task.
- `program_id` identifies one submitted program/task; `step_index` starts at 1 within that program and is separate from the session turn.

## Program measurements

- **Program:** one submitted task; one SWE-bench Pro instance attempt.
- **Repeated attempts:** distinct program and run IDs.
- **Step:** one model request, its selected tool calls, and their observations.
- **Final response:** counts as a step even without tools.
- **Failed model attempts:** count as steps; SDK retries are disabled.
- **Read APIs:** `GET /telemetry/programs/{program_id}` and `GET /telemetry/runs/{run_id}`.
- **API contents:** run, summary, saved steps, procedural graph, and a bounded event page.
- **Units:** milliseconds; unavailable measurements are `null`.

| Field | Definition and limits |
| --- | --- |
| `task_status` | `Queued`, `Running`, `Success`, `Failed`, or `Timeout`. `Success` means normal agent return, not passing benchmark tests. |
| `inference_steps`, `completed_steps` | Attempted model requests; successfully completed steps. |
| `task_completion_time_ms` | Submission to terminal processing, including queue wait; excludes checkout preparation and benchmark grading. Null while running. |
| `task_wait_time_ms` | Submission to worker admission, including worker queue wait. |
| `llm_inference_time_ms` | Client request wall time, including inference queueing and transport. |
| `step_t_reasoning_ms` | vLLM CUDA model-forward time for batches containing the request. Includes prefill/decode; not exclusive per-request GPU occupancy. |
| `step_t_acting_ms` | Wall time for tool calls and observation insertion; zero for no-tool steps. |
| `tool_name`, `tool_call_id`, `tool_index` | Tool identity and order within the step. |
| `tool_latency_ms`, `tool_ttf_ms`, `tool_status` | Tool duration, delay from acting-phase start to tool start, and `Success`/`Failed`/`Timeout` result. |
| `prompt_tokens`, `completion_tokens`, `context_tokens` | Server usage. Context is current request prompt plus response, not repeated-prompt sum across requests. Tool observations count in the next request's prompt. |
| `prefill_time_ms` | Inference-node first scheduling to first token; phase wall time, not isolated CUDA prefill kernels. |
| `decode_time_ms` | First token to request finish; includes interleaved scheduling/preemption. Zero is valid for a one-token result. |
| `inference_queue_time_ms` | Initial vLLM scheduling wait, separate from the agent task queue. |
| `kv_recomputed_tokens` | Request-scoped recomputation when the engine exposes lifecycle attribution. Null in the shipped vLLM 0.8.5 V0 adapter. |

### Program summary fields

- **Model and tools:** `total_llm_inference_time_ms`, `total_tool_time_ms`, `total_tool_wait_time_ms`.
- **Engine phases:** `total_reasoning_time_ms`, `total_prefill_time_ms`, `total_decode_time_ms`, `total_inference_queue_time_ms`.
- **Usage:** token totals, `peak_context_tokens`, `tool_calls`, `tool_failures`.
- **Engine availability:** `engine_measured_steps`; `kv_recomputed_tokens` remains null when unsupported.

### Measurement limits

- An engine total is null if any included step lacks the measurement.
- Batch-attributed GPU times can overcount physical occupancy when summed across concurrent programs.
- Phase times overlap inference wall time; do not add them to request time.
- Run records include submission/start/completion timestamps, task/session/agent/benchmark/case IDs, result/error, and known termination reason.
- Program/step/request IDs are span attributes and ledger fields, not high-cardinality Prometheus labels.

### Agent actions, tools, and artifacts

After a model response, the runtime records the observable next action, not hidden reasoning:

- `agent.intent.decision`: `use_tool` or `respond_directly`.
- `agent.intent.tool_required`, `agent.intent.action`, `agent.intent.reason_code`.
- Primary `agent.intent.tool_name`, all `agent.intent.tool_names`, and aligned `agent.intent.tool_actions`.
- Action categories include workspace inspection/read/write/command, current-information retrieval, calculations, delegation, coordination, and direct response.
- Tool events carry program/step IDs, tool name/call ID, start/completion, duration, status, exception, and arguments/results subject to capture policy.
- Successful `read_file`/`write_file` calls emit `artifact.read`/`artifact.written`; the worker also records the final task-result artifact.

## Inference engine telemetry

The shipped adapter is pinned to vLLM **0.8.5 V0**, with prefix caching, one sequence per request, and detailed tracing. Revalidate it before upgrading vLLM.

- Worker sends W3C trace context and a unique `X-Request-Id` containing program and step identity.
- The adapter writes engine measurements under that exact request ID in Redis with a one-hour TTL; the worker joins them into the durable step.
- A bounded background queue keeps Redis I/O off the scheduler.
- Missing, late, or dropped measurements remain null. The join may wait up to 0.5 seconds for a missing record; that wait contributes to task time, not model time.
- The adapter does not expose request-scoped KV allocation/free/swap ownership. KV recomputation, retention, eviction, restoration, tier, residency, and affinity remain null with an unavailable reason. Prefix-hash overlap is not treated as eviction or recomputation.
- vLLM's native Prometheus endpoint is scraped by the gateway Collector every 15 seconds.

Pinned implementation references: [request timing fields](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/sequence.py), [trace completion hook](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/engine/llm_engine.py), [prefix-cache allocation](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/core/block_manager.py).

## Semantic events, graph, and external ingestion

Native event families include:

- Task lifecycle: `task.queued`, `task.started`, `task.completed`, `task.failed`, `task.timeout`.
- Steps: `step.started`, `step.completed`.
- Agent: `agent.started`, `agent.completed`, `agent.delegated`, and `agent.completion.rejected` when coding returns without required workspace changes.
- Model/tool: `model.requested`, `model.completed`, `model.failed`, `tool.started`, `tool.completed`.
- Artifacts: `artifact.read`, `artifact.written`.
- Graph/phase/handoff and procedural graph events record observed execution structure and delegation.

The procedural graph is materialized during execution, independently of the bounded event tail:

- It records observed model, tool, delegation, and terminal nodes/edges.
- Normalized action-path signatures index repeated procedures; small canonical action windows are stored as owner-scoped subgraph definitions and occurrences.
- Handoffs add parent-to-child causal edges. The graph does not inspect hidden reasoning or judge task quality.
- Terminal records and the graph span can carry final graph/summary snapshots. Phoenix shows the JSON in span details; it does not render a dedicated graph canvas.
- Redacted mode masks graph strings; off mode fingerprints string values while preserving JSON structure.

External runtimes authenticate, create an owned run, then append events:

```json
POST /telemetry/runs
{
  "framework": "custom-runtime",
  "session_id": "...",
  "task_id": "...",
  "agent_id": "researcher",
  "agent_run_id": "...",
  "parent_agent_run_id": "...",
  "root_run_id": "..."
}
```

```json
POST /telemetry/events
{
  "type": "agent.handoff.accepted",
  "run_id": "...",
  "agent.run.id": "...",
  "handoff.id": "...",
  "attributes": {"handoff.from": "planner", "handoff.to": "researcher"},
  "payload": {"summary": "Investigate the failing test."}
}
```

- Gateway verifies ownership and assigns `user.id` from the authenticated token.
- External event payloads may use known names or namespaced types such as `custom.policy.decision`.
- In redacted mode, externally ingested payload strings and attributes are masked before storage; full mode retains them.

## OpenTelemetry traces, metrics, and logs

- Trace context propagates gateway → worker → vLLM and across queued delegation. Spans cover submission, program, agent, model, tool, and graph/node execution.
- Phoenix/OpenInference span kinds: graph/node `CHAIN`, agent `AGENT`, model `LLM`, tool `TOOL`.
- LLM spans expose numeric `llm.token_count.*` and corresponding `gen_ai.usage.*` attributes.
- Each persisted ledger event is submitted best-effort as a child span, subject to semantic capture controls. Event spans include event ID/type/time/severity/schema, available identities, and structured attributes/payload. Error/fatal events get OTel error status. Export exceptions do not stop durable ledger writes or task execution.
- Runtime metrics: `agent.tasks.total`, `agent.task.errors.total`, `agent.inference.requests.total`, `agent.telemetry.events.total`, `agent.handoffs.total`, `agent.program.duration`, `agent.program.queue.duration`, `agent.program.inference.steps`, `agent.inference.step.duration`, `agent.tool.duration`, `agent.tool.wait`, `agent.tool.calls.total`.
- Python telemetry installs a root logging handler when OTLP log export is configured. This exports Python application log records; it does not scrape container stdout/stderr.
- Gateway Collector routes OTLP traces to Phoenix, metrics to Prometheus remote write, and logs to Loki.
- Phoenix and Loki exporters use file-backed sending queues. Prometheus uses `remote_write_queue` and a persistent write-ahead log (WAL); its exporter does not support `sending_queue`.
- Export retries run for up to five minutes; buffering helps through brief outages but does not guarantee delivery.
- Loki is internal: `kubectl -n agentic-ai port-forward service/loki-svc 3100:3100`.

## Host and Kubernetes infrastructure

`otel-node-collector` runs as a DaemonSet and uses OpenTelemetry hostmetrics every 10 seconds:

- CPU, load, memory, network, disk, filesystem, process, and process-count metrics.
- Resource attributes identify the node and `kubernetes-node-telemetry` service.
- These host measurements do not provide per-request GPU utilization, syscall events, or causal attribution to one program.

## Kernel syscall telemetry (eBPF/BCC)

### What runs where

- **Execution:** eBPF programs run in the host Linux kernel.
- **Loader:** BCC compiles and loads them through its Python `BPF` class (`from bcc import BPF`). This uses eBPF.
- `kernel/Dockerfile` builds `agentic-kernel:0.1`, which packages the user-space Python collector, BCC runtime, health check, and Redis client. The image is a build artifact; Kubernetes does not build it. It must be available on every selected node.
- `manifests/kernel-telemetry.yaml` defines a privileged DaemonSet for nodes labelled `agentic.io/role=agent` or `agentic.io/role=inference`. It uses host PID visibility and mounts cgroup v2, kernel modules/sources, debugfs, and tracefs.
- Collector implementation: [`kernel/collector.py`](kernel/collector.py); worker socket client: [`code/kernel_telemetry.py`](code/kernel_telemetry.py); deployment and smoke-check details: [`kernel/README.md`](kernel/README.md).

### What is captured and attributed

- BCC attaches eBPF probes to `raw_syscalls:sys_enter` and `raw_syscalls:sys_exit`. An active-cgroup map filters observed processes. There is no syscall allowlist or intentional sampling.
- **Entry:** syscall ID and six raw numeric argument slots.
- **Exit:** return value and elapsed time, paired with entry using PID/TID.
- **Missing entry:** exit is marked `unmatched_exit`.
- **Process exit:** removes stale in-flight state.
- Record fields include collector ID, node, architecture, type, timestamps, `entry_monotonic_ns`, syscall/cgroup/scope IDs, PID/TID/UID, command, arguments, return value/duration, and available run/program/step/tool or pod identity.
- Raw argument slots are not dereferenced; pointer values are not filenames, buffers, or decoded structures. Syscall IDs require the node architecture/ABI to interpret.
- Worker registers and changes scope through `/run/agentic-kernel/collector.sock`. The collector uses Unix-socket `SO_PEERCRED` to identify the host PID and resolves its cgroup v2 inode itself; the client cannot choose another cgroup.
- The collector ties ownership to PID and process start time, rejects competing program registrations in one cgroup, checks each scope update against the socket peer, and removes stale ownership after process exit/PID reuse.
- **Program scope:** run/task/program/root/session/agent IDs.
- **Nested scopes:** step and tool identifiers.
- **Descendants:** child processes and threads are captured while they remain in the selected cgroup.
- **Attribution limit:** scope identifies the active container phase; background activity may share that scope.
- **Inference discovery:** every five seconds, list local pods labelled `telemetry.agentic.io/syscalls=enabled` and match visible `/proc/*/cgroup` entries.
- **Inference identity:** pod/container fields and `scope_kind=inference`.
- **Inference limits:** shared vLLM syscalls have no exclusive request/run ID; discovery may miss short-lived cgroups.

### Buffering, storage, and health

- Perf callbacks enqueue records without doing Redis or disk I/O. The userspace queue holds 16,384 records by default; the in-flight BPF map holds 65,536 syscalls. Other buffers/maps are finite too.
- Background writer appends per-node JSONL under `/var/log/agentic-kernel`; default archive rotation is 64 MiB across five rotated files plus active file.
- Run-attributed records go to Redis: newest 10,000 per run, 30-day TTL by default. Shared inference records remain in node archives.
- Authenticated `GET /telemetry/runs/{run_id}/kernel?offset=0&limit=200` returns records, counts, truncation, run capture status, and node health; maximum page size is 500.
- Node heartbeat publishes every five seconds and expires after 20 seconds. Cumulative node counters include perf losses, map overflow, queue drops, discovery errors, identity misses, and export errors. These counters are node-level, not exact per-run loss totals.
- Redis outage, disk failure, process termination, perf/map/queue overflow, or collector restart can lose records. Archives are bounded and do not automatically replay into Redis; asynchronous exports are snapshots, not a completion barrier.

### Controls and validation limits

- `KERNEL_CAPTURE_ENABLED=false`: idle collector; does not load BCC probes and rejects capture registration. Requires rollout.
- `KERNEL_INFERENCE_SYSCALLS_ENABLED=false`: disables inference discovery while worker scopes may remain enabled.
- `KERNEL_TELEMETRY_ENABLED`: worker-side registration control. Supplied agent manifest enables it as required; failed registration/scope acknowledgement fails the task.
- Kernel deployment needs cgroup v2, tracepoint/BCC support, compatible kernel headers, service-account pod-list permissions, host mounts, and privileged execution on both nodes.
- Readiness checks cover configured idle state or basic attachment/socket and discovery status. They do not prove complete coverage, lossless export, compatibility for every workload, or actual hardware-level performance.
- GPU execution itself is outside syscall telemetry. Host metrics and vLLM metrics do not substitute for syscall tracing.

## Capture policies, retention, and failure behavior

### Capture switches

- Console preview renders `AGENT_PROGRAM_TELEMETRY_ENABLED` and `AGENT_SEMANTIC_TELEMETRY_ENABLED`. Program capture controls saved summaries/steps/program events; semantic capture controls LangGraph, phase, handoff, and procedural-graph observations.
- Disabling both still keeps minimal lifecycle/ownership bookkeeping needed to run and own tasks. Kernel capture is independent.
- `CONSOLE_K8S_DISCOVERY` controls read-only node/pod discovery. Console previews do not apply manifests; an operator must review, apply, and roll out changes.

### Content policy

- `AGENT_TELEMETRY_CONTENT_CAPTURE=off`: content becomes SHA-256/length metadata.
- `redacted`: default in application code; strings are secret-masked and bounded (normally 4 KiB, event lists to 100 items).
- `full`: supplied research manifests set this mode. Raw prompts, model responses, tool arguments/results, event payloads, and string attributes may be retained; lists are not capped at 100. Treat Redis and observability backends as sensitive research storage.
- OTLP model spans include exact per-step messages and tool spans include arguments/results under full mode. These can contain credentials, private code, or user data.
- Reports deliberately exclude prompts, responses, tool arguments/results, and workspace content. Kernel numeric syscall arguments and process/workload identifiers remain in exports.

### Task failure and persistence

- Task deadline includes queue time. Command timeout kills the shell process group. Step-budget overflow or a truncated model response fails the program; a tool failure may be recovered by a later step.
- Queue claims move atomically into `task_processing`. Deadline reaper marks abandoned/overdue tasks Timeout after a 30-second grace period and does not rerun tools.
- Worker loss can make partial timings unknown; summary reports `timing_complete=false`. Reaper time is the observed terminal time, not a reconstructed crash time.
- Redis uses AOF with `appendfsync everysec` and a persistent volume; abrupt failure can lose up to about one second of recent writes. Phoenix and Prometheus also use persistent volumes. Local-path storage survives pod replacement, not loss of its node disk.
- Ledger retention defaults: 30 days and 10,000 events per run. Program summaries/steps are stored separately from the bounded event tail.
- Missing or late measurements mean unavailable, not zero. Application export is best-effort; Collector queues/retries do not guarantee delivery.

## Console and report snapshots

The gateway serves `/dashboard/` and the authenticated `/console` API:

- Overview/Collection: collection drafts, signal controls, previewed deployment files, and capture settings.
- Devices: observed Kubernetes nodes, pods, and collector heartbeats when read-only discovery is enabled.
- Runs: owned run summaries, steps/tools, semantic events, and PID/TID-correlated syscall records.
- Reports: bounded JSON, Markdown, or CSV snapshots for selected owned runs. Unavailable measurements remain unknown; capture/retention gaps are included.
- Reports exclude prompts, model responses, tool arguments/results, and workspace content. Treat syscall argument slots and workload identifiers as research data. Kernel export is asynchronous, so report contents are a snapshot.
- Offline/demo mode is labelled sample data; it does not indicate a connected cluster. Previewing collection settings does not change deployed resources.

See [`GUI.md`](GUI.md) for startup, API routes, report bounds, and read-only discovery details.

## Explicitly unsupported or unverified

- Agent `Success` and a generated benchmark patch do not mean benchmark tests passed. SWE-bench runner is for telemetry/patch generation, not leaderboard-equivalent grading.
- vLLM 0.8.5 V0 does not provide request-scoped KV allocation/free/swap ownership; KV lifecycle fields remain null with an unavailable reason.
- vLLM batch-forward GPU time is not exclusive per-request GPU occupancy. Shared inference syscalls have no exclusive request/run attribution.
- Kernel syscall coverage, BPF verifier acceptance, losslessness, and performance have not been established merely by local tests or manifest validation; validate on the actual nodes and workload.
- Configured OTLP paths and manifests do not prove that Collector, Phoenix, Prometheus, Loki, or kernel collectors are currently healthy. Check rollout, collector logs, heartbeats, and backend data in the target cluster.
- Telemetry does not inspect hidden model reasoning, decode syscall pointer targets, or observe GPU device execution.
