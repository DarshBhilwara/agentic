"""Framework-neutral, Redis-backed execution ledger for autonomous agents."""

import json
import os
import time
import uuid
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
        return event

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
