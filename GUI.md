# Telemetry console

The console is a local, buildless interface served by the existing FastAPI
gateway at `/dashboard/`. It configures the program, semantic, and kernel
layers, inspects available observations, and exports reports. It does not
apply manifests or change Kubernetes resources.

## Run from this checkout

Install `requirements-dev.txt` in a virtual environment, then run:

```sh
PYTHONPATH=code python -m uvicorn gateway:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/dashboard/`. No frontend package installation or build
is required. Use an API token configured through the existing `AGENT_API_KEYS`
or `AGENT_API_KEY` settings. Tokens stay in browser memory; disconnecting clears
the connection and observed run data. The static interface is public; the
console data, preview, and report APIs require authentication.

This checkout has no running platform. The initial screen therefore provides
configuration drafts and explicit unavailable states. **Explore demo** loads
labelled sample data in the browser; it never represents a connected cluster.
Only non-secret configuration drafts are optionally saved in local storage.

## Views

| View | Purpose |
| --- | --- |
| Overview | Three collection layers, draft switches, observed devices/runs, and connection state |
| Collection | Layer/signal controls, capture policy, retention, archive limits, discovery interval, worker count, paths, and inference settings |
| Devices | Kubernetes node readiness and scheduling capacity, pod/container states, and available collector heartbeats |
| Runs | Search owned runs, inspect summaries, steps/tools, semantic events, and PID/TID-correlated syscalls |
| Reports | Select owned runs and download bounded JSON, Markdown, or CSV snapshots |

A draft switch does not indicate observed runtime state. **Validate & preview**
asks the server to validate the configuration and render reviewable deployment
files. Downloading files also does not apply them. A deployment operator must
review and roll out those manifests in the actual Kubernetes environment.
Existing task ownership/lifecycle bookkeeping remains necessary even when
optional program or semantic capture is disabled.

Program capture controls persisted summaries/steps and program metrics; internal
measurements needed for execution can still be computed. Semantic capture
controls intent, graph, phase, and procedure observations. Turning semantic
capture off alone retains program model/tool execution measurements. Turning
both off suppresses detailed model/tool telemetry and retains minimal lifecycle
bookkeeping. Kernel capture remains an independent switch; when globally off,
the collector reports an idle state and does not load BPF probes.

Traces, metrics, and logs control their export paths. Disabling inference tracing
can also make adapter measurements unavailable because the pinned vLLM adapter
uses a trace-completion hook. Missing engine fields remain null. Toggles affect
future capture after rollout; they do not erase previously retained records.

## Device discovery

Live node/pod discovery is explicitly enabled with
`CONSOLE_K8S_DISCOVERY=true` on the gateway. The Collection view’s
**Read-only device discovery** switch renders that setting into the gateway
manifest; it takes effect only after a rollout. It uses the in-cluster API and its
service-account CA/token, with bounded requests. `manifests/console-access.yaml`
provides read-only node access and namespace-scoped pod access. The gateway
must use the `agentic-console` service account for those permissions.

Discovery does not scan a LAN or connect to user-entered device addresses.
Node CPU/memory/GPU capacity and allocatable quantities are scheduler resource
information. They do not establish physical GPU utilization, temperature,
per-device health, or cgroup-v2/BPF compatibility. Those measurements require
additional exporters or explicit node validation. No observations means
unknown/unavailable, rather than healthy or zero utilization.

## API and reports

All `/console` routes use `X-API-Key` authentication:

- `GET /console/config`: repository defaults, not deployed configuration.
- `POST /console/config/preview`: validate a draft and return rendered files.
- `GET /console/devices`: available Kubernetes and collector observations.
- `GET /console/runs`: the authenticated user's retained runs.
- `GET /console/runs/{run_id}`: bounded run detail and capture status.
- `POST /console/reports`: selected owned runs in JSON, Markdown, or CSV format.

Reports are bounded snapshots, with explicit capture/retention gaps. Missing
measurements remain unknown. Raw prompt, response, tool arguments/results, and
workspace content are excluded from report exports. Syscall records still
contain numeric argument slots and process/workload identifiers; treat them as
research data. Shared inference syscalls remain workload-attributed, not assigned
to an arbitrary request. Kernel export is asynchronous; a report is not proof
that capture/export has finished.

## Validation boundary

Unit tests cover authentication, ownership, configuration artifacts, runtime
controls, unavailable dependencies, and report bounds. Browser verification
covers offline/demo flows and dashboard integration where available. Neither
establishes that Kubernetes manifests roll out or eBPF probes load on hardware.
See [kernel smoke checks](kernel/README.md) for those later checks.

Local verification on 2026-10-18: 58 tests passed, with one pre-existing KV
helper test failure. Nineteen isolated Firefox smoke checks passed, including
authentication, actual manifest/report downloads, tool/PID syscall inspection,
disconnect privacy, and mobile layout for all five views. No cluster was queried.

The optional browser smoke check uses installed Firefox, an isolated temporary
profile, loopback-only test servers, and fake Redis. It requires the development
dependencies and `websockets` (already used by the current LangGraph dependency):

```sh
python scripts/check_console_browser.py
```

It stops its temporary servers/browser and writes screenshots under
`/tmp/telemetry-console-preview`. Ports 18765 and 19222 must be free.
