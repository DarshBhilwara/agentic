"""Process-local capture gates; mandatory run ownership and orchestration stay enabled."""
import os


SEMANTIC_EVENT_PREFIXES = ("procedural_graph.", "langgraph.")
SEMANTIC_EVENT_TYPES = {"agent.phase.transition", "agent.handoff.queued", "agent.handoff.checked", "agent.delegated"}
PROGRAM_EVENT_TYPES = {"step.started", "step.completed"}
LIFECYCLE_EVENT_TYPES = {"task.queued", "task.started", "task.completed", "task.failed", "task.timeout"}


def enabled(name, default=True):
    return os.getenv(name, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}


def program_capture_enabled():
    return enabled("AGENT_PROGRAM_TELEMETRY_ENABLED")


def semantic_capture_enabled():
    return enabled("AGENT_SEMANTIC_TELEMETRY_ENABLED")


def is_semantic_event(event_type):
    return event_type.startswith(SEMANTIC_EVENT_PREFIXES) or event_type in SEMANTIC_EVENT_TYPES


def detailed_capture_enabled():
    return program_capture_enabled() or semantic_capture_enabled()


def is_lifecycle_event(event_type):
    return event_type in LIFECYCLE_EVENT_TYPES
