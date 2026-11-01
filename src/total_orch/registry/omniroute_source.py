"""OmniRoute-specific catalog source and compatibility exports."""

from .gateway_source import GatewayCatalogSource, normalize_capability_flags


class OmniRouteCatalogSource(GatewayCatalogSource):
    """Catalog source using OmniRoute's management-aware adapter methods."""

    source = "omniroute"


__all__ = ["GatewayCatalogSource", "OmniRouteCatalogSource", "normalize_capability_flags"]
