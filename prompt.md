# Project handoff: telemetry deployment, single sample, and overhead comparison

Updated 2026-11-02, Asia/Kolkata.

## Your role and the immediate objective

Help the user execute both workflows on their research cluster:

1. The existing single small CSV repair sample, with multiple real ReAct cycles
   and correlated program, semantic, inference, Phoenix, and kernel evidence.
2. The same task five measured times with task telemetry on and five measured
   times with it off, with one separate warmup per mode, then mean runtime and
   overhead in milliseconds and percent.

**You give instructions; the user runs them. Do not execute tests, sample scripts,
measurements, compilation, builds, browser checks, or cluster operations yourself.**
Read source and edit authorized files when needed. Provide short host-labelled
command blocks, wait for actual user output, interpret it, then give the next
step. Do not claim commands were executed because you supplied them. This handoff
does not change the user's no-execution instruction.

Read `AGENTS.md` if present, `TELEMETRY.md`, `test.md`, and `kernel/README.md`.
Inspect the comparison runner and inference adapter as needed. `test.md` is the
operator guide; section 6 contains the comparison commands. Do not produce new
presentation Markdown. The user handles presentation/code explanation.

## Hosts, source, and access

| Host | Responsibility | Checkout |
| --- | --- | --- |
| ryzen01, user agentic, node agent-01 | Agent host; worker image builds/imports; sample and comparison clients | `/home/agentic/agentic` |
| iiitd-desktop | K3s administration and inference; inference image builds/imports | `/home/iiitd/Documents/agentic` |
| Assistant workspace | Source inspection/authorized edits only; no verified remote cluster access | `/home/darsh/research/agentic` |

- Do not assume a local kubeconfig points at the research cluster or invent SSH
  access. The user runs remote commands.
- Namespace: `agentic-ai`. Gateway: `http://<agent-host>:30080`; dashboard:
  `/dashboard/`. Preserve the working `AGENTCTL_GATEWAY_URL`; clients default to
  `http://localhost:30080` on the agent host.
- Token already exists at `~/.agentic_token`; clients read it. Never ask for or
  print its contents.
- Phoenix: `http://<agent-host>:30006`. Inference service:
  `http://vllm-service:8000/v1`, served model `qwen`, pinned vLLM 0.8.5 V0 and
  Qwen2.5-14B-Instruct-AWQ. Preserve model, prefix caching, resources, scheduling,
  worker count and background monitoring across comparison modes.

## Exact deployment checkpoint

The user previously confirmed the gateway and two workers running
`agentic-worker:dd63cac`, with old workers gone and both Redis task lists empty.
This is historical evidence, not a current image inventory.

The assistant workspace HEAD was observed as `05a1775` (`test fix`) when this
handoff was prepared, with many modified and untracked files. **Latest retention,
inference/KV, timing, and comparison changes are not confirmed committed,
synchronized, built, or deployed.** Do not use that hash as a deployment tag
without checking the actual source. A `git archive HEAD` build excludes uncommitted
edits and untracked files; the user must deliberately commit/synchronize the
desired source first. Never commit or overwrite their changes yourself.

Latest work was statically reviewed; review findings were corrected and
`git diff --check` passed. Tests, compilation, BPF verifier checks, builds and
hardware/runtime validation were NOT run for these changes. Do not present
historical test counts as validation of current source.

Before a new sample, guide the user through:

1. Inspecting Git status/revision on the relevant hosts without discarding edits.
2. Inspecting live images, readiness and the two Redis lists `task_queue` and
   `task_processing`. Wait for both lengths to be zero; never clear them.
3. Synchronizing the complete intended committed source, including new files.
4. Building versioned worker, inference and kernel images from that source.
   Worker image serves gateway, workers and reaper. Kernel image must be imported
   on both selected nodes; inference image belongs on the inference node.
   Compare relevant in-image source hashes with the committed source.
5. Applying only targeted image/config updates. Base manifests contain `:0.1`
   tags and can revert live images; do not blindly apply them. No Kubernetes
   reinstall, PVC deletion, namespace cleanup or inference upgrade is needed.
6. Installing `manifests/inference-telemetry-switch.patch.json` using the targeted
   strategic patch in `test.md`. An image update alone does not update Kubernetes
   startup arguments. This patch makes native vLLM tracing conditional while
   preserving the live container image/environment.
7. Waiting for every relevant rollout and verifying actual images/settings.
   Stop for diagnosis if a rollout fails; do not submit new tasks on that basis.

## What is implemented in source

- `scripts/run_telemetry_sample.py`: same small single-request CSV task. Seeds an
  otherwise implemented analyzer with one floor-division bug. Agent must inspect,
  edit, run tests, generate North-filtered `report.json`, and read it. Fresh
  workspace and seed-only Git baseline; independent host verification follows
  runtime Success and is outside captured task time.
- `code/agent.py`: actual structured tool calls only; no execution of printed
  code, JSON or XML-like tags. Bounded recovery can require `write_file` after
  unchanged-workspace completion or `execute_command` after an unverified edit.
  An unforced truncated coding response can be discarded and recovered once
  when an action remains required; forced/repeated truncation fails. The output
  cap remains 4096. A write attempt invalidates earlier command verification.
- `kernel/collector.py`: `KERNEL_REDIS_MAX_EVENTS=0` disables count trimming;
  `KERNEL_REDIS_TTL=0` removes expiry, including existing-key TTLs;
  `KERNEL_ARCHIVE_MAX_BYTES=0` disables node archive rotation. Research manifest
  and console defaults use these values. Redis and disk consumption grow.
- Kernel `process_exit_unmatched` records explain an in-flight syscall when its
  thread/process terminates. Correlation separates these dispositions from real
  syscall returns and unexplained gaps; it never invents a return value.
- `scripts/export_telemetry_sample.py`: exports all initially retained kernel
  records by default; optional `--max-kernel-records` cap. Report/semantic privacy
  bounds remain. Asynchronous exports are not a final capture barrier.
- `inference/agentic_vllm.py`: request-ID joined timing, TTFT, request latency,
  scheduler/model execution/forward time, output tokens, single-sequence decode
  throughput excluding the first token, and actual prefix-cache hit tokens.
- New exclusive KV tracker observes all sequence owners, including external
  requests. It reports GPU/CPU exclusive block counts/peaks, block-ms residency,
  scheduled exclusive swap-out/in counts, shared-block observations and explicit
  recompute-preemption relinquished-reference counts. Exclusive means one owning
  request; sharing within that request is deduplicated. Unknown ownership/errors
  make affected measurements unavailable. Abort/weakref cleanup bounds lifetime.
- KV values are **partial block-manager observations**, not exclusive tensor
  bytes, confirmed CUDA DMA or proof of physical cache eviction. Cache-only
  retained blocks have no request owner. Token-level recomputed/retained/evicted/
  restored values and cache affinity remain null. Publication still depends on
  the native tracing completion hook; enable both inference switches for evidence.
- `code/worker.py`: new mandatory `task_end_to_end_time_ms` includes queue wait
  and synchronous final ledger writes before terminal publication. It is present
  in the task API in both modes. Existing `task_completion_time_ms` stops earlier;
  these fields are not interchangeable for the overhead comparison.
- New global `AGENT_TELEMETRY_ENABLED=false` disables OTel initialization/export,
  program/semantic capture, default worker kernel registration and inference joins.
  Minimal task lifecycle/ownership/timing remains. Collector and inference flags
  also change for the off batch, as specified in the guide.
- `scripts/run_telemetry_comparison.py`: separate `run --mode on`, `run --mode off`
  and `compare` commands. Saves attempts/IDs, performs independent verification,
  exports on-mode evidence, validates snapshot flags/readiness/startup, compares
  configuration and workload hashes, and refuses unsupported conclusions.

## Single-sample workflow

Only after rollout/settings confirmation, ask the user to run on **ryzen01**:

```sh
cd /home/agentic/agentic
SAMPLE_OUTPUT="$PWD/benchmark-results/react-sample-$(date +%Y%m%d-%H%M%S)"
python3 scripts/run_telemetry_sample.py \
  --workspace-root "$PWD/benchmark-workspaces" \
  --output "$SAMPLE_OUTPUT" --timeout 300 --max-steps 20
cat "$SAMPLE_OUTPUT/result.json"
```

One invocation submits one task. ReAct cycles are `step_index`, not additional
user messages. Suitability requires independent correctness verification,
at least two actual tool-bearing model steps, and a successful final no-tool
response. Never fabricate steps to satisfy this criterion.

Inspect `result.json`, `react_steps.json`, `verification.json`, and
`evidence/{report.json,correlation.json,kernel.jsonl}`. For a failure inspect the
actual task error and real tool arguments/results. `task.json` is deliberately
minimal; the worker error may require the host-labelled Redis HGET procedure.
Ctrl+C stops the client, not necessarily the backend task. Recover through saved
session/task/run IDs; never blindly resubmit an ambiguous submission/poll outcome.

## Five-run comparison workflow

Use **section 6 of `test.md`**, which has the exact flag, rollout, snapshot,
batch, comparison and restoration commands. Do not reuse the older
`benchmarks/run_telemetry_overhead.py` for this purpose: it compares kernel-only
capture with a different smoke workload.

1. Deploy the latest source and enable all task instrumentation. Save a fresh
   ready deployment/DaemonSet JSON snapshot on iiitd-desktop; copy it to the same
   `/tmp/telemetry-on-snapshot.json` path on ryzen01.
2. On ryzen01 create one unique `COMPARISON_ROOT` under `benchmark-results`.
   Run the on batch: exactly five measured attempts plus one excluded warmup.
   Preserve the path and all outputs.
3. After batch completion and empty queues, switch task telemetry off, wait for
   rollouts, save/copy `/tmp/telemetry-off-snapshot.json`, and run the off batch
   into `$COMPARISON_ROOT/off` with identical timeout/step limits and inputs.
4. Compare the on/off directories using the runner; inspect `comparison.json`,
   each batch's `summary.json`, `experiment.json`, `results.jsonl`, attempt JSON
   files and on-mode evidence directories. Each attempt is a distinct program.
5. Restore the usual on settings and verify rollouts, after a quiet window.

The primary metric is `task_end_to_end_time_ms` in BOTH modes. Missing new timing
fields mean the deployment is too old or incomplete; do not fall back silently.
Client submission-to-terminal polling time is secondary and has up to two seconds
of polling quantization. Fixture preparation, independent grading, client export
and rollout/model startup are excluded; final HSET transport and asynchronous
export flush are outside the primary boundary.

Report arithmetic means, medians and sample standard deviations, then:

```text
overhead_ms = mean_on_ms - mean_off_ms
overhead_percent = 100 * overhead_ms / mean_off_ms
```

The runner withholds an eligible overhead estimate unless both modes have exactly
five independently verified successes, on-mode capture is demonstrated, and
workload/settings/deployment configurations match. Failures are retained and
never silently replaced. Explain an ineligible batch instead of inventing or
cherry-picking a number. Five runs are exploratory; model/tool choices, caches
and unrelated load can affect runtime. A negative measured difference is possible.
Host/native Prometheus monitoring remains constant: this measures incremental
task instrumentation overhead, not removal of all cluster telemetry.

## Evidence already supplied by the user

| Run | Meaning |
| --- | --- |
| `e098d392-a343-44df-ba78-83a0f5e6bdae` | Verified successful small sample: 3 model steps, 6 successful tools, host checks passed; old program timer 14.183 s. |
| `fd3ddee0-d79b-4e77-850b-dd419fa86531` | Runtime Success but incorrect output: 4 steps, 4 tools, 2 tool failures, failed host verification, report absent; old program timer 22.399 s. |
| `bf84055b-61c6-4fd6-9fd9-5d44a5ec0f9c` | Earlier failed sample: model response hit the token limit after successful reads. |

Successful run output on ryzen01:
`/home/agentic/agentic/benchmark-results/react-sample-20261102-055019`.
Its trace ID is `9d5ce7fd50688db05837a3e1d6d953f3`. Kernel total 22,505,
retained/exported 10,000, trimmed 12,505; retained identities/tool joins matched,
but pairing gaps existed. This is not lossless evidence.

Incorrect run output:
`/home/agentic/agentic/benchmark-results/react-sample-20261102-055750`.
Phoenix span JSON was actually supplied for trace
`1d4c3652c9d107d23eb09232ff179159`, proving delivery for that trace. Its program,
semantic, model/tool and request identities agree with the run. Kernel total
21,283, retained 10,000, trimmed 11,283, 4,992 matched pairs and three explicit
unmatched-exit markers. Do not relabel its task as correct or use it as a
successful sample. Prometheus/Loki delivery remains unverified; prior Prometheus
out-of-order errors were not shown resolved.

These historical runs predate the new full-retention/KV/comparison deployment
validation. No five-run overhead dataset or measured overhead result exists yet.

## Persistent safeguards and communication

- Preserve all user edits, workspaces, queues, PVCs, failed runs and evidence.
  Never delete, reset, overwrite or commit them without explicit instruction.
- Do not rerun merely to obtain a favourable result. No automatic retries of
  uncertain submissions. Every new submitted message is a new task/run.
- Runtime Success, correct artifacts, telemetry capture and benchmark grading
  are separate assessments. Explain their differences using actual evidence.
- Do not claim complete kernel capture from zero loss counters or readiness.
  Check totals/retention, pairing gaps, collector health and changing snapshots.
- Shared inference syscalls stay pod/workload-attributed, never exclusively joined
  to one request. Model-forward time is batch-attributed; hidden reasoning is
  not observable. A group with zero swap events has not demonstrated restoration.
- For nontrivial code changes follow the user's AGENTS workflow: coder, then
  independent read-only reviewer; both inherit the no-execution restriction.
- Keep responses concise and host-labelled. Your first response should summarize
  the checkpoint briefly and give the smallest useful source/deployment inspection
  commands for the user, not start either sample prematurely.
