#!/usr/bin/env python3
"""Node-local raw syscall collector. Requires Linux cgroup v2 and BCC."""
import ctypes
import hashlib
import json
import logging
import os
import platform
import queue
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

NS = os.getenv("KERNEL_SOCKET", "/run/agentic-kernel/collector.sock")
ROOT = os.getenv("KERNEL_ARCHIVE", "/var/log/agentic-kernel")
NODE = os.getenv("NODE_NAME", "unknown")
MAX_BYTES = int(os.getenv("KERNEL_ARCHIVE_MAX_BYTES", "67108864"))
MAX_FILES = int(os.getenv("KERNEL_ARCHIVE_FILES", "5"))
EVENTS = queue.Queue(maxsize=16384)
COUNTS = {"events": 0, "queue_drops": 0, "perf_lost": 0, "map_overflow": 0,
          "discovery_errors": 0, "identity_misses": 0, "export_errors": 0}
COLLECTOR_ID = str(uuid.uuid4())
IDENTITIES = OrderedDict()
IDENTITY_LOCK = threading.RLock()
ACTIVE_LOCK = threading.RLock()
ACTIVE_CGROUPS = {}
OWNERS = {}
INFERENCE_CGROUPS = {}
DISCOVERY_READY = False
CAPTURE_ENABLED = os.getenv("KERNEL_CAPTURE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
INFERENCE_CAPTURE_ENABLED = os.getenv("KERNEL_INFERENCE_SYSCALLS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
IDENTITY_TTL_NS = int(os.getenv("KERNEL_IDENTITY_TTL_SECONDS", "3600")) * 1_000_000_000
IDENTITY_LIMIT = int(os.getenv("KERNEL_IDENTITY_LIMIT", "65536"))

PROGRAM = r"""
#include <uapi/linux/ptrace.h>
struct inflight_t { u64 start; u64 scope; u64 cgroup; int id; u64 args[6]; };
struct event_t {
  u64 ts; u64 duration; u64 args[6]; s64 ret; u64 scope; u64 cgroup; u32 pid; u32 tid; u32 uid;
  s32 id; u8 kind; char comm[16];
};
BPF_HASH(active, u64, u64, 16384);
BPF_HASH(inflight, u64, struct inflight_t, 65536);
BPF_ARRAY(map_drops, u64, 1);
BPF_PERF_OUTPUT(events);
TRACEPOINT_PROBE(raw_syscalls, sys_enter) {
  u64 cgid = bpf_get_current_cgroup_id();
  u64 *scope = active.lookup(&cgid);
  if (!scope || !*scope) return 0;
  u64 key = bpf_get_current_pid_tgid();
  struct inflight_t v = {};
  v.start = bpf_ktime_get_ns(); v.scope = *scope; v.cgroup = cgid; v.id = args->id;
  __builtin_memcpy(v.args, args->args, sizeof(v.args));
  if (inflight.update(&key, &v) < 0) { u32 zero = 0; u64 *drop = map_drops.lookup(&zero); if (drop) __sync_fetch_and_add(drop, 1); }
  struct event_t e = {};
  e.ts = v.start; e.scope = v.scope; e.cgroup = cgid; e.pid = key >> 32; e.tid = key; e.uid = bpf_get_current_uid_gid();
  e.id = v.id; e.kind = 0; __builtin_memcpy(e.args, v.args, sizeof(e.args)); bpf_get_current_comm(&e.comm, sizeof(e.comm));
  events.perf_submit(args, &e, sizeof(e));
  return 0;
}
TRACEPOINT_PROBE(raw_syscalls, sys_exit) {
  u64 key = bpf_get_current_pid_tgid();
  struct inflight_t *v = inflight.lookup(&key);
  u64 cgid = bpf_get_current_cgroup_id();
  u64 *active_scope = active.lookup(&cgid);
  if (!v && (!active_scope || !*active_scope)) return 0;
  struct event_t e = {};
  e.ts = bpf_ktime_get_ns(); e.cgroup = cgid; e.pid = key >> 32; e.tid = key; e.uid = bpf_get_current_uid_gid();
  e.ret = args->ret; e.kind = 1; bpf_get_current_comm(&e.comm, sizeof(e.comm));
  if (v) { e.scope = v->scope; e.cgroup = v->cgroup; e.id = v->id; e.duration = e.ts - v->start; __builtin_memcpy(e.args, v->args, sizeof(e.args)); inflight.delete(&key); }
  else { e.scope = *active_scope; e.id = args->id; e.kind = 2; }
  events.perf_submit(args, &e, sizeof(e));
  return 0;
}
TRACEPOINT_PROBE(sched, sched_process_exit) {
  u64 key = bpf_get_current_pid_tgid();
  inflight.delete(&key);
  return 0;
}
"""


def cgroup_for_pid(pid):
    with open(f"/proc/{pid}/cgroup", encoding="utf-8") as stream:
        for line in stream:
            hierarchy, controllers, path = line.rstrip().split(":", 2)
            if hierarchy == "0" and controllers == "":
                full = os.path.join("/sys/fs/cgroup", path.lstrip("/"))
                return os.stat(full).st_ino
    raise RuntimeError("peer is not in a cgroup v2 hierarchy")


def process_start_time(pid):
    """Read Linux /proc start ticks to distinguish a reused numeric PID."""
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
        fields = stream.read().rsplit(")", 1)[1].strip().split()
    return fields[19]


def remember_identity(scope):
    sid = int(scope.get("scope_id", 0))
    if not sid:
        return
    with IDENTITY_LOCK:
        IDENTITIES[sid] = (dict(scope), time.monotonic_ns())
        IDENTITIES.move_to_end(sid)


def retire_identity(sid):
    with IDENTITY_LOCK:
        item = IDENTITIES.get(int(sid))
        if item:
            IDENTITIES[int(sid)] = (item[0], time.monotonic_ns())


def identity_for(sid):
    with IDENTITY_LOCK:
        item = IDENTITIES.get(sid)
        return dict(item[0]) if item else {}


def prune_identities(bpf):
    """Keep scopes referenced by active cgroups or in-flight syscall exits."""
    with ACTIVE_LOCK:
        live = set(ACTIVE_CGROUPS.values()) | set(INFERENCE_CGROUPS.values())
    try:
        live.update(int(value.scope) for _, value in bpf["inflight"].items() if int(value.scope))
    except Exception:
        # Retaining extra identities is safer than losing an in-flight exit's labels.
        return
    cutoff = time.monotonic_ns() - IDENTITY_TTL_NS
    with IDENTITY_LOCK:
        for sid, (_, updated) in list(IDENTITIES.items()):
            if sid not in live and updated < cutoff:
                del IDENTITIES[sid]
        while len(IDENTITIES) > IDENTITY_LIMIT:
            sid = next(iter(IDENTITIES))
            if sid in live:
                IDENTITIES.move_to_end(sid)
                if all(key in live for key in IDENTITIES):
                    break
            else:
                del IDENTITIES[sid]


def prune_worker_scopes(bpf):
    """Remove filters left by dead workers and reused host PIDs."""
    with ACTIVE_LOCK:
        for cgid, owner in list(OWNERS.items()):
            pid, start_time = owner
            try:
                still_owner = cgroup_for_pid(pid) == cgid and process_start_time(pid) == start_time
            except (OSError, RuntimeError):
                still_owner = False
            if not still_owner:
                sid = ACTIVE_CGROUPS.pop(cgid, 0)
                OWNERS.pop(cgid, None)
                bpf["active"].pop(ctypes.c_ulonglong(cgid), None)
                retire_identity(sid)


def pod_matches_cgroup(path, pod):
    """Match a cgroup path to an explicitly labelled, non-terminal pod."""
    metadata = pod.get("metadata") or {}
    labels = metadata.get("labels") or {}
    uid = metadata.get("uid", "")
    phase = (pod.get("status") or {}).get("phase")
    if labels.get("telemetry.agentic.io/syscalls") != "enabled" or not uid or phase in {"Succeeded", "Failed"}:
        return False
    normalized = path.replace("_", "-")
    return uid in normalized or uid.replace("-", "_") in path


def scan_inference_cgroups(pods, proc_root="/proc", cgroup_root="/sys/fs/cgroup"):
    """Return leaf cgroup inode and pod identity for matching local processes."""
    found = {}
    try:
        entries = os.scandir(proc_root)
    except OSError:
        raise
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "cgroup"), encoding="utf-8") as stream:
                    lines = stream.readlines()
            except (OSError, PermissionError):
                continue
            for line in lines:
                parts = line.rstrip().split(":", 2)
                if len(parts) != 3 or parts[0] != "0" or parts[1] != "":
                    continue
                cgroup_path = parts[2]
                pod = next((candidate for candidate in pods if pod_matches_cgroup(cgroup_path, candidate)), None)
                if not pod:
                    continue
                try:
                    cgid = os.stat(os.path.join(cgroup_root, cgroup_path.lstrip("/"))).st_ino
                except OSError:
                    continue
                metadata = pod.get("metadata") or {}
                status = pod.get("status") or {}
                labels = metadata.get("labels") or {}
                found[cgid] = {"scope_id": (1 << 63) | int.from_bytes(
                    hashlib.blake2b(str(cgid).encode(), digest_size=8).digest(), "big") & ((1 << 63) - 1),
                    "scope_kind": "inference", "pod_uid": metadata.get("uid"),
                    "pod_name": metadata.get("name"), "pod_namespace": metadata.get("namespace"),
                    "container": "vllm", "model": labels.get("app"),
                    "container_id": next((item.get("containerID", "") for item in status.get("containerStatuses", [])
                                           if item.get("name") == "vllm"), "")}
    return found


def kubernetes_pods_for_node():
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    namespace = os.getenv("POD_NAMESPACE", "agentic-ai")
    node = NODE
    params = urllib.parse.urlencode({
        "fieldSelector": f"spec.nodeName={node}",
        "labelSelector": "telemetry.agentic.io/syscalls=enabled"})
    url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods?{params}"
    with open("/var/run/secrets/kubernetes.io/serviceaccount/token", encoding="utf-8") as stream:
        token = stream.read().strip()
    context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, context=context, timeout=5) as response:
        return json.loads(response.read()).get("items", [])


def update_inference_maps(bpf, desired):
    desired_ids = set(desired)
    with ACTIVE_LOCK:
        for cgid in set(INFERENCE_CGROUPS) - desired_ids:
            bpf["active"].pop(ctypes.c_ulonglong(cgid), None)
            sid = INFERENCE_CGROUPS.pop(cgid)
            ACTIVE_CGROUPS.pop(cgid, None)
            retire_identity(sid)
        for cgid, scope in desired.items():
            sid = int(scope["scope_id"])
            remember_identity(scope)  # publish identity before enabling the BPF filter
            bpf["active"][ctypes.c_ulonglong(cgid)] = ctypes.c_ulonglong(sid)
            INFERENCE_CGROUPS[cgid] = sid
            ACTIVE_CGROUPS[cgid] = sid


def inference_discovery(bpf):
    global DISCOVERY_READY
    if not INFERENCE_CAPTURE_ENABLED:
        DISCOVERY_READY = True
        return
    while True:
        try:
            pods = kubernetes_pods_for_node()
            update_inference_maps(bpf, scan_inference_cgroups(pods))
            DISCOVERY_READY = True
        except Exception:
            DISCOVERY_READY = False
            COUNTS["discovery_errors"] += 1
            logging.exception("Inference syscall cgroup discovery failed; preserving last successful set")
        prune_identities(bpf)
        prune_worker_scopes(bpf)
        time.sleep(float(os.getenv("KERNEL_DISCOVERY_INTERVAL_SECONDS", "5")))


def publish(value):
    try:
        EVENTS.put_nowait(value)
    except queue.Full:
        COUNTS["queue_drops"] += 1


def publish_run_status(scope):
    run = scope.get("run_id")
    if run:
        publish({"_status_only": True, "run_id": run})


def rotate_archive(path):
    if os.path.exists(path) and os.path.getsize(path) >= MAX_BYTES:
        for i in range(MAX_FILES - 1, 0, -1):
            old, new = f"{path}.{i}", f"{path}.{i+1}"
            if os.path.exists(old):
                os.replace(old, new)
        os.replace(path, path + ".1")


def export_run_event(client, value):
    run = value.get("run_id")
    if not run:
        return
    key = f"kernel:run:{run}"
    pipe = client.pipeline()
    pipe.rpush(key, json.dumps(value, separators=(",", ":")))
    pipe.ltrim(key, -10000, -1)
    pipe.expire(key, int(os.getenv("KERNEL_REDIS_TTL", "2592000")))
    pipe.hincrby(f"{key}:status", "events", 1)
    pipe.hset(f"{key}:status", "node", NODE)
    pipe.hset(f"{key}:status", mapping={name: COUNTS[name] for name in
             ("perf_lost", "map_overflow", "queue_drops", "export_errors", "identity_misses")})
    pipe.expire(f"{key}:status", int(os.getenv("KERNEL_REDIS_TTL", "2592000")))
    pipe.execute()


def mark_run_active(client, run_id):
    key = f"kernel:run:{run_id}:status"
    pipe = client.pipeline()
    pipe.hset(key, mapping={"node": NODE, "capture_started_unix": time.time()})
    pipe.hsetnx(key, "events", 0)
    pipe.expire(key, int(os.getenv("KERNEL_REDIS_TTL", "2592000")))
    pipe.execute()


def writer():
    import redis
    os.makedirs(ROOT, exist_ok=True)
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis-service:6379/0"), socket_timeout=2)
    path = os.path.join(ROOT, f"{NODE}.jsonl")
    while True:
        value = EVENTS.get()
        try:
            if value.get("_status_only"):
                mark_run_active(client, value["run_id"])
                continue
            rotate_archive(path)
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, separators=(",", ":")) + "\n")
            export_run_event(client, value)
            COUNTS["events"] += 1
        except Exception:
            COUNTS["export_errors"] += 1


def health_writer():
    import redis
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis-service:6379/0"), socket_timeout=2)
    key = f"kernel:node:{NODE}:status"
    while True:
        try:
            values = {name: COUNTS[name] for name in COUNTS}
            values.update({"node": NODE, "heartbeat_unix": time.time(),
                           "inference_cgroups": len(INFERENCE_CGROUPS),
                           "queue_depth": EVENTS.qsize(), "capture_enabled": str(CAPTURE_ENABLED).lower(),
                           "status": "active" if CAPTURE_ENABLED else "idle"})
            client.hset(key, mapping=values)
            client.expire(key, 20)
        except Exception:
            COUNTS["export_errors"] += 1
            logging.exception("Unable to publish kernel collector heartbeat")
        time.sleep(5)


def event_record(e, offset_ns):
    kind = "unmatched_exit" if e.kind == 2 else "exit" if e.kind else "entry"
    scope = identity_for(int(e.scope))
    if e.scope and not scope:
        COUNTS["identity_misses"] += 1
    entry_ts = (int(e.ts) - int(e.duration) if kind == "exit" else
                int(e.ts) if kind == "entry" else None)
    return {"collector_id": COLLECTOR_ID, "node": NODE, "type": kind,
            "architecture": platform.machine(),
            "attribution": "unknown" if e.scope and not scope else scope.get("scope_kind", "program"),
            "scope_id": int(e.scope), "entry_monotonic_ns": entry_ts,
            "syscall_id": int(e.id), "cgroup_id": int(e.cgroup),
            "args": None if kind == "unmatched_exit" else [int(x) for x in e.args],
            "ret": int(e.ret) if kind in {"exit", "unmatched_exit"} else None,
            "duration_ns": int(e.duration) if kind == "exit" else None,
            "pid": int(e.pid), "tid": int(e.tid), "uid": int(e.uid),
            "comm": bytes(e.comm).split(b"\0", 1)[0].decode(errors="replace"),
            "monotonic_ns": int(e.ts), "unix_ns": int(e.ts) + offset_ns,
            "timestamp": datetime.fromtimestamp((int(e.ts) + offset_ns) / 1e9, timezone.utc).isoformat(),
            **{key: (int(value) if key in {"step_index", "tool_index"} and value is not None else value)
               for key, value in scope.items() if key != "scope_id"}}


def send_ack(conn, result):
    try:
        conn.sendall((json.dumps(result) + "\n").encode())
    except OSError:
        logging.debug("Kernel telemetry client disconnected before acknowledgement")


def serve(conn, bpf):
    try:
        conn.settimeout(1)
        pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        peer_cgroup = cgroup_for_pid(pid)
        data = b""
        while b"\n" not in data and len(data) < 65536:
            part = conn.recv(4096)
            if not part:
                return
            data += part
        command = json.loads(data.split(b"\n", 1)[0])
        op = command.get("op")
        if op == "ping":
            send_ack(conn, {"ok": DISCOVERY_READY, "node": NODE,
                            "discovery_ready": DISCOVERY_READY, "status": dict(COUNTS)})
        elif op == "register":
            cgid = peer_cgroup
            scope = command.get("scope") or {}
            sid = int(scope.get("scope_id", 0))
            if sid <= 0:
                raise RuntimeError("register requires a nonzero scope ID")
            with ACTIVE_LOCK:
                previous = ACTIVE_CGROUPS.get(cgid)
                owner = OWNERS.get(cgid)
                try:
                    owner_alive_here = (owner is not None and cgroup_for_pid(owner[0]) == cgid and
                                        process_start_time(owner[0]) == owner[1])
                except (OSError, RuntimeError):
                    owner_alive_here = False
                if previous and owner_alive_here:
                    raise RuntimeError("another worker program is already active in this cgroup")
                if owner is not None:
                    OWNERS.pop(cgid, None)
                    ACTIVE_CGROUPS.pop(cgid, None)
                    retire_identity(previous)
                remember_identity(scope)  # identity must exist before the map enables capture
                bpf["active"][ctypes.c_ulonglong(cgid)] = ctypes.c_ulonglong(sid)
                ACTIVE_CGROUPS[cgid] = sid
                OWNERS[cgid] = (pid, process_start_time(pid))
            publish_run_status(scope)
            send_ack(conn, {"ok": True, "cgroup_id": cgid})
        elif op == "recover":
            with ACTIVE_LOCK:
                owner = OWNERS.get(peer_cgroup)
                if owner is not None and owner[0] != pid:
                    raise RuntimeError("cannot recover another process's active cgroup scope")
                if owner is not None:
                    previous = ACTIVE_CGROUPS.pop(peer_cgroup, 0)
                    OWNERS.pop(peer_cgroup, None)
                    bpf["active"].pop(ctypes.c_ulonglong(peer_cgroup), None)
                    retire_identity(previous)
                elif ACTIVE_CGROUPS.get(peer_cgroup):
                    previous = ACTIVE_CGROUPS[peer_cgroup]
                    if identity_for(previous).get("scope_kind") != "inference":
                        ACTIVE_CGROUPS.pop(peer_cgroup, None)
                        bpf["active"].pop(ctypes.c_ulonglong(peer_cgroup), None)
                        retire_identity(previous)
            send_ack(conn, {"ok": True})
        elif op == "scope":
            cgid = int(command["cgroup_id"])
            if cgid != peer_cgroup:
                raise RuntimeError("scope cgroup does not match the socket peer")
            scope = command.get("scope") or {}
            sid = int(scope.get("scope_id", 0))
            with ACTIVE_LOCK:
                if OWNERS.get(cgid) != (pid, process_start_time(pid)):
                    raise RuntimeError("socket peer does not own the registered cgroup scope")
                previous = ACTIVE_CGROUPS.get(cgid, 0)
                if sid:
                    remember_identity(scope)
                    bpf["active"][ctypes.c_ulonglong(cgid)] = ctypes.c_ulonglong(sid)
                    ACTIVE_CGROUPS[cgid] = sid
                else:
                    bpf["active"].pop(ctypes.c_ulonglong(cgid), None)
                    ACTIVE_CGROUPS.pop(cgid, None)
                    OWNERS.pop(cgid, None)
                if previous and previous != sid:
                    retire_identity(previous)
            send_ack(conn, {"ok": True})
        else:
            send_ack(conn, {"ok": False, "error": "unknown operation"})
    except Exception as exc:
        send_ack(conn, {"ok": False, "error": str(exc)})
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve_idle(conn):
    """Answer local health probes without loading BPF or requiring cgroup/kernel access."""
    try:
        conn.settimeout(1)
        data = b""
        while b"\n" not in data and len(data) < 65536:
            part = conn.recv(4096)
            if not part:
                return
            data += part
        command = json.loads(data.split(b"\n", 1)[0])
        send_ack(conn, disabled_response(command.get("op")))
    except Exception as exc:
        send_ack(conn, {"ok": False, "error": str(exc)})
    finally:
        try:
            conn.close()
        except OSError:
            pass


def disabled_response(operation):
    if operation == "ping":
        return {"ok": True, "node": NODE, "status": "idle", "capture_enabled": False,
                "discovery_ready": True, "status_counts": dict(COUNTS)}
    return {"ok": False, "error": "kernel capture is disabled"}


def _listen(handler):
    try:
        os.unlink(NS)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(NS), exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(NS)
    os.chmod(NS, 0o666)
    server.listen(128)
    while True:
        if handler is not None:
            handler[0].perf_buffer_poll(timeout=100)
        server.settimeout(0.01)
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        if handler is None:
            serve_idle(conn)
        else:
            serve(conn, handler[0])


def main():
    if not CAPTURE_ENABLED:
        threading.Thread(target=health_writer, daemon=True).start()
        _listen(None)
        return
    from bcc import BPF
    if not os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        raise RuntimeError("cgroup v2 is required")
    bpf = BPF(text=PROGRAM)
    offset = time.time_ns() - time.monotonic_ns()

    def on_event(cpu, data, size):
        e = bpf["events"].event(data)
        publish(event_record(e, offset))

    def lost(cpu, count):
        COUNTS["perf_lost"] += count

    bpf["events"].open_perf_buffer(on_event, lost_cb=lost, page_cnt=256)
    def refresh_map_drops():
        while True:
            try:
                COUNTS["map_overflow"] = int(bpf["map_drops"][ctypes.c_int(0)].value)
            except Exception:
                pass
            time.sleep(1)
    threading.Thread(target=writer, daemon=True).start()
    threading.Thread(target=health_writer, daemon=True).start()
    threading.Thread(target=refresh_map_drops, daemon=True).start()
    threading.Thread(target=inference_discovery, args=(bpf,), daemon=True).start()
    _listen((bpf,))


if __name__ == "__main__":
    main()
