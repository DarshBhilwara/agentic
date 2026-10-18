# Three-layer telemetry: context and implementation plan

Updated: 2026-10-18. Kernel deployment validation remains pending. The user has
now requested the GUI as codebase work, so the console is implemented
without waiting for or performing cluster deployment.

This checkout is code only: Kubernetes and the complete platform are not running
here. Implementation and local verification must not deploy or modify a cluster.
The hardware validation checklist below is for a later deployment environment.

## Goal and scope

Observe an execution through three connected layers:

1. **Program:** submission, queue wait, execution outcome, inference steps,
   tools, tokens, inference-engine measurements, and artifacts.
2. **Semantic:** observable intent, selected and executed actions, graph
   transitions, procedures, constraints, and delegation. This does not capture
   hidden model reasoning.
3. **Kernel:** syscall entry and exit events from eBPF on Kubernetes nodes,
   joined to available execution identities and workload metadata.

Keep the combined cluster: agent workers/gateway/Redis on the agent node and
vLLM on the inference node, with shared observability services. Remove the
standalone `agents-cluster/` deployment and its bootstrap/documentation paths.
This interprets “remove agent-only” as removing that deployment mode; telemetry
itself should also accept instrumentation from ordinary programs and other
runtimes. Existing external run/event ingestion remains available.

## Existing context

`code/worker.py` executes one program at a time per worker process; replicas
provide concurrency. `code/program_telemetry.py` records program and step
measurements. `code/agent.py` emits model, tool, graph, and semantic events.
`code/telemetry_ledger.py` stores run metadata, summaries, steps, and a bounded
event tail in Redis. `code/gateway.py` exposes authenticated ingestion/export.
The pinned vLLM adapter joins engine measurements by exact request ID.

OpenTelemetry exports traces to Phoenix, metrics to Prometheus, and logs to
Loki. The existing node collector exports host/process statistics; these are
resource metrics and do not constitute syscall tracing. Parent-folder notes
describe an earlier snapshot; this repository and its tests are the source of
truth for the implementation.

## Kernel design

Use a dedicated privileged BCC/eBPF DaemonSet, separate from the OTel collector.
Attach to `raw_syscalls:sys_enter` and `raw_syscalls:sys_exit` with no syscall
allowlist or intentional sampling. Require Linux cgroup v2 and matching node
kernel headers. Filter by selected workload cgroups before exporting events.
Capture host PID/TID, UID, command name, syscall number, raw scalar argument
slots, return value where available, duration, timestamps, node, cgroup, and
available pod/container identities. Numeric pointer arguments are addresses;
they are not dereferenced filenames, buffers, or decoded structures.

Use synchronous local registration at execution boundaries so capture starts
before the measured body. Carry program/run identities, step index, and tool
identity when supplied by the runtime. Snapshot correlation at syscall entry
so a later scope change does not relabel its exit. Native workers have one
active program per container; cgroup activity includes runtime/background
threads and child processes in that cgroup. Arbitrary programs can use the
same client protocol. They need a registered run to appear in the run API.

Collect inference workload syscalls as workload-level observations. A shared
vLLM process or GPU batch can serve several requests at once: temporal overlap
does not establish an exclusive syscall-to-request relationship. Preserve
unknown attribution rather than assigning shared events to an arbitrary run.
GPU device execution itself is outside syscall tracing.

Store high-volume syscall records separately from the semantic event ledger.
Provide bounded node archives and a bounded, paginated per-run query store.
Expose loss, queue overflow, retention/truncation, collector failures, and
unmatched boundaries explicitly. No finite-buffer implementation can promise
lossless capture at arbitrary syscall rates. Node archive persistence is local
disk persistence, not replicated storage or guaranteed recovery after node loss.

## Work before the GUI

- [x] Inspect existing program/semantic telemetry and deployment paths.
- [x] Record the overall context, scope, and phased plan here.
- [x] Implement eBPF collector, correlation protocol, bounded export, and API.
- [x] Wire worker/step/tool scopes and Kubernetes collector permissions/mounts.
- [x] Remove standalone deployment files and documentation paths.
- [x] Run focused tests, manifest rendering, and Python/shell checks.
- [x] Record changes and actual validation in `~/research/changelog.md`.
- [ ] Validate privileged loading, real syscall capture, loss accounting,
      program boundaries, inference metadata, and overhead on cluster hardware.

Live deployment and eBPF validation require access to suitable Kubernetes nodes;
unit tests and static checks cannot establish that probes load on those kernels.
Do not call the kernel layer operationally validated until the cluster smoke
checks in `kernel/README.md` have actually run.

Local validation on 2026-10-18: 44 tests passed; one pre-existing test failed
because `test_kv_matching_does_not_count_new_or_changed_context` references
`matching_prefix_tokens`, which is absent from the unchanged vLLM adapter.
The 12 kernel tests passed. Python compilation, manifest rendering/parsing
(36 resources), shell syntax, and whitespace checks passed. Independent review
findings about disconnected clients and ambiguous acknowledgements were fixed
and rechecked. No live cluster operations were performed.

## GUI phase — local console

The user has brought the GUI phase forward. Implement the following interface
locally, preserving explicit unavailable states until a platform is connected:

- Cluster/node prerequisites, collector deployment state, and health.
- Workload selection and program/semantic/kernel collection toggles.
- Capture policy, retention, archive limits, export destinations, and credentials.
- Run → step → tool drilldown with correlated syscall views and explicit loss.
- Configuration validation and previewable deployment files. Live apply,
  rollout management, and rollback remain outside this local-only console.

Keep deployment credentials in Kubernetes Secrets and browser API tokens in
memory. Use authenticated server endpoints for previews/reports and preserve
run ownership. Use buildless HTML/CSS/JavaScript served by the existing gateway,
with optional non-secret local drafts and an explicitly labelled sample mode.
See [GUI.md](GUI.md) for the interface, API, launch steps, and validation limits.

Implemented: buildless gateway dashboard, configuration drafts and complete
manifest previews, read-only Kubernetes/collector discovery, owned-run detail,
and bounded JSON/Markdown/CSV reports. Deployment changes remain manual.

GUI validation: the full local suite reports 58 passed and the same pre-existing
KV-helper failure. Nineteen Firefox smoke checks pass using fake Redis and an
isolated profile, including YAML download parsing, run/syscall inspection,
report privacy after disconnect, and all five views at mobile width. Python
compilation, JavaScript syntax, shell syntax, whitespace checks, and rendering/
parsing of six manifests containing 41 Kubernetes objects pass.
