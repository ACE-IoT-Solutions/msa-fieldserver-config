# Changelog

All notable changes to this project are documented here. This project follows
[semantic versioning](https://semver.org/).

## 0.1.0 — 2026-07-30

Initial release.

### Added

- `FieldServerClient` — read-only HTTP client for the FieldServer happner-rest
  bridge (`POST /rest/login`, `POST /rest/method/{component}/{method}`), with
  bearer-token auth, transparent re-login on token expiry, request pacing, and
  retry-with-backoff on transport failures.
- `collect_config()` / `collect_stats()` — per-device collection that records
  individual read failures inline instead of aborting, so firmware and
  permission differences yield a partial document rather than nothing.
- `export_host()` / `export_hosts()` — fleet fan-out writing one JSON file per
  device, parallel across devices, where one unreachable device does not fail
  the run.
- Pydantic v2 models for all documents; unrecognised device fields are preserved.
- CLI `msa-fieldserver` with `export`, `stats`, and `probe` commands. Hosts may
  be bare IPs, `host:port` pairs, or full URLs; credentials come from flags, the
  environment, or `.env`.
- `FieldServerPermissionError` distinguishing "your account may not call this"
  from "this call failed".
- Support for both happner-rest calling conventions: keyword arguments send an
  options object, positional arguments send an argument array. Scalar-argument
  methods (`pe/getConfig`, `pe/fsReadMsgScreen`) require the latter.
- Recovery Mode detection in `probe`. A unit in Recovery Mode has no
  protocol-engine databases registered and fails config reads, while
  `pe.isOnline` and `systemStatus` both still report healthy.
- `ConfigFile` model for the parsed `config.csv` (`errors` + ordered `sections`),
  with `.section(name)` to flatten repeated section names.
- Crash guard: `call()` refuses to send a known scalar-argument method
  (`pe/getConfig`, `pe/fsReadMsgScreen`) in object form. That shape does not
  return an error — it crashes the device's protocol engine, 502s every endpoint
  for ~75s, and on repetition forces the unit into Recovery Mode.

- MIT license.
- Supports Python 3.10+ (verified on 3.10 through 3.13), rather than 3.13 only.
  Only two constructs required 3.11 — `typing.Self` and `datetime.UTC` — and
  both had direct pre-3.11 equivalents.

### Notes

Verified against a live MSA Safety BACnet Router, firmware 8.0.1. Protocol
notes and the list of still-unconfirmed endpoints are in
`docs/plans/fieldserver-config-export.md`.
