#!/usr/bin/env python3
"""Run one multi-step coding sample and collect its telemetry evidence."""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from export_telemetry_sample import Gateway, export_run, read_token, safe_error


DATA = "region,amount\nNorth,100\nSouth,80\nNorth,50\nWest,70\nSouth,20\nWest,30\n"
SEED_ANALYZER = '''import csv\nimport json\nimport sys\n\nwith open(sys.argv[1], newline="") as stream:\n    rows = list(csv.DictReader(stream))\ntotals = {}\nfor row in rows:\n    totals[row["region"]] = totals.get(row["region"], 0) + int(row["amount"])\ntotal = sum(totals.values())\nprint(json.dumps({"count": len(rows), "total": total, "average": total // len(rows),\n                  "totals_by_region": totals}))\n'''
SEED_TESTS = '''import csv\nimport json\nimport subprocess\nimport sys\nimport tempfile\nimport unittest\nfrom pathlib import Path\n\nSCRIPT = Path(__file__).with_name("analyze.py")\n\ndef run(rows):\n    with tempfile.TemporaryDirectory() as directory:\n        data = Path(directory) / "input.csv"\n        with data.open("w", newline="") as stream:\n            writer = csv.writer(stream)\n            writer.writerow(["region", "amount"])\n            writer.writerows(rows)\n        result = subprocess.run([sys.executable, str(SCRIPT), str(data)],\n                                text=True, capture_output=True, timeout=10)\n        return result\n\nclass AnalyzerTests(unittest.TestCase):\n    def test_dataset_totals(self):\n        result = run([("North",100),("South",80),("North",50),("West",70),("South",20),("West",30)])\n        self.assertEqual(result.returncode, 0)\n        self.assertEqual(json.loads(result.stdout), {"count":6,"total":350,"average":350/6,\n            "totals_by_region":{"North":150,"South":100,"West":100}})\n    def test_decimal_refund_and_empty(self):\n        result = run([("A",1.5),("A",-0.25),("B",2.0)])\n        self.assertEqual(json.loads(result.stdout)["total"], 3.25)\n        empty = run([])\n        self.assertEqual(json.loads(empty.stdout), {"count":0,"total":0,"average":None,"totals_by_region":{}})\n    def test_bad_or_missing_amount_rejected(self):\n        for rows in [[("A","bad")], [("A",)]]:\n            self.assertNotEqual(run(rows).returncode, 0)\n\nif __name__ == "__main__":\n    unittest.main()\n'''
PROMPT = (
    "Use Python 3.8-compatible standard-library code. "
    "Inspect the existing analyze.py, sales.csv, and test_analyze.py. Repair the CSV analysis "
    "tool so it meets the tests and supports the requested interface: positional CSV input, "
    "optional --region filter, optional --output JSON file, and JSON output with count, total, "
    "average (null for empty input), and totals_by_region. Reject malformed/missing amounts. "
    "Run the tests, fix any failures, then run the tool filtered to North and write report.json. "
    "Use the provided tools for file edits and command execution; do not install packages, use "
    "the internet, delegate, or merely describe commands. Finish with a short summary."
)
_OWNED_OUTPUTS = set()


def _write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


def _write_failure(output, manifest, stage, exc=None, status="unknown"):
    failure = dict(manifest)
    failure.update({"task_status": status, "failure_stage": stage})
    if exc is not None:
        failure["error"] = safe_error(exc)
    _write_json(output / "failure.json", failure)
    _write_json(output / "result.json", failure)
    print(json.dumps({"state": "failed", "task_status": status, "failure_stage": stage,
                      "session_id": failure.get("session_id"), "program_id": failure.get("program_id"),
                      "run_id": failure.get("run_id"), "output": str(output)}, indent=2), flush=True)


def _seed_workspace(path):
    path.mkdir(parents=True)
    (path / "sales.csv").write_text(DATA)
    (path / "analyze.py").write_text(SEED_ANALYZER)
    (path / "test_analyze.py").write_text(
        SEED_TESTS.replace("text=True, capture_output=True)",
                           "text=True, capture_output=True, timeout=10)"))
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, timeout=10)
    subprocess.run(["git", "add", "sales.csv", "analyze.py", "test_analyze.py"], cwd=str(path), check=True, timeout=10)
    subprocess.run(["git", "-c", "user.name=Telemetry Sample", "-c", "user.email=sample@localhost",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                    "commit", "-qm", "Seed sample task"], cwd=str(path), check=True, timeout=10)


def _invoke(api, method, route, **kwargs):
    return api.request(method, route, **kwargs)


def _run_process_group(argv, cwd, timeout):
    process = subprocess.Popen(argv, cwd=str(cwd), text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr), timed_out


def _host_verify(workspace):
    outcomes = []
    report_path = workspace / "report.json"
    try:
        original_report = report_path.read_bytes()
        original_report_exists = True
        try:
            saved_report = json.loads(original_report.decode("utf-8"))
            report_valid = saved_report == {"count": 2, "total": 150, "average": 75,
                                            "totals_by_region": {"North": 150}}
        except Exception:
            report_valid = False
    except OSError:
        original_report = None
        original_report_exists = False
        report_valid = False

    def run(label, argv):
        try:
            result, timed_out = _run_process_group(argv, workspace, 30)
            outcomes.append({"name": label, "returncode": result.returncode,
                             "timed_out": timed_out, "stdout": result.stdout[-4000:],
                             "stderr": result.stderr[-2000:]})
            return result
        except Exception as exc:
            outcomes.append({"name": label, "error": safe_error(exc)})
            return None

    tests = run("unit_tests", [sys.executable, "-m", "unittest", "-v", "test_analyze"])
    csv_check = run("original_dataset", [sys.executable, "-c",
        "from pathlib import Path; import sys; expected=sys.argv[1]; actual=Path('sales.csv').read_text(); sys.exit(0 if actual == expected else 1)", DATA])
    good = tests is not None and tests.returncode == 0 and csv_check is not None and csv_check.returncode == 0
    good = good and report_valid
    # Independently exercise the filtered CLI and edge inputs without touching task artifacts.
    with tempfile.TemporaryDirectory(prefix="telemetry-host-check-") as directory:
        temp_root = Path(directory)
        north_output = temp_root / "north.json"
        filtered = run("north_cli_check", [sys.executable, "analyze.py", "sales.csv", "--region", "North",
                                             "--output", str(north_output)])
        try:
            good = good and filtered is not None and filtered.returncode == 0
            good = good and json.loads(north_output.read_text()) == {
                "count": 2, "total": 150, "average": 75, "totals_by_region": {"North": 150}}
        except Exception:
            good = False
        for name, body, expected, should_pass in (
            ("refund_decimal", "region,amount\nA,1.5\nA,-0.25\nB,2.0\n", {"count":3,"total":3.25,"average":3.25/3,"totals_by_region":{"A":1.25,"B":2.0}}, True),
            ("empty_input", "region,amount\n", {"count":0,"total":0,"average":None,"totals_by_region":{}}, True),
            ("invalid_amount", "region,amount\nA,bad\n", None, False),
            ("missing_amount", "region,amount\nA\n", None, False)):
            source = temp_root / (name + ".csv")
            source.write_text(body)
            result = run(name, [sys.executable, "analyze.py", str(source)])
            passed = result is not None and ((result.returncode == 0) == should_pass)
            if passed and should_pass:
                try:
                    passed = json.loads(result.stdout) == expected
                except Exception:
                    passed = False
            good = good and passed
    try:
        report_unchanged = original_report_exists and report_path.read_bytes() == original_report
    except OSError:
        report_unchanged = False
    good = good and report_unchanged
    outcomes.append({"name": "agent_report_preserved", "passed": report_unchanged})
    return {"passed": bool(good), "commands": outcomes}


def _make_react_steps(evidence):
    report = json.loads((Path(evidence) / "report.json").read_text())
    correlation = json.loads((Path(evidence) / "correlation.json").read_text())["correlation"]
    inventory = correlation.get("identity_inventory") or {}
    inventory_steps = {step.get("step_index"): step for step in inventory.get("steps", [])}
    report_run = report.get("runs", [{}])[0]
    summaries = report_run.get("steps", [])
    joined = []
    for step in summaries:
        index = step.get("step_index")
        identity = inventory_steps.get(index, {})
        joined.append({"step_index": index, "request_id": identity.get("request_id"),
                       "step_status": identity.get("step_status"),
                       "step_time_ms": step.get("step_time_ms"),
                       "llm_inference_time_ms": step.get("llm_inference_time_ms"),
                       "engine_metrics_status": step.get("engine_metrics_status"),
                       "tools": []})
        report_tools = step.get("tools", [])
        for tool in identity.get("tools", []):
            detail = next((item for item in report_tools if item.get("tool_index") == tool.get("index") and
                           item.get("tool_name") == tool.get("name")), {})
            call_id = tool.get("call_id")
            scope_key = "tool:%s:%s:%s" % (index, tool.get("name"), call_id) if call_id else None
            joined[-1]["tools"].append({"name": tool.get("name"), "call_id": call_id,
                "status": tool.get("status"), "tool_ttf_ms": detail.get("tool_ttf_ms"),
                "tool_latency_ms": detail.get("tool_latency_ms"),
                "kernel_scope_count": correlation.get("scope_counts", {}).get(scope_key) if scope_key else None})
        scope_key = "step:%s" % index
        joined[-1]["kernel_scope_count"] = correlation.get("scope_counts", {}).get(scope_key)
    return joined, report_run.get("summary", {})


def sample_is_valid(steps, verification):
    action_steps = [step for step in steps if step.get("tools")]
    has_command = any(tool.get("name") == "execute_command"
                      for step in steps for tool in step.get("tools", []))
    final = steps[-1] if steps else {}
    return bool(verification and len(action_steps) >= 2 and has_command and
                all(step.get("request_id") and step.get("step_status") for step in steps) and
                not final.get("tools") and final.get("step_status") == "Success")


def _verification_for(runtime_status, workspace):
    if runtime_status != "Success":
        return {"passed": False, "skipped_reason": "runtime_task_not_successful", "commands": []}
    try:
        return _host_verify(workspace)
    except Exception as exc:
        return {"passed": False, "skipped_reason": "verification_exception",
                "error": safe_error(exc), "commands": []}


def run(args):
    if not (1 <= args.timeout <= 3600) or not (1 <= args.max_steps <= 1000):
        raise ValueError("timeout/max-steps outside gateway limits")
    output = args.output.expanduser().resolve()
    results_root = Path(__file__).resolve().parents[1] / "benchmark-results"
    try:
        output.relative_to(results_root.resolve())
    except ValueError:
        raise ValueError("--output must be a new directory under benchmark-results")
    output.mkdir(parents=True, exist_ok=False)
    _OWNED_OUTPUTS.add(str(output))
    workspace_parent = args.workspace_root.expanduser().resolve()
    workspace_parent.mkdir(parents=True, exist_ok=True)
    workspace = workspace_parent / ("telemetry-sample-" + str(uuid.uuid4()))
    manifest = {"workspace": str(workspace), "task_status": "not_submitted", "sample_valid": False}
    stage = "preparation"
    try:
        _seed_workspace(workspace)
    except Exception as exc:
        _write_failure(output, manifest, "preparation", exc, status="not_submitted")
        return 2
    manifest["seed_files"] = ["sales.csv", "analyze.py", "test_analyze.py"]
    _write_json(output / "preparation.json", manifest)
    try:
        api = Gateway(args.gateway, read_token(args.token_file), min(args.timeout, 60))
    except Exception as exc:
        _write_failure(output, manifest, "session", exc)
        return 2
    try:
        stage = "session"
        session = _invoke(api, "POST", "/sessions", params={"workspace": str(workspace)})
        manifest.update({"session_id": session.get("session_id"), "agent_id": session.get("agent_id")})
        _write_json(output / "preparation.json", manifest)
        stage = "submission"
        submitted = _invoke(api, "POST", "/sessions/%s/messages" % session["session_id"],
                            params={"prompt": PROMPT, "max_steps": args.max_steps,
                                    "timeout_seconds": args.timeout, "coding_only": "true"})
        manifest.update({"task_id": submitted.get("task_id"), "program_id": submitted.get("program_id"),
                         "run_id": submitted.get("run_id"), "turn_id": submitted.get("turn_id"),
                         "task_status": "queued"})
        _write_json(output / "submission.json", manifest)
        print(json.dumps({"state": "submitted", "session_id": session.get("session_id"),
                          "program_id": submitted.get("program_id"), "run_id": submitted.get("run_id"),
                          "output": str(output)}, indent=2), flush=True)
    except Exception as exc:
        _write_failure(output, manifest, stage, exc)
        return 2
    deadline = time.monotonic() + args.timeout + 45
    terminal = {"completed", "failed", "timeout", "cancelled"}
    task = None
    stage = "poll"
    while time.monotonic() < deadline:
        try:
            task = _invoke(api, "GET", "/tasks/%s" % submitted["task_id"])
        except Exception as exc:
            manifest.update({"task_status": "unknown", "poll_error": safe_error(exc)})
            break
        status = str(task.get("status", "unknown")).lower()
        if status in terminal:
            manifest["task_status"] = task.get("task_status", status)
            break
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    if task is None or str(task.get("status", "")).lower() not in terminal:
        _write_failure(output, manifest, "poll", status="unknown")
        return 2
    _write_json(output / "task.json", {"task_id": submitted.get("task_id"), "run_id": submitted.get("run_id"),
                                        "program_id": submitted.get("program_id"),
                                        "status": task.get("status"), "task_status": task.get("task_status")})
    evidence = output / "evidence"
    stage = "export"
    try:
        exported = export_run(api, submitted["run_id"], evidence)
    except Exception as exc:
        manifest.update({"task_status": task.get("task_status", task.get("status")),
                         "session_id": session.get("session_id"), "task_id": submitted.get("task_id"),
                         "program_id": submitted.get("program_id"), "run_id": submitted.get("run_id")})
        _write_failure(output, manifest, "export", exc, status=manifest["task_status"])
        return 1
    runtime_status = task.get("task_status", task.get("status"))
    stage = "verification"
    verification = _verification_for(runtime_status, workspace)
    _write_json(output / "verification.json", verification)
    try:
        steps, stats = _make_react_steps(evidence)
    except Exception as exc:
        manifest.update({"session_id": session.get("session_id"), "task_id": submitted.get("task_id"),
                         "program_id": submitted.get("program_id"), "run_id": submitted.get("run_id"),
                         "workspace": str(workspace)})
        _write_failure(output, manifest, "export", exc, status=runtime_status)
        return 1
    enough = runtime_status == "Success" and sample_is_valid(steps, verification["passed"])
    result = {"task_status": runtime_status,
              "runtime_status": task.get("status"),
              "session_id": session.get("session_id"), "task_id": submitted.get("task_id"),
              "run_id": submitted.get("run_id"), "program_id": submitted.get("program_id"),
              "workspace": str(workspace),
              "sample_valid": enough,
              "sample_assessment": "multi_turn_react_sample" if enough else "insufficient_or_unverified_sample",
              "host_verification": verification, "react_steps": steps,
              "program_stats": stats, "export": exported,
              "note": "Host verification runs after task completion and is outside captured telemetry."}
    _write_json(output / "react_steps.json", {"run_id": submitted.get("run_id"),
                                                "program_id": submitted.get("program_id"),
                                                "task_status": runtime_status, "steps": steps,
                                                "program_stats": stats})
    _write_json(output / "result.json", result)
    print(json.dumps({"task_status": result["task_status"], "sample_valid": result["sample_valid"],
                      "run_id": submitted.get("run_id"), "output": str(output)}, indent=2))
    return 0 if result["sample_valid"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default=os.getenv("AGENTCTL_GATEWAY_URL", "http://localhost:30080"))
    parser.add_argument("--token-file", type=Path, default=Path("~/.agentic_token"))
    parser.add_argument("--workspace-root", type=Path, default=Path("/home/agentic/agentic/benchmark-workspaces"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        output = args.output.expanduser().resolve()
        preparation = output / "preparation.json"
        if str(output) in _OWNED_OUTPUTS and preparation.is_file():
            try:
                details = json.loads(preparation.read_text())
                accepted = output / "submission.json"
                if accepted.is_file():
                    details.update(json.loads(accepted.read_text()))
                _write_failure(output, details, "preparation", exc,
                               status=details.get("task_status", "unknown"))
            except Exception:
                pass
        print(json.dumps({"state": "failed", "error": safe_error(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
