"""Read-only LAN client for Toshiba TEC B-FV4 printers.

The B-FV4 speaks Toshiba's documented TPCL command protocol over a raw TCP
socket.  This tool deliberately implements read-only commands only:

* ESC WS - current printer status
* ESC WB - status plus receive-buffer capacity
* ESC WV - firmware/version response (when supported)
* ESC IR - stored model and serial number

Each command uses a separate connection, as required by the B-FV4 socket
specification.  No printer settings or print data are sent by this script.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from ipaddress import IPv4Address
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_HOSTS: Final[tuple[str, ...]] = ()
ESC: Final[bytes] = b"\x1b"
LF_NUL: Final[bytes] = b"\x0a\x00"

COMMANDS: Final[dict[str, bytes]] = {
    "status": ESC + b"WS" + LF_NUL,
    "buffer": ESC + b"WB" + LF_NUL,
    "version": ESC + b"WV" + LF_NUL,
    "info": ESC + b"IR" + LF_NUL,
}

STATUS_DETAILS: Final[dict[str, str]] = {
    "00": "ready",
    "01": "head-open",
    "02": "operating",
    "03": "accessed-by-other-host",
    "04": "paused",
    "05": "waiting-for-strip",
    "06": "command-error",
    "11": "paper-jam",
    "12": "cutter-error",
    "13": "no-paper",
    "15": "head-open-feed-or-issue",
    "17": "head-error",
    "18": "head-temperature-high",
    "21": "ribbon-error",
    "23": "last-label-issued",
    "36": "reserved",
    "50": "memory-write-error",
    "51": "memory-format-error",
    "54": "memory-full",
    "55": "memory-or-eeprom-state",
}


class PrinterSnapshot(BaseModel):
    """Validated, JSON-serializable result of a read-only probe."""

    model_config = ConfigDict(extra="forbid")

    host: IPv4Address
    port: int = Field(default=9100, ge=1, le=65535)
    status_detail: str | None = None
    status_name: str | None = None
    status_type: str | None = None
    remaining_count: int | None = Field(default=None, ge=0)
    buffer_status_detail: str | None = None
    buffer_status_name: str | None = None
    buffer_free_kb: int | None = Field(default=None, ge=0)
    buffer_capacity_kb: int | None = Field(default=None, ge=0)
    firmware_creation_date: str | None = None
    firmware_model: str | None = None
    firmware_version: str | None = None
    model_name: str | None = None
    serial_number: str | None = None
    raw: dict[str, str] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


def exchange(host: str, port: int, payload: bytes, timeout: float) -> bytes:
    """Send one command and collect its response without printing data."""

    chunks = bytearray()
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(payload)
        while len(chunks) < 512:
            try:
                chunk = connection.recv(512 - len(chunks))
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.extend(chunk)
    return bytes(chunks)


def parse_status(response: bytes) -> tuple[str, str, int]:
    """Parse the 13-byte WS response: detail, type, remaining count."""

    if len(response) != 13 or response[:2] != b"\x01\x02" or response[-4:] != b"\x03\x04\r\n":
        raise ValueError(f"unexpected WS response ({len(response)} bytes)")
    body = response[2:-4]
    detail = body[:2].decode("ascii")
    status_type = body[2:3].decode("ascii")
    remaining = int(body[3:])
    return detail, status_type, remaining


def parse_buffer(response: bytes) -> tuple[str, str, int, int, int]:
    """Parse the 23-byte WB response."""

    if len(response) != 23 or response[:2] != b"\x01\x02" or response[-2:] != b"\r\n":
        raise ValueError(f"unexpected WB response ({len(response)} bytes)")
    body = response[2:-2]
    detail = body[:2].decode("ascii")
    status_type = body[2:3].decode("ascii")
    remaining = int(body[3:7])
    length = int(body[7:9])
    if length != 23:
        raise ValueError(f"WB response declares length {length}, expected 23")
    free = int(body[9:14])
    capacity = int(body[14:19])
    return detail, status_type, remaining, free, capacity


def parse_info(response: bytes) -> tuple[str, str]:
    """Parse the 31-byte IR response (20-byte model, 11-byte serial)."""

    if len(response) != 31:
        raise ValueError(f"unexpected IR response ({len(response)} bytes)")
    try:
        model = response[:20].decode("ascii").rstrip()
        serial = response[20:].decode("ascii").rstrip()
    except UnicodeDecodeError as error:
        raise ValueError("IR response is not ASCII") from error
    return model, serial


def parse_version(response: bytes) -> tuple[str, str, str]:
    """Parse the documented 27-byte WV response."""

    if len(response) != 27 or response[:2] != b"\x01\x02" or response[-4:] != b"\x03\x04\r\n":
        raise ValueError(f"unexpected WV response ({len(response)} bytes)")
    body = response[2:-4]
    return (
        body[:9].decode("ascii").rstrip(),
        body[9:16].decode("ascii").rstrip(),
        body[16:21].decode("ascii").rstrip(),
    )


def probe(
    host: str,
    port: int,
    timeout: float,
    requested: tuple[str, ...],
    settle_delay: float,
) -> PrinterSnapshot:
    """Probe one printer using only the requested read-only commands."""

    snapshot = PrinterSnapshot(host=IPv4Address(host), port=port)
    for name in requested:
        try:
            response = exchange(host, port, COMMANDS[name], timeout)
            snapshot.raw[name] = response.hex(" ")
            if name == "status" and response:
                detail, status_type, remaining = parse_status(response)
                snapshot.status_detail = detail
                snapshot.status_name = STATUS_DETAILS.get(detail, "unknown")
                snapshot.status_type = status_type
                snapshot.remaining_count = remaining
            elif name == "buffer" and response:
                detail, status_type, remaining, free, capacity = parse_buffer(response)
                snapshot.buffer_status_detail = detail
                snapshot.buffer_status_name = STATUS_DETAILS.get(detail, "unknown")
                snapshot.buffer_free_kb = free
                snapshot.buffer_capacity_kb = capacity
            elif name == "version" and response:
                (
                    snapshot.firmware_creation_date,
                    snapshot.firmware_model,
                    snapshot.firmware_version,
                ) = parse_version(response)
            elif name == "info" and response:
                snapshot.model_name, snapshot.serial_number = parse_info(response)
            elif name in {"version", "info"} and not response:
                snapshot.errors[name] = "no response (printer firmware may not implement this query)"
        except (OSError, ValueError) as error:
            snapshot.errors[name] = str(error)
        # B-FV4 may keep the previous socket in its close handshake briefly.
        # The vendor specification requires the next connection only after
        # that handshake, so leave a small settling interval between queries.
        time.sleep(settle_delay)
    return snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="+", help="printer IPv4 addresses")
    parser.add_argument("--port", type=int, default=9100, help="TCP port (default: 9100)")
    parser.add_argument("--timeout", type=float, default=1.5, help="per-read timeout in seconds")
    parser.add_argument(
        "--settle-delay",
        type=float,
        default=0.75,
        help="delay between socket queries (default: 0.75 seconds)",
    )
    parser.add_argument(
        "--only",
        choices=tuple(COMMANDS),
        action="append",
        help="only run this read-only query (repeatable; default: all)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    requested = tuple(args.only or COMMANDS)
    results: list[dict[str, object]] = []
    for host in args.hosts:
        print(f"Probe {host}:{args.port} ({', '.join(requested)})", flush=True)
        results.append(probe(host, args.port, args.timeout, requested, args.settle_delay).model_dump(mode="json"))
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
