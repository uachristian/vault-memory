"""Hermetic bootstrap for standalone obsidian_vault plugin tests."""

from __future__ import annotations

import json
import sys
import types


agent_module = types.ModuleType("agent")
memory_provider_module = types.ModuleType("agent.memory_provider")


class MemoryProvider:
    """Minimal test double for Hermes's provider base class."""


setattr(memory_provider_module, "MemoryProvider", MemoryProvider)
setattr(agent_module, "memory_provider", memory_provider_module)
sys.modules["agent"] = agent_module
sys.modules["agent.memory_provider"] = memory_provider_module

tools_module = types.ModuleType("tools")
registry_module = types.ModuleType("tools.registry")


def tool_error(message, **extra) -> str:
    result = {"error": str(message)}
    result.update(extra)
    return json.dumps(result, ensure_ascii=False)


setattr(registry_module, "tool_error", tool_error)
setattr(tools_module, "registry", registry_module)
sys.modules["tools"] = tools_module
sys.modules["tools.registry"] = registry_module
