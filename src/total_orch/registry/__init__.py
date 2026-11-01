"""Model Intelligence Registry (MVP Phase 3).

A deterministic, local, normalized model catalog: gateway discovery and public
catalog metadata (models.dev, OpenRouter) are merged into canonical
family/variant records with per-field provenance, then persisted to SQLite so
routing never blocks on a live external catalog request.
"""

from .gateway_source import classify_model
from .http import CatalogResponse, CatalogTransport, default_transport
from .identity import (
    KNOWN_PUBLISHERS,
    CanonicalIdentity,
    IdentityRules,
    endpoint_id,
    split_endpoint_id,
    split_variant_id,
    strip_qualifier,
    strip_trailing_version,
    variant_id,
)
from .merge import (
    FIELD_AUTHORITY,
    assemble_endpoint,
    assemble_family,
    assemble_variant,
    merge_fields,
)
from .models_endpoint_source import ModelsEndpointCatalogSource
from .modelsdev_source import ModelsDevCatalogSource
from .observations import (
    CatalogBatch,
    EndpointObservation,
    EndpointRef,
    FamilyObservation,
    FieldValue,
    Observation,
    VariantObservation,
    capability_fields,
    claimed,
)
from .omniroute_source import (
    GatewayCatalogSource,
    OmniRouteCatalogSource,
    normalize_capability_flags,
)
from .openrouter_source import OpenRouterCatalogSource
from .service import (
    ModelRegistryService,
    RegistryCounts,
    SourceSyncResult,
    StaleReport,
    SyncSummary,
)
from .store import EntityKind, ModelRegistryStore

__all__ = [
    "FIELD_AUTHORITY",
    "KNOWN_PUBLISHERS",
    "CanonicalIdentity",
    "CatalogBatch",
    "CatalogResponse",
    "CatalogTransport",
    "EndpointObservation",
    "EndpointRef",
    "EntityKind",
    "FamilyObservation",
    "FieldValue",
    "GatewayCatalogSource",
    "IdentityRules",
    "ModelRegistryService",
    "ModelRegistryStore",
    "ModelsDevCatalogSource",
    "ModelsEndpointCatalogSource",
    "Observation",
    "OmniRouteCatalogSource",
    "OpenRouterCatalogSource",
    "RegistryCounts",
    "SourceSyncResult",
    "StaleReport",
    "SyncSummary",
    "VariantObservation",
    "assemble_endpoint",
    "assemble_family",
    "assemble_variant",
    "capability_fields",
    "claimed",
    "classify_model",
    "default_transport",
    "endpoint_id",
    "merge_fields",
    "normalize_capability_flags",
    "split_endpoint_id",
    "split_variant_id",
    "strip_qualifier",
    "strip_trailing_version",
    "variant_id",
]
