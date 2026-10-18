# Kernel syscall telemetry

This is the third layer, alongside program measurements and semantic execution
events. This repository is a codebase; deployment and privileged eBPF validation
must happen later on the research Kubernetes nodes. No cluster is required for
the unit tests.

## Collection and attribution

`collector.py` uses BCC to attach to `raw_syscalls:sys_enter` and
`raw_syscalls:sys_exit`. It has no syscall allowlist or intentional sampling.
See the upstream [BCC reference](https://github.com/iovisor/bcc/blob/master/docs/reference_guide.md)
and [syscount implementation](https://github.com/iovisor/bcc/blob/master/tools/syscount.py)
for the tracepoint and perf-buffer APIs.

The console can render `KERNEL_CAPTURE_ENABLED=false` for a complete collection
stop after rollout. In that mode the collector serves an explicit idle health
state, does not import/load BCC, and rejects capture registration. Separately,
`KERNEL_INFERENCE_SYSCALLS_ENABLED=false` disables inference discovery while
leaving registered worker scopes available. See [GUI.md](../GUI.md).

Worker programs synchronously register their cgroup through a node-local Unix
socket. The collector derives the host PID from `SO_PEERCRED` and resolves its
cgroup v2 inode; clients cannot select another cgroup. One program may own a
worker container at a time. Nested step/tool scopes inherit its run identities.
The native worker enables this through `KERNEL_TELEMETRY_ENABLED=true` and fails
the task if registration or a scope update cannot be acknowledged. Setting it
to `false` disables client instrumentation; it does not disable inference
workload selection in the collector.

All threads and descendants that stay in the selected cgroup are observed,
including background runtime activity. Scope means the container's active
execution phase, not proof that every background syscall was caused by that
phase. Processes moved to different cgroups, remote services, other unselected
pods, and GPU device execution are outside that scope.

The collector also lists pods in its namespace on its own node with the label
`telemetry.agentic.io/syscalls=enabled`, then scans host `/proc/*/cgroup` for
matching pod UIDs. The supplied vLLM pod template has this label. These events
carry pod/container metadata and `scope_kind=inference`, and are stored in the
node archive. They have no exclusive program/request attribution: shared vLLM
processes and batches may serve concurrent requests. Discovery is polled every
five seconds by default, so startup and short-lived cgroups can be missed.

Records contain a collector instance ID, node, cgroup ID, host PID/TID, UID,
command name, syscall number, six numeric raw argument slots, timestamps, return
value and duration when available, plus supplied scope metadata. Pair entry and
exit by collector ID, PID, TID, and `entry_monotonic_ns`; perf buffers on different
CPUs need not deliver global timestamp order. Exit records retain the scope
observed at entry. `unmatched_exit` marks a syscall whose entry was not observed;
its return is available but its arguments and duration are unknown. Nonreturning
calls, crashes, probe restarts, and dropped events can leave an entry without an
exit. Numeric pointers are addresses; paths, buffers, and structures are not
dereferenced. Syscall numbers need the node architecture/ABI for decoding;
compatibility ABIs are not normalized by this collector.

## Storage and completeness

The callback queues records without Redis or file I/O. A background writer
writes rotating node-local JSONL archives and sends correlated records to a
separate bounded Redis list. It does not put every syscall into the semantic
ledger or export every syscall as an OTel span.

`GET /telemetry/runs/{run_id}/kernel?offset=0&limit=500` checks the existing run
owner, returns `records`, retention counts, `truncated`, capture status, and
node health when known. Offsets refer to the currently retained list; active
capture and trimming can move that list between requests. Query terminal runs
for analysis and retain the status snapshot. Benchmark exports write
`kernel.jsonl` and `kernel-status.json`; asynchronous export means these are
snapshots, not a completion barrier. Shared inference records remain in node
archives rather than an arbitrary run's API response.

Node health is published every five seconds with a 20-second TTL. Loss counters
are cumulative **node/collector** observations, not exact per-program losses:
perf-buffer losses, in-flight map overflow, userspace queue drops, identity
misses, discovery errors, and export errors. Absence of records or a heartbeat
is not proof of zero syscalls or zero loss. The writer makes no automatic Redis
replay from archives after an outage. A disk-write failure can prevent both
archive and Redis export for that record. Abrupt shutdown can lose queued data.
Node archives survive pod replacement, but not loss of the node's disk.

| Collector setting | Default |
| --- | --- |
| `KERNEL_CAPTURE_ENABLED` | `true` |
| `KERNEL_INFERENCE_SYSCALLS_ENABLED` | `true` |
| `KERNEL_SOCKET` | `/run/agentic-kernel/collector.sock` |
| `KERNEL_ARCHIVE` | `/var/log/agentic-kernel` |
| `KERNEL_ARCHIVE_MAX_BYTES` | 67,108,864 bytes per archive file |
| `KERNEL_ARCHIVE_FILES` | 5 rotated files, plus the active file |
| `KERNEL_REDIS_TTL` | 2,592,000 seconds |
| Redis records per run | Newest 10,000 |
| Userspace queue | 16,384 records |
| In-flight syscall map | 65,536 entries |
| `KERNEL_DISCOVERY_INTERVAL_SECONDS` | 5 |
| `KERNEL_IDENTITY_TTL_SECONDS` | 3,600 for inactive identities |
| `KERNEL_IDENTITY_LIMIT` | 65,536, with live/in-flight scopes retained |

Archives can exceed their nominal file threshold by one record. Storage and
buffers are finite; measure overhead and losses at your actual syscall rate.
The socket, raw archives, and Redis are intended for trusted research workloads.
The syscall layer's numeric argument capture is independent of the semantic
content-redaction setting.

## Ordinary programs

The client has no LangGraph or agent dependency. Include `code/kernel_telemetry.py`
in the instrumented application, mount the collector's socket directory, and
create a run through the existing authenticated `POST /telemetry/runs` API.
Supply that returned run ID:

```python
from kernel_telemetry import KernelTelemetry

capture = KernelTelemetry(required=True)
with capture.scope(run_id=run_id, program_id=program_id):
    with capture.scope(step_index=1, tool_name="example", tool_call_id="call-1"):
        execute_work()
```

Use one sequential scope stack per container. Independently concurrent programs
need separate containers/cgroups. Registration is a local trusted-runtime
protocol; it does not create gateway runs or authenticate users on their behalf.

## Future Kubernetes deployment and smoke checks

1. Check Linux cgroup v2, BPF/tracepoint support, mounted tracefs, and matching
   BCC-compatible kernel headers on both nodes. The collector needs privileged
   execution, host PID visibility, the host cgroup namespace, and read-only
   host cgroup, module, source, debugfs, and tracefs mounts. The manifest supplies
   the mounts and privileges; validate namespace behavior on the actual runtime.
2. Build from the repository root:
   `docker build -t agentic-kernel:0.1 -f kernel/Dockerfile .`.
   Import/push the image for both nodes. Render all manifests using
   `scripts/render_manifests.py`, then follow the order in [SETUP.md](../SETUP.md).
3. Check `kubectl -n agentic-ai rollout status daemonset/kernel-telemetry` and
   collector logs for compile, attach, discovery, and archive errors. Readiness
   checks validate the configured idle state or attachment/socket availability
   and successful enabled pod discovery;
   it is not proof of lossless export or complete inference discovery.
4. Submit a program that reads/writes a file, creates a subprocess, and opens a
   connection. Inspect its authenticated kernel endpoint after export settles.
   Verify run/program/step/tool IDs, raw syscall IDs, paired durations, child
   processes, and separate records from simultaneous worker replicas.
5. Run consecutive programs in one worker and exercise failure/timeout paths.
   Ensure scope restoration and ownership prevent cross-run relabelling. Check
   inference node archives for pod metadata and absent exclusive run IDs.
6. Stop a collector, interrupt Redis, exceed retention, and generate a burst of
   syscalls. Verify enabled-worker failures, stale/missing heartbeat reporting,
   loss counters, bounded archives, and `truncated`. Compare overhead with capture
   disabled before using kernel measurements in research results.

Local validation runs Python tests, manifest rendering/parsing, compilation, and
shell syntax checks. It cannot validate BPF verifier acceptance, actual syscall
coverage, Kubernetes rollout, hardware performance, or losslessness.
