#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(CODE_DIR))
from agent import run  # noqa: E402


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
    args = parser.parse_args()

    started = time.time()
    count = 0
    for case in load_cases(args.dataset, args.limit):
        case_id = str(case.get("id", count))
        result, _ = run(prompt_for(case), args.user,
                        workspace=os.path.abspath(args.location),
                        agent_id="bfcl-benchmark", session_id=f"bfcl-{case_id}",
                        turn_id=case_id, benchmark="bfcl", case_id=case_id)
        print(json.dumps({"id": case_id, "result": result}, ensure_ascii=False), flush=True)
        count += 1
    print(json.dumps({"benchmark": "bfcl", "cases": count, "elapsed_s": round(time.time() - started, 3)}))


if __name__ == "__main__":
    main()
