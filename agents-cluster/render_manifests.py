#!/usr/bin/env python3
"""Render the standalone agent-cluster manifest."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vllm-url", required=True)
    parser.add_argument("--phoenix-endpoint", default="phoenix.example:4317")
    parser.add_argument("--prometheus-endpoint", default="http://prometheus.example:9090/api/v1/write")
    parser.add_argument("--agent-root", default="/home/agentic/agentic")
    parser.add_argument("--output", type=Path, default=Path("build/agents-cluster/agents.yaml"))
    args = parser.parse_args()
    if not Path(args.agent_root).is_absolute() or not args.vllm_url:
        parser.error("--agent-root must be absolute and --vllm-url must be non-empty")
    source = Path(__file__).with_name("manifests") / "agents.yaml"
    text = source.read_text().replace("__VLLM_URL__", json.dumps(args.vllm_url.rstrip("/")))
    text = text.replace("__AGENT_BASE_DIR__", args.agent_root.rstrip("/"))
    text = text.replace("__OTEL_PHOENIX_ENDPOINT__", args.phoenix_endpoint.rstrip("/"))
    text = text.replace("__OTEL_PROMETHEUS_ENDPOINT__", args.prometheus_endpoint.rstrip("/"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text)


if __name__ == "__main__":
    main()
