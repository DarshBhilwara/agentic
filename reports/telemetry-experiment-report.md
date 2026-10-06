# Agent telemetry experiment report

Date: 6 October 2026

This experiment validated program, inference-step, tool, and engine telemetry across a two-node agent deployment. The instrumentation is complete and internally consistent for the tested paths. The principal runtime cost is model inference—especially decoding—while task effectiveness is limited by repeated unproductive steps and tool failures. Patch generation was observed, but correctness was not graded.

## Scope

- Verified gateway authentication and a two-step file write/read smoke test.
- Verified per-request telemetry joins between the agent worker and vLLM inference node.
- Ran 10 pinned SWE-bench Pro cases at dataset revision `7ab5114912baf22bb098818e604c02fe7ad2c11f`, with two concurrent tasks, a 20-step limit, and a 240-second task timeout.
- Used the shared worker environment for trace and patch generation. The official hidden grading harness and per-instance benchmark containers were not used.

## Ten-case results

| Metric | Result |
|---|---:|
| Runner records | 10 of 10 |
| Runtime-successful tasks | 4 of 10 |
| Tasks producing a patch | 4 of 10 |
| Officially evaluated or resolved | 0 |
| Inference steps | 92 |
| Engine-measured steps | 92 (100%) |
| Tool calls | 91 |
| Tool failures | 23 (25.3%) |
| Aggregate task runtime | 789.6 seconds |
| Mean / median task runtime | 79.0 / 66.9 seconds |
| Minimum / maximum task runtime | 23.9 / 182.6 seconds |
| Prompt / completion tokens | 510,656 / 32,010 |
| Maximum observed context | 18,473 tokens |
| Patches generated | 4, totaling 21,518 bytes |

The six failed tasks comprised four unchanged-workspace rejections, one response truncated at the model token limit, and one task reaching the 20-step limit. A generated patch is only an effectiveness signal; it is not evidence that the issue was resolved.

## Telemetry findings

Engine measurement coverage was complete: all 92 inference steps had corresponding vLLM measurements. Mean task wait was 2.65 ms, and aggregate inference queue time was 399 ms, about 4.3 ms per step. With two clients and two workers, the system showed no meaningful queue pressure.

Model calls accounted for 738.3 seconds, or 93.5% of aggregate task runtime. Tool execution accounted for 49.3 seconds, or 6.2%, and was concentrated in a small number of slow or failing commands. Decode time was 695.6 seconds versus 36.2 seconds of prefill time. The main latency opportunity is therefore fewer model steps and shorter generations, not faster ordinary file tools.

The runs accumulated 510,656 prompt tokens across 92 steps. Long trajectories amplified context and cost without reliably producing changes: one case consumed 20 steps and 72,988 prompt tokens before hitting the step limit. `kv_recomputed_tokens` remained zero, but the current metric measures recomputation rather than positive cache reuse, so it cannot establish cache effectiveness by itself.

## Decisions

1. Keep execution status, patch generation, and official evaluation as separate outcome dimensions.
2. Retain the unchanged-workspace completion guard; it prevents fabricated completion from being recorded as success.
3. Use a 20-step and four-minute bound for exploratory runs with the 14B model.
4. Prioritize reducing ineffective model loops and output length. Tool latency is not the primary bottleneck.
5. Add structured tool failure reason codes so command, path, dependency, and timeout failures can be aggregated without parsing event text.
6. Add positive KV-cache reuse measurements before making cache-sizing decisions.
7. Run a separate workload with client concurrency above two before changing worker capacity; this experiment only tested concurrency equal to worker count.

## Implementation changes made during validation

- Added Python 3.8 compatibility for postponed annotations and request-ID prefix parsing.
- Added Node.js and npm to the worker image.
- Corrected the SWE-bench dataset configuration guidance.
- Added explicit `patch_generated` and `patch_bytes` fields.
- Added and repaired the unchanged-workspace completion guard.
- Restricted project pytest discovery so benchmark checkouts are not collected.

The deployment is suitable for telemetry experiments and patch-generation studies. It is not yet a leaderboard-equivalent SWE-bench Pro evaluator because cases do not run in their dataset-specified environments and generated patches are not graded.
