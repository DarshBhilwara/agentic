"""Framework-neutral, Redis-backed execution ledger for autonomous agents."""

import json
import os
import time
import uuid
import hashlib
from redis.exceptions import WatchError
from datetime import datetime, timezone


LEDGER_VERSION = "1.1"
EVENT_TTL_SECONDS = int(os.getenv("AGENT_TELEMETRY_RETENTION_SECONDS", "2592000"))
MAX_EVENTS_PER_RUN = int(os.getenv("AGENT_TELEMETRY_MAX_EVENTS_PER_RUN", "10000"))


def now():
    return datetime.now(timezone.utc).isoformat()


class TelemetryLedger:
    """Stores an execution ledger independently of any tracing backend.

    Redis lists deliberately keep the dependency footprint small.  The event
    envelope is JSON so another runtime or language can emit the same schema
    through the gateway ingestion endpoint.
    """

    def __init__(self, redis_client):
        self.redis = redis_client

    def start_run(self, run_id, **fields):
        record = {
            "id": run_id,
            "schema_version": LEDGER_VERSION,
            "status": "running",
            "started_at": now(),
            **{key: str(value) for key, value in fields.items() if value not in (None, "")},
        }
        key = f"telemetry:run:{run_id}"
        self.redis.hset(key, mapping=record)
        self.redis.expire(key, EVENT_TTL_SECONDS)
        self.redis.zadd("telemetry:runs", {run_id: time.time()})
        self.redis.zremrangebyscore("telemetry:runs", "-inf", time.time() - EVENT_TTL_SECONDS)
        return record

    def finish_run(self, run_id, status, **fields):
        record = {"status": status, "completed_at": now()}
        record.update({key: str(value) for key, value in fields.items() if value not in (None, "")})
        self.redis.hset(f"telemetry:run:{run_id}", mapping=record)
        self.redis.expire(f"telemetry:run:{run_id}", EVENT_TTL_SECONDS)

    def save_summary(self, run_id, summary):
        self.redis.hset(f"telemetry:run:{run_id}", "summary", json.dumps(summary))
        self.redis.expire(f"telemetry:run:{run_id}", EVENT_TTL_SECONDS)

    def save_step(self, run_id, step):
        key = f"telemetry:run:{run_id}:steps"
        self.redis.hset(key, str(step["step_index"]), json.dumps(step))
        self.redis.expire(key, EVENT_TTL_SECONDS)

    def steps(self, run_id):
        raw = self.redis.hgetall(f"telemetry:run:{run_id}:steps")
        return [json.loads(raw[key]) for key in sorted(raw, key=int)]

    def emit(self, event_type, *, run_id, attributes=None, payload=None, severity="info", **identity):
        event = {
            "id": str(uuid.uuid4()),
            "schema_version": LEDGER_VERSION,
            "timestamp": now(),
            "type": event_type,
            "severity": severity,
            "run.id": run_id,
            **{key: value for key, value in identity.items() if value not in (None, "")},
        }
        if attributes:
            event["attributes"] = attributes
        if payload:
            event["payload"] = payload
        event_key = f"telemetry:run:{run_id}:events"
        encoded = json.dumps(event, default=str, separators=(",", ":"))
        pipe = self.redis.pipeline()
        pipe.rpush(event_key, encoded)
        pipe.ltrim(event_key, -MAX_EVENTS_PER_RUN, -1)
        pipe.expire(event_key, EVENT_TTL_SECONDS)
        pipe.execute()
        self._materialize_graph(run_id, event_type, attributes or {}, payload or {}, identity.get("user.id"))
        try:
            from telemetry import export_ledger_event
            terminal = event_type in {"task.completed", "task.failed", "task.timeout"}
            export_ledger_event(
                event,
                procedural_graph=self.graph(run_id) if terminal else None,
                summary=(self.run(run_id) or {}).get("summary") if terminal else None,
            )
        except Exception:
            # Export outages must not interrupt task execution or durable writes.
            pass
        return event

    def _materialize_graph(self, run_id, event_type, attributes, payload, owner=None):
        """Materialize observed execution topology independently of the bounded event tail."""
        if not event_type.startswith("procedural_graph."):
            return
        owner = owner or self.run(run_id).get("user.id", "unknown")
        key = f"telemetry:run:{run_id}:graph"
        graph = self.redis.hgetall(key)
        nodes = json.loads(graph.get(b"nodes", b"[]"))
        edges = json.loads(graph.get(b"edges", b"[]"))
        node = attributes.get("node")
        previous = attributes.get("previous_node")
        if node and node not in nodes:
            nodes.append(node)
        if previous and previous not in nodes:
            nodes.append(previous)
        if previous and node:
            edge = [previous, node]
            if edge not in edges:
                edges.append(edge)
        action_path = json.loads(graph.get(b"action_path", b"[]"))
        action = attributes.get("action")
        if action:
            action_path.append(action)
        signature = hashlib.sha256("|".join(action_path).encode()).hexdigest() if action_path else None
        mapping = {"nodes": json.dumps(nodes), "edges": json.dumps(edges),
                   "action_path": json.dumps(action_path), "signature": signature or "", "owner": owner}
        if attributes.get("validation"):
            mapping["validation"] = json.dumps(attributes["validation"])
        # WATCH/MULTI retries ensure concurrent tool/model events cannot lose edges.
        committed = False
        for _ in range(5):
            pipe = self.redis.pipeline()
            try:
                pipe.watch(key)
                current = pipe.hgetall(key)
                if current:
                    nodes = json.loads(current.get(b"nodes", b"[]"))
                    edges = json.loads(current.get(b"edges", b"[]"))
                    action_path = json.loads(current.get(b"action_path", b"[]"))
                    if node and node not in nodes: nodes.append(node)
                    if previous and previous not in nodes: nodes.append(previous)
                    if previous and node and [previous, node] not in edges: edges.append([previous, node])
                    if action: action_path.append(action)
                    signature = hashlib.sha256("|".join(action_path).encode()).hexdigest() if action_path else None
                    mapping.update({"nodes": json.dumps(nodes), "edges": json.dumps(edges),
                                    "action_path": json.dumps(action_path), "signature": signature or ""})
                pipe.multi(); pipe.hset(key, mapping=mapping); pipe.expire(key, EVENT_TTL_SECONDS); pipe.execute(); committed = True; break
            except WatchError:
                continue
            except Exception:
                self.redis.pipeline().reset()
                return
            finally:
                pipe.reset()
        if committed and signature:
            index = f"telemetry:procedures:{owner}:{signature}"
            self.redis.sadd(index, run_id)
            self.redis.expire(index, EVENT_TTL_SECONDS)
            # Reusable canonical subgraphs are bounded action windows, so a
            # long run does not become one monolithic procedure definition.
            for width in (3, 4, 5):
                for start in range(max(0, len(action_path) - width + 1)):
                    pattern = action_path[start:start + width]
                    subgraph_id = hashlib.sha256("|".join(pattern).encode()).hexdigest()
                    subkey = f"telemetry:subgraph:{owner}:{subgraph_id}"
                    self.redis.hset(subkey, mapping={"id": subgraph_id,
                        "actions": json.dumps(pattern), "version": "1"})
                    self.redis.sadd(f"{subkey}:occurrences", run_id)
                    self.redis.expire(subkey, EVENT_TTL_SECONDS)
                    self.redis.expire(f"{subkey}:occurrences", EVENT_TTL_SECONDS)

    def graph(self, run_id):
        raw = self.redis.hgetall(f"telemetry:run:{run_id}:graph")
        if not raw:
            return None
        result = {k.decode(): v.decode() for k, v in raw.items()}
        for key in ("nodes", "edges", "action_path", "validation"):
            if key in result:
                result[key] = json.loads(result[key])
        signature = result.get("signature")
        owner = result.get("owner", "")
        result["procedure_runs"] = sorted(x.decode() for x in self.redis.smembers(
            f"telemetry:procedures:{owner}:{signature}")) if signature and owner else []
        subgraphs = []
        actions = result.get("action_path", [])
        for width in (3, 4, 5):
            for start in range(max(0, len(actions) - width + 1)):
                pattern = actions[start:start + width]
                sid = hashlib.sha256("|".join(pattern).encode()).hexdigest()
                raw_def = self.redis.hgetall(f"telemetry:subgraph:{owner}:{sid}")
                if raw_def:
                    subgraphs.append({"id": sid, "actions": pattern, "version": "1",
                                      "occurrence_run_ids": sorted(x.decode() for x in self.redis.smembers(
                                          f"telemetry:subgraph:{owner}:{sid}:occurrences"))})
        result["subgraphs"] = {item["id"]: item for item in subgraphs}
        return result

    def link_delegation(self, parent_run_id, child_run_id, owner, handoff_id):
        """Persist reciprocal cross-run causal graph linkage before child execution."""
        parent_key = f"telemetry:run:{parent_run_id}:graph"
        child_key = f"telemetry:run:{child_run_id}:graph"
        pipe = self.redis.pipeline()
        for key, source, target in ((parent_key, f"run:{parent_run_id}", f"run:{child_run_id}"),
                                    (child_key, f"run:{parent_run_id}", f"run:{child_run_id}")):
            raw = self.redis.hgetall(key)
            nodes = json.loads(raw.get(b"nodes", b"[]")); edges = json.loads(raw.get(b"edges", b"[]"))
            for item in (source, target):
                if item not in nodes: nodes.append(item)
            edge = {"from": source, "to": target, "type": "delegation", "handoff_id": handoff_id}
            if edge not in edges: edges.append(edge)
            pipe.hset(key, mapping={"owner": owner, "nodes": json.dumps(nodes), "edges": json.dumps(edges),
                                    "action_path": json.dumps(json.loads(raw.get(b"action_path", b"[]")))})
            pipe.expire(key, EVENT_TTL_SECONDS)
        pipe.execute()

    def run(self, run_id):
        raw = self.redis.hgetall(f"telemetry:run:{run_id}")
        if not raw:
            return None
        result = {key.decode(): value.decode() for key, value in raw.items()}
        if "summary" in result:
            result["summary"] = json.loads(result["summary"])
        return result

    def events(self, run_id, offset=0, limit=200):
        offset, limit = max(0, offset), min(max(1, limit), 1000)
        raw = self.redis.lrange(f"telemetry:run:{run_id}:events", offset, offset + limit - 1)
        return [json.loads(item) for item in raw]

    def list_runs(self, *, user=None, session_id=None, task_id=None, agent_id=None, limit=100):
        ids = self.redis.zrevrange("telemetry:runs", 0, min(max(1, limit) * 10, 10000) - 1)
        matched = []
        filters = {"user.id": user, "session.id": session_id, "task.id": task_id, "agent.id": agent_id}
        for raw_id in ids:
            run = self.run(raw_id.decode())
            if run and all(not value or run.get(key) == value for key, value in filters.items()):
                matched.append(run)
            if len(matched) >= limit:
                break
        return matched
