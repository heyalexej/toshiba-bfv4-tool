# AGENTS.md

Guidance for coding agents and human contributors working on this repository.
This is an independent, community-maintained CLI for Toshiba B-FV4 and B-FV4D
label printers, built on the printers' documented TPCL and raw-socket
interfaces. The project is not affiliated with Toshiba and ships no vendor
assets. Keep that positioning neutral in all files.

## Architecture

Two thin frontends over one offline protocol core:

- `src/toshiba_bfv4_tool/core.py` — the `toshiba-bfv4` CLI: pure byte builders
  for status, LAN, TPCL, emulation, maintenance, and printer-filesystem
  operations, plus strict response parsers and the preview/apply gate.
- `src/toshiba_bfv4_tool/queries.py` — the self-describing read-only query
  registry, strict parsers for the four documented snapshot frames, and
  diagnostic-query outcomes that preserve raw response bytes.
- `src/toshiba_bfv4_tool/lan.py` — the `toshiba-bfv4-lan` CLI: a read-only
  multi-printer probe. It must stay read-only.
- `src/toshiba_bfv4_tool/firmware.py` — validated operator-supplied `.abin`
  package parsing and the guarded firmware transport. It must never bundle or
  download vendor firmware.
- `tests/test_core.py` — offline, byte-exact protocol tests. No test requires
  a live printer or network access.
- `pyproject.toml` / `uv.lock` — uv + hatchling, Python >= 3.11, pydantic 2.

Layering rule: builders and parsers are pure functions with no I/O; all
network I/O lives in the small `exchange()` helpers; argparse frontends only
wire arguments to builders. Preserve this separation in every change. Data
crossing a boundary is validated by a pydantic model with
`extra="forbid"` so unknown or malformed fields fail loudly.

## Command reference

Use `uv run` for every command during development. The installed console
scripts are:

```text
toshiba-bfv4 <command> ...       single-printer CLI
toshiba-bfv4-lan <host> [...]    read-only multi-printer probe
```

The single-printer CLI provides these commands:

- `capabilities` — print the supported feature groups without contacting a
  printer.
- `status HOST` — read status, receive-buffer capacity, firmware/version, and
  printer identity.
- `query-list` — list the read-only maintenance query names and wire details.
- `query HOST QUERY [VALUE]` — run one maintenance query. Supported queries
  include the documented snapshots `status`, `buffer`, `version`, and
  `identity`, plus `system-version`, `config`, `media-info`, `tph-info`,
  `form-list`, `font-list`, `graphic-list`, `info`, `task-status`,
  `burn-status`, and `last-state`.
  Diagnostic responses use a query-specific 64 KiB receive limit and expose
  `response_truncated` when that bound is exceeded; the fixed snapshot
  frames retain their strict documented lengths.
- `lan HOST [OPTIONS]` — preview or apply IP, gateway, subnet, DHCP, client
  ID, raw socket, and socket-port settings.
- `tpcl-parameter HOST [OPTIONS]` — preview or apply the TPCL ESC Z2;1
  parameter page.
- `tpcl-fine HOST [OPTIONS]` — preview or apply the TPCL ESC Z2;2 fine
  adjustment.
- `emulation HOST MODE` — preview or apply an emulation mode: `D`, `E`, `I`,
  `Z`, `TPCL`, `AUTO`, or `AUTO2`.
- `tpcl-page HOST --protocol PROTOCOL --count COUNT --body BODY` — preview or
  apply a TPCL parameter page using `setnvrr` or `setnvrs`.
- `tpcl-general HOST CODE=VALUE ...` — preview or apply TPCL-General parameter
  updates.
- `settings-export FILE [OPTIONS]` — write a partial, validated local JSON
  bundle; it does not contact a printer or read settings back from one.
- `settings-apply HOST --file FILE` — preview or apply a bundle through the
  same `--apply --yes` gate as individual writes.
- `pc-save-start --id ID` and `pc-save-end` — preview the TPCL PC-command
  save-mode envelope; neither command transmits because raw save bodies are
  intentionally not implemented.
- `pc-save-call HOST --id ID` — preview or apply a stored PC command call;
  `--auto-call` is an explicit persistent power-on change.
- `download-paths` — list supported printer-side filesystem paths.
- `download-header PATH --filename NAME --size SIZE_BYTES` — preview a
  filesystem-download header. It never transmits file data.
- `firmware HOST --package PATH` — validate an operator-supplied `.zip` or
  `.abin` package and print the flash plan. Applying it additionally requires
  `--apply --yes`; `--force` is required to retransmit the same master version.
- `single HOST OPERATION [VALUE]` — preview or apply a mapped operation:
  `media-calibration`, `ribbon-calibration`, `reboot`, `self-test`,
  `factory-reset`, `reset-command`, `reset`, or `wr-reset`.

Network commands accept `--port`, `--timeout`, and `--settle-delay`. The LAN
probe accepts one or more hosts and can be narrowed with repeated `--only
status`, `--only buffer`, `--only version`, or `--only info` options:

```bash
uv run toshiba-bfv4 status 192.0.2.10
uv run toshiba-bfv4 query-list
uv run toshiba-bfv4-lan 192.0.2.10 192.0.2.11 --only version --only status
```

Run `uv run toshiba-bfv4 COMMAND --help` for the complete option set. Every
command that can change printer state is preview-only unless both `--apply`
and `--yes` are supplied.

## Safe change rules

- Read-only by default. No new code path may open a socket and transmit
  unless it goes through the existing preview/apply gate with explicit
  `--apply --yes`.
- Every mutating `CommandPreview` must set `dangerous` and/or
  `requires_reset` truthfully; previews are the user's risk assessment.
- Validate before building bytes: addresses via `ipaddress`, ports and sizes
  via pydantic field constraints, download filenames as single ASCII path
  components (path traversal is rejected by design — keep it that way).
- Never weaken the `parse_*` length and framing checks. A response that does
  not match the documented shape must raise, not guess.
- One command per fresh connection, with the documented settle delay between
  queries. Do not add automatic retries for mutating commands.
- Raw printer-filesystem transfers (file bytes) remain disabled; only the
  header/plan preview exists. Do not add file transmission casually.
- Firmware is a separate flash-image protocol, not a filesystem transfer. It
  may transmit only after package/header/payload CRC validation, target
  preflight, and explicit `--apply --yes`. Keep the default chunk size at 8192
  bytes unless a protocol source proves a different safe limit. `burnstatus`
  returns a nonzero first field while burning is still in progress; poll until
  the first field is `00` before sending reboot/exit.

## Protocol-registry principle

All wire-format knowledge is centralized, never inlined:

- Framing and envelopes come from the shared constants and helpers
  (`frame()`, `ESC`, `LF_NUL`, `CR_LF`, `BYTE_SPECIAL`, `BYTE_EXIT`,
  `BYTE_REBOOT_1`).
- Command families are registered in mappings and typed registries
  (`REGISTRY` in `queries.py`, `DOWNLOAD_PATHS`, `TPCL_GENERAL_CODES`,
  `STATUS_DETAILS`, and the `CAPABILITIES` manifest) instead of scattered
  literals.
- Adding protocol support means: new registry entry, new pure builder
  returning a `CommandPreview`, new offline test pinning the exact bytes. Do
  not embed raw byte strings in CLI handlers, and do not fork a second
  framing path.

## Preview/Apply gate

`apply_previews()` is the choke point for ordinary command writes, and
`apply_firmware_update()` is the corresponding choke point for the streaming
flash-image protocol. Both print a full offline plan first and transmit only
when both `--apply` and `--yes` are present. Never add a side channel that
bypasses these gates. Read paths (`status`, `query`, the LAN probe) never
mutate printer state.

## Testing obligations

- Every protocol change ships with offline tests asserting the exact
  `payload_hex` / `payload_ascii` output and the exact parser behavior for
  documented response shapes.
- Run before every commit or PR — CI enforces the same:

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pytest
  ```

- Use `uv run …` exclusively; never `python` or `pip` directly.
- Tests use synthetic fixtures only. Never point tests, examples, or
  docstrings at real printers.

## Compatibility profiles

The B-FV4 family varies by model, firmware, and optional modules:

- Keep the `CAPABILITIES` manifest accurate: `bfv4d_relevant` versus
  `family_optional` must reflect what a bare B-FV4D needs versus what depends
  on options (WLAN, Bluetooth, emulation pages, downloads).
- Firmware-dependent queries (for example `WV`) must degrade gracefully into
  the snapshot `errors` dict, not crash the run.
- Do not assume capabilities across models; gate new features behind an
  explicit registry entry and document the firmware/module dependency.
- Users must verify behavior against their exact model and firmware before
  applying changes; docs should keep saying so.

## Public data hygiene

This repository is public. Never commit:

- private IP addresses, hostnames, MAC addresses, queue names, or any
  site-specific defaults (network targets are always explicit arguments);
- serial-number inventories or other data read from real printers;
- vendor firmware, fonts, icons, proprietary application assets, or printer
  configuration files;
- credentials of any kind.

Examples and fixtures use documentation ranges such as `192.0.2.0/24`
(TEST-NET-1). Scrub diffs, commit messages, and issue templates with the same
care as code.

## Release checklist

1. All changes landed with tests; `ruff check`, `ruff format --check`, and
   `pytest` pass locally and in CI (`uv sync --locked` must stay clean —
   commit `uv.lock` together with `pyproject.toml`).
2. `CAPABILITIES`, README examples, and CLI help reviewed for consistency
   with actual behavior.
3. Diff scanned for private data (addresses, serials, hostnames, paths).
4. Version bumped in `pyproject.toml` following SemVer; breaking CLI or wire
   behavior is a MAJOR change.
5. Release commit follows Conventional Commits; tag as `vX.Y.Z`.
6. After tagging, verify a clean install: `uv tool install
   git+<repo-url>@vX.Y.Z` and a preview-mode smoke run against a
   documentation-range address.
