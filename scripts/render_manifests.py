#!/usr/bin/env python3
"""Render node-local paths without contacting or changing a cluster."""
import argparse
from pathlib import Path

import yaml


def replace(value, substitutions):
    if isinstance(value, str):
        for old, new in substitutions.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [replace(item, substitutions) for item in value]
    if isinstance(value, dict):
        return {key: replace(item, substitutions) for key, item in value.items()}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", default="/home/agentic/agentic")
    parser.add_argument("--inference-cache", required=True)
    parser.add_argument("--output", type=Path, default=Path("build/manifests"))
    args = parser.parse_args()
    if not Path(args.agent_root).is_absolute() or not Path(args.inference_cache).is_absolute():
        parser.error("Both node paths must be absolute")
    source = Path(__file__).resolve().parents[1] / "manifests"
    if args.output.resolve() == source:
        parser.error("Choose an output directory other than source manifests")
    args.output.mkdir(parents=True, exist_ok=True)
    substitutions = {"/home/agentic/agentic": args.agent_root.rstrip("/"),
                     "__AGENT_BASE_DIR__": args.agent_root.rstrip("/"),
                     "__INFERENCE_CACHE_DIR__": args.inference_cache}
    for path in source.glob("*.yaml"):
        docs = [replace(doc, substitutions) for doc in yaml.safe_load_all(path.read_text())]
        (args.output / path.name).write_text(yaml.safe_dump_all(docs, sort_keys=False))


if __name__ == "__main__":
    main()
