#!/usr/bin/env python3
"""Export one terminal run using the privacy-bounded report and kernel APIs."""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


TERMINAL = {"completed", "failed", "timeout", "cancelled"}
MAX_KERNEL_RECORDS = 10000
KERNEL_PAGE_SIZE = 500
MAX_SEMANTIC_EVENTS = 1000
COUNTERS = ("events", "perf_lost", "map_overflow", "queue_drops", "export_errors",
            "discovery_errors", "identity_misses")


def safe_error(exc):
    response = getattr(exc, "response", None)
    return {"type": type(exc).__name__,
            "http_status": getattr(response, "status_code", None)}


class Gateway:
    def __init__(self, url, token, timeout):
        self.url = _checked_base_url(url).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = token

    def request(self, method, path, **kwargs):
        response = self.session.request(method, self.url + path, timeout=self.timeout,
                                        allow_redirects=False, **kwargs)
        _raise_http_error(response)
        return response.json()


def _checked_base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError("service URL must be an http(s) base URL without credentials, query, or fragment")
    return value


def _raise_http_error(response):
    if 300 <= response.status_code < 400:
        raise requests.HTTPError(response=response)
    response.raise_for_status()


def read_token(path):
    try:
        token = Path(path).expanduser().read_text().strip()
    except OSError as exc:
        raise RuntimeError("API token file is unavailable") from exc
    if not token:
        raise RuntimeError("API token file is empty")
    return token


def _report_snapshot(api, run_id):
    return api.request("POST", "/console/reports", json={"run_ids": [run_id], "format": "json"})


def _kernel_page(api, run_id, offset, limit):
    return api.request("GET", f"/telemetry/runs/{quote(run_id, safe='')}/kernel",
                       params={"offset": offset, "limit": limit})


def _scope(record):
    tool = record.get("tool_call_id") or record.get("tool_name")
    if tool:
        return f"tool:{record.get('step_index')}:{record.get('tool_name')}:{tool}"
    if record.get("step_index") is not None:
        return f"step:{record.get('step_index')}"
    return "program"


def correlate(records, run_id, program_id):
    scopes, warnings = Counter(), []
    missing_run = missing_program = mismatched_run = mismatched_program = 0
    entries, exits = defaultdict(list), []
    unpairable = 0
    unmatched = []
    for index, record in enumerate(records):
        scopes[_scope(record)] += 1
        record_run, record_program = record.get("run_id"), record.get("program_id")
        if record_run is None: missing_run += 1
        elif record_run != run_id: mismatched_run += 1
        if record_program is None: missing_program += 1
        elif program_id is not None and record_program != program_id: mismatched_program += 1
        pair_key = (record.get("collector_id"), record.get("pid"), record.get("tid"),
                    record.get("entry_monotonic_ns"), record.get("syscall_id"))
        if record.get("type") == "entry":
            if any(value is None for value in pair_key):
                unpairable += 1
            else:
                entries[pair_key].append((index, record))
        elif record.get("type") == "exit":
            if any(value is None for value in pair_key):
                unpairable += 1
            else:
                exits.append((index, record, pair_key))
        elif record.get("type") == "unmatched_exit":
            unmatched.append({"scope": _scope(record), "step_index": record.get("step_index"),
                              "tool_name": record.get("tool_name"), "tool_call_id": record.get("tool_call_id"),
                              "run_id": record.get("run_id"), "program_id": record.get("program_id"),
                              "pid": record.get("pid"), "tid": record.get("tid"),
                              "syscall_id": record.get("syscall_id"), "ret": record.get("ret")})
    pairs = []
    paired_entries = set()
    paired_exits = set()
    for exit_index, exit_record, key in exits:
        candidates = entries.get(key, [])
        candidate = next(((index, entry) for index, entry in candidates if index not in paired_entries), None)
        if candidate is None:
            continue
        index, entry = candidate
        paired_entries.add(index)
        paired_exits.add(exit_index)
        entry_scope, exit_scope = _scope(entry), _scope(exit_record)
        pairs.append({"scope": entry_scope, "exit_scope": exit_scope,
                      "scope_mismatch": entry_scope != exit_scope,
                      "run_id": entry.get("run_id"), "program_id": entry.get("program_id"),
                      "step_index": entry.get("step_index"), "tool_name": entry.get("tool_name"),
                      "tool_call_id": entry.get("tool_call_id"), "collector_id": entry.get("collector_id"),
                      "pid": entry.get("pid"), "tid": entry.get("tid"), "syscall_id": entry.get("syscall_id"),
                      "entry_monotonic_ns": entry.get("entry_monotonic_ns"),
                      "entry_timestamp": entry.get("timestamp"), "exit_timestamp": exit_record.get("timestamp"),
                      "args": entry.get("args"), "ret": exit_record.get("ret"),
                      "entry_type": entry.get("type"), "exit_type": exit_record.get("type"),
                      "duration_ns": exit_record.get("duration_ns")})
    if missing_run: warnings.append(f"{missing_run} kernel records have no run identity")
    if missing_program: warnings.append(f"{missing_program} kernel records have no program identity")
    if mismatched_run: warnings.append(f"{mismatched_run} kernel records have a different run identity")
    if mismatched_program: warnings.append(f"{mismatched_program} kernel records have a different program identity")
    if program_id is None: warnings.append("selected program identity is unavailable from the report summary")
    if unmatched: warnings.append(f"{len(unmatched)} explicit unmatched_exit markers retained")
    if len(paired_entries) < sum(map(len, entries.values())):
        warnings.append("some syscall entries have no matching exit in the exported snapshot")
    if len(paired_exits) < len(exits):
        warnings.append("some syscall EXIT records have no matching entry in the exported snapshot")
    if unpairable:
        warnings.append(f"{unpairable} entry/exit records lack pairing identity fields and were not inferred as pairs")
    if any(pair["scope_mismatch"] for pair in pairs):
        warnings.append("some paired syscall EXIT scope differs from the ENTRY scope")
    by_scope = {}
    for pair in pairs:
        by_scope.setdefault(pair["scope"], pair)
    return {"selected_run_id": run_id, "selected_program_id": program_id,
            "observed_run_ids": sorted({str(record.get("run_id")) for record in records if record.get("run_id") is not None}),
            "observed_program_ids": sorted({str(record.get("program_id")) for record in records if record.get("program_id") is not None}),
            "scope_counts": dict(scopes), "record_identity_counts": {
                "missing_run_id": missing_run, "missing_program_id": missing_program,
                "mismatched_run_id": mismatched_run, "mismatched_program_id": mismatched_program},
            "matched_pair_count": len(pairs), "representative_pairs": list(by_scope.values()),
            "unmatched_exits": unmatched, "warnings": warnings}


def _owned_terminal_run(api, run_id):
    data = api.request("GET", f"/telemetry/runs/{quote(run_id, safe='')}",
                       params={"offset": 0, "limit": MAX_SEMANTIC_EVENTS})
    run = data.get("run") or {}
    if run.get("id") != run_id:
        raise ValueError("gateway returned a different run identity")
    if str(run.get("status", "")).lower() not in TERMINAL:
        raise ValueError("run must be terminal before export")
    summary = data.get("summary") or {}
    safe_steps = []
    for step in data.get("steps", []):
        safe_step = {key: step.get(key) for key in
                     ("program_id", "step_index", "request_id", "step_status") if key in step}
        safe_step["tools"] = [{key: tool.get(source) for key, source in
                               (("name", "tool_name"), ("call_id", "tool_call_id"),
                                ("index", "tool_index"), ("status", "tool_status"))
                               if source in tool} for tool in step.get("tools", [])]
        safe_steps.append(safe_step)
    safe_event_fields = ("id", "type", "timestamp", "run.id", "program_id", "step_index", "tool_call_id",
                         "tool_name", "trace.id", "span.id", "task.id", "agent.id", "agent.run.id",
                         "agent.parent.run.id", "root.run.id", "handoff.id")
    safe_events = []
    for event in data.get("events", [])[:MAX_SEMANTIC_EVENTS]:
        safe = {key: event[key] for key in safe_event_fields if key in event}
        attributes = event.get("attributes") or {}
        if isinstance(attributes, dict):
            safe.update({key: attributes[key] for key in safe_event_fields
                         if key in attributes and key not in safe})
        safe_events.append(safe)
    return {"run": {key: run.get(key) for key in
                     ("id", "program_id", "task.id", "session.id", "agent.id", "agent.run.id",
                      "agent.parent.run.id", "root.run.id", "trace.id", "status") if key in run},
            "summary": {key: summary.get(key) for key in ("program_id", "task_status", "inference_steps",
                          "completed_steps", "tool_calls", "tool_failures") if key in summary},
            "steps": safe_steps, "events": safe_events,
            "events_truncated": len(data.get("events", [])) >= MAX_SEMANTIC_EVENTS}


def export_run(api, run_id, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    terminal = _owned_terminal_run(api, run_id)
    report = _report_snapshot(api, run_id)
    runs = report.get("runs", [])
    report_run = next((item for item in runs if (item.get("run") or {}).get("id") == run_id), None)
    if report_run is None:
        raise ValueError("privacy-bounded report omitted the selected run")
    report_program = (report_run.get("summary") or {}).get("program_id")
    selected_program = (terminal["summary"].get("program_id") or terminal["run"].get("program_id") or report_program)
    first = _kernel_page(api, run_id, 0, KERNEL_PAGE_SIZE)
    initial_retained = first.get("retained")
    records = list(first.get("records", []))[:MAX_KERNEL_RECORDS]
    target = min(initial_retained, MAX_KERNEL_RECORDS) if isinstance(initial_retained, int) else MAX_KERNEL_RECORDS
    while len(records) < target:
        page_limit = min(KERNEL_PAGE_SIZE, target - len(records))
        page = _kernel_page(api, run_id, len(records), page_limit)
        page_records = page.get("records", [])
        if not page_records:
            break
        records.extend(page_records[:target - len(records)])
        if len(page_records) < page_limit:
            break
    final = _kernel_page(api, run_id, 0, 1)
    warnings = []
    if initial_retained is None:
        warnings.append("initial retained count unavailable")
    if isinstance(initial_retained, int) and len(records) != target:
        warnings.append("kernel pagination ended before the initial retained count")
    if initial_retained is not None and final.get("retained") != initial_retained:
        warnings.append("kernel retained count changed during export; pagination is an asynchronous snapshot")
    if first.get("total") is not None and final.get("total") != first.get("total"):
        warnings.append("kernel total count changed during export; the collector export is an asynchronous snapshot")
    initial_total, initial_retained = first.get("total"), first.get("retained")
    initial_trimmed = (initial_total - initial_retained
                       if isinstance(initial_total, int) and not isinstance(initial_total, bool) and
                       isinstance(initial_retained, int) and not isinstance(initial_retained, bool) else None)
    if first.get("truncated") is True or (initial_trimmed is not None and initial_trimmed > 0):
        warnings.append("initial kernel snapshot is truncated; records beyond retention are unavailable")
    if ((initial_retained is not None and initial_retained > MAX_KERNEL_RECORDS) or
            (initial_retained is None and len(records) == MAX_KERNEL_RECORDS)):
        warnings.append("export capped at the API retention maximum of 10,000 records")
    correlation = correlate(records, run_id, selected_program)
    correlation["warnings"].extend(warnings)
    correlation["identity_inventory"] = {"run": terminal["run"], "summary": terminal["summary"],
                                           "steps": terminal["steps"], "semantic_events": terminal["events"],
                                           "semantic_events_capped_at": MAX_SEMANTIC_EVENTS,
                                           "semantic_events_truncated": terminal["events_truncated"]}
    known_calls = {(tool.get("call_id"), step.get("step_index"), tool.get("name"))
                   for step in terminal["steps"] for tool in step.get("tools", []) if tool.get("call_id")}
    observed_calls = {(record.get("tool_call_id"), record.get("step_index"), record.get("tool_name"))
                      for record in records if record.get("tool_call_id")}
    kernel_call_ids = {call_id for call_id, _, _ in observed_calls}
    known_call_ids = {call_id for call_id, _, _ in known_calls}
    unjoined_calls = sorted(str(call_id) for call_id in kernel_call_ids - known_call_ids)
    if unjoined_calls:
        correlation["warnings"].append("kernel tool call IDs are absent from the step inventory")
    missing_kernel_calls = sum(bool(record.get("tool_name")) and not record.get("tool_call_id") for record in records)
    if missing_kernel_calls:
        correlation["warnings"].append(f"{missing_kernel_calls} kernel tool-scope records lack tool_call_id")
    scope_mismatches = sorted(str(call_id) for call_id, step, name in observed_calls
                              if (call_id, step, name) not in known_calls)
    if scope_mismatches:
        correlation["warnings"].append("kernel tool scopes disagree with the step inventory for call ID, step, or tool name")
    correlation["kernel_tool_call_ids"] = sorted(str(value) for value in kernel_call_ids)
    correlation["step_tool_call_ids"] = sorted(str(value) for value in known_call_ids)
    correlation["unjoined_kernel_tool_call_ids"] = unjoined_calls
    correlation["tool_scope_mismatches"] = scope_mismatches
    if report_program is not None and selected_program is not None and report_program != selected_program:
        correlation["warnings"].append("privacy report program ID differs from the owned run response")
    correlation["procedural_graph"] = report_run.get("procedural_graph")
    correlation["run"] = {"run_id": run_id, "program_id": selected_program,
                           "status": (report_run.get("run") or {}).get("status"),
                           "task_status": (report_run.get("summary") or {}).get("task_status")}
    kernel_snapshot = {"initial_total": first.get("total"), "initial_retained": initial_retained,
                       "initial_trimmed_records": initial_trimmed,
                       "initial_truncated": first.get("truncated"),
                       "initial_status": first.get("status"), "initial_node_health": first.get("node_health"),
                       "exported_records": len(records), "final_total": final.get("total"),
                       "final_retained": final.get("retained"), "final_truncated": final.get("truncated"),
                       "final_status": final.get("status"), "final_node_health": final.get("node_health"),
                       "asynchronous_snapshot": True, "warnings": warnings}
    report_envelope = {"runs": [report_run]}
    (output / "report.json").write_text(json.dumps(report_envelope, indent=2))
    with (output / "kernel.jsonl").open("x") as stream:
        for record in records:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    (output / "correlation.json").write_text(json.dumps({"kernel": kernel_snapshot,
                                                          "correlation": correlation}, indent=2))
    return {"run_id": run_id, "task_status": terminal["summary"].get("task_status"),
            "exported_records": len(records), "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default=os.getenv("AGENTCTL_GATEWAY_URL", "http://localhost:30080"))
    parser.add_argument("--token-file", type=Path, default=Path("~/.agentic_token"))
    parser.add_argument("--timeout", type=int, default=10, help="HTTP timeout in seconds, 1..60")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error("timeout must be from 1 to 60 seconds")
    try:
        api = Gateway(args.gateway, read_token(args.token_file), args.timeout)
    except Exception as exc:
        parser.error(f"gateway setup failed ({safe_error(exc)})")
    output_was_absent = not args.output.exists()
    try:
        result = export_run(api, args.run_id, args.output)
    except Exception as exc:
        if output_was_absent and args.output.is_dir():
            try:
                with (args.output / "failure.json").open("x") as stream:
                    json.dump({"state": "failed", "error": safe_error(exc)}, stream, indent=2)
            except OSError:
                pass
        print(json.dumps({"state": "failed", "error": safe_error(exc)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
