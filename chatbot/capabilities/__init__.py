"""Capability registry. Adding a capability = adding ONE module in this package that exports

    CAPABILITY = {
        "name": str,          # unique, used by load_capability(name)
        "one_line": str,      # always-on index line in the system prompt; keep it short
        "instructions": str,  # full text, returned only when the model calls load_capability
        "tools": [(schema, fn), ...],  # schema={"name","description","parameters"}, fn(user_id, thread_id, **args)->str
    }

Modules are discovered with pkgutil; files starting with "_" are skipped.
"""
import importlib
import pkgutil
from typing import Callable


def discover() -> dict[str, dict]:
    caps = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        cap = getattr(importlib.import_module(f"{__name__}.{mod.name}"), "CAPABILITY", None)
        if cap is None:
            continue
        if cap["name"] in caps:
            raise ValueError(f"duplicate capability name {cap['name']!r}")
        caps[cap["name"]] = cap
    return dict(sorted(caps.items()))


def capability_index() -> str:
    """Compact text for the system prompt: one line per capability plus how to load details."""
    lines = ["Capabilities (call load_capability(name) to read full instructions before first use):"]
    for cap in discover().values():
        tools = ", ".join(schema["name"] for schema, _ in cap["tools"])
        lines.append(f"- {cap['name']}: {cap['one_line']} [tools: {tools}]")
    return "\n".join(lines)


def tool_owners() -> dict[str, str]:
    """Tool name -> capability name. Core exposes these tools only after load_capability(name)."""
    return {schema["name"]: cap["name"] for cap in discover().values() for schema, _ in cap["tools"]}


def load_capability(user_id: str, thread_id: str, name: str = "") -> str:
    cap = discover().get(name)
    if cap is None:
        return f"error: unknown capability {name!r}; known: {', '.join(discover())}"
    return f"# {cap['name']} instructions\n{cap['instructions']}"


LOAD_CAPABILITY_SCHEMA = {
    "name": "load_capability",
    "description": "Return the full instructions for one capability from the index.",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Capability name from the index."}},
        "required": ["name"],
    },
}


def register_all(register: Callable[[str, dict, Callable[..., str]], None] | None = None) -> list[str]:
    """Register every capability tool plus load_capability. Returns the registered tool names.

    `register` defaults to core.register_tool, imported lazily so core may import this package.
    """
    if register is None:
        from chatbot.core import register_tool as register
    names = []
    for cap in discover().values():
        for schema, fn in cap["tools"]:
            register(schema["name"], schema, fn)
            names.append(schema["name"])
    register("load_capability", LOAD_CAPABILITY_SCHEMA, load_capability)
    return names + ["load_capability"]
