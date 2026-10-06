# Collected telemetry

This document inventories the telemetry collected by the native agent runtime.
For exact field semantics and the external ingestion contract, see
[`TELEMETRY.md`](TELEMETRY.md).

## Collection flow

```text
gateway -> Redis task queue -> worker/agent -> vLLM
   |              |                |           |
   +--------------+----------------+-----------+
                  telemetry
                      |
          +-----------+----------------+
          |                            |
  Redis execution ledger       OpenTelemetry Collector
   runs, steps, events          traces, logs, metrics
                                        |
                               Phoenix and Prometheus
```

Telemetry is correlated with the identities that are available at the point of
collection: `user.id`, `session.id`, `task.id`, `run.id`, `root.run.id`,
`agent.id`, `agent.run.id`, `agent.parent.run.id`, `handoff.id`, `trace.id`, and
`span.id`.

## Task and program telemetry

One submitted task is one program. The gateway and worker record:

| Field | Meaning |
| --- | --- |
| `task_status` | `Queued`, `Running`, `Success`, `Failed`, or `Timeout` |
| `task_wait_time_ms` | Submission to worker admission, including queueing |
| `task_completion_time_ms` | Submission to terminal processing |
| `inference_steps` | Attempted model requests, including failed requests and the final response |
| `completed_steps` | Steps that completed successfully |
| `tool_calls` | Total tool invocations |
| `tool_failures` | Tool invocations that failed or timed out |
| `engine_measured_steps` | Steps successfully joined to inference-node measurements |

The run also stores submission, start, and completion timestamps; task, session,
agent, benchmark, and case identities; errors; results; and the termination
reason when known.

Program summaries contain total model, tool, reasoning, prefill, decode, and
inference-queue times; token totals; final and peak context size; and total KV
recomputation. A task timeout includes time spent waiting in the task queue.

`Success` means that the agent returned normally. It does not mean that a
benchmark patch passed its tests. Benchmark correctness is recorded separately
by the benchmark runner.

## Step and model telemetry

A step consists of one model request followed by every tool call selected by
that response. For each step the runtime records:

- program ID, step index, step status, and unique inference request ID;
- total step time and client-observed model request time;
- total acting time for sequential tool execution and observation insertion;
- prompt, completion, and context token counts;
- all tool measurements associated with the step; and
- whether inference-engine telemetry was available.

The vLLM adapter adds per-request engine measurements:

| Field | Meaning |
| --- | --- |
| `step_t_reasoning_ms` | Batch-attributed CUDA model-forward time |
| `prefill_time_ms` | First scheduling to first generated token |
| `decode_time_ms` | First generated token to request completion |
| `inference_queue_time_ms` | Initial scheduling wait inside vLLM |
| `kv_recomputed_tokens` | Reusable full prefix-cache tokens unavailable at admission |

Model spans and events also record the model name, finish reason, request
failure type, and client-observed latency. `step_t_reasoning_ms` is a timing
measurement; it does not contain private model reasoning or chain-of-thought.

Engine timing is joined to the step using the exact request ID. Missing, late,
or dropped measurements remain `null`. Batch-attributed GPU time is not
exclusive per-request GPU occupancy and must not be summed across concurrent
programs to estimate physical GPU utilization.

## Agent intent telemetry

After each model response, the runtime records the observable next-action
decision:

- `agent.intent.decision`: `use_tool` or `respond_directly`;
- `agent.intent.tool_required`;
- `agent.intent.action` and `agent.intent.reason_code`;
- `agent.intent.tool_name` for the primary selected tool;
- `agent.intent.tool_names` for all selected tools; and
- `agent.intent.tool_actions`, aligned with the selected tools.

Action categories include workspace inspection, file reads and writes, command
execution, current-information retrieval, calculations, agent delegation,
agent coordination, and direct response. These fields describe the selected
action, not hidden reasoning.

## Tool and artifact telemetry

Every tool invocation records:

- program ID and step index;
- tool name and tool-call ID;
- start and completion events;
- `tool_latency_ms`;
- `tool_status`: `Success`, `Failed`, or `Timeout`;
- recorded exceptions; and
- bounded tool arguments and results according to the content-capture policy.

Successful `read_file` and `write_file` calls also produce `artifact.read` and
`artifact.written` events. The worker produces an `artifact.written` event for
the final task-result file.

## Multi-agent telemetry

Delegation records the parent and child task/run relationship, target agent
identity and name, handoff ID, shared root-run ID, and a bounded delegated-task
description. This permits reconstruction of the dynamic agent tree without
requiring a predefined workflow graph.

## Execution-ledger events

The native runtime currently emits events in these categories:

- task lifecycle: `task.queued`, `task.started`, `task.completed`,
  `task.failed`, and `task.timeout`;
- step lifecycle: `step.started` and `step.completed`;
- agent lifecycle and coordination: `agent.started`, `agent.completed`, and
  `agent.delegated`;
- model lifecycle: `model.requested`, `model.completed`, and `model.failed`;
- tool lifecycle: `tool.started` and `tool.completed`;
- artifacts: `artifact.read` and `artifact.written`; and
- validation: `agent.completion.rejected` when a coding task returns without
  required workspace changes.

External runtimes can append compatible or namespaced events through the
authenticated telemetry API.

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
- `agent.program.inference.steps`.

The collector also scrapes vLLM's native Prometheus endpoint every 15 seconds.

## Host and process telemetry

An OpenTelemetry Collector runs on every node and collects the following every
10 seconds:

- CPU and system load;
- memory;
- network interfaces;
- disks and filesystems;
- individual process measurements; and
- process counts and states.

The measurements include the host and Kubernetes node identity so agent-node
and inference-node behavior can be separated.

## Storage, retention, and content capture

| Destination | Data |
| --- | --- |
| Redis | Durable run metadata, program summaries, per-step records, and events |
| Phoenix | Correlated OpenTelemetry traces |
| Prometheus | Application, host, process, and native vLLM metrics |
| Collector debug exporter | Received OTLP logs |

The Redis ledger defaults to 30-day retention and keeps the latest 10,000
events per run. Step records and summaries are stored separately from the
bounded event tail.

Content capture is controlled by `AGENT_TELEMETRY_CONTENT_CAPTURE`:

- `off`: retain hashes and lengths instead of content;
- `redacted`: mask likely credentials and bound captured content (default); or
- `full`: retain bounded content, intended only for trusted evaluation systems.

Unknown measurements are represented as JSON `null`, never fabricated zeroes.
If the deadline reaper finalizes an abandoned worker, unrecoverable partial
durations remain unknown and the summary records `timing_complete=false`.
