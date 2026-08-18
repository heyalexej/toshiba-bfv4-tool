# Toshiba B-FV4 Tool

Community-maintained command-line tooling for Toshiba B-FV4 and B-FV4D label
printers. The project is independent, is not affiliated with Toshiba, and is
provided for operators who need predictable LAN diagnostics and configuration.

## Features

- read printer status, receive-buffer state, firmware, model, and serial data;
- inspect and preview LAN, socket, TPCL, emulation, and maintenance commands;
- inspect a self-describing read-only query registry with exact request bytes;
- apply mutating commands only with an explicit `--apply --yes` confirmation;
- preview printer filesystem transfer headers without transmitting file data;
- validate operator-supplied `.abin` firmware packages and apply them through
  a guarded raw-image update flow;
- probe several printers from one command with the read-only LAN client.

The project deliberately keeps network targets explicit. No private IP
addresses, queue names, hostnames, or organization-specific defaults are
embedded in the package.

## Install

With `uv`:

```bash
uv tool install .
```

After publication, the tool can also be installed directly from the Git
repository.

For a checkout:

```bash
uv sync
uv run pytest
```

## Examples

Read one printer:

```bash
toshiba-bfv4 status 192.0.2.10
```

Probe several printers without changing them:

```bash
toshiba-bfv4-lan 192.0.2.10 192.0.2.11 --only status --only info
```

List available maintenance queries without contacting a printer:

```bash
toshiba-bfv4 query-list
```

The four documented snapshot queries (`status`, `buffer`, `version`, and
`identity`) use strict length and framing checks. Additional maintenance
queries are diagnostic and read-only; an empty answer means that the exact
firmware does not support that query and must not be retried automatically.

Preview a LAN change:

```bash
toshiba-bfv4 lan 192.0.2.10 --socket on --socket-port 9100
```

Apply a change only after reviewing the preview:

```bash
toshiba-bfv4 lan 192.0.2.10 --socket on --socket-port 9100 --apply --yes
```

Validate a firmware package without contacting a printer:

```bash
toshiba-bfv4 firmware 192.0.2.10 --package ./B-FV4-firmware.zip
```

The firmware command checks every `.abin` header and payload CRC32 and prints
the complete plan without transmitting bytes by default. After reviewing the
plan, an operator may explicitly apply it with `--apply --yes`. The package is
operator-supplied; this project neither ships nor downloads Toshiba firmware.
During an apply, `burnstatus` is polled until the printer reports `00`; a
nonzero status means that the flash operation is still in progress. The wait is
bounded by `--burn-timeout` (300 seconds by default).

## Safety model

Status and preview commands are read-only. Network settings, emulation,
parameter, reset, filesystem-transfer headers, and firmware operations never
transmit changes unless both `--apply` and `--yes` are present. Firmware also
requires a ready or idle-after-label B-FV4 target, validates the package before opening a socket,
checks `burnstatus` until completion, and only then sends `reboot 1`/`exit`. Test changes on one
printer first and keep a recovery path available before changing LAN settings.

The tool speaks the printer's documented TPCL/socket interfaces. It does not
ship vendor firmware, fonts, icons, proprietary application assets, or printer
configuration files.

## Compatibility

The tool is intended for the Toshiba B-FV4 family. Firmware and optional
modules can expose different capabilities, so every command should be tested
against the exact printer model and firmware in use.

Toshiba and B-FV4 are trademarks of their respective owner. This is an
independent community project and is not an official Toshiba product.

## License

MIT. See [LICENSE](LICENSE).
