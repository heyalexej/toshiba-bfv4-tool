# Toshiba B-FV4 Tool

Community-maintained command-line tooling for Toshiba B-FV4 and B-FV4D label
printers. The project is independent, is not affiliated with Toshiba, and is
provided for operators who need predictable LAN diagnostics and configuration.

## Features

- read printer status, receive-buffer state, firmware, model, and serial data;
- inspect and preview LAN, socket, TPCL, emulation, and maintenance commands;
- apply mutating commands only with an explicit `--apply --yes` confirmation;
- preview printer filesystem transfer headers without transmitting file data;
- probe several printers from one command with the read-only LAN client.

The project deliberately keeps network targets explicit. No private IP
addresses, queue names, hostnames, or organization-specific defaults are
embedded in the package.

## Install

With `uv`:

```bash
uv tool install .
```

Nach der Veröffentlichung kann das Tool auch direkt aus dem Git-Repository
installiert werden.

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

Preview a LAN change:

```bash
toshiba-bfv4 lan 192.0.2.10 --socket on --socket-port 9100
```

Apply a change only after reviewing the preview:

```bash
toshiba-bfv4 lan 192.0.2.10 --socket on --socket-port 9100 --apply --yes
```

## Safety model

Status and preview commands are read-only. Network settings, emulation,
parameter, reset, and filesystem-transfer operations never transmit changes
unless both `--apply` and `--yes` are present. Test changes on one printer
first and keep a recovery path available before changing LAN settings.

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
