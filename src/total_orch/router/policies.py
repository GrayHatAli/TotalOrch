"""Routing policies and the weighted scoring model (MVP-0509).

A ``RoutingPolicy`` assigns one non-negative weight to every scoring factor:

    Final Score =
      Role Fit            × w_role
    + Capability Fit      × w_capability
    + Quality / Task Fit  × w_quality
    + Health              × w_health
    + Latency             × w_latency
    + Cost                × w_cost
    + Quota               × w_quota
    + Historical Success  × w_history

Weights do not need to sum to one — the engine normalizes them — but every
preset below is written to sum to one so its meaning is readable at a glance.
The five MVP presets (quality-first, balanced, cost-first, latency-first,
reliable-first) are fixed objects: a decision records its ``policy_version``
and a ``factor_weights`` snapshot, so a historical routing decision can be
reconstructed after policy definitions change.

Agent profiles carry coarse axis preferences (quality / reliability / latency
/ cost). ``policy_from_preferences`` derives a factor-level policy from them by
spreading each axis's weight evenly over the factors that express that axis:

- ``quality``     -> role fit, capability fit, quality
- ``reliability`` -> health, quota, history
- ``latency``     -> latency
- ``cost``        -> cost

The spreading recipe is versioned: bump ``derivation_version`` when the
mapping changes so stored ``role-derived-v{n}`` decisions stay attributable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: The scoring factors, in display order. A policy may leave a factor without
#: weight (weight zero) but never names a factor outside this set.
FACTOR_KEYS: tuple[str, ...] = (
    "role_fit",
    "capability_fit",
    "quality",
    "health",
    "latency",
    "cost",
    "quota",
    "history",
)

FACTOR_LABELS: Mapping[str, str] = {
    "role_fit": "Role Fit",
    "capability_fit": "Capability Fit",
    "quality": "Quality / Task Fit",
    "health": "Health",
    "latency": "Latency",
    "cost": "Cost",
    "quota": "Quota",
    "history": "Historical Success",
}

#: Coarse preference axes an AgentProfile may carry (MVP-0404 defaults).
PREFERENCE_AXES: tuple[str, ...] = ("quality", "reliability", "latency", "cost")

_AXIS_TO_FACTORS: Mapping[str, tuple[str, ...]] = {
    "quality": ("role_fit", "capability_fit", "quality"),
    "reliability": ("health", "quota", "history"),
    "latency": ("latency",),
    "cost": ("cost",),
}


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """A versioned set of factor weights.

    ``id`` names the policy family (a preset name or ``role-derived``) and
    ``version`` distinguishes weight recipes within that family. Only factors
    from ``FACTOR_KEYS`` may carry weight, weights must be non-negative, and
    at least one weight must be positive (an all-zero policy would make every
    endpoint tie).
    """

    id: str
    weights: Mapping[str, float]
    version: int = 1

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("a routing policy needs a non-empty id")
        if self.version < 1:
            raise ValueError("a routing policy version must be at least 1")
        unknown = set(self.weights) - set(FACTOR_KEYS)
        if unknown:
            raise ValueError(f"unknown scoring factors in weights: {sorted(unknown)}")
        if any(weight < 0 for weight in self.weights.values()):
            raise ValueError(f"weights for policy {self.id!r} must be non-negative")
        if sum(self.weights.values()) <= 0:
            raise ValueError(f"policy {self.id!r} must assign positive total weight")

    @property
    def policy_version(self) -> str:
        return f"{self.id}-v{self.version}"

    def normalized_weights(self) -> dict[str, float]:
        """Weights over every factor, normalized to sum to one."""

        total = sum(self.weights.values())
        return {factor: self.weights.get(factor, 0.0) / total for factor in FACTOR_KEYS}


def _preset(name: str, weights: Mapping[str, float]) -> RoutingPolicy:
    return RoutingPolicy(id=name, weights=dict(weights), version=1)


#: The five MVP policy presets (MVP-0509), each normalized to sum to one.
POLICY_PRESETS: Mapping[str, RoutingPolicy] = {
    "quality-first": _preset(
        "quality-first",
        {
            "role_fit": 0.15,
            "capability_fit": 0.15,
            "quality": 0.25,
            "health": 0.1,
            "latency": 0.05,
            "cost": 0.1,
            "quota": 0.05,
            "history": 0.15,
        },
    ),
    "balanced": _preset(
        "balanced",
        {
            "role_fit": 0.1,
            "capability_fit": 0.1,
            "quality": 0.2,
            "health": 0.15,
            "latency": 0.15,
            "cost": 0.15,
            "quota": 0.075,
            "history": 0.075,
        },
    ),
    "cost-first": _preset(
        "cost-first",
        {
            "role_fit": 0.05,
            "capability_fit": 0.05,
            "quality": 0.1,
            "health": 0.1,
            "latency": 0.05,
            "cost": 0.35,
            "quota": 0.15,
            "history": 0.15,
        },
    ),
    "latency-first": _preset(
        "latency-first",
        {
            "role_fit": 0.05,
            "capability_fit": 0.05,
            "quality": 0.1,
            "health": 0.1,
            "latency": 0.4,
            "cost": 0.1,
            "quota": 0.1,
            "history": 0.1,
        },
    ),
    "reliable-first": _preset(
        "reliable-first",
        {
            "role_fit": 0.05,
            "capability_fit": 0.05,
            "quality": 0.1,
            "health": 0.3,
            "latency": 0.1,
            "cost": 0.05,
            "quota": 0.15,
            "history": 0.2,
        },
    ),
}

#: Version of the axis-to-factor spreading recipe used by
#: ``policy_from_preferences`` (stored as ``role-derived-v{n}`` decisions).
DERIVATION_VERSION = 1


def preset_policy(name: str) -> RoutingPolicy:
    if name not in POLICY_PRESETS:
        raise ValueError(
            f"unknown policy preset {name!r}; expected one of {sorted(POLICY_PRESETS)}"
        )
    return POLICY_PRESETS[name]


def policy_from_preferences(
    preferences: Mapping[str, float],
    *,
    derivation_version: int = DERIVATION_VERSION,
) -> RoutingPolicy:
    """Derive a factor policy from coarse axis preferences.

    Every supported axis with a positive weight is spread evenly over the
    factors that express it (see the module docstring). Unsupported axis keys
    are ignored — a profile preference that names no supported axis produces
    no policy and raises. Weights are not renormalized here; the engine
    normalizes whatever policy it applies.
    """

    weights = {factor: 0.0 for factor in FACTOR_KEYS}
    for axis, weight in preferences.items():
        if axis not in _AXIS_TO_FACTORS or weight <= 0:
            continue
        factors = _AXIS_TO_FACTORS[axis]
        share = weight / len(factors)
        for factor in factors:
            weights[factor] += share
    if sum(weights.values()) <= 0:
        raise ValueError(
            "cannot derive a policy: preferences must give positive weight to at "
            f"least one of {PREFERENCE_AXES}"
        )
    return RoutingPolicy(id="role-derived", weights=weights, version=derivation_version)
