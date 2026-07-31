"""Collect configuration and statistics from a FieldServer.

Reads are individually fault-tolerant. Firmware varies in which mesh components
it mounts, so one unavailable method must not cost us the rest of the document:
failures are recorded in ``errors`` and collection continues.

The split between the two documents is meaningful. ``pe.rpc_db_read_json`` returns
port configuration, counters and the routing table in one payload; the counters and
routing state are point-in-time and would make every config diff dirty, so they are
separated out here rather than downstream.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from .client import FieldServerClient, FieldServerError
from .models import (
    CollectionError,
    ConfigDocument,
    ConfigFile,
    PortConfig,
    PortStatistics,
    PortStatisticsEntry,
    ProductInfo,
    RouteEntry,
    StatsDocument,
)

__all__ = ["collect_config", "collect_stats", "split_port"]

T = TypeVar("T")

#: Keys inside a port definition that are volatile rather than configuration.
_VOLATILE_PORT_KEYS = ("Stats", "Routing Table")

#: Sub-paths of the ``bacnet_router`` database to try when the whole-database
#: read is not supported. ``Ports`` is confirmed; the rest are attempted
#: opportunistically and their absence is not an error.
_ROUTER_DB_PATHS = ("Ports", "Device", "Settings", "Network")


def _now() -> datetime:
    return datetime.now(UTC)


class _Collector:
    """Runs reads, capturing failures instead of propagating them."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.errors: list[CollectionError] = []

    def try_read(self, source: str, fn: Callable[[], T]) -> T | None:
        try:
            return fn()
        except FieldServerError as exc:
            self.errors.append(CollectionError(source=source, message=str(exc)))
            return None

    def product(self, client: FieldServerClient) -> ProductInfo | None:
        raw = self.try_read("smcCore/getProductInfo", client.get_product_info)
        if isinstance(raw, dict):
            return ProductInfo.model_validate(raw)
        return None


def split_port(port: dict[str, Any]) -> tuple[dict[str, Any], PortStatisticsEntry]:
    """Split one port payload into its stable config and its volatile state."""
    config = {k: v for k, v in port.items() if k not in _VOLATILE_PORT_KEYS}

    raw_stats = port.get("Stats")
    statistics = (
        PortStatistics.model_validate(raw_stats)
        if isinstance(raw_stats, dict)
        else PortStatistics()
    )

    raw_routes = port.get("Routing Table")
    routes = [
        RouteEntry.model_validate(entry)
        for entry in (raw_routes if isinstance(raw_routes, list) else [])
        if isinstance(entry, dict)
    ]

    return config, PortStatisticsEntry(statistics=statistics, routing_table=routes)


def _read_ports(client: FieldServerClient, collector: _Collector) -> dict[str, Any]:
    ports = collector.try_read("pe/rpc_db_read_json(bacnet_router/Ports)", client.read_bacnet_ports)
    return ports if isinstance(ports, dict) else {}


def _require_session(client: FieldServerClient) -> None:
    """Establish the session up front so a dead device fails loudly.

    Without this, an unreachable or unauthorised device would have every read
    fail individually and yield a near-empty document flagged merely "partial",
    which reads as success. Authentication failure is not partial.
    """
    if not client.authenticated:
        client.login()


def collect_config(client: FieldServerClient) -> ConfigDocument:
    """Gather the stable configuration of one FieldServer.

    Raises :class:`FieldServerError` if the device cannot be reached or
    authenticated; individual read failures are recorded in ``errors`` instead.
    """
    _require_session(client)
    collector = _Collector(client.host)

    # Read order is deliberate and load-bearing. A read that exhausts its retries
    # against a failing gateway can knock the bridge out for the calls that
    # follow it, so the highest-value reads go first and speculative ones last.
    # This was originally forced by pe/getConfig appearing to fail
    # deterministically and taking the Ports read down with it; that turned out
    # to be the wrong argument envelope plus a degraded bridge, not the method.
    # The ordering is kept because the underlying fragility is real.
    product = collector.product(client)

    bacnet_ports = {
        name: PortConfig.model_validate(split_port(port)[0])
        for name, port in _read_ports(client, collector).items()
        if isinstance(port, dict)
    }

    # The stored config file: the most dependable configuration source, since it
    # survives a protocol engine in Recovery Mode. Read early, right after Ports.
    raw_config = collector.try_read("pe/getConfig", client.get_config)
    config_file = ConfigFile.model_validate(raw_config) if isinstance(raw_config, dict) else None

    stored = collector.try_read("pe/rpc_bacnet_read_stored_settings", client.read_stored_settings)

    # Opportunistically pull the other router-database sections. These are
    # unconfirmed on 8.0.1, so a failure here is silent rather than recorded —
    # it means "this firmware has no such section", not "the read broke".
    router_db: dict[str, Any] = {}
    for path in _ROUTER_DB_PATHS:
        if path == "Ports":
            continue
        try:
            section = client.read_router_db(path)
        except FieldServerError:
            continue
        if isinstance(section, dict) and section.get("Data"):
            router_db[path] = section["Data"]

    firmware = collector.try_read("pe/getFirmwareVersion", client.get_firmware_version)
    network = collector.try_read(
        "smcNetwork/getAllNetworkSettings", client.get_all_network_settings
    )

    return ConfigDocument(
        host=client.host,
        collected_at=_now(),
        product=product,
        firmware_version=firmware,
        bacnet_ports=bacnet_ports,
        router_db=router_db,
        network_settings=network,
        stored_settings=stored,
        config_file=config_file,
        errors=collector.errors,
    )


def collect_stats(client: FieldServerClient) -> StatsDocument:
    """Gather point-in-time usage statistics from one FieldServer.

    Raises :class:`FieldServerError` if the device cannot be reached or
    authenticated; individual read failures are recorded in ``errors`` instead.
    """
    _require_session(client)
    collector = _Collector(client.host)

    product = collector.product(client)
    system_status = collector.try_read("systemStatus/getSystemStatus", client.get_system_status)
    # getNetworkStatus needs an interface name; getSnapshot covers the whole
    # network in one parameterless call.
    network_status = collector.try_read("smcNetwork/getSnapshot", client.get_network_snapshot)
    stats = collector.try_read("stats/getAllStats", client.get_all_stats)

    ports = {
        name: split_port(port)[1]
        for name, port in _read_ports(client, collector).items()
        if isinstance(port, dict)
    }

    return StatsDocument(
        host=client.host,
        collected_at=_now(),
        product=product,
        ports=ports,
        system_status=system_status,
        network_status=network_status,
        stats=stats,
        errors=collector.errors,
    )
