import json
import math
import os
import re
import subprocess
import signal
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Dict, List
import requests
from openai import OpenAI, APITimeoutError
from telemetry import event_counter, handoff_counter, inference_counter, inject_context, intent_span, safe_content, span, trace_identity
from program_telemetry import ProgramTelemetry, TaskTimeout, StepLimitExceeded, collect_engine_metrics

MODEL_NAME = os.getenv("AGENT_MODEL_NAME", "qwen")
VLLM_URL = os.getenv("AGENT_VLLM_URL", "http://vllm-service:8000/v1")
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
    redis_client = context["redis"]
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
        **{"handoff.id": str(uuid.uuid4()), "child.run.id": child_run_id,
           "child.agent.run.id": child_agent_run_id},
    )
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


def run(prompt: str, user: str, *, workspace=None, session_id="standalone", agent_id="standalone",
        turn_id="standalone", turn_number=1, conversation=None, benchmark=None,
        case_id=None, task_id="standalone", run_id=None, root_run_id=None,
        agent_run_id=None, parent_agent_run_id=None, agent_name="enterprise-agent",
        event_sink=None, redis_client=None, measurements=None, max_steps=100,
        timeout_seconds=1800, enabled_tools=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", user):
        raise ValueError("invalid user identity")
    workspace = workspace or os.path.join("/workspace/users", user)
    workspace = os.path.realpath(workspace)
    if not os.path.isdir(workspace):
        raise ValueError(f"workspace does not exist: {workspace}")
    client = OpenAI(base_url=VLLM_URL, api_key="EMPTY", max_retries=0,
                    timeout=float(os.getenv("AGENT_INFERENCE_TIMEOUT_SECONDS", "300")))
    run_id = run_id or str(uuid.uuid4())
    agent_run_id = agent_run_id or str(uuid.uuid4())
    context_token = RUNTIME_CONTEXT.set({
        "user": user, "workspace": workspace, "session_id": session_id,
        "task_id": task_id, "run_id": run_id, "root_run_id": root_run_id or run_id,
        "agent_id": agent_id, "agent_run_id": agent_run_id,
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
            for step_index in range(1, max_steps + 1):
                with measurements.step(step_index) as record:
                    request_id = f"{measurements.program_id}.{step_index}.{uuid.uuid4().hex}"
                    record.request_id = "chatcmpl-" + request_id
                    attrs = {"program_id": measurements.program_id, "step_index": step_index,
                             "agent.step.number": step_index, "request_id": record.request_id}
                    with span("agent.inference", model=MODEL_NAME, **attrs) as inference_span:
                        inference_counter.add(1, {"model": MODEL_NAME})
                        _emit("model.requested", attributes=attrs)
                        started = time.perf_counter()
                        try:
                            response = client.chat.completions.create(
                                model=MODEL_NAME, messages=messages, tools=tools or None,
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
                        collect_engine_metrics(redis_client, record.request_id, record)
                        message = response.choices[0].message
                        calls = message.tool_calls or []
                        decision = intent_decision([call.function.name for call in calls])
                        for key, value in decision.items():
                            value = json.dumps(value) if isinstance(value, list) else value
                            inference_span.set_attribute("agent.intent." + key, value)
                            turn_span.set_attribute("agent.intent." + key, value)
                        _emit("model.completed", attributes={**attrs,
                              "llm_inference_time_ms": record.llm_inference_time_ms,
                              "context_tokens": record.context_tokens,
                              "finish_reason": response.choices[0].finish_reason},
                              payload={"response": safe_content(message.content or "")})
                    messages.append(message.model_dump(exclude_none=True))
                    if response.choices[0].finish_reason == "length":
                        raise RuntimeError("Model response truncated by token limit")
                    if not calls:
                        _emit("agent.completed", attributes=attrs)
                        return message.content or "", messages
                    acting_started = time.perf_counter()
                    try:
                        for call in calls:
                            result = _execute_tool(call, workspace, record, tools)
                            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
                    finally:
                        record.step_t_acting_ms = (time.perf_counter() - acting_started) * 1000
            raise StepLimitExceeded(f"Agent reached the limit of {max_steps} inference steps")
    finally:
        RUNTIME_CONTEXT.reset(context_token)
        client.close()


def _execute_tool(call, workspace, record, tools):
    attrs = {"program_id": record.program_id, "step_index": record.step_index,
             "tool_name": call.function.name, "tool_call_id": call.id}
    result, status = "", "Failed"
    with span("agent.tool", **attrs) as current:
        _emit("tool.started", attributes=attrs,
              payload={"arguments": safe_content(call.function.arguments)})
        started = time.perf_counter()
        try:
            args = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object")
            if call.function.name not in {t["function"]["name"] for t in tools}:
                raise ValueError(f"Tool is unavailable: {call.function.name}")
            result = IMPLEMENTATIONS[call.function.name](args, workspace)
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
                        "tool_latency_ms": (time.perf_counter() - started) * 1000}
            record.tools.append(measured)
            for key, value in measured.items():
                current.set_attribute(key, value)
            _emit("tool.completed", attributes=measured,
                  severity="info" if status == "Success" else "error",
                  payload={"result": safe_content(result)})
            if status == "Success" and call.function.name in ("read_file", "write_file"):
                _emit("artifact." + ("read" if call.function.name == "read_file" else "written"), attributes=attrs)
    return result
