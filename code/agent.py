import json
import math
import os
import re
import subprocess
import signal
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Dict, List, Literal, TypedDict
import requests
from langgraph.graph import END, START, StateGraph
from openai import OpenAI, APITimeoutError
from telemetry import (event_counter, handoff_counter, inference_counter, inject_context,
                       graph_snapshot_json, intent_span, safe_content, safe_payload, span, trace_identity,
                       tool_calls as tool_call_metric, tool_duration, tool_wait)
from program_telemetry import ProgramTelemetry, TaskTimeout, StepLimitExceeded, collect_engine_metrics
from telemetry_ledger import TelemetryLedger

MODEL_NAME = os.getenv("AGENT_MODEL_NAME", "qwen")
DEFAULT_VLLM_URL = "http://vllm-service:8000/v1"


def vllm_url() -> str:
    """Return the configured OpenAI-compatible inference endpoint.

    ``AGENT_VLLM_URL_FILE`` supports a mounted Kubernetes ConfigMap and is
    read for every agent run. ``AGENT_VLLM_URL`` preserves the existing
    deployment and local-development configuration.
    """
    config_path = os.getenv("AGENT_VLLM_URL_FILE")
    if config_path:
        try:
            with open(config_path, encoding="utf-8") as config_file:
                configured = config_file.read().strip()
        except OSError:
            configured = ""
        if configured:
            return configured
    return os.getenv("AGENT_VLLM_URL", DEFAULT_VLLM_URL)
SYSTEM_PROMPT = """You are the enterprise agent running in the agent node.f
You help with research, information lookup, file and workspace management, and
general task execution using the tools available to you.

The current user workspace is: {workspace}

Operational rules:
1. Use a tool as much as possible whenever you need information or an effect you do not already have or something that can be better done by tools.
2. Never fabricate tool output.
3. Prefer read-only tools when they can accomplish the task.
4. Keep changes inside the current user workspace unless the task explicitly requires another path.
5. When the task is complete, give a concise plain-language summary.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delegate_to_agent",
            "description": "Delegate an independent subtask to another agent. Use for work that can continue separately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "agent_name": {"type": "string", "default": "subagent"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_delegated_task",
            "description": "Check the status and result of a previously delegated agent task.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Execute a shell command in the current user workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file relative to the current user workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file relative to the current user workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories relative to the current user workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information using Serper.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
]


RUNTIME_CONTEXT = ContextVar("agent_runtime_context", default=None)


def _emit(event_type, *, attributes=None, payload=None, severity="info", **extra):
    context = RUNTIME_CONTEXT.get()
    if not context or not context.get("event_sink"):
        return None
    event_counter.add(1, {"event.type": event_type})
    return context["event_sink"](
        event_type,
        attributes={"program_id": context["task_id"], **(attributes or {})},
        payload=payload,
        severity=severity,
        **{"root.run.id": context.get("root_run_id", context.get("run_id")),
           "parent.task.id": context.get("parent_task_id", ""),
           "parent.run.id": context.get("parent_run_id", context.get("parent_agent_run_id", ""))},
        **trace_identity(),
        **extra,
    )

# BFCL calculator tools. These are deliberately deterministic and local: they
# give the model real structured tools to call while keeping the benchmark
# independent of external services.
TOOLS += [
    {
        "type": "function",
        "function": {
            "name": "calc_binomial_probability",
            "description": "Calculate the probability of exactly k successes in n independent trials with success probability p.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "Number of trials."},
                    "k": {"type": "integer", "description": "Number of successes."},
                    "p": {"type": "number", "description": "Probability of success, from 0 to 1."},
                },
                "required": ["n", "k", "p"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_cosine_similarity",
            "description": "Calculate the cosine similarity of two numeric vectors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vectorA": {"type": "array", "items": {"type": "number"}},
                    "vectorB": {"type": "array", "items": {"type": "number"}},
                },
                "required": ["vectorA", "vectorB"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_density",
            "description": "Calculate density from mass in kilograms and volume in cubic meters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mass": {"type": "number"},
                    "volume": {"type": "number"},
                },
                "required": ["mass", "volume"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_displacement",
            "description": "Calculate displacement using constant acceleration: s = ut + 0.5*a*t^2.",
            "parameters": {
                "type": "object",
                "properties": {
                    "initial_velocity": {"type": "number"},
                    "acceleration": {"type": "number"},
                    "time": {"type": "number"},
                },
                "required": ["initial_velocity", "acceleration", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_electrostatic_potential_energy",
            "description": "Calculate electrostatic potential energy from charge and voltage: U = qV.",
            "parameters": {
                "type": "object",
                "properties": {
                    "charge": {"type": "number"},
                    "voltage": {"type": "number"},
                },
                "required": ["charge", "voltage"],
            },
        },
    },
]


def _path(path: str, workspace: str) -> str:
    """Resolve a tool path while keeping it inside the user's workspace."""
    candidate = os.path.realpath(path if os.path.isabs(path) else os.path.join(workspace, path))
    root = os.path.realpath(workspace)
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError("path escapes the current user workspace")
    return candidate


def workspace_has_changes(workspace: str) -> bool:
    """Return whether a Git workspace has tracked or untracked changes."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={workspace}", "-C", workspace,
         "status", "--porcelain"],
        text=True, capture_output=True, timeout=30,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"Could not inspect workspace changes: {detail}")
    return bool(result.stdout.strip())


def execute_command(args: Dict, workspace: str) -> str:
    command = (args or {}).get("command", "")
    if not command:
        return "Error: no command provided."
    # The command runs from this user's directory. Tool callers must still
    # follow the workspace rule; file tools enforce it independently below.
    process = subprocess.Popen(command, shell=True, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=workspace, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=float(os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "120")))
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    output = ""
    if result.stdout:
        output += f"STDOUT:\n{result.stdout}\n"
    if result.stderr:
        output += f"STDERR:\n{result.stderr}\n"
    if result.returncode:
        raise RuntimeError(f"Command exited {result.returncode}:\n{output[:20000]}")
    return (output or "Executed successfully with no stdout/stderr.\n")[:20000] + f"Exit code: {result.returncode}"


def read_file(args: Dict, workspace: str) -> str:
    path = (args or {}).get("path", "")
    if not path:
        return "Error: no path provided."
    try:
        with open(_path(path, workspace), "r", errors="replace") as file:
            content = file.read()
        return (content[:20000] + ("\n... (truncated)" if len(content) > 20000 else "")) or "(file is empty)"
    except TaskTimeout:
        raise
    except Exception as exc:
        return f"Error reading file: {exc}"


def write_file(args: Dict, workspace: str) -> str:
    path, content = (args or {}).get("path", ""), (args or {}).get("content", "")
    if not path:
        return "Error: no path provided."
    try:
        full_path = _path(path, workspace)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as file:
            file.write(content)
        return f"Wrote {len(content)} bytes to {full_path}"
    except TaskTimeout:
        raise
    except Exception as exc:
        return f"Error writing file: {exc}"


def list_directory(args: Dict, workspace: str) -> str:
    try:
        entries = sorted(os.listdir(_path((args or {}).get("path", ".") or ".", workspace)))
        return "\n".join(entries) if entries else "(empty directory)"
    except TaskTimeout:
        raise
    except Exception as exc:
        return f"Error listing directory: {exc}"


def web_search(args: Dict, _workspace: str) -> str:
    query, max_results = (args or {}).get("query", ""), (args or {}).get("max_results", 5)
    api_key = os.getenv("SERPER_API_KEY")
    if not query:
        return "Error: no query provided."
    if not api_key:
        return "Error: web_search is not configured on the agent node."
    try:
        response = requests.post("https://google.serper.dev/search", headers={"X-API-KEY": api_key, "Content-Type": "application/json"}, json={"q": query, "num": max_results}, timeout=15)
        response.raise_for_status()
        results = response.json().get("organic", [])[:max_results]
        return "\n".join(f"- {item.get('title')} ({item.get('link')})\n  {(item.get('snippet') or '')[:300]}" for item in results) or "No search results found."
    except requests.RequestException as exc:
        return f"Error performing web search: {exc}"


def _number(args: Dict, name: str) -> float:
    value = (args or {}).get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def calc_binomial_probability(args: Dict, _workspace: str) -> str:
    n, k = (args or {}).get("n"), (args or {}).get("k")
    p = _number(args, "p")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError("n must be a non-negative integer")
    if isinstance(k, bool) or not isinstance(k, int) or k < 0 or k > n:
        raise ValueError("k must be an integer between 0 and n")
    if not 0 <= p <= 1:
        raise ValueError("p must be between 0 and 1")
    probability = math.comb(n, k) * (p ** k) * ((1 - p) ** (n - k))
    return json.dumps({"probability": probability})


def calculate_cosine_similarity(args: Dict, _workspace: str) -> str:
    vector_a = (args or {}).get("vectorA")
    vector_b = (args or {}).get("vectorB")
    if not isinstance(vector_a, list) or not isinstance(vector_b, list):
        raise ValueError("vectorA and vectorB must be arrays")
    if len(vector_a) != len(vector_b) or not vector_a:
        raise ValueError("vectors must be non-empty and have equal length")
    a = [_number({"value": value}, "value") for value in vector_a]
    b = [_number({"value": value}, "value") for value in vector_b]
    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    if norm_a == 0 or norm_b == 0:
        raise ValueError("cosine similarity is undefined for a zero vector")
    similarity = sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)
    return json.dumps({"cosine_similarity": similarity})


def calculate_density(args: Dict, _workspace: str) -> str:
    mass = _number(args, "mass")
    volume = _number(args, "volume")
    if volume == 0:
        raise ValueError("volume must not be zero")
    return json.dumps({"density": mass / volume})


def calculate_displacement(args: Dict, _workspace: str) -> str:
    initial_velocity = _number(args, "initial_velocity")
    acceleration = _number(args, "acceleration")
    elapsed_time = _number(args, "time")
    return json.dumps({"displacement": initial_velocity * elapsed_time + 0.5 * acceleration * elapsed_time ** 2})


def calculate_electrostatic_potential_energy(args: Dict, _workspace: str) -> str:
    charge = _number(args, "charge")
    voltage = _number(args, "voltage")
    return json.dumps({"electrostatic_potential_energy": charge * voltage})


def delegate_to_agent(args: Dict, _workspace: str) -> str:
    """Create a child task without requiring a predefined orchestration graph."""
    context = RUNTIME_CONTEXT.get()
    task = (args or {}).get("task", "").strip()
    name = (args or {}).get("agent_name", "subagent").strip()[:64] or "subagent"
    if not task:
        return "Error: no delegated task provided."
    if not context or not context.get("redis"):
        return "Error: delegation is unavailable outside the queued agent runtime."
    child_task_id = str(uuid.uuid4())
    child_agent_id = str(uuid.uuid4())
    child_agent_run_id = str(uuid.uuid4())
    child_run_id = str(uuid.uuid4())
    handoff_id = str(uuid.uuid4())
    redis_client = context["redis"]
    TelemetryLedger(redis_client).link_delegation(context["run_id"], child_run_id,
                                                   context["user"], handoff_id)
    redis_client.hset(f"task:{child_task_id}", mapping={
        "id": child_task_id,
        "user": context["user"],
        "prompt": task,
        "status": "pending",
        "session_id": context["session_id"],
        "agent_id": child_agent_id,
        "agent_name": name,
        "agent_run_id": child_agent_run_id,
        "parent_agent_run_id": context["agent_run_id"],
        "parent_task_id": context["task_id"],
        "parent_run_id": context["run_id"],
        "handoff_id": handoff_id,
        "run_id": child_run_id,
        "root_run_id": context["root_run_id"],
        "workspace": context["workspace"],
        "turn_id": str(uuid.uuid4()),
        "turn_number": context["turn_number"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "submitted_at_unix": time.time(),
        "timeout_seconds": context.get("timeout_seconds", 1800),
        "max_steps": context.get("max_steps", 100),
        "trace_context": json.dumps(inject_context()),
    })
    pipe = redis_client.pipeline()
    pipe.zadd("task_deadlines", {child_task_id: time.time() + context.get("timeout_seconds", 1800)})
    pipe.lpush("task_queue", child_task_id)
    pipe.execute()
    handoff_counter.add(1, {"agent.name": name})
    _emit(
        "agent.delegated",
        attributes={"handoff.to": child_agent_id, "agent.target.name": name},
        payload={"task": safe_content(task), "child_task_id": child_task_id},
        **{"handoff.id": handoff_id, "child.run.id": child_run_id,
           "child.agent.run.id": child_agent_run_id},
    )
    _emit("procedural_graph.observed", attributes={
        "node": f"delegation:{child_run_id}",
        "previous_node": f"run:{context['run_id']}",
        "action": "delegation.handoff"},
        payload={"parent_run_id": context["run_id"], "child_run_id": child_run_id,
                 "handoff_id": handoff_id})
    _emit("agent.handoff.queued", attributes={"handoff.id": handoff_id,
          "child.task.id": child_task_id, "child.run.id": child_run_id},
          payload={"parent_task_id": context["task_id"], "parent_run_id": context["run_id"]})
    return json.dumps({"task_id": child_task_id, "agent_id": child_agent_id, "agent_name": name, "status": "queued"})


def check_delegated_task(args: Dict, _workspace: str) -> str:
    context = RUNTIME_CONTEXT.get()
    task_id = (args or {}).get("task_id", "")
    if not context or not context.get("redis"):
        return "Error: delegated task lookup is unavailable outside the queued agent runtime."
    task = context["redis"].hgetall(f"task:{task_id}")
    if not task or task.get(b"user", b"").decode() != context["user"]:
        return "Error: delegated task not found."
    result = {key.decode(): value.decode() for key, value in task.items()}
    _emit("agent.handoff.checked", attributes={
        "handoff.id": result.get("handoff_id", ""), "child.task.id": task_id,
        "child.run.id": result.get("run_id", ""), "child.status": result.get("status", "")})
    return json.dumps({key: result.get(key, "") for key in ("id", "status", "result", "error", "agent_id", "agent_name")})


IMPLEMENTATIONS = {
    "execute_command": execute_command,
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
    "web_search": web_search,
    "calc_binomial_probability": calc_binomial_probability,
    "calculate_cosine_similarity": calculate_cosine_similarity,
    "calculate_density": calculate_density,
    "calculate_displacement": calculate_displacement,
    "calculate_electrostatic_potential_energy": calculate_electrostatic_potential_energy,
    "delegate_to_agent": delegate_to_agent,
    "check_delegated_task": check_delegated_task,
}

TOOL_INTENT = {
    "execute_command": ("workspace_command_execution", "execute_workspace_command"),
    "read_file": ("workspace_file_read", "read_workspace_file"),
    "write_file": ("workspace_file_write", "write_workspace_file"),
    "list_directory": ("workspace_directory_inspection", "inspect_workspace_directory"),
    "web_search": ("current_information_retrieval", "retrieve_current_information"),
    "calc_binomial_probability": ("probability_calculation", "calculate_binomial_probability"),
    "calculate_cosine_similarity": ("vector_calculation", "calculate_vector_similarity"),
    "calculate_density": ("physics_calculation", "calculate_density"),
    "calculate_displacement": ("physics_calculation", "calculate_displacement"),
    "calculate_electrostatic_potential_energy": (
        "physics_calculation", "calculate_electrostatic_potential_energy"
    ),
    "delegate_to_agent": ("agent_delegation", "delegate_independent_subtask"),
    "check_delegated_task": ("agent_coordination", "check_delegated_task"),
}


def tool_intent(tool_name):
    return TOOL_INTENT.get(
        tool_name, ("unsupported_tool_action", "unknown_tool_requested")
    )


def intent_decision(tool_names):
    if not tool_names:
        return {
            "decision": "respond_directly",
            "tool_required": False,
            "action": "direct_response",
            "reason_code": "no_tool_call_selected",
            "tool_name": "",
            "tool_names": [],
            "tool_actions": [],
        }

    primary_tool = tool_names[0]
    action, reason_code = tool_intent(primary_tool)
    return {
        "decision": "use_tool",
        "tool_required": True,
        "action": action,
        "reason_code": reason_code,
        "tool_name": primary_tool,
        "tool_names": tool_names,
        "tool_actions": [tool_intent(name)[0] for name in tool_names],
    }


class AgentGraphState(TypedDict):
    """Explicit state passed between the model and tool graph nodes."""

    messages: List[Dict]
    step_index: int
    tool_calls: List[Dict[str, str]]
    route: Literal["model", "tools", "end"]
    previous_node: str
    result: str


def _build_agent_graph(*, client, workspace, tools, measurements, redis_client,
                       max_steps, require_workspace_changes, turn_span):
    """Build the model -> tools -> model LangGraph workflow for one agent run.

    Runtime dependencies stay in this closure rather than graph state, keeping
    the state explicit and serializable for a future durable checkpointer.
    """
    active_step = {"manager": None, "record": None}
    rejected_completions = {"count": 0}

    def close_step(exc_info=(None, None, None)):
        manager = active_step["manager"]
        if manager is not None:
            active_step["manager"] = None
            active_step["record"] = None
            manager.__exit__(*exc_info)

    def call_model(state: AgentGraphState):
        step_index = state["step_index"] + 1
        if step_index > max_steps:
            raise StepLimitExceeded(f"Agent reached the limit of {max_steps} inference steps")
        manager = measurements.step(step_index)
        record = manager.__enter__()
        active_step["manager"] = manager
        active_step["record"] = record
        request_id = f"{measurements.program_id}.{step_index}.{uuid.uuid4().hex}"
        record.request_id = "chatcmpl-" + request_id
        # Canonical procedure/subgraph IDs are materialized from observed
        # action windows by the ledger; do not stamp a constant topology ID.
        record.procedure_id = None
        record.subgraph_id = None
        previous_node = state.get("previous_node", "START")
        _emit("procedural_graph.observed", attributes={
            "node": f"model:{step_index}", "previous_node": previous_node,
            "action": "model.request", "validation": {"allowed": ["model->tools", "model->model", "model->END"]}})
        _emit("agent.phase.transition", attributes={"program_id": measurements.program_id,
              "step_index": step_index, "phase": "reasoning", "phase_status": "started",
              "procedure.id": record.procedure_id, "subgraph.id": record.subgraph_id})
        attrs = {"program_id": measurements.program_id, "step_index": step_index,
                 "agent.step.number": step_index, "request_id": record.request_id}
        node_span = span("langgraph.node.model", **{"openinference.span.kind": "CHAIN", **attrs})
        node_span.__enter__()
        _emit("langgraph.node.started", attributes={**attrs, "graph.node": "model"})
        node_exc_info = (None, None, None)
        try:
            with span("agent.inference", model=MODEL_NAME,
                      **{"openinference.span.kind": "LLM",
                         "llm.model_name": MODEL_NAME, **attrs}) as inference_span:
                inference_counter.add(1, {"model": MODEL_NAME})
                request_messages = state["messages"]
                _emit("model.requested", attributes=attrs,
                      payload={"messages": safe_payload(request_messages)})
                if os.getenv("AGENT_TELEMETRY_CONTENT_CAPTURE", "redacted").lower() == "full":
                    inference_span.add_event("gen_ai.request.messages", {
                        "messages": json.dumps(request_messages, default=str, separators=(",", ":"))
                    })
                started = time.perf_counter()
                try:
                    response = client.chat.completions.create(
                        model=MODEL_NAME, messages=state["messages"], tools=tools or None,
                        tool_choice="auto" if tools else None, max_tokens=4096, temperature=0.1,
                        extra_headers={**inject_context(), "X-Request-Id": request_id})
                except APITimeoutError as exc:
                    _emit("model.failed", attributes={**attrs, "error.type": type(exc).__name__}, severity="error")
                    raise TaskTimeout("Inference request timed out") from exc
                except Exception as exc:
                    _emit("model.failed", attributes={**attrs, "error.type": type(exc).__name__}, severity="error")
                    raise
                finally:
                    record.llm_inference_time_ms = (time.perf_counter() - started) * 1000
                usage = response.usage
                record.prompt_tokens = getattr(usage, "prompt_tokens", None)
                record.completion_tokens = getattr(usage, "completion_tokens", None)
                if record.prompt_tokens is not None and record.completion_tokens is not None:
                    record.context_tokens = record.prompt_tokens + record.completion_tokens
                # Phoenix/OpenInference uses these stable attributes for its
                # native LLM view. Keep them bounded numeric values; full
                # messages remain span events in research capture mode.
                inference_span.set_attribute("llm.token_count.prompt", record.prompt_tokens)
                inference_span.set_attribute("llm.token_count.completion", record.completion_tokens)
                inference_span.set_attribute("llm.token_count.total", record.context_tokens)
                inference_span.set_attribute("gen_ai.usage.input_tokens", record.prompt_tokens)
                inference_span.set_attribute("gen_ai.usage.output_tokens", record.completion_tokens)
                inference_span.set_attribute("gen_ai.usage.total_tokens", record.context_tokens)
                collect_engine_metrics(redis_client, record.request_id, record)
                message = response.choices[0].message
                calls = message.tool_calls or []
                tool_calls = [{"id": call.id, "name": call.function.name,
                               "arguments": call.function.arguments} for call in calls]
                decision = intent_decision([call["name"] for call in tool_calls])
                for key, value in decision.items():
                    value = json.dumps(value) if isinstance(value, list) else value
                    inference_span.set_attribute("agent.intent." + key, value)
                    turn_span.set_attribute("agent.intent." + key, value)
                _emit("model.completed", attributes={**attrs,
                      "llm_inference_time_ms": record.llm_inference_time_ms,
                      "context_tokens": record.context_tokens,
                      "finish_reason": response.choices[0].finish_reason},
                      payload={"response": safe_content(message.content or "")})
                if os.getenv("AGENT_TELEMETRY_CONTENT_CAPTURE", "redacted").lower() == "full":
                    inference_span.add_event("gen_ai.response.message", {
                        "message": json.dumps(message.model_dump(exclude_none=True), default=str,
                                               separators=(",", ":"))
                    })

            messages = [*state["messages"], message.model_dump(exclude_none=True)]
            if response.choices[0].finish_reason == "length":
                raise RuntimeError("Model response truncated by token limit")
            if calls:
                _emit("procedural_graph.observed", attributes={"node": f"tools:{step_index}",
                      "previous_node": f"model:{step_index}", "action": "tool.dispatch"})
                record.actual_transition = "model->tools"
                record.expected_transition = "model->tools"
                record.constraint_violation = False
                _emit("agent.phase.transition", attributes={"program_id": record.program_id,
                      "step_index": step_index, "phase": "reasoning", "phase_status": "ended"})
                return {"messages": messages, "step_index": step_index,
                        "tool_calls": tool_calls, "route": "tools"}
            if require_workspace_changes and not workspace_has_changes(workspace):
                rejected_completions["count"] += 1
                record.actual_transition = "model->model"
                record.expected_transition = "model->end"
                record.constraint_violation = True
                _emit("agent.phase.transition", attributes={"program_id": record.program_id,
                      "step_index": step_index, "phase": "reasoning", "phase_status": "ended",
                      "constraint_violation": True})
                _emit("agent.completion.rejected", attributes={
                    **attrs, "reason": "workspace_unchanged",
                    "rejected_completions": rejected_completions["count"],
                }, severity="error")
                close_step()
                if rejected_completions["count"] >= 2:
                    raise RuntimeError("Coding task completed without making workspace changes")
                messages.append({
                    "role": "user",
                    "content": (
                        "No workspace changes were detected, so the coding task is not complete. "
                        "Continue inspecting the repository and make the required changes. Do not "
                        "describe changes unless you actually apply them."
                    ),
                })
                return {"messages": messages, "step_index": step_index,
                        "tool_calls": [], "route": "model",
                        "previous_node": f"model:{step_index}"}
            result = message.content or ""
            _emit("procedural_graph.observed", attributes={"node": "END",
                  "previous_node": f"model:{step_index}", "action": "terminal.success"})
            record.actual_transition = "model->end"
            record.expected_transition = "model->end"
            record.constraint_violation = False
            _emit("agent.phase.transition", attributes={"program_id": record.program_id,
                  "step_index": step_index, "phase": "reasoning", "phase_status": "ended"})
            _emit("agent.completed", attributes=attrs)
            close_step()
            return {"messages": messages, "step_index": step_index,
                    "tool_calls": [], "result": result, "route": "end"}
        except BaseException as exc:
            node_exc_info = sys.exc_info()
            _emit("langgraph.node.error", attributes={**attrs, "graph.node": "model",
                  "error.type": type(exc).__name__}, severity="error")
            close_step(sys.exc_info())
            raise
        finally:
            _emit("langgraph.node.ended", attributes={**attrs, "graph.node": "model",
                  "status": "terminal"})
            node_span.__exit__(*node_exc_info)

    def execute_tools(state: AgentGraphState):
        record = active_step["record"]
        if record is None:
            raise RuntimeError("LangGraph tools node reached without an active model step")
        messages = list(state["messages"])
        acting_started = time.perf_counter()
        _emit("agent.phase.transition", attributes={"program_id": record.program_id,
              "step_index": record.step_index, "phase": "tool_wait", "phase_status": "started"})
        node_attrs = {"graph.node": "tools", "step_index": state["step_index"],
                      "tool_count": len(state["tool_calls"])}
        try:
          with span("langgraph.node.tools", **{"openinference.span.kind": "CHAIN", **node_attrs}):
            _emit("langgraph.node.started", attributes=node_attrs)
            try:
              for tool_index, call in enumerate(state["tool_calls"], 1):
                  _emit("procedural_graph.observed", attributes={
                        "node": f"tool:{call['name']}:{record.step_index}:{tool_index}",
                        "previous_node": f"tools:{record.step_index}",
                        "action": f"tool.invoke:{call['name']}"})
                  _emit("agent.phase.transition", attributes={"program_id": record.program_id,
                        "step_index": record.step_index, "phase": "tool_running",
                        "tool_name": call["name"], "phase_status": "started"})
                  result = _execute_tool(call, workspace, record, tools, tool_index=tool_index,
                                         step_acting_started=acting_started)
                  messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            except BaseException as exc:
              _emit("langgraph.node.error", attributes={**node_attrs, "error.type": type(exc).__name__}, severity="error")
              raise
            finally:
              _emit("langgraph.node.ended", attributes={**node_attrs, "status": "terminal"})
        except BaseException:
            close_step(sys.exc_info())
            raise
        record.step_t_acting_ms = (time.perf_counter() - acting_started) * 1000
        _emit("agent.phase.transition", attributes={"program_id": record.program_id,
              "step_index": record.step_index, "phase": "tool_wait", "phase_status": "ended",
              "acting_time_ms": record.step_t_acting_ms,
              "idleness_ratio": record.step_t_acting_ms / max(record.step_t_acting_ms + record.llm_inference_time_ms, 1)})
        close_step()
        return {"messages": messages, "tool_calls": [], "route": "model",
                "previous_node": f"tools:{state['step_index']}"}

    def route_after_model(state: AgentGraphState):
        return state["route"]

    graph = StateGraph(AgentGraphState)
    graph.add_node("model", call_model)
    graph.add_node("tools", execute_tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route_after_model,
                                {"model": "model", "tools": "tools", "end": END})
    graph.add_edge("tools", "model")
    return graph.compile(), close_step


def run(prompt: str, user: str, *, workspace=None, session_id="standalone", agent_id="standalone",
        turn_id="standalone", turn_number=1, conversation=None, benchmark=None,
        case_id=None, task_id="standalone", run_id=None, root_run_id=None,
        agent_run_id=None, parent_agent_run_id=None, parent_task_id=None, parent_run_id=None,
        handoff_id=None,
        agent_name="enterprise-agent",
        event_sink=None, redis_client=None, measurements=None, max_steps=100,
        timeout_seconds=1800, enabled_tools=None, require_workspace_changes=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", user):
        raise ValueError("invalid user identity")
    workspace = workspace or os.path.join("/workspace/users", user)
    workspace = os.path.realpath(workspace)
    if not os.path.isdir(workspace):
        raise ValueError(f"workspace does not exist: {workspace}")
    client = OpenAI(base_url=vllm_url(), api_key="EMPTY", max_retries=0,
                    timeout=float(os.getenv("AGENT_INFERENCE_TIMEOUT_SECONDS", "300")))
    run_id = run_id or str(uuid.uuid4())
    agent_run_id = agent_run_id or str(uuid.uuid4())
    context_token = RUNTIME_CONTEXT.set({
        "user": user, "workspace": workspace, "session_id": session_id,
        "task_id": task_id, "run_id": run_id, "root_run_id": root_run_id or run_id,
        "agent_id": agent_id, "agent_run_id": agent_run_id,
        "parent_task_id": parent_task_id, "parent_run_id": parent_run_id or parent_agent_run_id,
        "handoff_id": handoff_id or "",
        "turn_number": turn_number, "event_sink": event_sink, "redis": redis_client,
        "timeout_seconds": timeout_seconds, "max_steps": max_steps,
    })
    measurements = measurements or ProgramTelemetry(task_id if task_id != "standalone" else run_id)
    tools = [tool for tool in TOOLS if enabled_tools is None or tool["function"]["name"] in enabled_tools]
    messages: List[Dict] = list(conversation) if conversation else [{"role": "system", "content": SYSTEM_PROMPT.format(workspace=workspace)}]
    messages.append({"role": "user", "content": prompt})
    try:
        with intent_span(prompt, user=user, agent_id=agent_id, session_id=session_id,
                     turn_id=turn_id, turn_number=turn_number, benchmark=benchmark,
                     case_id=case_id, model=MODEL_NAME, run_id=run_id,
                     agent_run_id=agent_run_id, parent_agent_run_id=parent_agent_run_id) as turn_span:
            _emit("agent.started", attributes={"agent.name": agent_name, "model.name": MODEL_NAME})
            graph, close_step = _build_agent_graph(
                client=client, workspace=workspace, tools=tools, measurements=measurements,
                redis_client=redis_client, max_steps=max_steps,
                require_workspace_changes=require_workspace_changes, turn_span=turn_span,
            )
            graph_attrs = {"graph.id": "agent.model-tools", "graph.type": "langgraph.state_graph",
                           "graph.version": "1", "task.id": task_id, "run.id": run_id,
                           "agent.id": agent_id, "agent.run.id": agent_run_id,
                           "root.run.id": root_run_id or run_id,
                           "parent.task.id": parent_task_id or "",
                           "parent.run.id": parent_run_id or parent_agent_run_id or "",
                           "agent.parent.run.id": parent_agent_run_id or "",
                           "handoff.id": RUNTIME_CONTEXT.get().get("handoff_id", "")}
            with span("langgraph.graph", **{"openinference.span.kind": "CHAIN", **graph_attrs}) as graph_span:
                _emit("langgraph.graph.started", attributes=graph_attrs)
                graph_status = "success"
                try:
                  final_state = graph.invoke(
                {"messages": messages, "step_index": 0, "tool_calls": [],
                 "route": "model", "result": ""},
                {"recursion_limit": max_steps * 2 + 2},
                  )
                except BaseException as exc:
                  graph_status = "error"
                  _emit("langgraph.graph.error", attributes={**graph_attrs, "error.type": type(exc).__name__}, severity="error")
                  raise
                finally:
                  _emit("langgraph.graph.ended", attributes={**graph_attrs, "status": "terminal"})
                  if redis_client is not None:
                    try:
                      snapshot = TelemetryLedger(redis_client).graph(run_id)
                      if snapshot is not None:
                        graph_span.set_attribute("procedural_graph", graph_snapshot_json(snapshot))
                    except Exception:
                      pass
                  graph_span.set_attribute("procedural_graph.status", graph_status)
            return final_state["result"], final_state["messages"]
    finally:
        if "close_step" in locals():
            close_step()
        RUNTIME_CONTEXT.reset(context_token)
        client.close()


def _otel_content(value):
    """Keep full-mode strings scalar; encode protected metadata for OTel."""
    captured = safe_content(value)
    return captured if isinstance(captured, str) else json.dumps(
        captured, default=str, separators=(",", ":"))


def _execute_tool(call, workspace, record, tools, *, tool_index=1, step_acting_started=None):
    attrs = {"program_id": record.program_id, "step_index": record.step_index,
             "tool_index": tool_index, "tool_name": call["name"], "tool_call_id": call["id"]}
    result, status = "", "Failed"
    # OpenInference tool fields follow the configured content policy and are
    # normalized to valid scalar OTel attributes. In full research mode these
    # fields intentionally retain raw content; protected modes retain only
    # redacted text or hash/length metadata.
    safe_arguments = _otel_content(call["arguments"])
    with span("agent.tool", **{"openinference.span.kind": "TOOL",
                              "tool.name": call["name"],
                              "tool.parameters": safe_arguments,
                              "input.value": safe_arguments, **attrs}) as current:
        _emit("tool.started", attributes=attrs,
              payload={"arguments": safe_content(call["arguments"])})
        if os.getenv("AGENT_TELEMETRY_CONTENT_CAPTURE", "redacted").lower() == "full":
            current.add_event("agent.tool.arguments", {"arguments": call["arguments"]})
        started = time.perf_counter()
        tool_ttf_ms = ((started - step_acting_started) * 1000
                       if step_acting_started is not None else None)
        try:
            args = json.loads(call["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object")
            if call["name"] not in {t["function"]["name"] for t in tools}:
                raise ValueError(f"Tool is unavailable: {call['name']}")
            result = IMPLEMENTATIONS[call["name"]](args, workspace)
            # Legacy tools return readable errors; include all legacy prefixes.
            status = "Failed" if result.startswith("Error") else "Success"
        except TaskTimeout:
            status = "Timeout"
            result = "Task deadline exceeded"
            raise
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            status, result = "Timeout", f"Error: {exc}"
            current.record_exception(exc)
        except Exception as exc:
            result = f"Error: {exc}"
            current.record_exception(exc)
        finally:
            measured = {**attrs, "tool_status": status,
                        "tool_ttf_ms": tool_ttf_ms,
                        "tool_latency_ms": (time.perf_counter() - started) * 1000}
            latency = measured["tool_latency_ms"]
            record.tools.append(measured)
            tool_call_metric.add(1, {"tool_name": call["name"], "tool_status": status})
            tool_duration.record(measured["tool_latency_ms"], {"tool_name": call["name"], "tool_status": status})
            if tool_ttf_ms is not None:
                tool_wait.record(tool_ttf_ms, {"tool_name": call["name"]})
            for key, value in measured.items():
                current.set_attribute(key, value)
            current.set_attribute(
                "output.value",
                _otel_content(result),
            )
            _emit("tool.completed", attributes=measured,
                  severity="info" if status == "Success" else "error",
                  payload={"result": safe_content(result)})
            if os.getenv("AGENT_TELEMETRY_CONTENT_CAPTURE", "redacted").lower() == "full":
                current.add_event("agent.tool.result", {"result": result, "tool_status": status})
            if status == "Success" and call["name"] in ("read_file", "write_file"):
                _emit("artifact." + ("read" if call["name"] == "read_file" else "written"), attributes=attrs)
    return result
