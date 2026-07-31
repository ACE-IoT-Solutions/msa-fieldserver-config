# msa-fieldserver-config

Download configuration and usage statistics from MSA Safety / Sierra Monitor
**FieldServer** BACnet gateways — as a CLI, and as a Python library.

Point it at one FieldServer or a fleet of them, give it one set of credentials,
and get a JSON document per device.

## Install

```bash
uv sync                     # development
uv tool install .           # as a CLI on your PATH
```

Requires **Python 3.10 or newer**. The test suite is verified on 3.10, 3.11,
3.12 and 3.13; development happens on 3.13 (`.python-version`).

If you change the floor, move `[tool.ruff] target-version` with it — otherwise
ruff's `UP` rules rewrite code into syntax the floor doesn't support.

## Credentials

Put them in a `.env` beside the project (see `.env.example`); `.env` is
gitignored:

```
FIELDSERVER_USERNAME=ssi
FIELDSERVER_PASSWORD=...
FIELDSERVER_HOSTS=192.0.2.6,192.0.2.8     # optional default host list
```

They can equally come from the real environment or `--username` / `--password`.
CLI flags win over the environment, which wins over `.env`.

## Usage

```bash
# Configuration -> out/<host>.json
msa-fieldserver export 192.0.2.6 192.0.2.8

# A whole fleet from a file ('#' comments allowed)
msa-fieldserver export --hosts-file fleet.txt --out-dir snapshots

# Usage statistics -> out/<host>-stats.json
msa-fieldserver stats --hosts-file fleet.txt

# Check one device: reachable? credentials good? which reads does it support?
msa-fieldserver probe 192.0.2.6
```

Hosts may be bare IPs (`192.0.2.6`), `host:port` pairs (`localhost:6080`, e.g.
an SSH port-forward), or full URLs. Bare hosts default to `http://` on port 80.

Exit codes: `0` all hosts succeeded, `1` at least one host failed, `2` bad
arguments.

### Config and statistics are separate on purpose

The device returns port configuration, message counters and the routing table in
a single payload. This tool splits them:

- **`<host>.json`** — configuration only. Stable between runs, so `diff` against
  a previous snapshot shows real configuration drift.
- **`<host>-stats.json`** — counters, routing table, system status. These move
  constantly and would otherwise make every config diff noisy.

### Partial results are normal

Firmware builds differ in which mesh components they expose, and accounts differ
in what they may call. A device that answers some reads and refuses others still
produces a document; the failures are listed inline:

```json
{
  "host": "192.0.2.6",
  "product": {"product_name": "BACnet Router", "product_version": "8.0.1"},
  "bacnet_ports": {"ETH1 - BACnet IP Wired 1": {"Network Number": 15051}},
  "errors": [
    {"source": "pe/getFirmwareVersion", "message": "pe/getFirmwareVersion: Access denied"}
  ]
}
```

`Access denied` means the account lacks permission for that method, not that the
firmware is missing it — the `ssi` service account is refused several reads on
8.0.1. Use `probe` to see exactly what a given device and account allow.

### If a device returns "Unknown db name bacnet_router"

The unit is almost certainly in **Recovery Mode**, where the protocol engine's
runtime databases are not registered. It needs a reboot; no client-side change
will help.

This is worth knowing because the usual health checks miss it — `pe.isOnline`
still returns `true` and `systemStatus` still reports "System running smoothly".
Only the device's message screen admits it, so `probe` checks that first and
tells you before listing failures that would otherwise look like your problem.

## Library

```python
from msa_fieldserver_config import FieldServerClient, collect_config, collect_stats

with FieldServerClient("192.0.2.6", "ssi", password) as fs:
    config = collect_config(fs)
    stats = collect_stats(fs)

    print(config.model_dump_json(indent=2))
    for name, port in stats.ports.items():
        print(name, port.statistics.info, len(port.routing_table), "routes")

    # Anything the wrappers don't cover:
    raw = fs.call("pe", "rpc_db_read_json", db="bacnet_router", path="Ports")
```

When calling methods directly, the argument style is not cosmetic — it selects
the wire envelope, and the device's methods disagree about which they want:

```python
fs.call("pe", "rpc_db_read_json", db="bacnet_router", path="Ports")  # -> {"parameters": {...}}
fs.call("pe", "fsReadMsgScreen", "Errors")  # -> {"parameters": [...]}
```

Keyword arguments become an options object; positional arguments become an
argument array. Methods the UI calls with a bare scalar need the positional form
— given an object they reply `Invalid selectString: null`, which looks like a
missing parameter name but isn't.

Fan out over a fleet:

```python
from pathlib import Path
from msa_fieldserver_config import export_hosts

for result in export_hosts(hosts, "ssi", password, Path("out"), kind="config"):
    print(result.host, "ok" if result.ok else result.error)
```

Everything is Pydantic v2, so `.model_dump()` / `.model_dump_json()` work
throughout, and unrecognised device fields are preserved rather than dropped.

### The configuration file

`config.csv` comes back already parsed, as ordered sections. Section names
repeat — one `Connections` block per connection — so `sections` is a list of
single-key dicts rather than a mapping, and `.section()` flattens them:

```python
cfg = config.config_file
for conn in cfg.section("Connections"):
    print(conn["Connection_Name"], conn["Protocol"], conn["Router_Network_Number"])
```

This is stored configuration rather than runtime state, so it still reads on a
device whose protocol engine is in Recovery Mode — making it the most dependable
configuration source available.

### Read-only

Only read methods are wrapped. The device exposes destructive neighbours on the
same components — `pe.postConfig`, `pe.restart`, `smcNetwork.setNetworkSettings`
— and reaching those requires deliberately calling `fs.call(...)`. Nothing in
this tool's normal operation writes to a device.

## A malformed call can crash the device

This is the most important thing to know before extending this client.

Sending `pe/getConfig` the object envelope — `{"parameters": {"filename": "..."}}`
instead of `{"parameters": ["..."]}` — does not return a tidy error. It takes the
**protocol engine down**. The web layer then returns plain-text `502 Bad Gateway`
for every endpoint, including ones that worked a second earlier, for roughly 75
seconds while the engine restarts. Repeat it enough and the unit comes back in
**Recovery Mode** and needs a physical reboot.

The client guards against this: `call()` refuses to send a known scalar-argument
method (`pe/getConfig`, `pe/fsReadMsgScreen`) in object form. Keep that list
current if you add wrappers — the whole reason it exists is that the failure is
silent, delayed, and expensive.

A plain-text `502` from this device usually means *the engine just died*, not
*slow down*. Retrying harder makes it worse.

### Pacing

Requests to one device are paced 0.5s apart by default, and parallelism is
**across** devices rather than within one.

Honest caveat: pacing was originally added to explain the 502s above, and that
diagnosis was wrong — there's no evidence these gateways are rate-sensitive, and
nine sequential reads at 0.15s spacing were fine. It's kept because the risk is
asymmetric: pacing costs seconds, while being wrong about an embedded gateway
costs a site visit. Tune it freely once you know a deployment:

```bash
msa-fieldserver export --hosts-file fleet.txt --pacing 0.15 --workers 8
```

`--pacing 0` disables it entirely.

## Development

```bash
uv run pytest                 # tests
uv run pytest --cov           # with coverage
uv run ruff check --fix .     # lint
uv run ruff format .          # format
uv run pyrefly check          # type check (must pass)
uv run pre-commit install     # enable hooks
```

### Before extending the client

The device is a **Happner (happn-3) mesh**, not a REST API — the web UI drives it
over a Primus WebSocket, and the HTTP surface used here is a `happner-rest`
bridge onto the same component exchange. Almost nothing about it is guessable,
and `/rest/describe` is `403`, so the API is not self-documenting.

Practical consequences, all learned the hard way against real hardware:

- **Argument shape selects the calling convention** — object vs array, and
  getting it wrong on a scalar-argument method crashes the protocol engine
  rather than returning an error. See the section above; the guard in
  `client.py` exists for exactly this.
- **A plain-text `502` means the engine died**, not "slow down". Retrying
  aggressively compounds it.
- **`Access denied` is a device-side permission**, not a missing feature — it
  varies by account.
- **Partial documents are normal** and are the design, not a fallback.

The mesh exposes 34 components; `msa-fieldserver probe` is the quickest way to
see what a given device and account actually allow. Destructive methods
(`pe.postConfig`, `pe.restart`, `smcNetwork.setNetworkSettings`) sit right next
to the read methods on the same components, so add wrappers deliberately.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by MSA Safety or Sierra Monitor. "FieldServer"
is their trademark; this is an independent client built by observing a device's
own web interface.
