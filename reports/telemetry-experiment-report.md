# Agent telemetry experiment
- Verified gateway authentication and a two-step file write/read smoke test.
- Verified per-request telemetry joins between the agent worker and vLLM inference node.
- Ran 10 pinned SWE-bench Pro cases at dataset revision `7ab5114912baf22bb098818e604c02fe7ad2c11f`, with two concurrent tasks, a 20-step limit, and a 240-second task timeout.
- The quality of responses was not checked.
- Patch is the git diff produced by agent while working on a task.


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


## Telemetry findings

Engine measurement coverage was complete: all 92 inference steps had corresponding vLLM measurements. Mean task wait was 2.65 ms, and aggregate inference queue time was 399 ms, about 4.3 ms per step. With two clients and two workers, the system showed no meaningful queue pressure.

Model calls accounted for 738.3 seconds, or 93.5% of aggregate task runtime. Tool execution accounted for 49.3 seconds, or 6.2%, and was concentrated in a small number of slow or failing commands. Decode time was 695.6 seconds versus 36.2 seconds of prefill time. The main latency opportunity is therefore fewer model steps and shorter generations, not faster ordinary file tools.

The runs accumulated 510,656 prompt tokens across 92 steps. Long trajectories amplified context and cost without reliably producing changes: one case consumed 20 steps and 72,988 prompt tokens before hitting the step limit. `kv_recomputed_tokens` remained zero, but the current metric measures recomputation rather than positive cache reuse, so it cannot establish cache effectiveness by itself.

