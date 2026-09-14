#!/usr/bin/env python3

import argparse
import json
import os
import time
from pathlib import Path

import requests


def load_cases(path, limit):
    with open(path, encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if limit and index >= limit:
                break
            if line.strip():
                yield json.loads(line)


def prompt_for(case):
    functions = json.dumps(case.get("function", []), ensure_ascii=False)
    return (
        "You are being evaluated on a BFCL function-calling case. "
        "Determine the user's intent and respond using the available tools when appropriate.\n"
        f"Candidate function documentation:\n{functions}\n"
        f"User request:\n{case.get('question', '')}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path, help="A BFCL JSONL category file")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--user", default="bfcl-benchmark")
    parser.add_argument("--location", default=os.getcwd(),
                        help="Project directory the agent may inspect or edit")
    parser.add_argument("--gateway-url", default=os.getenv("AGENTCTL_GATEWAY_URL", "http://localhost:30080"))
    parser.add_argument("--api-key", default=os.getenv("AGENT_API_KEY"),
                        help="Gateway token; defaults to AGENT_API_KEY or ~/.agentic_token")
    args = parser.parse_args()
    if not args.dataset.is_file():
        parser.error(f"dataset must be a file; got {args.dataset}. Set BFCL_CASES to a BFCL JSONL file.")
    if not os.path.isdir(args.location):
        parser.error(f"location must be an existing directory: {args.location}")

    api_key = args.api_key
    if not api_key:
        try:
            with open(os.path.expanduser("~/.agentic_token"), encoding="utf-8") as stream:
                api_key = stream.read().strip()
        except OSError:
            parser.error("missing gateway token; use --api-key, AGENT_API_KEY, or agentctl login")
    headers = {"X-API-Key": api_key}

    try:
        response = requests.post(f"{args.gateway_url.rstrip('/')}/sessions",
                                 params={"workspace": os.path.abspath(args.location)},
                                 headers=headers, timeout=30)
        response.raise_for_status()
        session_id = response.json()["session_id"]
    except requests.RequestException as exc:
        parser.error(f"could not create backend session: {exc}")

    started = time.time()
    count = 0
    for case in load_cases(args.dataset, args.limit):
        case_id = str(case.get("id", count))
        try:
            response = requests.post(
                f"{args.gateway_url.rstrip('/')}/sessions/{session_id}/messages",
                params={"prompt": prompt_for(case), "benchmark": "bfcl", "case_id": case_id},
                headers=headers, timeout=30)
            response.raise_for_status()
            task_id = response.json()["task_id"]
            while True:
                status = requests.get(f"{args.gateway_url.rstrip('/')}/tasks/{task_id}",
                                      headers=headers, timeout=30)
                status.raise_for_status()
                task = status.json()
                if task["status"] == "completed":
                    result = task.get("result", "")
                    break
                if task["status"] == "failed":
                    raise RuntimeError(task.get("error", "unknown backend error"))
                time.sleep(1)
        except (requests.RequestException, RuntimeError) as exc:
            parser.error(f"BFCL case {case_id} failed through backend: {exc}")
        print(json.dumps({"id": case_id, "result": result}, ensure_ascii=False), flush=True)
        count += 1
    print(json.dumps({"benchmark": "bfcl", "cases": count, "elapsed_s": round(time.time() - started, 3)}))


if __name__ == "__main__":
    main()
