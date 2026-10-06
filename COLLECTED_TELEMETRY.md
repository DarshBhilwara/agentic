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

Telemetry identities that are available at the point of
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

Batch-attributed GPU time is not
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



## Execution events

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

