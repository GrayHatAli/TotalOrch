"""The explainable router (MVP Phase 5).

Public surface:

- ``RoutingPolicy`` and the five ``POLICY_PRESETS`` (MVP-0509), plus
  ``policy_from_preferences`` for deriving a policy from an agent profile's
  coarse axis preferences.
- ``ExplainableRouter.route`` for persisted decisions and
  ``ExplainableRouter.preview`` for debugging/UI previews (MVP-0514).
- ``RouteDecisionStore`` for SQLite persistence (MVP-0513).
"""

from .candidates import RouteCandidate, assemble_candidates
from .engine import ExplainableRouter, RouteOutcome, RoutePreview
from .policies import (
    DERIVATION_VERSION,
    FACTOR_KEYS,
    FACTOR_LABELS,
    POLICY_PRESETS,
    PREFERENCE_AXES,
    RoutingPolicy,
    policy_from_preferences,
    preset_policy,
)
from .scoring import HealthObservation, score_candidates
from .store import RouteDecisionStore

__all__ = [
    "DERIVATION_VERSION",
    "FACTOR_KEYS",
    "FACTOR_LABELS",
    "POLICY_PRESETS",
    "PREFERENCE_AXES",
    "ExplainableRouter",
    "HealthObservation",
    "RouteCandidate",
    "RouteDecisionStore",
    "RouteOutcome",
    "RoutePreview",
    "RoutingPolicy",
    "assemble_candidates",
    "policy_from_preferences",
    "preset_policy",
    "score_candidates",
]
