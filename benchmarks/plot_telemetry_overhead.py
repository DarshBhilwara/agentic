#!/usr/bin/env python3
"""Compare enabled/disabled overhead runs and plot only qualifying observations."""
import argparse
import json
import math
import statistics
from pathlib import Path


COUNTERS = ("events", "perf_lost", "map_overflow", "queue_drops", "export_errors",
            "discovery_errors", "identity_misses")
LOSS_COUNTERS = COUNTERS[1:]
MATCH_FIELDS = (
    ("workload_sha256",),
    ("deployment", "comparable_config_sha256"),
    ("node",), ("concurrency",), ("timeout_seconds",),
)


def _get(value, path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _read_block(directory):
    directory = Path(directory)
    try:
        metadata = json.loads((directory / "experiment.json").read_text())
        if not isinstance(metadata, dict):
            metadata = {"_input_error": "experiment.json must contain a JSON object"}
    except Exception as exc:
        metadata = {"_input_error": f"experiment.json could not be read ({type(exc).__name__})"}
    rows = []
    try:
        lines = (directory / "results.jsonl").read_text().splitlines()
    except Exception as exc:
        lines = []
        rows.append({"_input_error": f"results.jsonl could not be read ({type(exc).__name__})"})
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            rows.append(row if isinstance(row, dict) else {"_input_error": f"line {number} must be a JSON object"})
        except json.JSONDecodeError:
            rows.append({"_input_error": f"line {number} is not valid JSON"})
    return metadata, rows


def _duration(row):
    program = row.get("program") or {}
    if not isinstance(program, dict):
        return None, "missing program summary"
    value, queue_wait = program.get("task_completion_time_ms"), program.get("task_wait_time_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None, "missing or invalid task completion time"
    if isinstance(queue_wait, bool) or not isinstance(queue_wait, (int, float)) or not math.isfinite(queue_wait) or queue_wait < 0:
        return None, "missing or invalid queue wait"
    return float(value), None


def _qualification_reasons(row):
    reasons = []
    if row.get("warmup"):
        reasons.append("warmup")
    if row.get("task_status") != "Success":
        reasons.append(f"task status: {row.get('task_status', 'missing')}")
    if row.get("runner_error"):
        reasons.append("runner error")
    if row.get("telemetry_error"):
        reasons.append("telemetry error")
    if row.get("file_verified") is not True:
        reasons.append("workspace file was not verified")
    steps = row.get("steps")
    if not isinstance(steps, list) or len(steps) != 2:
        reasons.append("expected exactly two inference steps")
    else:
        if any(not isinstance(step, dict) for step in steps):
            reasons.append("malformed inference step")
            tools = []
        else:
            tools = []
            for step in steps:
                step_tools = step.get("tools", [])
                if not isinstance(step_tools, list):
                    reasons.append("malformed tool list")
                    continue
                tools.extend(step_tools)
        if (any(not isinstance(tool, dict) for tool in tools) or
                [tool.get("tool_name") for tool in tools if isinstance(tool, dict)] != ["write_file", "execute_command"] or
                any(tool.get("tool_status") != "Success" for tool in tools if isinstance(tool, dict))):
            reasons.append("expected successful write_file then execute_command")
    _, duration_error = _duration(row)
    if duration_error:
        reasons.append(duration_error)
    return reasons


def _percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _counter(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _capture_enabled(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def _health_diagnostics(metadata, setting):
    issues, counters, resets, losses = [], {}, [], []
    expected_capture = setting == "enabled"
    before, after = metadata.get("collector_before"), metadata.get("collector_after")
    if metadata.get("collector_postflight_ok") is not True:
        issues.append("collector postflight health check failed or is missing")
    for phase, health in (("before", before), ("after", after)):
        if not isinstance(health, dict):
            issues.append(f"collector {phase} health snapshot is missing")
            continue
        if health.get("node") != metadata.get("node"):
            issues.append(f"collector {phase} node does not match experiment node")
        if health.get("heartbeat_ok") is not True:
            issues.append(f"collector {phase} heartbeat is stale or unavailable")
        age = health.get("heartbeat_age_seconds")
        if (isinstance(age, bool) or not isinstance(age, (int, float)) or
                not math.isfinite(age) or age < 0 or age > 20):
            issues.append(f"collector {phase} heartbeat age is missing or stale")
        if _capture_enabled(health.get("capture_enabled")) is not expected_capture:
            issues.append(f"collector {phase} capture state does not match experiment setting")
        for field in COUNTERS:
            if _counter(health.get(field)) is None:
                issues.append(f"collector {phase} counter {field} is missing or invalid")
    if isinstance(before, dict) and isinstance(after, dict):
        for field in COUNTERS:
            left, right = _counter(before.get(field)), _counter(after.get(field))
            counters[field] = None if left is None or right is None else right - left
            if counters[field] is not None and counters[field] < 0:
                resets.append(field)
            elif field in LOSS_COUNTERS and counters[field] is not None and counters[field] > 0:
                losses.append({"counter": field, "increase": counters[field]})
    return {"issues": issues, "counter_deltas": counters,
            "counter_resets": resets, "positive_loss_increases": losses}


def build_summary(enabled_dir, disabled_dir):
    blocks = {}
    health_issues = []
    input_issues = []
    loaded = {setting: _read_block(directory)
              for setting, directory in (("enabled", enabled_dir), ("disabled", disabled_dir))}
    row_issues = {setting: [[] for _ in loaded[setting][1]] for setting in loaded}
    first_identity = {}
    for setting, directory in (("enabled", enabled_dir), ("disabled", disabled_dir)):
        metadata, rows = loaded[setting]
        if metadata.get("setting") != setting:
            input_issues.append(f"{setting} experiment metadata declares setting={metadata.get('setting')!r}")
        for index, row in enumerate(rows):
            if row.get("_input_error"):
                row_issues[setting][index].append(row["_input_error"])
                input_issues.append(f"{setting} row {index + 1}: {row['_input_error']}")
            if row.get("setting") != setting:
                reason = f"row setting does not match {setting} block"
                row_issues[setting][index].append(reason)
                input_issues.append(f"{setting} row {index + 1}: {reason}")
            for key in ("attempt_id", "run_id", "task_id"):
                value = row.get(key)
                if not isinstance(value, str) or not value.strip():
                    reason = f"missing {key}"
                    row_issues[setting][index].append(reason)
                    input_issues.append(f"{setting} row {index + 1}: {reason}")
                    continue
                identity = (key, value)
                if identity in first_identity:
                    previous_setting, previous_index = first_identity[identity]
                    reason = f"duplicate {key} across experiment blocks"
                    row_issues[setting][index].append(reason)
                    row_issues[previous_setting][previous_index].append(reason)
                    input_issues.append(f"{setting} row {index + 1}: {reason} ({key} reused)")
                else:
                    first_identity[identity] = (setting, index)

    for setting in ("enabled", "disabled"):
        metadata, rows = loaded[setting]
        accepted, excluded = [], []
        for index, row in enumerate(rows):
            reasons = _qualification_reasons(row) + row_issues[setting][index]
            duration, _ = _duration(row)
            if reasons:
                excluded.append({"attempt_id": row.get("attempt_id"), "index": row.get("index"),
                                 "warmup": bool(row.get("warmup")), "task_status": row.get("task_status"),
                                 "started_unix": row.get("started_unix"),
                                 "reasons": reasons})
            else:
                accepted.append({"attempt_id": row.get("attempt_id"), "index": row.get("index"),
                                 "task_completion_time_ms": duration,
                                 "task_wait_time_ms": row["program"]["task_wait_time_ms"],
                                 "started_unix": row.get("started_unix")})
        times = [row["task_completion_time_ms"] for row in accepted]
        before, after = metadata.get("collector_before") or {}, metadata.get("collector_after") or {}
        health = _health_diagnostics(metadata, setting)
        health_issues.extend(f"{setting}: {issue}" for issue in health["issues"])
        blocks[setting] = {
            "metadata": metadata,
            "attempt_count": len(rows),
            "task_status_counts": {status: sum(str(row.get("task_status", "missing") or "missing") == status
                                                for row in rows)
                                   for status in sorted({str(row.get("task_status", "missing") or "missing")
                                                         for row in rows})},
            "qualified_count": len(accepted), "qualified_observations": accepted,
            "excluded_count": len(excluded), "excluded_observations": excluded,
            "latency_ms": ({"median": statistics.median(times), "q1": _percentile(times, 0.25),
                            "q3": _percentile(times, 0.75), "min": min(times), "max": max(times)}
                           if times else None),
            "collector_counters": {"before": {key: before.get(key) for key in COUNTERS},
                                   "after": {key: after.get(key) for key in COUNTERS}},
            "collector_counter_diagnostics": {key: health[key] for key in
                                               ("counter_deltas", "counter_resets", "positive_loss_increases")},
            "collector_health_capture_order": {"before_heartbeat_unix": before.get("heartbeat_unix"),
                                               "after_heartbeat_unix": after.get("heartbeat_unix")},
        }
    baseline = blocks["enabled"]["metadata"]
    candidate = blocks["disabled"]["metadata"]
    mismatches = []
    for path in MATCH_FIELDS:
        enabled_value, disabled_value = _get(baseline, path), _get(candidate, path)
        if enabled_value is None or disabled_value is None or enabled_value != disabled_value:
            mismatches.append({"field": ".".join(path), "enabled": enabled_value, "disabled": disabled_value})
    for path in (("deployment", "vllm", "image"), ("deployment", "vllm", "spec_sha256")):
        enabled_value, disabled_value = _get(baseline, path), _get(candidate, path)
        if (enabled_value is not None or disabled_value is not None) and enabled_value != disabled_value:
            mismatches.append({"field": ".".join(path), "enabled": enabled_value, "disabled": disabled_value})
    capture_order = []
    for setting, block in blocks.items():
        for row in block["qualified_observations"] + block["excluded_observations"]:
            stamp = row.get("started_unix")
            if isinstance(stamp, (int, float)) and math.isfinite(stamp):
                capture_order.append({"setting": setting, "attempt_id": row.get("attempt_id"),
                                      "warmup": row.get("warmup", False), "started_unix": stamp})
    capture_order.sort(key=lambda row: row["started_unix"])
    plot_issues = ([f"configuration mismatch: {item['field']}" for item in mismatches] +
                   health_issues + input_issues)
    return {"measure": "task_completion_time_ms", "units": "ms",
            "interpretation": "End-to-end task latency includes queue wait and model variability; not isolated probe cost.",
            "settings_comparable": not plot_issues, "configuration_mismatches": mismatches,
            "input_validation_issues": input_issues, "plot_eligibility_issues": plot_issues,
            "capture_order": capture_order, "blocks": blocks}


def ensure_plot_eligible(summary):
    insufficient = [name for name, block in summary["blocks"].items() if block["qualified_count"] < 5]
    sampling_issues = (["need at least five qualifying non-warmup observations for: " + ", ".join(insufficient)]
                       if insufficient else [])
    summary["sampling_issues"] = sampling_issues
    summary["plot_eligibility_issues"] = summary.get("plot_eligibility_issues", []) + sampling_issues
    if not summary["settings_comparable"]:
        raise ValueError("experiments are not comparable: " + "; ".join(summary["plot_eligibility_issues"]))
    if insufficient:
        raise ValueError(sampling_issues[0])
    enabled = summary["blocks"]["enabled"]["latency_ms"]["median"]
    disabled = summary["blocks"]["disabled"]["latency_ms"]["median"]
    summary["median_difference_enabled_vs_disabled_percent"] = (enabled - disabled) / disabled * 100


def render(summary, output):
    ensure_plot_eligible(summary)
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("plotting requires matplotlib") from exc
    output = Path(output)
    fig, axis = plt.subplots(figsize=(8, 5))
    for x, (setting, color) in enumerate((("enabled", "#2563eb"), ("disabled", "#f97316"))):
        block = summary["blocks"][setting]
        observations = block["qualified_observations"]
        values = [row["task_completion_time_ms"] for row in observations]
        jitter = [x + ((i % 5) - 2) * 0.035 for i in range(len(values))]
        axis.scatter(jitter, values, color=color, alpha=0.72,
                     label=f"{setting}: qualified={len(values)}, attempts={block['attempt_count']}, excluded={block['excluded_count']}")
        stats = block["latency_ms"]
        median = stats["median"]
        axis.errorbar(x, median, yerr=[[median - stats["q1"]], [stats["q3"] - median]],
                      fmt="D", color="black", capsize=7, label="median and IQR" if x == 0 else None)
    axis.set_xticks([0, 1], ["Kernel capture enabled", "Kernel capture disabled"])
    axis.set_ylabel("Task completion time (ms)")
    axis.set_title("End-to-end agent task latency by kernel capture setting")
    diagnostics = [f"{setting}: {item['counter']} +{item['increase']}"
                   for setting, block in summary["blocks"].items()
                   for item in block["collector_counter_diagnostics"]["positive_loss_increases"]]
    diagnostics.extend(f"{setting}: counter reset ({name})"
                       for setting, block in summary["blocks"].items()
                       for name in block["collector_counter_diagnostics"]["counter_resets"])
    if diagnostics:
        axis.text(0.01, 0.99, "Collector counter diagnostics: " + "; ".join(diagnostics),
                  transform=axis.transAxes, va="top", fontsize=8, color="#991b1b")
    axis.grid(axis="y", alpha=0.25)
    axis.legend()
    fig.tight_layout()
    fig.savefig(output / "telemetry-overhead.png", dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enabled", required=True, type=Path, help="enabled-setting experiment directory")
    parser.add_argument("--disabled", required=True, type=Path, help="disabled-setting experiment directory")
    parser.add_argument("--output", required=True, type=Path, help="new output directory")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output path must not already exist")
    args.output.mkdir(parents=True)
    summary = build_summary(args.enabled, args.disabled)
    try:
        render(summary, args.output)
    except (ValueError, RuntimeError) as exc:
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
        parser.error(f"{exc}; counts and exclusions are saved in summary.json")
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Saved plot and summary under {args.output}")


if __name__ == "__main__":
    main()
