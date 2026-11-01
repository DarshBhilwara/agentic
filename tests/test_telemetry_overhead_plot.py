import json
import sys

import pytest

import plot_telemetry_overhead as plot


def metadata(setting):
    health = {"node": "agent-01", "heartbeat_ok": True, "heartbeat_age_seconds": 2.0,
              "capture_enabled": setting == "enabled",
              "events": 10, "perf_lost": 0, "map_overflow": 0, "queue_drops": 0,
              "export_errors": 0, "discovery_errors": 0, "identity_misses": 0}
    return {"setting": setting, "node": "agent-01", "concurrency": 1, "timeout_seconds": 60,
            "workload_sha256": "workload", "deployment": {"comparable_config_sha256": "config",
            "vllm": {"image": "vllm:tag", "spec_sha256": "vllm-spec"}},
            "collector_postflight_ok": True,
            "collector_before": {**health, "heartbeat_unix": 10},
            "collector_after": {**health, "perf_lost": 1, "heartbeat_unix": 20}}


def observation(index, *, warmup=False, status="Success", completion=100):
    return {"attempt_id": f"attempt-{index}", "task_id": f"task-{index}", "run_id": f"run-{index}",
            "index": index, "warmup": warmup,
            "task_status": status, "file_verified": status == "Success",
            "program": {"task_completion_time_ms": completion, "task_wait_time_ms": 2},
            "steps": [{"tools": [{"tool_name": "write_file", "tool_status": "Success"}]},
                      {"tools": [{"tool_name": "execute_command", "tool_status": "Success"}]}]}


def write_block(path, setting, rows):
    path.mkdir()
    (path / "experiment.json").write_text(json.dumps(metadata(setting)))
    for row in rows:
        row["setting"] = setting
    (path / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_summary_counts_attempts_warmups_failures_and_keeps_counter_order(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    write_block(enabled, "enabled", [observation(i) for i in range(5)] +
                [observation(6, warmup=True), observation(7, status="Failed")])
    write_block(disabled, "disabled", [observation(i + 10, completion=120) for i in range(5)])
    summary = plot.build_summary(enabled, disabled)
    plot.ensure_plot_eligible(summary)
    block = summary["blocks"]["enabled"]
    assert block["attempt_count"] == 7 and block["qualified_count"] == 5
    assert block["task_status_counts"] == {"Failed": 1, "Success": 6}
    assert [item["reasons"] for item in block["excluded_observations"]] == [["warmup"], ["task status: Failed", "workspace file was not verified"]]
    assert block["collector_counters"]["before"]["perf_lost"] == 0
    assert block["collector_counters"]["after"]["perf_lost"] == 1
    assert block["latency_ms"]["median"] == 100


def test_plot_refuses_changed_config_and_records_mismatch(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    write_block(enabled, "enabled", [observation(i) for i in range(5)])
    write_block(disabled, "disabled", [observation(i + 10) for i in range(5)])
    path = disabled / "experiment.json"
    data = json.loads(path.read_text())
    data["deployment"]["vllm"]["image"] = "different"
    path.write_text(json.dumps(data))
    summary = plot.build_summary(enabled, disabled)
    assert summary["configuration_mismatches"][0]["field"] == "deployment.vllm.image"
    with pytest.raises(ValueError, match="not comparable"):
        plot.ensure_plot_eligible(summary)


def test_missing_measurements_are_excluded_and_saved_as_reasons(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    rows = [observation(i) for i in range(5)]
    rows[0]["program"]["task_completion_time_ms"] = None
    write_block(enabled, "enabled", rows)
    write_block(disabled, "disabled", [observation(i + 10, completion=120) for i in range(5)])
    summary = plot.build_summary(enabled, disabled)
    excluded = summary["blocks"]["enabled"]["excluded_observations"][0]
    assert excluded["reasons"] == ["missing or invalid task completion time"]
    with pytest.raises(ValueError, match="at least five"):
        plot.ensure_plot_eligible(summary)


def test_four_qualified_samples_do_not_render(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    write_block(enabled, "enabled", [observation(i) for i in range(4)])
    write_block(disabled, "disabled", [observation(i + 10) for i in range(5)])
    with pytest.raises(ValueError, match="at least five"):
        plot.ensure_plot_eligible(plot.build_summary(enabled, disabled))


def test_missing_health_counter_refuses_plot_and_preserves_summary(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    write_block(enabled, "enabled", [observation(i) for i in range(5)])
    write_block(disabled, "disabled", [observation(i + 10) for i in range(5)])
    path = enabled / "experiment.json"
    data = json.loads(path.read_text())
    del data["collector_after"]["queue_drops"]
    path.write_text(json.dumps(data))
    summary = plot.build_summary(enabled, disabled)
    assert any("queue_drops" in issue for issue in summary["plot_eligibility_issues"])
    with pytest.raises(ValueError, match="queue_drops"):
        plot.ensure_plot_eligible(summary)


def test_stale_postflight_health_refuses_plot(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    write_block(enabled, "enabled", [observation(i) for i in range(5)])
    write_block(disabled, "disabled", [observation(i + 10) for i in range(5)])
    path = disabled / "experiment.json"
    data = json.loads(path.read_text())
    data["collector_postflight_ok"] = False
    data["collector_after"]["heartbeat_ok"] = False
    data["collector_after"]["heartbeat_age_seconds"] = 25
    path.write_text(json.dumps(data))
    summary = plot.build_summary(enabled, disabled)
    with pytest.raises(ValueError, match="postflight health check failed"):
        plot.ensure_plot_eligible(summary)


def test_counter_increases_are_flagged_but_do_not_exclude_latency(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    write_block(enabled, "enabled", [observation(i) for i in range(5)])
    write_block(disabled, "disabled", [observation(i + 10, completion=120) for i in range(5)])
    path = enabled / "experiment.json"
    data = json.loads(path.read_text())
    data["collector_after"]["perf_lost"] = 3
    path.write_text(json.dumps(data))
    summary = plot.build_summary(enabled, disabled)
    assert summary["blocks"]["enabled"]["qualified_count"] == 5
    assert summary["blocks"]["enabled"]["collector_counter_diagnostics"]["positive_loss_increases"] == [
        {"counter": "perf_lost", "increase": 3}]
    plot.ensure_plot_eligible(summary)
    assert summary["median_difference_enabled_vs_disabled_percent"] == pytest.approx(-16.6666666667)


def test_duplicate_attempt_ids_across_blocks_invalidate_comparison(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    rows = [observation(i) for i in range(5)]
    write_block(enabled, "enabled", rows)
    copied = [dict(row) for row in rows]
    write_block(disabled, "disabled", copied)
    summary = plot.build_summary(enabled, disabled)
    assert any("duplicate attempt_id" in issue for issue in summary["input_validation_issues"])
    with pytest.raises(ValueError, match="duplicate attempt_id"):
        plot.ensure_plot_eligible(summary)


def test_row_setting_mismatch_is_reported_in_saved_comparison_summary(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    write_block(enabled, "enabled", [observation(i) for i in range(5)])
    write_block(disabled, "disabled", [observation(i + 10) for i in range(5)])
    rows_path = enabled / "results.jsonl"
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    rows[0]["setting"] = "disabled"
    rows_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    summary = plot.build_summary(enabled, disabled)
    assert any("row setting does not match" in issue for issue in summary["input_validation_issues"])
    assert summary["blocks"]["enabled"]["qualified_count"] == 4


def test_missing_run_identity_invalidates_comparison(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    rows = [observation(i) for i in range(5)]
    del rows[0]["run_id"]
    write_block(enabled, "enabled", rows)
    write_block(disabled, "disabled", [observation(i + 10) for i in range(5)])
    summary = plot.build_summary(enabled, disabled)
    assert any("missing run_id" in issue for issue in summary["input_validation_issues"])
    with pytest.raises(ValueError, match="missing run_id"):
        plot.ensure_plot_eligible(summary)


def test_malformed_json_line_is_preserved_as_input_issue(tmp_path):
    enabled, disabled = tmp_path / "enabled", tmp_path / "disabled"
    write_block(enabled, "enabled", [observation(i) for i in range(5)])
    write_block(disabled, "disabled", [observation(i + 10) for i in range(5)])
    with (enabled / "results.jsonl").open("a") as stream:
        stream.write("{invalid json}\n")
    summary = plot.build_summary(enabled, disabled)
    assert any("line 6 is not valid JSON" in issue for issue in summary["input_validation_issues"])
    with pytest.raises(ValueError, match="not valid JSON"):
        plot.ensure_plot_eligible(summary)


def test_cli_saves_median_difference_after_render(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    monkeypatch.setenv("MPLBACKEND", "Agg")
    enabled, disabled, output = tmp_path / "enabled", tmp_path / "disabled", tmp_path / "plot"
    write_block(enabled, "enabled", [observation(i) for i in range(5)])
    write_block(disabled, "disabled", [observation(i + 10, completion=120) for i in range(5)])
    monkeypatch.setattr(sys, "argv", ["plot_telemetry_overhead.py", "--enabled", str(enabled),
                                      "--disabled", str(disabled), "--output", str(output)])
    plot.main()
    saved = json.loads((output / "summary.json").read_text())
    assert saved["median_difference_enabled_vs_disabled_percent"] == pytest.approx(-16.6666666667)
    assert (output / "telemetry-overhead.png").is_file()


def test_cli_keeps_diagnostic_summary_when_plot_is_refused(tmp_path, monkeypatch):
    enabled, disabled, output = tmp_path / "enabled", tmp_path / "disabled", tmp_path / "plot"
    write_block(enabled, "enabled", [observation(i) for i in range(4)])
    write_block(disabled, "disabled", [observation(i + 10) for i in range(5)])
    monkeypatch.setattr(sys, "argv", ["plot_telemetry_overhead.py", "--enabled", str(enabled),
                                      "--disabled", str(disabled), "--output", str(output)])
    with pytest.raises(SystemExit):
        plot.main()
    saved = json.loads((output / "summary.json").read_text())
    assert saved["blocks"]["enabled"]["qualified_count"] == 4
    assert "at least five" in " ".join(saved["plot_eligibility_issues"])
    assert not (output / "telemetry-overhead.png").exists()
