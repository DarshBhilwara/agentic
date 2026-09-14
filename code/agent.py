import json
import math
import os
import re
import subprocess
from typing import Dict, List
import requests
from openai import OpenAI
from telemetry import inference_counter, intent_span, span

MODEL_NAME = os.getenv("AGENT_MODEL_NAME", "qwen")
VLLM_URL = os.getenv("AGENT_VLLM_URL", "http://vllm-service:8000/v1")
SYSTEM_PROMPT = """You are the enterprise agent running in the agent node.
You help with research, information lookup, file and workspace management, and
general task execution using the tools available to you.

The current user workspace is: {workspace}

Operational rules:
1. Use a tool whenever you need information or an effect you do not already have.
2. Never fabricate tool output.
3. Prefer read-only tools when they can accomplish the task.
4. Keep changes inside the current user workspace unless the task explicitly requires another path.
5. When the task is complete, give a concise plain-language summary.
"""

TOOLS = [
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
    result = subprocess.run(command, shell=True, text=True, capture_output=True, cwd=workspace)
    output = ""
    if result.stdout:
        output += f"STDOUT:\n{result.stdout}\n"
    if result.stderr:
        output += f"STDERR:\n{result.stderr}\n"
    return (output or "Executed successfully with no stdout/stderr.\n") + f"Exit code: {result.returncode}"


def read_file(args: Dict, workspace: str) -> str:
    path = (args or {}).get("path", "")
    if not path:
        return "Error: no path provided."
    try:
        with open(_path(path, workspace), "r", errors="replace") as file:
            content = file.read()
        return (content[:20000] + ("\n... (truncated)" if len(content) > 20000 else "")) or "(file is empty)"
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
    except Exception as exc:
        return f"Error writing file: {exc}"


def list_directory(args: Dict, workspace: str) -> str:
    try:
        entries = sorted(os.listdir(_path((args or {}).get("path", ".") or ".", workspace)))
        return "\n".join(entries) if entries else "(empty directory)"
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
}


def run(prompt: str, user: str, *, workspace=None, session_id="standalone", agent_id="standalone",
        turn_id="standalone", turn_number=1, conversation=None, benchmark=None,
        case_id=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", user):
        raise ValueError("invalid user identity")
    workspace = workspace or os.path.join("/workspace/users", user)
    workspace = os.path.realpath(workspace)
    if not os.path.isdir(workspace):
        raise ValueError(f"workspace does not exist: {workspace}")
    client = OpenAI(base_url=VLLM_URL, api_key="EMPTY")
    messages: List[Dict] = conversation or [{"role": "system", "content": SYSTEM_PROMPT.format(workspace=workspace)}]
    messages.append({"role": "user", "content": prompt})

    with intent_span(prompt, user=user, agent_id=agent_id, session_id=session_id,
                     turn_id=turn_id, turn_number=turn_number, benchmark=benchmark,
                     case_id=case_id, model=MODEL_NAME):
        with span("agent.task.process", user=user, benchmark=benchmark, case_id=case_id):
            for step_number in range(1, 13):
                with span("agent.inference", model=MODEL_NAME,
                          **{
                              "agent.step.number": step_number,
                              "agent.step.kind": "model_inference",
                          }) as inference_span:
                    inference_counter.add(1, {"model": MODEL_NAME})
                    response = client.chat.completions.create(model=MODEL_NAME, messages=messages, tools=TOOLS, tool_choice="auto", max_tokens=4096, temperature=0.1)
                    message = response.choices[0].message
                    tool_calls = message.tool_calls or []
                    tool_names = [call.function.name for call in tool_calls]
                    step_intent = (
                        f"execute_tool:{tool_names[0]}"
                        if tool_names else "produce_final_response"
                    )
                    inference_span.set_attribute("agent.step.intent", step_intent)
                    inference_span.set_attribute("agent.step.next_action", "tool_execution" if tool_names else "return_response")
                    inference_span.set_attribute("agent.step.tool_names", json.dumps(tool_names))
                    inference_span.add_event("agent.step.intent", {
                        "agent.step.number": step_number,
                        "agent.step.intent": step_intent,
                        "agent.step.next_action": "tool_execution" if tool_names else "return_response",
                        "agent.step.tool_names": json.dumps(tool_names),
                    })
                    messages.append(message.model_dump(exclude_none=True))
                    if not tool_calls:
                        return message.content or "", messages
                    for call in tool_calls:
                        try:
                            args = json.loads(call.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        implementation = IMPLEMENTATIONS.get(call.function.name)
                        with span(f"agent.tool.{call.function.name}", user=user, **{
                            "gen_ai.tool.name": call.function.name,
                            "gen_ai.tool.call.id": call.id,
                            "gen_ai.tool.call.arguments": call.function.arguments or "{}",
                            "agent.step.number": step_number,
                            "agent.step.kind": "tool_execution",
                            "agent.step.intent": f"execute_tool:{call.function.name}",
                            "agent.step.next_action": "return_observation_to_model",
                        }) as tool_span:
                            tool_span.add_event("agent.tool.intent", {
                                "agent.step.number": step_number,
                                "agent.step.intent": f"execute_tool:{call.function.name}",
                                "gen_ai.tool.name": call.function.name,
                                "gen_ai.tool.call.arguments": call.function.arguments or "{}",
                            })
                            try:
                                result = implementation(args, workspace) if implementation else f"Error: unknown tool '{call.function.name}'."
                            except Exception as exc:
                                tool_span.record_exception(exc)
                                tool_span.set_attribute("error.type", type(exc).__name__)
                                result = f"Error: {exc}"
                            tool_span.set_attribute("gen_ai.tool.result", result)
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            return "The agent stopped after reaching the maximum number of tool rounds.", messages
