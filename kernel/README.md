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
its return is available but its arguments and duration are unknown. If a thread
exits with an in-flight syscall, `process_exit_unmatched` records the observed
entry and its process-exit disposition without inventing a return value.
Nonreturning calls, crashes, probe restarts, and dropped events can still leave
an entry without a matching exit or process-exit marker. Numeric pointers are
addresses; paths, buffers, and structures are not
dereferenced. Syscall numbers need the node architecture/ABI for decoding;
compatibility ABIs are not normalized by this collector.

## Storage and completeness

The callback queues records without Redis or file I/O. A background writer
writes node-local JSONL archives, optionally rotates them by configured size,
and sends correlated records to per-run Redis lists. Count trimming is disabled
when `KERNEL_REDIS_MAX_EVENTS=0`. It does not put every syscall into the
semantic ledger or export every syscall as an OTel span.

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
| `KERNEL_ARCHIVE_MAX_BYTES` | 67,108,864 bytes per file; `0` disables rotation |
| `KERNEL_ARCHIVE_FILES` | 5 rotated files, plus the active file |
| `KERNEL_REDIS_TTL` | 0 (no expiry) |
| `KERNEL_REDIS_MAX_EVENTS` | `0`: retain all records until TTL; positive value keeps the newest N |
| Userspace queue | 16,384 records |
| In-flight syscall map | 65,536 entries |
| `KERNEL_DISCOVERY_INTERVAL_SECONDS` | 5 |
| `KERNEL_IDENTITY_TTL_SECONDS` | 3,600 for inactive identities |
| `KERNEL_IDENTITY_LIMIT` | 65,536, with live/in-flight scopes retained |

With the manifest defaults, Redis keeps all per-run records without expiry, and
node archives do not rotate. Redis memory and node disk usage therefore grow
without a bound until an operator intervenes; capacity planning and external
monitoring are required. Setting `KERNEL_REDIS_TTL` to a positive value expires
Redis records after that many seconds. Setting `KERNEL_ARCHIVE_MAX_BYTES` to a
positive value enables size/count rotation; archives can exceed the size limit
by one record. The userspace queue, BPF maps, perf buffers, outages, and node
disk failures can still cause missing records, so this configuration prevents
automatic retention trimming but does not promise lossless capture.
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

### Relocate one node's archive without changing the DaemonSet

The research collector can grow `/var/log/agentic-kernel` without rotation.
To move only the `iiitd-desktop` archive onto its Documents filesystem, keep
the DaemonSet hostPath unchanged and bind-mount the relocated directory at the
same host path. Do not change the shared DaemonSet path: it also runs on
`agent-01`. This procedure requires an administrator for the systemd mount.

1. Choose a quiet window. Confirm Redis `task_queue` and `task_processing` are
   empty, then set `KERNEL_CAPTURE_ENABLED=false` and wait for the
   `kernel-telemetry` DaemonSet rollout to finish. This stops collection and
   closes the archive writer on both nodes.
2. On `iiitd-desktop`, confirm
   `/home/iiitd/Documents/agentic-kernel` does not exist; stop if it exists.
   Record a file listing and checksums, then move `/var/log/agentic-kernel`
   there with metadata preserved. This is a cross-filesystem move: compare the
   destination listing and checksums before proceeding. Stop on any error and
   do not remove, truncate, or rotate archived evidence.
3. Install a systemd mount unit named
   `/etc/systemd/system/var-log-agentic\x2dkernel.mount` with:

   ```ini
   [Unit]
   Description=Agentic kernel archive on Documents storage
   RequiresMountsFor=/home/iiitd/Documents

   [Mount]
   What=/home/iiitd/Documents/agentic-kernel
   Where=/var/log/agentic-kernel
   Type=none
   Options=bind

   [Install]
   WantedBy=local-fs.target
   ```

   Reload systemd, enable and start this mount unit, then use `findmnt` to
   verify `/var/log/agentic-kernel` is backed by the Documents filesystem.
   Add a systemd drop-in for `k3s.service` containing
   `RequiresMountsFor=/var/log/agentic-kernel`, then reload systemd so future
   K3s starts wait for the archive mount. This changes a shared cluster service;
   coordinate it with the administrator. Reloading systemd does not require
   restarting K3s. Do not restart K3s as part of this migration.
4. Restore `KERNEL_CAPTURE_ENABLED=true` and wait for rollout. Verify collector
   readiness and that new archive records appear under the bound directory.

This moves only the inference-node archive. Redis run records and all
`agent-01` archives remain in place. Shared system journals are not part of
this migration.

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
6. Stop a collector, interrupt Redis, configure a finite retention limit, and
   generate a burst of syscalls. Verify enabled-worker failures, stale/missing
   heartbeat reporting, loss counters, configured archive rotation, and
   `truncated`. Compare overhead with capture disabled before using kernel
   measurements in research results.

Local validation runs Python tests, manifest rendering/parsing, compilation, and
shell syntax checks. It cannot validate BPF verifier acceptance, actual syscall
coverage, Kubernetes rollout, hardware performance, or losslessness.
