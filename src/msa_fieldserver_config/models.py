"""Pydantic models for FieldServer responses and exported documents.

Device payloads use human-readable keys with spaces (``"Network Number"``,
``"MAC Address"``) and vary by firmware, so the device-shaped models are
permissive: known fields are aliased and typed, anything unrecognised is
preserved via ``extra="allow"`` rather than dropped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CollectionError",
    "ConfigDocument",
    "ConfigFile",
    "PortConfig",
    "PortStatistics",
    "PortStatisticsEntry",
    "ProductInfo",
    "RouteEntry",
    "StatsDocument",
]


class _DevicePayload(BaseModel):
    """Base for anything echoed back from the device: never lose unknown keys."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ProductInfo(_DevicePayload):
    """Response of ``smcCore/getProductInfo``."""

    product_name: str | None = Field(default=None, alias="productName")
    product_version: str | None = Field(default=None, alias="productVersion")
    customer_name: str | None = Field(default=None, alias="customerName")
    skin: str | None = None


class RouteEntry(_DevicePayload):
    """One row of a BACnet port's routing table."""

    dnet: int | None = Field(default=None, alias="DNET")
    mac_address: str | None = Field(default=None, alias="MAC Address")
    state: str | None = Field(default=None, alias="State")


class PortStatistics(_DevicePayload):
    """The ``Stats`` sub-object of a port: message counters and error counters."""

    info: dict[str, Any] = Field(default_factory=dict, alias="Info")
    error: dict[str, Any] = Field(default_factory=dict, alias="Error")


class PortConfig(_DevicePayload):
    """The stable, non-volatile part of a port definition.

    Everything the device reports for a port *except* ``Stats`` and
    ``Routing Table``, which are point-in-time and live in the stats document.
    """

    network_number: int | None = Field(default=None, alias="Network Number")


class PortStatisticsEntry(BaseModel):
    """The volatile part of a port: counters plus current reachability."""

    model_config = ConfigDict(populate_by_name=True)

    statistics: PortStatistics = Field(default_factory=PortStatistics)
    routing_table: list[RouteEntry] = Field(default_factory=list)


class ConfigFile(_DevicePayload):
    """The device's stored configuration file (``config.csv``), already parsed.

    ``pe/getConfig`` returns it as ordered sections rather than raw CSV, e.g.
    ``{"Bridge": [{"Title": ...}]}`` then one ``{"Connections": [...]}`` per
    configured connection. Section names repeat, which is why this is a list of
    single-key dicts and not a mapping.

    This is stored configuration, not runtime state, so it remains readable on a
    device whose protocol engine is in Recovery Mode — making it the most
    dependable configuration source available.
    """

    errors: list[Any] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)

    def section(self, name: str) -> list[dict[str, Any]]:
        """Every row across all sections with the given name, in order."""
        rows: list[dict[str, Any]] = []
        for entry in self.sections:
            value = entry.get(name)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
        return rows


class CollectionError(BaseModel):
    """A single read that failed, recorded inline instead of aborting collection.

    Firmware varies in which components it mounts, so a partial document is a
    normal outcome and must be distinguishable from a total failure.
    """

    source: str
    message: str


class _Document(BaseModel):
    """Fields common to every exported document."""

    model_config = ConfigDict(populate_by_name=True)

    host: str
    collected_at: datetime
    product: ProductInfo | None = None
    errors: list[CollectionError] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when every read succeeded."""
        return not self.errors


class ConfigDocument(_Document):
    """A FieldServer's configuration — intended to be stable across runs."""

    firmware_version: Any | None = None
    bacnet_ports: dict[str, PortConfig] = Field(default_factory=dict)
    router_db: dict[str, Any] = Field(default_factory=dict)
    network_settings: Any | None = None
    stored_settings: Any | None = None
    config_file: ConfigFile | None = None


class StatsDocument(_Document):
    """A FieldServer's usage statistics — point-in-time, expected to churn."""

    ports: dict[str, PortStatisticsEntry] = Field(default_factory=dict)
    system_status: Any | None = None
    network_status: Any | None = None
    stats: Any | None = None
