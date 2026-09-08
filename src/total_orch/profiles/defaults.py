"""Built-in agent definitions and default profile wiring (MVP-0401..0405, 0411).

The default profiles capture the MVP's five core agent roles. Their hard
capability requirements live in each ``AgentDefinition`` and are the minimum
bar the pool builder enforces; a high quality score never bypasses them (MVP
Definition of Done). ``default_agent_profiles`` is the only "configuration"
a fresh installation needs: it links every built-in definition to its
automatically generated role pool.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from total_orch.domain import AgentDefinition, AgentProfile, ModelCapabilities, ModelPool

BUILTIN_AGENT_DEFINITIONS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        id="planner",
        display_name="Planner",
        purpose="Breaks a task into an executable, reviewed plan with structured output.",
        required_capabilities=ModelCapabilities(reasoning=True, structured_output=True),
        minimum_context_window=32_000,
    ),
    AgentDefinition(
        id="developer",
        display_name="Developer / Coder",
        purpose="Implements repository changes through approved tools with streaming feedback.",
        required_capabilities=ModelCapabilities(tool_calling=True, streaming=True),
        minimum_context_window=32_000,
    ),
    AgentDefinition(
        id="reviewer",
        display_name="Reviewer",
        purpose="Evaluates diffs and test output and returns a structured verdict with reasoning.",
        required_capabilities=ModelCapabilities(reasoning=True, structured_output=True),
        minimum_context_window=16_000,
    ),
    AgentDefinition(
        id="researcher",
        display_name="Researcher",
        purpose="Gathers and summarizes evidence with tool access over a long context.",
        required_capabilities=ModelCapabilities(tool_calling=True),
        minimum_context_window=64_000,
    ),
    AgentDefinition(
        id="tester",
        display_name="Tester",
        purpose="Runs and validates test suites and returns deterministic structured results.",
        required_capabilities=ModelCapabilities(tool_calling=True, structured_output=True),
        minimum_context_window=16_000,
    ),
)

# Built-in role -> the role-specific default pool the builder generates
# (MVP-0411) and the membership-ordering preference applied to that pool.
ROLE_POOL: Mapping[str, str] = {
    "planner": "planner-quality",
    "developer": "coder-balanced",
    "reviewer": "reviewer-quality",
    "researcher": "researcher-reliable",
    "tester": "tester-fast",
}

# Policy-weight previews used by the Phase 5 router; pool membership ordering
# follows the dominant preference until weighted scoring lands.
ROLE_PREFERENCES: Mapping[str, dict[str, float]] = {
    "planner": {"quality": 0.5, "reliability": 0.3, "cost": 0.1, "latency": 0.1},
    "developer": {"quality": 0.3, "latency": 0.3, "reliability": 0.2, "cost": 0.2},
    "reviewer": {"quality": 0.6, "reliability": 0.3, "cost": 0.05, "latency": 0.05},
    "researcher": {"reliability": 0.5, "quality": 0.3, "cost": 0.1, "latency": 0.1},
    "tester": {"latency": 0.4, "reliability": 0.4, "cost": 0.2},
}


def default_agent_profiles(
    definitions: Sequence[AgentDefinition],
    pools: Mapping[str, ModelPool],
) -> tuple[AgentProfile, ...]:
    """Wire built-in definitions to their generated role pools (MVP-0411).

    Every built-in role must resolve to a pool the builder actually emitted;
    a fresh installation therefore starts with usable, linked profiles and no
    hand-written configuration (MVP DoD: "Planner, Developer/Coder, and
    Reviewer profiles exist by default").
    """

    profiles: list[AgentProfile] = []
    for definition in definitions:
        try:
            pool_id = ROLE_POOL[definition.id]
        except KeyError:
            raise ValueError(f"no default role pool is defined for agent {definition.id!r}") from None
        if pool_id not in pools:
            raise ValueError(f"agent {definition.id!r} expects pool {pool_id!r}, which was not generated")
        profiles.append(
            AgentProfile(
                id=f"{definition.id}-default",
                agent_definition_id=definition.id,
                pool_id=pool_id,
                preferences=dict(ROLE_PREFERENCES[definition.id]),
            )
        )
    return tuple(profiles)