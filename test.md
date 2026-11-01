# One task with multiple ReAct steps

Run this sample yourself on **ryzen01 / agent-01** from
`/home/agentic/agentic`. This guide covers only the fresh sample and its
telemetry. No tests, sample tasks or deployment changes have been executed for
you while preparing this procedure.

## Correct execution model

Submit **one user request once**. The worker keeps executing its LangGraph loop
until the task finishes, fails or reaches its limit:

```text
One session → one submitted task → one program/run ID

step 1: model request → inspect files → tool observations
step 2: model request → repair code / execute commands → observations
step 3+: model request → further tools or final answer
```

A ReAct step here means one model request and its associated tool calls and
observations. Multiple tools can belong to one step. The terminal model response
also counts as an inference step, even without tools. The actual number of steps
comes from execution; the sample does not manufacture a fixed count.

Do not send a separate user message for each work stage. That would create
separate programs. Observable model/tool choices are recorded; hidden model
reasoning is not inspected.

The existing API's `turn_id` identifies a submitted user message. It is not the
ReAct cycle counter. Use `step_index` and `request_id` to inspect successive
model/action/observation cycles within the run.

## 1. Leave the old chat and synchronize the client files

If the previous interactive chat is still open, type:

```text
/exit
```

Keep the previous workspaces and run records. They are separate evidence and
will not be reused or deleted.

Push the new client files from the development checkout and pull them on the
agent host through your usual source synchronization. The sample launcher and
exporter need `requests`; fixture preparation also needs `git`.

```sh
cd /home/agentic/agentic
ls scripts/run_telemetry_sample.py scripts/export_telemetry_sample.py
```

Use your existing agentctl credential and working gateway configuration.
Credentials are read from `~/.agentic_token` without printing them. If
`AGENTCTL_GATEWAY_URL` is unset, the default is `http://localhost:30080`.
Do not replace a working gateway URL or print your token.

These are client-side changes using existing gateway features. No Kubernetes
restart, pod rollout or application image rebuild is required for this sample.

## 2. Start the sample with one command

In the agent-host shell:

```sh
SAMPLE_OUTPUT="$PWD/benchmark-results/react-sample-$(date +%Y%m%d-%H%M%S)"

python3 scripts/run_telemetry_sample.py \
  --workspace-root "$PWD/benchmark-workspaces" \
  --output "$SAMPLE_OUTPUT" \
  --timeout 300 \
  --max-steps 20
```

Use the Python environment that already has `requests`; substitute
`.venv/bin/python` if needed.

The launcher:

1. Creates a fresh child workspace containing a small CSV dataset, an
   intentionally defective analyzer and standard-library tests.
2. Initializes a clean Git baseline **only inside that new sample workspace**.
   This is fixture preparation, outside the captured program; the platform
   repository and existing user workspaces are not committed or modified.
3. Creates one fresh session and submits one coding task. The agent must inspect,
   repair and verify the existing program using real workspace tools.
4. Saves the accepted session/task/program/run IDs before waiting for completion.
5. Leaves subsequent model/tool cycles to the existing worker loop. It does not
   submit a new user request for each cycle or silently resubmit on failure.
6. Exports the terminal run's statistics and retained syscall records, then
   independently verifies the generated program on the host.

The task uses the gateway's existing coding-only mode: only workspace tools are
available, and an answer without detected workspace changes is rejected within
the same run. This guard does not prove correctness; independent verification
is still required.

If the launcher reports an unknown outcome, timeout or failure, stop and keep
its output. Do not launch a replacement until the original task's state is
understood. A client timeout does not stop a backend task.
If submission acknowledgement is lost, `preparation.json` retains the session
ID for locating the accepted task without submitting another request.

## 3. Understand the two separate outcomes

| Result | Meaning |
|---|---|
| Runtime task status | What the worker recorded: Success, Failed or Timeout. |
| Sample assessment | Whether this particular execution provides the requested example: actual code work, independent functional verification and multiple tool-bearing ReAct steps. |

A runtime Success with an incorrect program or insufficient action steps is
not a validated sample. Its actual status and evidence are preserved; it is not
relabelled as runtime failure or padded with extra user messages.

The sample dataset contains six rows with total 350. Filtering North should
produce count 2, total 150 and average 75. Additional cases exercise decimals,
refunds, empty input and invalid amounts. These are validation expectations,
not claims about what an unexecuted run produced.

## 4. Inspect the saved evidence

| File under `$SAMPLE_OUTPUT` | What to inspect |
|---|---|
| `submission.json` | Accepted session, task/program and run IDs, saved before polling. |
| `result.json` | Actual runtime outcome and the separate verification/sample assessment. |
| `react_steps.json` | Actual model-step sequence, request IDs, tool calls and associated correlation information. |
| `evidence/report.json` | Program summary, model/tool timings, tokens, semantic snapshot and graph. |
| `evidence/correlation.json` | Run/step/tool identity inventory, scope counts, representative syscall pairs, heartbeat, counters, retention and warnings. |
| `evidence/kernel.jsonl` | The full retained run syscall snapshot, up to the per-run 10,000-record cap. |

Read the launcher result and actual step sequence:

```sh
python3 -m json.tool "$SAMPLE_OUTPUT/submission.json"
python3 -m json.tool "$SAMPLE_OUTPUT/result.json"
python3 -m json.tool "$SAMPLE_OUTPUT/react_steps.json"
```

Then inspect the numeric report and syscall correlation:

```sh
python3 -m json.tool "$SAMPLE_OUTPUT/evidence/report.json"
python3 -m json.tool "$SAMPLE_OUTPUT/evidence/correlation.json"
```

The helper's host-side verification is outside the captured agent task. Only
verification commands actually invoked through agent tools belong to its tool
spans and worker syscall scopes.

The export omits raw prompt/response/tool/workspace contents from reports while
retaining safe identity fields. Numeric syscall arguments and process identifiers
remain. Do not share the API token.

## 5. What establishes correlation

- One run/program identity spans all observed ReAct steps.
- Each model step has a distinct request ID; that exact ID joins supported vLLM
  engine measurements into the step.
- Each real tool call has a call ID, name, index and status; compare these with
  semantic events and worker kernel scopes.
- A syscall entry/exit pair matches collector ID, PID, TID, entry monotonic
  timestamp and syscall ID. Retain unmatched boundaries and scope warnings.
- Inspect capture totals, retained/exported counts, trimming, changing totals,
  heartbeat freshness and loss/export counters.

Longer programs may exceed retention. Exports are asynchronous snapshots even
after terminal task status. Zero reported counters do not establish lossless
capture. Worker scope describes the active container phase, not proven causality
for every background syscall. Shared vLLM syscalls retain workload identity only.
Engine timings overlap model wall time; unsupported KV measurements remain null.

## Next checkpoint

Paste the launcher output, followed by `result.json` and `react_steps.json`.
We will inspect the actual cycles under that one run ID, then locate that run's
trace in Phoenix. No additional user prompts are needed to advance the agent's
internal loop.
