"""Download configuration and usage statistics from MSA / Sierra Monitor FieldServers.

Library usage::

    from msa_fieldserver_config import FieldServerClient, collect_config

    with FieldServerClient("192.0.2.6", "ssi", secret) as fs:
        document = collect_config(fs)
        print(document.model_dump_json(indent=2))
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from .client import (
    FieldServerAPIError,
    FieldServerAuthError,
    FieldServerClient,
    FieldServerError,
    FieldServerPermissionError,
    FieldServerTransportError,
    normalize_base_url,
)
from .collect import collect_config, collect_stats
from .export import HostResult, export_host, export_hosts
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

try:
    __version__ = _version("msa-fieldserver-config")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    __version__ = "0.0.0+dev"

__all__ = [
    "CollectionError",
    "ConfigDocument",
    "ConfigFile",
    "FieldServerAPIError",
    "FieldServerAuthError",
    "FieldServerClient",
    "FieldServerError",
    "FieldServerPermissionError",
    "FieldServerTransportError",
    "HostResult",
    "PortConfig",
    "PortStatistics",
    "PortStatisticsEntry",
    "ProductInfo",
    "RouteEntry",
    "StatsDocument",
    "__version__",
    "collect_config",
    "collect_stats",
    "export_host",
    "export_hosts",
    "normalize_base_url",
]
