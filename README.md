# Toshiba B-FV4 Tool

Community-maintained command-line tooling for Toshiba B-FV4 and B-FV4D label
printers. The project is independent, is not affiliated with Toshiba, and is
provided for operators who need predictable LAN diagnostics and configuration.

## Features

- read printer status, receive-buffer state, firmware, model, and serial data;
- inspect and preview LAN, socket, TPCL, emulation, and maintenance commands;
- preview and, with explicit confirmation, build documented TPCL barcode jobs
  using the canonical `ESC XB` format, `ESC RB` data, and optional `ESC XS`
  issue sequence; Code 128, linear barcode types, QR, Data Matrix, and PDF417
  are covered, with MaxiCode available through the Python API;
- inspect a self-describing read-only query registry with exact request bytes;
- preserve long diagnostic/list responses up to 64 KiB and report truncation;
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

List available read-only queries without contacting a printer:

```bash
toshiba-bfv4 query-list
```

Run any registered read-only query directly:

```bash
toshiba-bfv4 query 192.0.2.10 status
toshiba-bfv4 query 192.0.2.10 form-list
```

The documented snapshot queries (`status`, `buffer`, `version`, and
`identity`) use strict length and framing checks. Additional maintenance
queries are diagnostic and read-only; an empty answer means that the exact
firmware does not support that query and must not be retried automatically.

Preview a LAN change:

```bash
toshiba-bfv4 lan 192.0.2.10 --socket on --socket-port 9100
```

Create a portable, partial settings bundle locally and preview it against a
printer:

```bash
toshiba-bfv4 settings-export ./printer-settings.json --socket on --socket-port 9100
toshiba-bfv4 settings-apply 192.0.2.10 --file ./printer-settings.json
```

Bundles are operator-authored; they are not read-backs of the printer. Applying
one still requires both `--apply` and `--yes`.

Stored TPCL command streams can be called safely, with auto-call disabled by
default:

```bash
toshiba-bfv4 pc-save-call 192.0.2.10 --id 7
toshiba-bfv4 pc-save-call 192.0.2.10 --id 7 --auto-call --apply --yes
```

`pc-save-start` and `pc-save-end` only generate previews. Raw command-body
streaming is deliberately not exposed because an interrupted save session can
leave the printer in PC-save mode without validating the stored commands.

Preview canonical barcode jobs (the default includes one `XS` issue command):

```bash
toshiba-bfv4 barcode-code128 192.0.2.10 --data 'ORDER-123' --x 50 --y 80
toshiba-bfv4 qr 192.0.2.10 --data 'https://example.invalid/ORDER-123' --x 50 --y 80
toshiba-bfv4 barcode 192.0.2.10 --type V --data 'TRACK-123' --no-issue
toshiba-bfv4 data-matrix 192.0.2.10 --data 'CUSTOMS-123' --count 2
toshiba-bfv4 pdf417 192.0.2.10 --data 'LONG-PAYLOAD'
toshiba-bfv4 maxicode 192.0.2.10 --mode 2 --postal-code 12345 \
  --postal-extension 6789 --class-of-service 001 --country-code 840 --message 'TRACK-123'
```

The command sequence is `XB` (define slot), `RB` (load data), and, unless
`--no-issue` is used, `XS` (issue labels). Data is ASCII-bounded until a
separate byte-oriented code-page path is verified. QR manual model, mask, and
structured-append options require `--mode M`; automatic mode leaves those
choices to the printer. Every write still requires `--apply --yes`.

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

## Toshiba protocol source

Barcode syntax and limits are implemented from Toshiba's B-FV4 TPCL interface
specification, not from a third-party renderer or a raster/image path:

- [B-FV4 Interface Specification](https://business.toshiba.com/downloads/KB/f1Ulds/12666/B-FV4_IF_Spec_2nd.pdf)
- [Toshiba B-FV4 base specification](https://business.toshiba.com/downloads/KB/f1Ulds/21209/BV400BASE_SPC_EXEIF_EN_0330.pdf)

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
