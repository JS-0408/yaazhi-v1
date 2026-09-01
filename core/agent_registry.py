"""
Yaazhi Agent Registry — dynamic plugin system.

Audit fix: Gap 03 — agents are now registered with a decorator.
Adding a new agent never requires editing orchestrator.py.

Usage:
    from core.agent_registry import AgentRegistry

    @AgentRegistry.register("my_agent")
    class MyAgent:
        ...

    agent = AgentRegistry.get("my_agent")   # returns a fresh instance
"""

from __future__ import annotations

from typing import Any

import logfire


class AgentRegistry:
    """
    Singleton registry mapping agent names to their classes.

    Thread-safe for reads; writes happen only at import time.
    """

    _agents: dict[str, type] = {}

    @classmethod
    def register(cls, name: str):
        """
        Class decorator that registers an agent under a given name.

        Example:
            @AgentRegistry.register("researcher")
            class ResearcherAgent: ...
        """
        def decorator(agent_class: type) -> type:
            if name in cls._agents:
                logfire.warning(
                    "AgentRegistry: overwriting existing registration",
                    name=name,
                    old=cls._agents[name].__name__,
                    new=agent_class.__name__,
                )
            cls._agents[name] = agent_class
            logfire.debug("AgentRegistry: registered agent", name=name, cls=agent_class.__name__)
            return agent_class

        return decorator

    @classmethod
    def get(cls, name: str) -> Any:
        """
        Instantiate and return an agent by name.

        Raises:
            KeyError: If the agent name is not registered.
        """
        if name not in cls._agents:
            available = list(cls._agents.keys())
            raise KeyError(
                f"Agent '{name}' not registered. "
                f"Available agents: {available}"
            )
        return cls._agents[name]()

    @classmethod
    def get_class(cls, name: str) -> type:
        """Return the agent class (not an instance)."""
        if name not in cls._agents:
            raise KeyError(f"Agent '{name}' not registered.")
        return cls._agents[name]

    @classmethod
    def list_agents(cls) -> list[str]:
        """Return sorted list of all registered agent names."""
        return sorted(cls._agents.keys())

    @classmethod
    def is_registered(cls, name: str) -> bool:
        return name in cls._agents

    @classmethod
    def reset(cls) -> None:
        """Clear all registrations (used in tests only)."""
        cls._agents.clear()
