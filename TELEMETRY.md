# Telemetry Contract

## Collection architecture

In the full single-cluster deployment, task execution and telemetry flow as
follows:

```text
gateway -> Redis task queue -> worker/LangGraph agent -> vLLM
   |              |                |           |
   +--------------+----------------+-----------+
                  telemetry
                      |
       OpenTelemetry Collector -> Phoenix and Prometheus
```

The execution ledger and OpenTelemetry data carry the available collection
identities: `user.id`, `session.id`, `task.id`, `run.id`, `root.run.id`,
`agent.id`, `agent.run.id`, `agent.parent.run.id`, `handoff.id`, `trace.id`,
and `span.id`.

Within a worker, every task follows the explicit LangGraph route
`START -> model -> tools -> model -> END`. The conditional model edge goes to
`tools` only when the model emits tool calls. Dynamic child-agent delegation
still crosses the Redis task queue as an independent task, so it preserves
causal identities without being represented as a fixed graph edge.

## Program-level benchmark measurements

A **program** is one submitted task (one SWE-bench Pro instance attempt).
`program_id` is the task UUID; `benchmark.case.id` / `instance_id` is the dataset
identifier, and `run.id` links the event ledger. Repeated attempts at the same
instance receive different program IDs. `step_index` starts at 1 for each
program, independently of the interactive session turn number.

One step is one LLM request, all tool calls selected by that request, and their
returned observations. Tools currently execute sequentially. A final answer
without tools is also one step. A failed LLM attempt counts as an inference
step; SDK retries are disabled so attempts cannot disappear inside a step.

`GET /telemetry/programs/{program_id}` or `GET /telemetry/runs/{run_id}` returns
`summary`, `steps`, `run`, and a page of `events`. Times are numeric milliseconds;
unknown measurements are JSON `null`, never fabricated zeroes.

| Field | Definition / measurement source |
| --- | --- |
| `task_status` | `Queued`, `Running`, `Success`, `Failed`, or `Timeout`. Success means the agent returned normally, not that benchmark tests passed. |
| `inference_steps` | Number of attempted model requests, including the final direct response and failed attempts. |
| `completed_steps` | Steps that completed successfully. |
| `task_completion_time_ms` | Submission-to-terminal-processing elapsed time, including queue wait; excludes checkout preparation and subsequent benchmark grading. Null while running. |
| `task_wait_time_ms` | Submission to worker admission. Includes waiting behind other programs. |
| `llm_inference_time_ms` | Per-step client request wall time, including inference queueing and transport. |
| `step_t_reasoning_ms` | vLLM CUDA model-forward milliseconds accumulated for batches containing this request. Includes prefill and decode; **not exclusive per-request GPU occupancy**. |
| `step_t_acting_ms` | Wall time for all tool execution and observation insertion in the step. Zero for no-tool steps. This is the total acting phase; per-tool `tool_ttf_ms` is the delay from that phase's start to an individual tool's start. |
| `tools[].tool_name`, `tool_call_id`, `tool_index` | Tool identity and ordinal within the step. |
| `tools[].tool_latency_ms`, `tool_ttf_ms`, `tool_status` | Total invocation duration, time from acting-phase start to tool start (TTF), and outcome. |
| `prompt_tokens`, `completion_tokens`, `context_tokens` | Server usage; context is this request's accumulated prompt plus this response, not the sum of repeated prompts across requests. Tool observations enter the next request's prompt count. |
| `prefill_time_ms` | Inference-node first scheduling to first generated token. A phase wall time, not isolated CUDA prefill kernel time. |
| `decode_time_ms` | First generated token to request finish on the inference node. Zero is valid for a one-token completion. Includes interleaved scheduling/preemption. |
| `inference_queue_time_ms` | Initial vLLM scheduling wait, distinct from the task queue on the agent node. |
| `kv_recomputed_tokens` | Per-request recomputation tokens when supported by engine lifecycle attribution. Null in the shipped vLLM 0.8.5 V0 adapter. See limits below. |

Program summaries contain `total_llm_inference_time_ms`, `total_tool_time_ms`
(sum of tool invocation durations), `total_tool_wait_time_ms`,
`total_reasoning_time_ms`, `total_prefill_time_ms`, `total_decode_time_ms`,
`total_inference_queue_time_ms`, `kv_recomputed_tokens`, token totals,
`peak_context_tokens`, `tool_calls`, and `tool_failures`. An engine total is
null if any constituent step lacks that measurement. `engine_measured_steps`
reports how many request records were joined. GPU times are batch-attributed,
so adding them across concurrent programs overcounts physical GPU occupancy.
Phase times overlap inference wall time and must not be added to it.

The run also stores submission, start, and completion timestamps; task,
session, agent, benchmark, and case identities; errors and results; and the
termination reason when it is known. `Success` means the agent returned
normally; it does not mean that a generated benchmark patch passed its tests.

Step records and summaries are persisted separately from the bounded event
tail. Program/step/request identifiers are span attributes and ledger fields,
not high-cardinality Prometheus labels. Prometheus receives status/benchmark
histograms for program duration, queue wait, and inference-step count.

### Agent decisions, tools, and artifacts

After each model response, the runtime records the observable next-action
decision: `agent.intent.decision` (`use_tool` or `respond_directly`),
`agent.intent.tool_required`, `agent.intent.action`,
`agent.intent.reason_code`, the primary `agent.intent.tool_name`, and the
complete `agent.intent.tool_names` and aligned `agent.intent.tool_actions`.
Action categories cover workspace inspection, file reads and writes, command
execution, current-information retrieval, calculations, delegation,
coordination, and direct response. These describe selected actions, not hidden
reasoning.

Every tool invocation records its program and step identity, tool name,
tool-call ID, start and completion events, `tool_latency_ms`, `tool_status`
(`Success`, `Failed`, or `Timeout`), recorded exceptions, and bounded arguments
and results subject to the content-capture policy. Successful `read_file` and
`write_file` calls emit `artifact.read` and `artifact.written`; the worker also
emits `artifact.written` for the final task-result file.

### Inference-node adapter

Build `inference/Dockerfile` and use the supplied inference manifest. It requires
vLLM **0.8.5 V0**, prefix caching, one sequence per request, and detailed tracing.
The worker sends W3C trace context and a unique `X-Request-Id` containing the
program and step identity. The adapter records engine measurements under that
exact request ID in Redis with a one-hour TTL; the worker joins them into the
durable step. A bounded background queue keeps Redis I/O off the scheduler.
Absent/late/dropped measurements stay null. Collection adds up to 0.5 seconds
of join wait per missing record, included in task time but not model time.

The shipped adapter does not expose request-scoped allocation/free/swap
ownership. KV recomputation, retention, eviction, restoration, tier, residency,
and affinity fields therefore remain null, including the first step. The
adapter supplies an unavailable status/reason; prefix-hash overlap is not used
as evidence of recomputation or eviction.

The implementation follows the pinned upstream
[request timing fields](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/sequence.py),
[trace completion hook](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/engine/llm_engine.py),
and [prefix cache allocation](https://github.com/vllm-project/vllm/blob/v0.8.5/vllm/core/block_manager.py).
Do not upgrade vLLM without revalidating the adapter on the inference node.

### Failure and retention behavior

The task deadline includes queue time. The worker interrupts timed-out tasks;
command timeouts kill the shell's process group. Exceeding the step budget or
receiving a truncated model response fails the program. A tool failure can be
recovered by a later step without automatically failing the whole task.

Queue claims move atomically into `task_processing`. A separate deadline
reaper marks abandoned/overdue tasks Timeout after a 30-second grace period;
it never reruns tools. Partial durations lost with a killed worker are unknown,
and the summary has `timing_complete=false`. Reaper time is the observed
terminal time, not a reconstructed crash timestamp. Normal timings use a
monotonic clock for execution and agent-node wall time for queue admission.

Redis uses AOF with `appendfsync everysec` and a persistent volume (up to one
second of recent writes can be lost on abrupt failure). Phoenix and Prometheus
also have persistent volumes. Local-path volumes survive pod replacement,
not loss of the agent node's disk. This is a single-agent-node research setup.

The execution ledger is an append-only event record for autonomous, dynamic
multi-agent systems. It does not orchestrate execution or persist a LangGraph
topology. The native worker's LangGraph graph governs a queued task; the ledger
records its events and reconstructs dynamic delegation from run identities,
parent agent invocations, handoff events, trace context, and timestamps.

## Identity

Every run has `run.id`. Events should include these fields whenever known:

```text
user.id
session.id
task.id
agent.id
agent.run.id
agent.parent.run.id
root.run.id
handoff.id
trace.id
span.id
```

`agent.id` is a durable logical identity. `agent.run.id` identifies one
invocation. A child agent has its own task and run IDs, an
`agent.parent.run.id`, and the same `root.run.id` as its initiating task.

## Event types

The native runtime emits task lifecycle events (`task.queued`, `task.started`,
`task.completed`, `task.failed`, and `task.timeout`); step lifecycle events
(`step.started`, `step.completed`); agent events (`agent.started`,
`agent.completed`, `agent.delegated`, and `agent.completion.rejected` for a
coding task that returns without required workspace changes); model events
(`model.requested`, `model.completed`, `model.failed`); tool events
(`tool.started`, `tool.completed`); and artifact events (`artifact.read`,
`artifact.written`).

External runtimes may use these names and add a namespaced event type such as
`langgraph.checkpoint.saved` or `custom.policy.decision`. Event payloads are
for bounded, redacted diagnostic data; searchable dimensions belong in
`attributes`.

## External runtime ingestion

Create a run first:

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

Then append events:

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

The gateway authenticates every request, verifies run ownership, and replaces
the supplied user identity with the authenticated user. In `redacted` mode,
payload strings and external attributes are masked before storage; in the
`full` research mode they are retained verbatim.

## Retention and capture

The Redis ledger uses TTL-based retention and keeps a bounded tail per run.
Defaults are 30 days and 10,000 events. Set
`AGENT_TELEMETRY_CONTENT_CAPTURE=off` for hashes and lengths only,
`redacted` for masked bounded content, or `full` only in a trusted evaluation
environment.

The supplied research deployment sets `full`. In that mode the
Redis ledger retains raw prompt, model response, tool argument/result, event
payload, and string attribute values, including full lists (no content
redaction or truncation). OTLP span events additionally retain the exact model
request/response and tool arguments/results. Every event accepted by the
central execution ledger is also exported as an individual OTLP span with
JSON-encoded structured attributes and payload. This is intentionally
unsuitable for production or untrusted users and may contain secrets.

Full mode also adds the exact per-step model `messages` request and response,
plus tool arguments and results, as correlated OpenTelemetry span events in
addition to the Redis ledger events.

## Traces and metrics

OpenTelemetry traces contain correlated spans for task submission, complete
program execution, agent invocation, model inference, and tool execution.
Trace context is propagated from the gateway through the worker to vLLM.
Exceptions are attached to the span where they occur. Phoenix receives these
traces.

The runtime exports these custom metrics to Prometheus through the collector:

- `agent.tasks.total`;
- `agent.task.errors.total`;
- `agent.inference.requests.total`;
- `agent.telemetry.events.total`;
- `agent.handoffs.total`;
- `agent.program.duration`;
- `agent.program.queue.duration`; and
- `agent.program.inference.steps`;
- `agent.inference.step.duration`;
- `agent.tool.duration`;
- `agent.tool.wait`; and
- `agent.tool.calls.total`.

The collector also scrapes vLLM's native Prometheus endpoint every 15 seconds.

The collectors use bounded file-backed sending queues on a
two-gigabyte persistent volume and exporter retry (up to five minutes) for
Phoenix and Prometheus remote write. This
protects against short observability outages; the Redis execution ledger
remains the durable source of task and step records. Application logs are
exported as OTLP logs to the persistent Loki deployment (`loki-svc:3100`),
which uses its own local-path volume.
Loki is a ClusterIP service; access its API locally with
`kubectl -n agentic-ai port-forward service/loki-svc 3100:3100`.

## LangGraph topology and delegation correlation

Phoenix mapping: graph and node spans use the OpenInference `CHAIN` kind,
agent invocation spans use `AGENT`, model requests use `LLM`, and tool calls
use `TOOL`. LLM spans also carry bounded numeric
`llm.token_count.prompt`, `llm.token_count.completion`, and
`llm.token_count.total` attributes (plus the equivalent `gen_ai.usage.*`
attributes), allowing Phoenix's native LLM views to render token counts.
Full model request/response content remains in span events. Tool OpenInference
input/output fields follow the capture policy and are scalar-normalized for
OTel; in full research mode they intentionally retain raw tool arguments and
results, while protected modes retain redacted or hash/length values.

Every queued task owns one `langgraph.state_graph` execution. The runtime emits
`langgraph.graph.started`, `langgraph.graph.ended`, and
`langgraph.graph.error` events and a `langgraph.graph` span with `graph.id`,
`graph.type`, `task.id`, `run.id`, `agent.id`, `agent.run.id`, and
`root.run.id`. Model and tools transitions are separate nested
`langgraph.node.model` and `langgraph.node.tools` spans with started, terminal,
and error ledger events; terminal events are emitted with an explicit status so
errors are not mislabeled as successful completion. The existing inference and individual tool spans nest
under those nodes.

Delegation is dynamic, not a fixed static subgraph. A delegated task is a child
LangGraph graph with `parent.task.id`, `parent.run.id`, `parent_agent_run_id`,
`root.run.id`, and a durable `handoff.id`. W3C trace context is stored with the
Redis task and extracted by the child worker, so the root trace remains
correlated across the queue boundary. Parent delegation, queue, check/poll,
and completion events include the handoff and child task/run identifiers.
The complete identity chain is `root.run.id -> parent.task.id` and
`parent.run.id` -> `handoff.id` -> child `task.id` and `run.id`; compare queue
wait and execution durations on each side of the handoff to localize delay.

Telemetry completeness limits are explicit: missing engine fields mean the
measurement was unavailable, late, failed before collection, or outside the
join window; they never mean zero. A worker crash can lose partial timings,
and the deadline reaper marks those fields unknown and writes
`timing_complete=false`. Every event persisted by `TelemetryLedger.emit` is exported as a standalone
child span named for its event type. This includes model/tool steps, phase
transitions, handoffs, graph observations, queued tasks, externally ingested
events, and reaper timeouts. Each span carries the event ID, timestamp,
severity, schema version, all supplied identity fields, and JSON-encoded
structured attributes and payload. Error and fatal events carry OpenTelemetry
error status. Export failures are logged and do not interrupt execution.

Terminal task events also carry the final procedural graph and saved program
summary, covering successful, failed, and reaped runs. The `langgraph.graph`
span carries the final `procedural_graph` JSON snapshot and
`procedural_graph.status`; `agent.program` carries its final summary JSON and
`agent.program.status`. Graph node IDs, action labels, signatures, procedure
run matches, subgraphs, and delegation edges remain readable in the snapshot.
Phoenix shows these values in span details; it does not render the JSON as a
separate native node-and-edge canvas. The trace tree shows event spans beneath
the active execution span while the snapshot preserves procedural topology.
In `redacted` mode, topology string values pass through secret masking. In
`off` mode, string values such as node names, action labels, and signatures are
replaced with hash/length metadata; JSON keys and numeric structure remain
available, while identifier values are intentionally obscured.

The existing manifests route OTLP traces, metrics, and logs through the
collector, whose trace pipeline forwards to Phoenix. This documents the
application export path; it does not verify a running collector or Phoenix
deployment.


# Durable procedural graphs and KV limits

Run and program responses include `procedural_graph`, a durable materialization
of observed model, tool, delegation, and terminal nodes/edges. A normalized
action-path signature indexes repeated procedures across runs. The graph is
constructed during execution and is independent of the bounded event tail. Small
canonical action windows are persisted as reusable subgraph definitions and
occurrences, scoped to the owning user. Delegation handoffs add causal parent to
child edges. It does not inspect hidden reasoning or claim task quality.
The `subgraphs` object in `procedural_graph` contains canonical IDs, action
patterns, version, and owner-scoped occurrence run IDs.

The vLLM 0.8.5 V0 adapter reports KV lifecycle fields only when request-scoped
allocation/free/swap ownership is exposed by the engine. Otherwise fields remain
null and `kv_measurement_reason` explains the unavailable attribution. Prefix
hash comparisons are not used as eviction or recomputation claims. Tool latency
is retained only as observed timing; it does not predict or control KV retention.

## Kernel syscall layer

The dedicated eBPF collector records syscall entry and exit separately from the
semantic execution ledger. See [kernel collection](kernel/README.md) for event
fields, cgroup scope, configuration, retention/loss behavior, and cluster smoke
checks. [The overall plan](TELEMETRY_PLAN.md) records the three-layer design and
the deferred GUI. Kernel observations require privileged node deployment;
ordinary host/process metrics do not substitute for syscall tracing.

## Console and report snapshots

The gateway's `/dashboard/` console provides configuration previews, device and
collector observations, run inspection, and bounded report exports. Its
authenticated `/console` API retains the existing run ownership boundary.
See [GUI.md](GUI.md) for startup, capture controls, offline/demo behavior,
report limits, and read-only Kubernetes discovery. Configuration drafts and
observed deployment state are distinct; previewing settings does not apply them.
