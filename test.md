# One sample run: telemetry, statistics and correlation

Run these commands yourself on **ryzen01 / agent-01**, from
`/home/agentic/agentic`. This guide covers only one fresh sample run and its
collected evidence. The commands have not been executed for you.

## 1. Use the existing deployment and login

```sh
cd /home/agentic/agentic
ls scripts/export_telemetry_sample.py
```

The exporter must be present in this host's checkout before the export step.
If it is missing, transfer that helper to the same path first. It needs
`requests`, like agentctl; no application rebuild is needed for the helper.

Use the gateway URL and agentctl login that already work on this host. If the
gateway URL is unset, both clients default to `http://localhost:30080`.
If another address is required, set it once:

```sh
export AGENTCTL_GATEWAY_URL=http://<agent-host>:30080
```

Do not change a working URL to the placeholder above. Credentials are read from
`~/.agentic_token`; do not print or share that file. Keep the running telemetry
configuration unchanged.

## 2. Submit a fresh task and save the output

In the same shell:

```sh
mkdir -p benchmark-workspaces
SAMPLE_DIR="$(mktemp -d "$PWD/benchmark-workspaces/telemetry-sample-XXXXXX")"

./agentctl submit --watch -C "$SAMPLE_DIR" \
  'Use write_file to create smoke.txt containing exactly "telemetry correlation sample" with no newline. Then use execute_command to run cat smoke.txt. Finally give a short confirmation. Do not use other tools.' \
  | tee "$SAMPLE_DIR/submission.txt"
```

Keep the printed **task ID** and **run ID**. The task ID is the program ID.
Do not assume a particular step count or timing; inspect what actually happened.

If the command reports failure, timeout, or a connection error, stop and inspect
that output before submitting another task. A client connection error does not
prove that the backend stopped executing.

## 3. Independently verify the file effect

```sh
cat "$SAMPLE_DIR/smoke.txt"
printf '\n'
```

Expected text: `telemetry correlation sample`.

The agent's confirmation alone does not establish the file effect. Preserve
`submission.txt` and `smoke.txt` in the sample workspace.

## 4. Export the completed run

Replace the value below with the **new run ID** printed by agentctl:

```sh
SAMPLE_RUN_ID='paste-the-new-run-id-here'
SAMPLE_EXPORT="$PWD/benchmark-results/telemetry-sample-$SAMPLE_RUN_ID"

python3 scripts/export_telemetry_sample.py \
  --run-id "$SAMPLE_RUN_ID" \
  --output "$SAMPLE_EXPORT"
```

Use the Python environment that already has `requests`; substitute
`.venv/bin/python` if needed. The exporter accepts terminal runs only and
requires a fresh output directory. If an export fails, preserve its
`failure.json`; use a new output path for a retry.

| File | What it contains |
|---|---|
| `report.json` | Program summary, model-step/tool measurements, bounded semantic events, observed graph, and a bounded kernel snapshot. |
| `kernel.jsonl` | Paginated retained syscall records, up to the configured 10,000-record run cap. |
| `correlation.json` | Run/program/request/step/tool identities, bounded semantic identity inventory, scope counts, representative entry/exit pairs, capture status, counters, retention and join warnings. |

The exporter reads additional identity fields from the run API without saving
its raw prompt/response/tool payloads. The official report excludes those
contents too. Numeric syscall arguments and process identifiers remain.

## 5. Show the collected statistics and correlation

Run this in the same shell after a successful export:

```sh
python3 - "$SAMPLE_EXPORT" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
report = json.loads((root / "report.json").read_text())["runs"][0]
export = json.loads((root / "correlation.json").read_text())
correlation = export["correlation"]
inventory = correlation.get("identity_inventory", {})
events = inventory.get("semantic_events", [])
shown = {
    "run_and_program": correlation.get("run"),
    "program_statistics": report.get("summary"),
    "model_steps_and_tool_statistics": report.get("steps"),
    "request_step_and_tool_ids": inventory.get("steps"),
    "semantic_event_type_counts_in_export": dict(Counter(e.get("type") for e in events)),
    "semantic_identity_page_truncated": inventory.get("semantic_events_truncated"),
    "procedural_graph": correlation.get("procedural_graph"),
    "kernel_scope_counts": correlation.get("scope_counts"),
    "kernel_record_identity_counts": correlation.get("record_identity_counts"),
    "matched_syscall_entry_exit_pairs": correlation.get("matched_pair_count"),
    "representative_pairs_by_scope": correlation.get("representative_pairs"),
    "step_tool_call_ids": correlation.get("step_tool_call_ids"),
    "kernel_tool_call_ids": correlation.get("kernel_tool_call_ids"),
    "unjoined_kernel_tool_call_ids": correlation.get("unjoined_kernel_tool_call_ids"),
    "tool_scope_mismatches": correlation.get("tool_scope_mismatches"),
    "kernel_capture_retention_and_health": export.get("kernel"),
    "correlation_warnings": correlation.get("warnings"),
}
text = json.dumps(shown, indent=2)
(root / "inspection.json").write_text(text + "\n")
print(text)
PY
```

This also saves the displayed values as `inspection.json`. To inspect every
exported semantic identity record, open `correlation.json`; to inspect raw
retained syscall records, open `kernel.jsonl`.

## What to check together

| Layer / join | Evidence to inspect |
|---|---|
| Program | Outcome, submission-to-completion time, queue wait, attempted/completed steps, token usage, tool count/failures and tool durations. |
| Inference | Per-step model wall time, queue/prefill/decode/forward measurements and their availability. Use the step's exact request ID for engine correlation. |
| Semantic | Actual model/tool event sequence, file artifact events and observed graph. The identity inventory is capped at 1,000 events; counts describe this exported page. |
| Kernel | Program, step and both tool scopes; syscall IDs, PID/TID, numeric arguments, return values and paired durations. |
| Execution identity | The same run/program IDs in the inventory and kernel records; inspect missing/mismatched identity counts. |
| Step identity | Step indices connect model steps, semantic events and worker kernel scopes. |
| Tool identity | Match each tool call ID, name and step against the kernel scope. Inspect unjoined IDs and scope mismatches. |
| Entry/exit pairing | Collector ID + PID + TID + entry monotonic timestamp + syscall ID. A representative pair should include return value and duration. |
| Capture quality | Initial/final totals, retained/exported counts, trimmed records, heartbeat freshness, perf/map/queue/export/identity counters and all warnings. |

Kernel export is asynchronous: terminal task status does not guarantee that
every record has reached Redis. If totals change during export, keep that
snapshot and export again into a fresh directory after collection settles.

Missing measurements remain unknown/null. Unsupported request-scoped KV
lifecycle fields remain null. Engine phase times overlap model wall time;
forward time is batch-attributed. Shared vLLM syscalls are workload-attributed,
not assigned to this request.

Do not describe retained events as lossless capture: retention trimming,
unmatched boundaries and collection/export gaps must remain visible. A syscall
entry and its exit are two event records. Worker scope identifies the active
container phase, rather than proving every background syscall was caused by
the tool.

## Share the first result

First paste the sample submission output and host-side file output from steps
2–3. Then share the export result and `inspection.json` so the actual collected
values, joins and gaps can be interpreted together. Do not share the token.
