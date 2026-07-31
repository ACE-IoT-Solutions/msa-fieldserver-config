"""Command-line interface for msa-fieldserver-config."""

from __future__ import annotations

import contextlib
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .client import (
    DEFAULT_PACING,
    DEFAULT_SCHEME,
    DEFAULT_TIMEOUT,
    RECOVERY_MODE_MARKER,
    FieldServerClient,
    FieldServerError,
)
from .export import DocumentKind, HostResult, export_hosts, read_hosts_file

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Download configuration and usage statistics from MSA / Sierra Monitor FieldServers.",
)
console = Console()
err_console = Console(stderr=True)

# CLI flags take precedence over these; .env does not clobber a real environment.
load_dotenv(override=False)


def _version() -> str:
    try:
        return _pkg_version("msa-fieldserver-config")
    except PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0+dev"


HostsArg = Annotated[
    list[str] | None,
    typer.Argument(help="FieldServer IPs, host:port pairs, or URLs.", metavar="[HOSTS]..."),
]
HostsFileOpt = Annotated[
    Path | None,
    typer.Option("--hosts-file", "-f", help="File of hosts, one per line ('#' comments allowed)."),
]
UserOpt = Annotated[
    str | None, typer.Option("--username", "-u", envvar="FIELDSERVER_USERNAME", help="Login user.")
]
PassOpt = Annotated[
    str | None,
    typer.Option("--password", "-p", envvar="FIELDSERVER_PASSWORD", help="Login password."),
]
OutOpt = Annotated[Path, typer.Option("--out-dir", "-o", help="Directory for JSON output.")]
SchemeOpt = Annotated[
    str, typer.Option("--scheme", envvar="FIELDSERVER_SCHEME", help="http or https.")
]
TimeoutOpt = Annotated[
    float, typer.Option("--timeout", envvar="FIELDSERVER_TIMEOUT", help="Per-request timeout (s).")
]
InsecureOpt = Annotated[
    bool, typer.Option("--insecure", help="Skip TLS verification (self-signed device certs).")
]
WorkersOpt = Annotated[int, typer.Option("--workers", "-w", help="Devices collected in parallel.")]
PacingOpt = Annotated[
    float,
    typer.Option(
        "--pacing",
        help="Minimum seconds between requests to one device. Raise it if the "
        "device starts returning 502s; 0 disables pacing.",
    ),
]
RetriesOpt = Annotated[
    int, typer.Option("--retries", help="Attempts per request when the device is unreachable.")
]


def _resolve_hosts(hosts: list[str] | None, hosts_file: Path | None) -> list[str]:
    resolved = list(hosts or [])
    if hosts_file:
        if not hosts_file.is_file():
            err_console.print(f"[red]No such hosts file:[/red] {hosts_file}")
            raise typer.Exit(2)
        resolved.extend(read_hosts_file(hosts_file))
    if not resolved:
        import os

        resolved.extend(
            h.strip() for h in os.environ.get("FIELDSERVER_HOSTS", "").split(",") if h.strip()
        )
    if not resolved:
        err_console.print(
            "[red]No hosts given.[/red] Pass them as arguments, use --hosts-file, "
            "or set FIELDSERVER_HOSTS."
        )
        raise typer.Exit(2)
    return resolved


def _resolve_credentials(username: str | None, password: str | None) -> tuple[str, str]:
    if not username or not password:
        missing = "username" if not username else "password"
        err_console.print(
            f"[red]No {missing}.[/red] Use --{missing}, set FIELDSERVER_{missing.upper()}, "
            "or put it in .env."
        )
        raise typer.Exit(2)
    return username, password


def _report(results: list[HostResult], kind: DocumentKind) -> int:
    table = Table(title=f"FieldServer {kind} export", header_style="bold")
    table.add_column("Host")
    table.add_column("Result")
    table.add_column("Output")

    for r in sorted(results, key=lambda x: x.host):
        if r.ok and r.partial_errors:
            status = f"[yellow]partial ({r.partial_errors} read(s) failed)[/yellow]"
        elif r.ok:
            status = "[green]ok[/green]"
        else:
            status = f"[red]failed[/red] {r.error}"
        table.add_row(r.host, status, str(r.path) if r.path else "-")

    console.print(table)

    failed = [r for r in results if not r.ok]
    partial = [r for r in results if r.ok and r.partial_errors]
    console.print(
        f"{len(results) - len(failed)}/{len(results)} succeeded"
        + (f", {len(partial)} partial" if partial else "")
    )
    if failed:
        return 1
    return 0


def _run(
    kind: DocumentKind,
    hosts: list[str] | None,
    hosts_file: Path | None,
    username: str | None,
    password: str | None,
    out_dir: Path,
    scheme: str,
    timeout: float,
    insecure: bool,
    workers: int,
    pacing: float,
    retries: int,
) -> None:
    resolved_hosts = _resolve_hosts(hosts, hosts_file)
    user, secret = _resolve_credentials(username, password)

    results = export_hosts(
        resolved_hosts,
        user,
        secret,
        out_dir,
        kind=kind,
        scheme=scheme,
        timeout=timeout,
        verify_tls=not insecure,
        workers=workers,
        pacing=pacing,
        max_retries=retries,
    )
    raise typer.Exit(_report(results, kind))


@app.command()
def export(
    hosts: HostsArg = None,
    hosts_file: HostsFileOpt = None,
    username: UserOpt = None,
    password: PassOpt = None,
    out_dir: OutOpt = Path("out"),
    scheme: SchemeOpt = DEFAULT_SCHEME,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    insecure: InsecureOpt = False,
    workers: WorkersOpt = 4,
    pacing: PacingOpt = DEFAULT_PACING,
    retries: RetriesOpt = 3,
) -> None:
    """Download each FieldServer's configuration to <host>.json."""
    _run(
        "config",
        hosts,
        hosts_file,
        username,
        password,
        out_dir,
        scheme,
        timeout,
        insecure,
        workers,
        pacing,
        retries,
    )


@app.command()
def stats(
    hosts: HostsArg = None,
    hosts_file: HostsFileOpt = None,
    username: UserOpt = None,
    password: PassOpt = None,
    out_dir: OutOpt = Path("out"),
    scheme: SchemeOpt = DEFAULT_SCHEME,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    insecure: InsecureOpt = False,
    workers: WorkersOpt = 4,
    pacing: PacingOpt = DEFAULT_PACING,
    retries: RetriesOpt = 3,
) -> None:
    """Download each FieldServer's usage statistics to <host>-stats.json."""
    _run(
        "stats",
        hosts,
        hosts_file,
        username,
        password,
        out_dir,
        scheme,
        timeout,
        insecure,
        workers,
        pacing,
        retries,
    )


@app.command()
def probe(
    host: Annotated[str, typer.Argument(help="A single FieldServer to check.")],
    username: UserOpt = None,
    password: PassOpt = None,
    scheme: SchemeOpt = DEFAULT_SCHEME,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    insecure: InsecureOpt = False,
    pacing: PacingOpt = DEFAULT_PACING,
) -> None:
    """Check reachability, credentials, and which reads this firmware supports."""
    user, secret = _resolve_credentials(username, password)

    checks: list[tuple[str, object]] = []
    recovery_mode = False
    try:
        with FieldServerClient(
            host,
            user,
            secret,
            scheme=scheme,
            timeout=timeout,
            verify_tls=not insecure,
            pacing=pacing,
        ) as client:
            client.login()
            console.print(f"[green]Authenticated[/green] to {client.base_url} as {user}")

            # Check this first: a unit in Recovery Mode has no protocol-engine
            # databases registered, so the reads below fail for reasons that
            # have nothing to do with the account or this tool.
            with contextlib.suppress(FieldServerError):
                recovery_mode = RECOVERY_MODE_MARKER in str(client.read_message_screen())

            for label, fn in [
                ("smcCore/getProductInfo", client.get_product_info),
                ("pe/getFirmwareVersion", client.get_firmware_version),
                ("systemStatus/getSystemStatus", client.get_system_status),
                ("smcNetwork/getAllNetworkSettings", client.get_all_network_settings),
                ("smcNetwork/getSnapshot", client.get_network_snapshot),
                ("stats/getAllStats", client.get_all_stats),
                ("pe/rpc_bacnet_read_stored_settings", client.read_stored_settings),
                ("pe/rpc_db_read_json(Ports)", client.read_bacnet_ports),
                ("pe/getConfig", client.get_config),
            ]:
                try:
                    fn()
                    checks.append((label, None))
                except FieldServerError as exc:
                    checks.append((label, exc))
    except FieldServerError as exc:
        err_console.print(f"[red]{host}: {exc}[/red]")
        raise typer.Exit(1) from exc

    if recovery_mode:
        err_console.print(
            "[yellow]This device is in Recovery Mode.[/yellow] Its protocol-engine "
            "databases are not registered, so configuration and statistics reads "
            "will fail until it is rebooted. Failures below are not conclusive."
        )

    table = Table(header_style="bold")
    table.add_column("Read")
    table.add_column("Status")
    for label, failure in checks:
        table.add_row(label, "[green]ok[/green]" if failure is None else f"[red]{failure}[/red]")
    console.print(table)
    raise typer.Exit(0 if all(f is None for _, f in checks) and not recovery_mode else 1)


@app.callback(invoke_without_command=True)
def main(
    show_version: Annotated[
        bool, typer.Option("--version", "-V", help="Show the version and exit.")
    ] = False,
) -> None:
    if show_version:
        console.print(_version())
        raise typer.Exit(0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
