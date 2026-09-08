"""Default Agent Profiles and automatic Model Pools (MVP Phase 4).

Built-in agent definitions with hard capability requirements
(``defaults.py``) and a deterministic, rule-based pool builder
(``builder.py``) that consumes the registry's local query APIs. Pool
membership is always explainable and user overrides (include, exclude,
pin) never change the underlying rules — a rebuild with no overrides
restores the defaults.
"""

from total_orch.registry.capabilities import BOOL_CAPABILITY_FIELDS

from .builder import (
    PoolBuild,
    PoolBuilder,
    PoolMembership,
    PoolOverrides,
    RegistryReader,
)
from .defaults import (
    BUILTIN_AGENT_DEFINITIONS,
    ROLE_POOL,
    ROLE_PREFERENCES,
    default_agent_profiles,
)

__all__ = [
    "BOOL_CAPABILITY_FIELDS",
    "BUILTIN_AGENT_DEFINITIONS",
    "ROLE_POOL",
    "ROLE_PREFERENCES",
    "PoolBuild",
    "PoolBuilder",
    "PoolMembership",
    "PoolOverrides",
    "RegistryReader",
    "default_agent_profiles",
]
