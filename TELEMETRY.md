# Telemetry Contract

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
| `task_completion_time_ms` | Submission-to-terminal-processing elapsed time, including queue wait; excludes checkout preparation and subsequent benchmark grading. Null while running. |
| `task_wait_time_ms` | Submission to worker admission. Includes waiting behind other programs. |
| `llm_inference_time_ms` | Per-step client request wall time, including inference queueing and transport. |
| `step_t_reasoning_ms` | vLLM CUDA model-forward milliseconds accumulated for batches containing this request. Includes prefill and decode; **not exclusive per-request GPU occupancy**. |
| `step_t_acting_ms` | Wall time for all tool execution and observation insertion in the step. Zero for no-tool steps. |
| `tools[].tool_name`, `tool_call_id` | Tool identity within the step. |
| `tools[].tool_latency_ms`, `tool_status` | Invocation duration and `Success`, `Failed`, or `Timeout`. Nonzero shell exits, malformed JSON, unavailable tools, and legacy error results count as failures. |
| `prompt_tokens`, `completion_tokens`, `context_tokens` | Server usage; context is this request's accumulated prompt plus this response, not the sum of repeated prompts across requests. Tool observations enter the next request's prompt count. |
| `prefill_time_ms` | Inference-node first scheduling to first generated token. A phase wall time, not isolated CUDA prefill kernel time. |
| `decode_time_ms` | First generated token to request finish on the inference node. Zero is valid for a one-token completion. Includes interleaved scheduling/preemption. |
| `inference_queue_time_ms` | Initial vLLM scheduling wait, distinct from the task queue on the agent node. |
| `kv_recomputed_tokens` | Previously computed **full prefix-cache blocks** shared with the immediately previous step but unavailable for reuse at this step's admission, in tokens. See limits below. |

Program summaries contain `total_llm_inference_time_ms`, `total_tool_time_ms`
(sum of tool invocation durations), `total_tool_wait_time_ms`,
`total_reasoning_time_ms`, `total_prefill_time_ms`, `total_decode_time_ms`,
`total_inference_queue_time_ms`, `kv_recomputed_tokens`, token totals,
`peak_context_tokens`, `tool_calls`, and `tool_failures`. An engine total is
null if any constituent step lacks that measurement. `engine_measured_steps`
reports how many request records were joined. GPU times are batch-attributed,
so adding them across concurrent programs overcounts physical GPU occupancy.
Phase times overlap inference wall time and must not be added to it.

Step records and summaries are persisted separately from the bounded event
tail. Program/step/request identifiers are span attributes and ledger fields,
not high-cardinality Prometheus labels. Prometheus receives status/benchmark
histograms for program duration, queue wait, and inference-step count.

### Inference-node adapter

Build `inference/Dockerfile` and use the supplied inference manifest. It requires
vLLM **0.8.5 V0**, prefix caching, one sequence per request, and detailed tracing.
The worker sends W3C trace context and a unique `X-Request-Id` containing the
program and step identity. The adapter records engine measurements under that
exact request ID in Redis with a one-hour TTL; the worker joins them into the
durable step. A bounded background queue keeps Redis I/O off the scheduler.
Absent/late/dropped measurements stay null. Collection adds up to 0.5 seconds
of join wait per missing record, included in task time but not model time.

KV accounting compares actual chained token-block hashes, not prompt lengths.
It excludes newly added context and incomplete blocks. It measures lost prefix
reuse between requests; it does not distinguish capacity eviction from an
explicit cache reset, and does not count within-request preemption recompute.
An inference restart, missing prior step, disabled prefix cache, or expiration
from the 10,000-program in-process history makes this field null. The first
step is zero when the adapter and prefix cache are active. These limits are
intentional: missing history is not evidence of zero eviction.

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
multi-agent systems. It deliberately does not describe a workflow or impose a
graph topology. A topology is reconstructed from run identities, parent agent
invocations, handoff events, trace context, and timestamps.

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

The native runtime emits `task.queued`, `task.started`, `task.completed`,
`task.failed`, `agent.started`, `agent.completed`, `agent.failed`,
`agent.delegated`, `model.requested`, `model.completed`, `tool.started`,
`tool.completed`, `artifact.read`, and `artifact.written`.

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
the supplied user identity with the authenticated user. Payload strings and
external attributes are redacted before storage.

## Retention and capture

The Redis ledger uses TTL-based retention and keeps a bounded tail per run.
Defaults are 30 days and 10,000 events. Set
`AGENT_TELEMETRY_CONTENT_CAPTURE=off` for hashes and lengths only,
`redacted` for masked bounded content, or `full` only in a trusted evaluation
environment.

## Traces and metrics

OpenTelemetry traces contain correlated  for task submission, complete
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
- `agent.program.inference.steps`.

The collector also scrapes vLLM's native Prometheus endpoint every 15 seconds.


