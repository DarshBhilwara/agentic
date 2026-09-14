import json
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


IMPLEMENTATIONS = {"execute_command": execute_command, "read_file": read_file, "write_file": write_file, "list_directory": list_directory, "web_search": web_search}


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
            for _ in range(12):
                with span("agent.inference", model=MODEL_NAME):
                    inference_counter.add(1, {"model": MODEL_NAME})
                    response = client.chat.completions.create(model=MODEL_NAME, messages=messages, tools=TOOLS, tool_choice="auto", max_tokens=4096, temperature=0.1)
                message = response.choices[0].message
                tool_calls = message.tool_calls or []
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
                    }):
                        result = implementation(args, workspace) if implementation else f"Error: unknown tool '{call.function.name}'."
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            return "The agent stopped after reaching the maximum number of tool rounds.", messages
