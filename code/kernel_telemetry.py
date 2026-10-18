"""Client for the node-local syscall collector. BCC is only needed by the collector."""
from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from contextlib import contextmanager


class KernelTelemetryError(RuntimeError):
    pass


_PROCESS_SCOPE_LOCK = threading.Lock()


class KernelTelemetry:
    """Synchronously set the worker cgroup's active program/step/tool scope."""
    def __init__(self, path=None, *, required=None):
        self.path = path or os.getenv("KERNEL_TELEMETRY_SOCKET", "/run/agentic-kernel/collector.sock")
        self.required = (os.getenv("KERNEL_TELEMETRY_ENABLED", "false").lower() == "true"
                         if required is None else required)
        self._cgroup = None
        self._stack = []

    def _send(self, payload):
        if not self.required:
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(2)
                conn.connect(self.path)
                conn.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
                response = b""
                while not response.endswith(b"\n"):
                    part = conn.recv(4096)
                    if not part:
                        break
                    response += part
            ack = json.loads(response)
            if not ack.get("ok"):
                raise KernelTelemetryError(ack.get("error", "collector rejected scope"))
            return ack
        except (OSError, ValueError) as exc:
            if self.required:
                raise KernelTelemetryError(f"kernel collector unavailable: {exc}") from exc
            return None

    def _set(self, scope):
        if self._cgroup is None:
            ack = self._send({"op": "register", "scope": scope})
            if ack:
                self._cgroup = ack.get("cgroup_id")
        else:
            self._send({"op": "scope", "cgroup_id": self._cgroup, "scope": scope})

    def _recover(self):
        self._send({"op": "recover"})
        self._cgroup = None

    @contextmanager
    def scope(self, **fields):
        previous = self._stack[-1] if self._stack else {}
        outer = not previous
        scope = {**previous, "scope_id": secrets.randbits(63) or 1,
                 **{k: str(v) for k, v in fields.items() if v is not None}}
        if not self.required:
            yield
            return
        acquired = False
        if outer:
            acquired = _PROCESS_SCOPE_LOCK.acquire(blocking=False)
            if not acquired:
                raise KernelTelemetryError("another program scope is active in this worker process")
        try:
            try:
                if outer:
                    self._recover()
                self._set(scope)
            except Exception:
                if outer:
                    try:
                        self._recover()
                    except Exception:
                        pass
                raise
            self._stack.append(scope)
            try:
                yield
            finally:
                self._stack.pop()
                try:
                    self._set(previous)
                except Exception:
                    if outer:
                        try:
                            self._recover()
                        except Exception:
                            pass
                    raise
                finally:
                    if outer:
                        self._cgroup = None
        finally:
            if acquired:
                _PROCESS_SCOPE_LOCK.release()


def kernel_telemetry_from_env():
    return KernelTelemetry()
