"""Fan out collection across many FieldServers, one JSON file per device.

An unreachable device must not abort the run: each host's outcome is captured in
a :class:`HostResult` and reported at the end.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .client import (
    DEFAULT_PACING,
    DEFAULT_SCHEME,
    DEFAULT_TIMEOUT,
    FieldServerClient,
    FieldServerError,
)
from .collect import collect_config, collect_stats
from .models import ConfigDocument, StatsDocument

__all__ = [
    "DocumentKind",
    "HostResult",
    "export_host",
    "export_hosts",
    "read_hosts_file",
    "safe_filename",
]

DocumentKind = Literal["config", "stats"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(slots=True)
class HostResult:
    """Outcome of collecting from one host."""

    host: str
    path: Path | None = None
    error: str | None = None
    partial_errors: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


def safe_filename(host: str, kind: DocumentKind) -> str:
    """Build a filesystem-safe output name for a host.

    Hosts may be bare IPs, ``host:port`` pairs or full URLs, so the scheme and
    any separators are flattened rather than trusted as path components.
    """
    stem = host.strip().removeprefix("https://").removeprefix("http://").strip("/")
    stem = _UNSAFE.sub("_", stem).strip("_") or "fieldserver"
    suffix = "-stats" if kind == "stats" else ""
    return f"{stem}{suffix}.json"


def read_hosts_file(path: Path) -> list[str]:
    """Read a newline-delimited host list, ignoring blanks and ``#`` comments."""
    hosts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            hosts.append(entry)
    return hosts


def export_host(
    host: str,
    username: str,
    password: str,
    out_dir: Path,
    *,
    kind: DocumentKind = "config",
    scheme: str = DEFAULT_SCHEME,
    timeout: float = DEFAULT_TIMEOUT,
    verify_tls: bool = True,
    max_retries: int = 3,
    backoff: float = 1.0,
    pacing: float = DEFAULT_PACING,
    indent: int = 2,
) -> HostResult:
    """Collect from one host and write its JSON document.

    Never raises for device-level problems: an unreachable or unauthorised host
    is returned as a failed :class:`HostResult`.
    """
    try:
        with FieldServerClient(
            host,
            username,
            password,
            scheme=scheme,
            timeout=timeout,
            verify_tls=verify_tls,
            max_retries=max_retries,
            backoff=backoff,
            pacing=pacing,
        ) as client:
            document: ConfigDocument | StatsDocument = (
                collect_stats(client) if kind == "stats" else collect_config(client)
            )
    except FieldServerError as exc:
        return HostResult(host=host, error=str(exc))
    except OSError as exc:
        return HostResult(host=host, error=f"{type(exc).__name__}: {exc}")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / safe_filename(host, kind)
    try:
        path.write_text(
            json.dumps(document.model_dump(mode="json", by_alias=False), indent=indent) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return HostResult(host=host, error=f"writing {path}: {exc}")

    return HostResult(host=host, path=path, partial_errors=len(document.errors))


def export_hosts(
    hosts: Iterable[str],
    username: str,
    password: str,
    out_dir: Path,
    *,
    kind: DocumentKind = "config",
    scheme: str = DEFAULT_SCHEME,
    timeout: float = DEFAULT_TIMEOUT,
    verify_tls: bool = True,
    max_retries: int = 3,
    backoff: float = 1.0,
    pacing: float = DEFAULT_PACING,
    workers: int = 4,
    indent: int = 2,
) -> list[HostResult]:
    """Collect from many hosts concurrently, one output file each.

    Concurrency is across devices only — each device is paced internally, since
    these gateways degrade under a burst of requests.
    """
    host_list: Sequence[str] = list(dict.fromkeys(h.strip() for h in hosts if h.strip()))
    if not host_list:
        return []

    def run(host: str) -> HostResult:
        return export_host(
            host,
            username,
            password,
            out_dir,
            kind=kind,
            scheme=scheme,
            timeout=timeout,
            verify_tls=verify_tls,
            max_retries=max_retries,
            backoff=backoff,
            pacing=pacing,
            indent=indent,
        )

    if len(host_list) == 1 or workers <= 1:
        return [run(host) for host in host_list]

    with ThreadPoolExecutor(max_workers=min(workers, len(host_list))) as pool:
        return list(pool.map(run, host_list))
