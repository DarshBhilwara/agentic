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
- `AGENT_TELEMETRY_ENABLED` gates optional task instrumentation and measurement joins, not task/run ownership or mandatory terminal timing. Each component must receive the intended mode through its deployment environment.

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
| `task_completion_time_ms` | Existing program-summary timer: queue wait plus worker-side execution and task-result handling. It stops before final ledger writes. Null while running. |
| `task_end_to_end_time_ms` | Mandatory worker timer returned by `GET /tasks/{task_id}` in both modes, from submission through synchronous telemetry/ledger finalization, computed immediately before publishing terminal task status. Includes queue wait and is the primary comparison metric. It is not a program-summary field. Excludes final Redis HSET transport and asynchronous exporter flush. |
| `task_wait_time_ms` | Submission to worker admission, including worker queue wait. The end-to-end timer adds this wait to worker-side elapsed time. |
| `llm_inference_time_ms` | Client request wall time, including inference queueing and transport. |
| `step_t_reasoning_ms` | vLLM CUDA model-forward time for batches containing the request. Includes prefill/decode; not exclusive per-request GPU occupancy. |
| `model_forward_time_ms` | Explicitly named alias for `step_t_reasoning_ms`; batch-attributed forward time. |
| `model_execute_time_ms` | vLLM model execute duration, including forward, worker synchronization, CPU/GPU synchronization, and sampling. |
| `scheduler_time_ms` | Time spent considering the request in the vLLM scheduler. |
| `step_t_acting_ms` | Wall time for tool calls and observation insertion; zero for no-tool steps. |
| `tool_name`, `tool_call_id`, `tool_index` | Tool identity and order within the step. |
| `tool_latency_ms`, `tool_ttf_ms`, `tool_status` | Tool duration, delay from acting-phase start to tool start, and `Success`/`Failed`/`Timeout` result. |
| `prompt_tokens`, `completion_tokens`, `context_tokens` | Server usage. Context is current request prompt plus response, not repeated-prompt sum across requests. Tool observations count in the next request's prompt. |
| `prefill_time_ms` | Inference-node first scheduling to first token; phase wall time, not isolated CUDA prefill kernels. |
| `decode_time_ms` | First token to request finish; includes interleaved scheduling/preemption. Zero is valid for a one-token result. |
| `inference_queue_time_ms` | Initial vLLM scheduling wait, separate from the agent task queue. |
| `time_to_first_token_ms`, `end_to_end_latency_ms` | vLLM arrival-to-first-token and arrival-to-finish wall times. |
| `output_tokens`, `output_tokens_per_second` | Generated sequence token count and subsequent-token rate after the first token. The rate is emitted only for a single sequence with at least two tokens and a positive first-token-to-finish interval; otherwise it is null. |
| `prefix_cached_tokens` | Tokens vLLM reports as prefix-cache hits for this request. This is cache reuse, not retained KV allocation. |
| `sequence_count`, `measurement_source` | Number of generated sequences and pinned adapter source/version. |
| `kv_gpu_exclusive_blocks_at_finish`, `kv_cpu_exclusive_blocks_at_finish` | Unique block-manager IDs with exactly one owning request at the completion observation; shared and cache-only blocks are excluded. |
| `kv_gpu_exclusive_peak_blocks`, `kv_cpu_exclusive_peak_blocks` | Maximum observed exclusive block counts at instrumented block-manager boundaries. |
| `kv_gpu_exclusive_block_ms`, `kv_cpu_exclusive_block_ms` | Exclusive logical block counts integrated between observed hook boundaries, in block-milliseconds. |
| `kv_swap_out_exclusive_blocks`, `kv_swap_in_exclusive_blocks`, `kv_recompute_discarded_exclusive_blocks` | Distinct exclusive blocks in scheduler-issued swap mappings, and exclusive blocks discarded by successful recompute preemption. These are block counts, not token counts. |
| `kv_shared_blocks_observed`, `kv_measurement`, `kv_measurement_reason` | Peak shared-block count and completeness/limitations of the block-manager observations. |
| `kv_recomputed_tokens` | Request-scoped recomputation when the engine exposes lifecycle attribution. Null in the shipped vLLM 0.8.5 V0 adapter. |

### Program summary fields

- **Model and tools:** `total_llm_inference_time_ms`, `total_tool_time_ms`, `total_tool_wait_time_ms`.
- **Engine phases:** `total_reasoning_time_ms`, `total_model_forward_time_ms`, `total_prefill_time_ms`, `total_decode_time_ms`, `total_inference_queue_time_ms`, `total_model_execute_time_ms`, `total_scheduler_time_ms`, `total_time_to_first_token_ms`, `total_end_to_end_latency_ms`.
- **Usage:** token totals, `total_prefix_cached_tokens`, `peak_context_tokens`, `tool_calls`, `tool_failures`.
- **Engine availability:** `engine_measured_steps`; totals for exclusive KV block-milliseconds are emitted only when the constituent observations are valid. Token-level KV lifecycle fields remain null when unsupported.
- **Exclusive KV:** `total_kv_gpu_exclusive_block_ms`, `total_kv_cpu_exclusive_block_ms`, `total_kv_swap_out_exclusive_blocks`, `total_kv_swap_in_exclusive_blocks`, and `total_kv_recompute_discarded_exclusive_blocks`. These sum observations across distinct model requests; they are not simultaneous GPU-memory occupancy.

### Measurement limits

- An engine total is null if any included step lacks the measurement.
- Batch-attributed GPU times can overcount physical occupancy when summed across concurrent programs.
- Phase times overlap inference wall time; do not add them to request time.
- Run records include submission/start/completion timestamps, task/session/agent/benchmark/case IDs, result/error, and known termination reason. The mandatory task end-to-end timer is kept even when optional task telemetry is disabled.
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
- Inference records are emitted from vLLM's native `LLMEngine.create_trace_span` completion hook. If native tracing is disabled while the adapter switch remains enabled, no Redis record is emitted; weak group references release per-request lifecycle state after the request object is collected.
- Prefix-cache hit tokens come from vLLM's per-sequence `get_num_cached_tokens()` counter. Prefix-cache hits are not treated as lifecycle allocation or eviction. Token-level KV recomputation, retained/evicted/restored counts, and cache affinity remain null because the adapter has no token-exact source for them.
- Block-manager lifecycle fields report logical block IDs observed at allocator hooks, not physical tensor bytes or confirmed GPU copy completion. `kv_gpu_exclusive_blocks_at_finish`, `kv_cpu_exclusive_blocks_at_finish`, their peak fields, and GPU/CPU block-milliseconds count unique blocks with exactly one owning request; blocks shared across requests are excluded and reported by `kv_shared_blocks_observed`. The tracker observes every sequence group, including non-agent requests, so external request ownership is included in the exclusivity check. Prefix-cache blocks retained without a request owner are not attributed to a prior request. `kv_residency` identifies this source as `block_manager_observed`; it is a partial logical observation.
- `kv_swap_out_exclusive_blocks` and `kv_swap_in_exclusive_blocks` count distinct exclusive IDs in the block manager's scheduler-issued mappings. They describe requested migrations, not completed DMA. `kv_recompute_discarded_exclusive_blocks` counts exclusive request references present before successful scheduler recompute preemption; it is a discard/recompute cause, never a restore or proof of physical block eviction (the released blocks may remain in prefix cache). Token-level `kv_recomputed_tokens`, `kv_evicted_tokens`, and `kv_restored_tokens` remain null. Residency is integrated between observed hook boundaries, so these measurements are partial samples rather than continuous hardware residency. Tracker errors and capacity exhaustion produce unavailable values rather than zero. Weak request-group references and abort cleanup bound retained per-request state; incomplete lifecycle data is reported as unavailable with a reason.
- vLLM's native Prometheus endpoint is scraped by the gateway Collector every 15 seconds.

Pinned implementation references: [request timing fields](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/sequence.py), [trace completion hook](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/engine/llm_engine.py), [block-table allocation, freeing, forking, and swapping](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/core/block_manager.py), [prefix-cache refcounts and cached blocks](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/core/block/prefix_caching_block.py), [GPU/CPU allocator mapping](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/core/block/cpu_gpu_block_allocator.py), and [scheduler swap/recompute preemption](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/core/scheduler.py).

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
- **Process exit:** emits `process_exit_unmatched` for each observed entry still in flight, recording terminal process-exit disposition without fabricating a return value.
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
- Background writer appends per-node JSONL under `/var/log/agentic-kernel`. The research manifest sets `KERNEL_ARCHIVE_MAX_BYTES=0` to disable rotation and `KERNEL_REDIS_MAX_EVENTS=0`, `KERNEL_REDIS_TTL=0` to disable per-run count trimming and expiry. Redis memory and node disk usage can therefore grow without bound until an operator intervenes.
- Standalone collector defaults differ when these variables are absent: archive rotation is 64 MiB across five rotated files plus active file; Redis defaults to no count limit and a 30-day expiry. These are configurable collector defaults, distinct from the manifest's full-retention research settings. A zero TTL also removes existing expirations on touched run list/status keys.
- Run-attributed records go to Redis; shared inference records remain in node archives. Changing retention cannot restore records already trimmed or expired from Redis; an independent node archive might still contain them, but its completeness/recovery is unverified. The exporter has no default 10,000-record cap; `--max-kernel-records` can impose a cap explicitly, while `0` means export all API-retained records.
- Authenticated `GET /telemetry/runs/{run_id}/kernel?offset=0&limit=200` returns records, counts, truncation, run capture status, and node health; maximum page size is 500.
- Node heartbeat publishes every five seconds and expires after 20 seconds. Cumulative node counters include perf losses, map overflow, queue drops, discovery errors, identity misses, and export errors. These counters are node-level, not exact per-run loss totals.
- Redis outage, disk failure, process termination, perf/map/queue overflow, or collector restart can lose records. With the research manifest archive rotation is disabled, but node disk is finite and failure still loses records. Archives do not automatically replay into Redis; asynchronous exports are snapshots, not a completion barrier.

### Controls and validation limits

- `KERNEL_CAPTURE_ENABLED=false`: idle collector; does not load BCC probes and rejects capture registration. Requires rollout. Worker-side task scope registration is also gated by `AGENT_TELEMETRY_ENABLED`.
- `KERNEL_INFERENCE_SYSCALLS_ENABLED=false`: disables inference discovery while worker scopes may remain enabled.
- `KERNEL_TELEMETRY_ENABLED`: worker-side registration control. Supplied agent manifest enables it as required; failed registration/scope acknowledgement fails the task when global task telemetry is enabled.
- Kernel deployment needs cgroup v2, tracepoint/BCC support, compatible kernel headers, service-account pod-list permissions, host mounts, and privileged execution on both nodes.
- Readiness checks cover configured idle state or basic attachment/socket and discovery status. They do not prove complete coverage, lossless export, compatibility for every workload, or actual hardware-level performance.
- GPU execution itself is outside syscall telemetry. Host metrics and vLLM metrics do not substitute for syscall tracing.

## Capture policies, retention, and failure behavior

### Capture switches

- `AGENT_TELEMETRY_ENABLED` is the global task-instrumentation gate. It controls SDK/exporter initialization, program and semantic capture, worker kernel registration, and inference measurement joins. Disabling it preserves mandatory task/lifecycle ownership and the end-to-end timer.
- `AGENT_PROGRAM_TELEMETRY_ENABLED` controls saved program summaries, steps, and program events; `AGENT_SEMANTIC_TELEMETRY_ENABLED` controls LangGraph, phase, handoff, and procedural-graph observations. Both are subordinate to the global gate.
- `KERNEL_CAPTURE_ENABLED` controls collector probe capture. `AGENT_INFERENCE_TELEMETRY_ENABLED` controls inference-side adapter hooks; it is not itself a worker-side join gate. Worker joins return immediately when program capture is disabled, including through the global task gate. Disabling only the inference adapter while leaving worker program capture on can still incur the bounded missing-record join wait. `AGENT_INFERENCE_TRACING_ENABLED` controls vLLM native trace initialization; it must be enabled for the adapter's native completion callback to publish measurements. The inference startup patch is needed when applying the switch to a deployment command that does not yet conditionally pass tracing arguments.
- For an on/off comparison, keep program/semantic sub-switches, OTLP destinations, `KERNEL_TELEMETRY_ENABLED`, and `KERNEL_INFERENCE_SYSCALLS_ENABLED` the same in both modes. Toggle global task instrumentation, kernel capture, and inference measurement/native tracing together according to the comparison guide. Host/native Prometheus monitoring stays enabled in both modes, so the result isolates incremental task instrumentation under that monitoring baseline rather than measuring a cluster with all telemetry absent.
- `CONSOLE_K8S_DISCOVERY` controls read-only node/pod discovery. Console previews do not apply manifests; an operator must review, apply, and roll out changes.

### Content policy

- `AGENT_TELEMETRY_CONTENT_CAPTURE=off`: content becomes SHA-256/length metadata.
- `redacted`: default in application code; strings are secret-masked and bounded (normally 4 KiB, event lists to 100 items).
- `full`: supplied research manifests set this mode. Raw prompts, model responses, tool arguments/results, event payloads, and string attributes may be retained; lists are not capped at 100. Treat Redis and observability backends as sensitive research storage.
- OTLP model spans include exact per-step messages and tool spans include arguments/results under full mode. These can contain credentials, private code, or user data.
- Reports deliberately exclude prompts, responses, tool arguments/results, and workspace content. Kernel numeric syscall arguments and process/workload identifiers remain in exports.

### Task failure and persistence

- Task deadline includes queue time. Command timeout kills the shell process group. Step-budget overflow fails the program. Model responses marked truncated (`finish_reason=length`) are discarded and their partial tool calls are not dispatched. One unforced truncation can recover by requiring a structured write/verification action only when a coding completion constraint remains unmet. Truncation with no unmet constraint, repeated truncation, or a truncated forced action fails the program. A tool failure may be recovered by a later step.
- Coding completion recovery is also bounded for unchanged-workspace and missing/unsuccessful command constraints. Required calls must be actual structured calls for the named tool. Every write attempt, even one that fails after a partial write, invalidates earlier command verification. A successful command does not establish that tests passed or requested artifacts exist; independent grading remains separate.
- Queue claims move atomically into `task_processing`. Deadline reaper marks abandoned/overdue tasks Timeout after a 30-second grace period and does not rerun tools.
- Worker loss can make partial timings unknown; summary reports `timing_complete=false`. Reaper time is the observed terminal time, not a reconstructed crash time.
- Redis uses AOF with `appendfsync everysec` and a persistent volume; abrupt failure can lose up to about one second of recent writes. Phoenix and Prometheus also use persistent volumes. Local-path storage survives pod replacement, not loss of its node disk.
- Semantic ledger retention defaults: 30 days and 10,000 events per run. Program summaries/steps are stored separately from the bounded event tail. Kernel record retention is separately controlled by the kernel collector settings above.
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

## Runtime overhead comparisons

The comparison runner supports an explicit `--eligibility runtime` policy for
overhead-only experiments: runtime Success, a nonempty answer, and mandatory
timing are required, while independent correctness checks remain diagnostic.
Both batches must select the same policy. The default `verified` policy retains
the correctness requirements described below. Neither policy retries failed
attempts or includes warmups; both require matching configuration and on-mode
capture. A runtime-only result concerns returned answers, not verified solutions.

The end-to-end procedure is documented in [`test.md`](test.md). `scripts/run_telemetry_sample.py` runs the single small CSV repair task: inspect the existing files, fix floor division in the average, run tests, generate a North-filtered JSON report and read it. Suitability requires independent correctness verification, at least two actual tool-bearing steps, and a successful final no-tool step. A single successful sample establishes evidence for that path; it is not an overhead estimate.

`scripts/run_telemetry_comparison.py` separately runs the same task sequentially with task telemetry on and off, exactly five measured attempts per mode and one separate warmup by default. It records workload and deployment configuration hashes and rejects incompatible comparisons. Use the same image/configuration, model, timeout, step budget and verification criteria, keep background host load stable, and retain each repetition's outputs.

- Primary measurement: worker `task_end_to_end_time_ms`, which includes queue wait and synchronous ledger finalization. Compare per-run values and report mean, median, spread, and percent change; retain individual repetitions because five observations are a small sample.
- Secondary measurements: `task_completion_time_ms`, client-observed submit-to-terminal time, and measured tool/LLM/inference timing. Client time includes polling cadence. Do not substitute it for the worker primary metric.
- Under the default verified policy, include a repetition only when runtime status is `Success`, independent fixture verification passed, and required telemetry evidence/identity checks are complete. Runtime-only policy accepts incorrect returned answers as described above. Keep all attempts and their exclusion reasons visible, including failures and warmups.
- Preserve a constant host/native metrics baseline. Changing kernel or inference switches requires applying/rolling out the corresponding manifests and confirming effective pod settings before measuring; do not compare mixed configurations. Run the paired modes close together and avoid unrelated load changes.
- No numeric overhead result is implied by the implementation or documentation. The comparison must be executed on the target deployment. Deployment and runtime validation of the current changes have not been confirmed; past sample traces only demonstrate telemetry delivery for those particular runs.

The runner retains all attempts and does not automatically replace them. It withholds an eligible estimate unless there are exactly five successes meeting the selected policy per mode, demonstrated on-mode capture, matching workload/settings/policy and matching comparable deployment images/configuration. Off-mode correctness checks do not require suppressed program steps or kernel records. After both batches, restore the on settings in a quiet window.

```text
overhead_ms = mean_on_ms - mean_off_ms
overhead_percent = 100 * overhead_ms / mean_off_ms
```

These workflows are operator-run: the assistant supplies host-labelled commands and interprets actual outputs. Latest retention, exclusive-KV and comparison changes have only static review and whitespace validation; authored tests, BPF compilation, builds, deployment and hardware measurements remain unexecuted. Historical user-supplied Phoenix spans establish delivery for those traces, not current backend health or task correctness.

## Explicitly unsupported or unverified

- Agent `Success` and a generated benchmark patch do not mean benchmark tests passed. SWE-bench runner is for telemetry/patch generation, not leaderboard-equivalent grading.
- vLLM 0.8.5 V0 lifecycle values are partial block-manager ownership observations, not kernel-level KV bytes or completed DMA. Shared blocks and cache-only blocks are excluded from exclusive request ownership; token-level KV lifecycle fields remain null. Swap mappings indicate scheduled movement, not completed DMA, and recompute-discard counts do not prove physical eviction.
- vLLM batch-forward GPU time is not exclusive per-request GPU occupancy. Shared inference syscalls have no exclusive request/run attribution.
- Kernel syscall coverage, BPF verifier acceptance, losslessness, and performance have not been established merely by local tests or manifest validation; validate on the actual nodes and workload. Disabling automatic Redis trimming/expiry and archive rotation does not remove finite buffers, finite disk, or collector/backend failure modes. Previously trimmed kernel records cannot be recovered.
- Configured OTLP paths and manifests do not prove that Collector, Phoenix, Prometheus, Loki, or kernel collectors are currently healthy. Check rollout, collector logs, heartbeats, and backend data in the target cluster.
- Telemetry does not inspect hidden model reasoning, decode syscall pointer targets, or observe GPU device execution.
