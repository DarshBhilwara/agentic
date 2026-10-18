"""Kubernetes readiness check: BPF is attached and pod discovery succeeded."""
import json
import os
import socket

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(1)
sock.connect(os.getenv("KERNEL_SOCKET", "/run/agentic-kernel/collector.sock"))
sock.sendall(b'{"op":"ping"}\n')
response = b""
while not response.endswith(b"\n"):
    part = sock.recv(4096)
    if not part:
        break
    response += part
sock.close()
status = json.loads(response)
if not status.get("discovery_ready") or status.get("ok") is not True:
    raise SystemExit(1)
