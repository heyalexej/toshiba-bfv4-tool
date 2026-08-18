"""Community CLI for Toshiba B-FV4/B-FV4D network management.

The package provides a machine-readable interface for status, LAN, TPCL,
emulation, and printer filesystem operations. Every mutating command is
preview-only unless both ``--apply`` and ``--yes`` are supplied.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

ESC: Final[bytes] = b"\x1b"
LF_NUL: Final[bytes] = b"\x0a\x00"
CR_LF: Final[bytes] = b"\x0d\x0a"
DEFAULT_PORT: Final[int] = 9100
MAX_RESPONSE: Final[int] = 512
DIAGNOSTIC_MAX_RESPONSE: Final[int] = 64 * 1024


class Emulation(StrEnum):
    TPCL = "tpcl"
    PPLZ = "pplz"
    PPLA = "ppla"
    PPLB = "pplb"
    IPL = "ipl"
    SBPL = "sbpl"


class PrinterTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: ipaddress.IPv4Address
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)


class CommandPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    effect: str
    payload_hex: str
    payload_ascii: str
    requires_reset: bool = False
    dangerous: bool = False


class DownloadPlan(BaseModel):
    """A validated destination plan for printer filesystem operations."""

    model_config = ConfigDict(extra="forbid")

    page: str
    destination: str | None
    filename: str
    size_bytes: int = Field(ge=0)
    transport: str
    status: str


class StatusSnapshot(BaseModel):
    """Validated result for the printer status operation."""

    model_config = ConfigDict(extra="forbid")

    host: ipaddress.IPv4Address
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    detail: str | None = None
    detail_name: str | None = None
    status_type: str | None = None
    remaining_count: int | None = Field(default=None, ge=0)
    buffer_free_kb: int | None = Field(default=None, ge=0)
    buffer_capacity_kb: int | None = Field(default=None, ge=0)
    model_name: str | None = None
    serial_number: str | None = None
    firmware_creation_date: str | None = None
    firmware_model: str | None = None
    firmware_version: str | None = None
    raw: dict[str, str] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


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


class LanSettings(BaseModel):
    """Validated LAN settings for a B-FV4 printer."""

    model_config = ConfigDict(extra="forbid")

    ip: ipaddress.IPv4Address | None = None
    gateway: ipaddress.IPv4Address | None = None
    subnet: ipaddress.IPv4Address | None = None
    dhcp: bool | None = None
    client_id: str | None = None
    socket_enabled: bool | None = None
    socket_port: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace(":", "").replace("-", "").strip().upper()
        if len(normalized) % 2 or len(normalized) > 32:
            raise ValueError("DHCP client ID must contain at most 16 bytes as even hex")
        if any(character not in "0123456789ABCDEF" for character in normalized):
            raise ValueError("DHCP client ID must be hexadecimal")
        if "FF" in (normalized[i : i + 2] for i in range(0, len(normalized), 2)):
            raise ValueError("DHCP client ID may not contain FF")
        return normalized


@dataclass(frozen=True)
class CapabilityManifest:
    """Static inventory of supported feature groups."""

    parameter_pages: tuple[str, ...]
    download_pages: tuple[str, ...]
    tool_pages: tuple[str, ...]
    bfv4d_relevant: tuple[str, ...]
    family_optional: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "parameter_pages": list(self.parameter_pages),
            "download_pages": list(self.download_pages),
            "tool_pages": list(self.tool_pages),
            "bfv4d_relevant": list(self.bfv4d_relevant),
            "family_optional": list(self.family_optional),
        }


CAPABILITIES: Final = CapabilityManifest(
    parameter_pages=(
        "COM",
        "LAN",
        "LANIPv6",
        "Bluetooth",
        "WLAN",
        "TPCLGeneral",
        "PPLZGeneral",
        "PPLAGeneral",
        "PPLBGeneral",
        "IPLGeneral",
        "SBPLGeneral",
    ),
    download_pages=("Firmware", "Font", "FontGeneral", "BASIC", "General"),
    tool_pages=("SingleCommand", "Status", "MaintenanceQueries", "Firmware"),
    bfv4d_relevant=(
        "LAN",
        "TPCLGeneral",
        "SingleCommand",
        "Status",
        "MaintenanceQueries",
        "Firmware (guarded)",
    ),
    family_optional=(
        "COM",
        "WLAN",
        "Bluetooth",
        "PPLZGeneral",
        "PPLAGeneral",
        "PPLBGeneral",
        "IPLGeneral",
        "SBPLGeneral",
        "Firmware/Font/BASIC/General download",
    ),
)


DOWNLOAD_PATHS: Final[dict[str, str | None]] = {
    "font-ttec": "/FS/FONT/TTEC/TTF/",
    "font-bitmap": "/FS/FONT/BITMAP/",
    "font-ttf": "/FS/FONT/TTF/",
    "basic-main": "/FS/FORM/E/CODE/",
    "basic-data": "/FS/FORM/E/",
    "general": None,
}


TPCL_GENERAL_CODES: Final[dict[int, str]] = {
    20: "image-char-code",
    21: "image-zero-font",
    22: "image-euro-code",
    23: "supply-ribbon-sensor",
    24: "control-feed-key",
    25: "control-auto-head-check",
    26: "control-auto-calibration",
    27: "action-forward-feed-wait",
    28: "position-x-tenths-mm",
    29: "basic-interpreter",
    30: "rtc-battery-check",
    32: "supply-multiple-label",
    190: "control-reprint-after-error",
    2000: "image-maxicode-spec",
    2002: "command-control-code",
    2003: "product-destination",
    3000: "product-serial-number",
}


BYTE_SPECIAL: Final[bytes] = b"\x1bArg"
BYTE_EXIT: Final[bytes] = b"\x1b\x1bexit\r\n"
BYTE_REBOOT_1: Final[bytes] = b"\x1b\x1breboot 1\r\n"


def _validate_download_filename(filename: str) -> str:
    if not filename or filename in {".", ".."}:
        raise ValueError("download filename must not be empty or dot-only")
    if any(character in filename for character in "/\\\x00\r\n"):
        raise ValueError("download filename must be a single ASCII path component")
    try:
        filename.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("download filename must be ASCII") from exc
    return filename


def build_download_plan(*, page: str, filename: str, size_bytes: int, destination: str | None = None) -> DownloadPlan:
    """Map a printer filesystem operation to its device-side target.

    Firmware is intentionally excluded: it uses flash erase/block-write
    transport rather than the filesystem ``cp`` command.
    """

    if page not in DOWNLOAD_PATHS:
        raise ValueError(f"unknown filesystem download page: {page}")
    if size_bytes < 0:
        raise ValueError("size_bytes must be non-negative")
    filename = _validate_download_filename(filename)
    if page == "basic-main" and filename != "MAIN.BAS":
        raise ValueError("basic-main always targets MAIN.BAS")
    expected_destination = DOWNLOAD_PATHS[page]
    if expected_destination is None:
        if destination is not None:
            raise ValueError("general downloads do not accept a destination")
    elif destination is not None and destination != expected_destination:
        raise ValueError(f"destination must be {expected_destination}")
    return DownloadPlan(
        page=page,
        destination=expected_destination,
        filename=filename,
        size_bytes=size_bytes,
        transport=(
            "ESC Arg + ESC ESC cp <destination><filename> <length> CR LF + raw bytes + ESC ESC exit CR LF"
            if page != "general"
            else "raw file bytes only; no header, destination, filename or trailer"
        ),
        status=(
            "offline byte builder; transmission remains disabled"
            if page != "general"
            else "offline raw-stream builder; transmission remains disabled"
        ),
    )


def build_download_header(plan: DownloadPlan) -> CommandPreview:
    """Build the exact filesystem-copy header used by BASIC/Font downloads."""

    if plan.page == "general":
        raise ValueError("General downloads do not use the verified BASIC/Font cp header")
    payload = (f"\x1b\x1bcp {plan.destination}{plan.filename} {plan.size_bytes}\r\n").encode("ascii")
    return CommandPreview(
        operation=f"download.{plan.page}.header",
        effect=(
            f"Prepare {plan.size_bytes} bytes for {plan.destination}{plan.filename}; "
            "raw transfer is deliberately not sent by this preview command."
        ),
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_download_transfer(plan: DownloadPlan, data: bytes) -> bytes:
    """Build one verified printer filesystem transfer byte stream offline."""

    if len(data) != plan.size_bytes:
        raise ValueError(f"download data length {len(data)} does not match plan size {plan.size_bytes}")
    if plan.page == "general":
        return data
    header = bytes.fromhex(build_download_header(plan).payload_hex.replace(" ", ""))
    return BYTE_SPECIAL + header + data + BYTE_EXIT


def frame(command: str) -> bytes:
    """Frame a TPCL command in the form used by the B-FV4 socket interface."""

    if any(ord(character) > 127 for character in command):
        raise ValueError("TPCL command must be ASCII")
    return ESC + command.encode("ascii") + LF_NUL


def display_payload(payload: bytes) -> str:
    """Render control bytes visibly while leaving printable ASCII untouched."""

    return (
        payload.decode("ascii", errors="backslashreplace")
        .replace("\x1b", r"\x1b")
        .replace("\r", r"\r")
        .replace("\n", r"\n")
        .replace("\x00", r"\x00")
    )


def _octets(address: ipaddress.IPv4Address) -> str:
    return ", ".join(f"{octet:03d}" for octet in address.packed)


def build_ip_command(settings: LanSettings) -> list[CommandPreview]:
    """Build documented ESC IP commands; only supplied fields are changed."""

    commands: list[CommandPreview] = []
    for selector, value, label in (
        (2, settings.ip, "printer IP address"),
        (3, settings.gateway, "gateway IP address"),
        (4, settings.subnet, "subnet mask"),
    ):
        if value is None:
            continue
        payload = frame(f"IP; {selector}, {_octets(value)}")
        commands.append(
            CommandPreview(
                operation=f"lan.ip.{label}",
                effect=f"Set {label} to {value}; network connectivity may change.",
                payload_hex=payload.hex(" "),
                payload_ascii=display_payload(payload),
                dangerous=True,
            )
        )
    return commands


def build_socket_command(settings: LanSettings) -> CommandPreview | None:
    if settings.socket_enabled is None and settings.socket_port is None:
        return None
    enabled = 1 if settings.socket_enabled is not False else 0
    port = settings.socket_port or DEFAULT_PORT
    payload = frame(f"IS; {enabled}, {port:05d}")
    return CommandPreview(
        operation="lan.socket",
        effect=f"Set raw socket {'enabled' if enabled else 'disabled'} on TCP port {port}.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=not bool(enabled),
    )


def build_dhcp_command(settings: LanSettings) -> CommandPreview | None:
    if settings.dhcp is None and settings.client_id is None:
        return None
    enabled = 1 if settings.dhcp is not False else 0
    client_id = (settings.client_id or "").ljust(32, "F")
    payload = frame(f"IH; {enabled}, {client_id}")
    return CommandPreview(
        operation="lan.dhcp",
        effect=f"Set DHCP {'on' if enabled else 'off'}; static network values may be ignored.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=bool(enabled),
    )


def build_lan_commands(settings: LanSettings) -> list[CommandPreview]:
    commands = build_ip_command(settings)
    socket_command = build_socket_command(settings)
    dhcp_command = build_dhcp_command(settings)
    if socket_command:
        commands.append(socket_command)
    if dhcp_command:
        commands.append(dhcp_command)
    if not commands:
        raise ValueError("at least one LAN setting must be supplied")
    return commands


def build_parameter_command(
    *,
    codepage: str = "8",
    zero_font: str = "0",
    baud: str = "2",
    data_bits: str = "1",
    stop_bits: str = "0",
    parity: str = "0",
    flow_control: str = "0",
    destination: str = "0",
    forward_feed: str = "0",
    control_code: str = "1",
    feed_key: str = "0",
    euro_code: str = "20",
    head_check: str = "0",
    auto_calibration: str = "0",
) -> CommandPreview:
    """Build the documented TPCL ESC Z2;1 parameter command.

    Fields marked ``ignore`` by Toshiba are deliberately not exposed here.
    Values use the exact one-character encoding required by the protocol.
    """

    fields = (
        codepage,
        zero_font,
        baud,
        data_bits,
        stop_bits,
        parity,
        flow_control,
        destination,
        forward_feed,
        "0",  # head-up: ignored by B-FV4
        "0",  # ribbon saving: ignored by B-FV4
        control_code,
        "0",  # ribbon type: ignored by B-FV4
        "0",  # strip status: ignored by B-FV4
        feed_key,
        "0",  # Kanji: ignored by B-FV4
        euro_code,
        head_check,
        "0",  # Centronics: ignored by B-FV4
        "0",  # Web printer: ignored by B-FV4
        "0",  # automatic home: ignored by B-FV4
        auto_calibration,
        "0",  # model: ignored by B-FV4
    )
    if any(len(value) != 1 for value in fields[:16] + fields[17:]):
        raise ValueError("Z2;1 single-character fields have invalid length")
    if len(euro_code) != 2 or any(character not in "0123456789ABCDEFabcdef" for character in euro_code):
        raise ValueError("euro_code must be two hexadecimal characters")
    body = "".join(fields[:16]) + euro_code + "".join(fields[17:])
    payload = frame(f"Z2; 1, {body}")
    return CommandPreview(
        operation="tpcl.parameter-set",
        effect="Set documented TPCL/RS-232 parameters; some values take effect after reset.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        requires_reset=True,
    )


def build_fine_adjustment_command(*, x_direction: str = "+", x_value: int = 0) -> CommandPreview:
    """Build the documented ESC Z2;2 fine-adjustment command.

    B-FV4 exposes the X-coordinate adjustment; the other fields are required
    by the wire format but are ignored by this printer family and therefore
    remain at their neutral values.
    """

    if x_direction not in {"+", "-"}:
        raise ValueError("x_direction must be '+' or '-'")
    if not 0 <= x_value <= 995:
        raise ValueError("x_value must be between 0 and 995 (0.1 mm units)")
    body = f"+000+000+00{x_direction}{x_value:03d}+00+00-00+00" + "000000"
    payload = frame(f"Z2; 2, {body}")
    return CommandPreview(
        operation="tpcl.fine-adjustment",
        effect=f"Set X-coordinate fine adjustment to {x_direction}{x_value / 10:.1f} mm.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        requires_reset=True,
    )


def build_nv_parameter_command(
    *,
    protocol: str,
    count: int,
    body: str,
    add_arg: bool = False,
) -> CommandPreview:
    """Build a parameter-page transport envelope.

    The ``setnvrr``/``setnvrs`` envelopes are separate from the public TPCL
    ``Z2`` command and are kept explicit so the two paths are not conflated.
    """

    if protocol not in {"setnvrr", "setnvrs"}:
        raise ValueError("protocol must be setnvrr or setnvrs")
    if not 0 <= count <= 9999:
        raise ValueError("count must be between 0 and 9999")
    if any(character in body for character in "\x00\r\n"):
        raise ValueError("parameter body may not contain NUL or line breaks")
    prefix = f"\x1b\x1b{protocol}"
    if add_arg:
        command = f"\x1bArg{prefix} {count}\r\n{body}\x1b\x1bexit\r\n"
    else:
        command = f"{prefix} {count}\r\n{body}"
    payload = command.encode("ascii")
    return CommandPreview(
        operation=f"parameter-page.{protocol}",
        effect="Transmit a parameter-page envelope.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_tpcl_general_command(parameters: Mapping[int, str]) -> CommandPreview:
    """Build a TPCL-General parameter update byte stream.

    ``GetData_Page_TPCLGeneral`` emits one ``code,length,value;`` item per
    selected control.  The Send handler wraps the ``setnvrr`` body with
    ``ESC Arg``, requests ``reboot 1`` and terminates with ``exit``.
    """

    if not parameters:
        raise ValueError("at least one TPCL-General parameter is required")
    unknown = sorted(set(parameters) - TPCL_GENERAL_CODES.keys())
    if unknown:
        raise ValueError(f"unsupported TPCL-General codes: {unknown}")
    if any(not isinstance(value, str) or not value for value in parameters.values()):
        raise ValueError("TPCL-General values must be non-empty strings")
    ordered_codes = [
        code
        for code in (20, 21, 22, 23, 24, 25, 26, 190, 27, 28, 29, 30, 32, 2000, 2002, 2003, 3000)
        if code in parameters
    ]
    body_parts: list[str] = []
    for code in ordered_codes:
        value = parameters[code]
        if any(character in value for character in "\x00\r\n"):
            raise ValueError("TPCL-General values may not contain NUL or line breaks")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("TPCL-General values must be ASCII") from exc
        body_parts.append(f"{code},{len(value)},{value};")
    body = "".join(body_parts)
    raw = f"\x1b\x1bsetnvrr {len(ordered_codes)}\r\n{body}".encode("ascii")
    payload = BYTE_SPECIAL + raw + BYTE_REBOOT_1 + BYTE_EXIT
    labels = ", ".join(f"{code}={TPCL_GENERAL_CODES[code]}" for code in ordered_codes)
    return CommandPreview(
        operation="parameter-page.tpcl-general",
        effect=f"Set TPCL-General parameters ({labels}), reboot and exit.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        requires_reset=True,
        dangerous=True,
    )


def build_emulation_command(mode: str, *, add_arg: bool = False) -> CommandPreview:
    """Build an emulation selector command."""

    values = {"D": 65, "E": 66, "I": 73, "Z": 90, "TPCL": 69}
    if mode == "AUTO":
        body, count = "33,1,1;", 1
    elif mode == "AUTO2":
        body, count = "33,1,2;", 1
    elif mode in values:
        body, count = f"31,2,{values[mode]};33,1,0;", 2
    else:
        raise ValueError("mode must be D, E, I, Z, TPCL, AUTO or AUTO2")
    preview = build_nv_parameter_command(protocol="setnvrs", count=count, body=body, add_arg=add_arg)
    return preview.model_copy(
        update={
            "operation": "single.change-emulation",
            "effect": f"Select printer emulation {mode} via the parameter path.",
        }
    )


def build_reset_command() -> CommandPreview:
    payload = frame("Z0")
    return CommandPreview(
        operation="single.reset",
        effect="Reset the printer after it reaches idle; pending parameter changes may take effect.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_single_command(name: str, value: str | None = None) -> CommandPreview:
    """Build supported single-command maintenance operations."""

    if name == "media-calibration":
        if value is not None and (not value or not value.isascii() or not value.isdigit()):
            raise ValueError("media-calibration value must contain only ASCII digits")
        command = "mc" if value is None else f"sc {value}"
        effect = "Run media calibration" if value is None else f"Run media calibration mode {value}"
    elif name == "ribbon-calibration":
        if value is None or not value.isdigit():
            raise ValueError("ribbon-calibration requires a numeric ribbon length")
        command = f"rsc {value}"
        effect = f"Run ribbon calibration for length {value}"
    elif name == "reset":
        return build_reset_command()
    elif name in {"reboot", "factory-reset", "reset-command", "self-test"}:
        defaults = {
            "reboot": "0",
            "factory-reset": "0",
            "reset-command": "0",
            "self-test": "0",
        }
        selected = defaults[name] if value is None else value
        allowed = {"reboot": {"0", "1", "3"}}.get(name, {"0"})
        if selected not in allowed:
            choices = ", ".join(sorted(allowed))
            raise ValueError(f"{name} value must be one of: {choices}")
        command_names = {
            "reboot": "reboot",
            "factory-reset": "facreset",
            "reset-command": "resetcommand",
            "self-test": "selftest",
        }
        command = f"{command_names[name]} {selected}"
        effect = {
            "reboot": f"Reboot printer using Toshiba mode {selected}.",
            "factory-reset": "Reset printer to factory defaults.",
            "reset-command": "Execute the printer reset command.",
            "self-test": "Run the printer self-test.",
        }[name]
        payload = ESC + ESC + command.encode("ascii") + CR_LF
        return CommandPreview(
            operation=f"single.{name}",
            effect=effect,
            payload_hex=payload.hex(" "),
            payload_ascii=display_payload(payload),
            dangerous=True,
        )
    elif name == "wr-reset":
        payload = frame("WR")
        return CommandPreview(
            operation="single.wr-reset",
            effect="Execute the documented TPCL reset command.",
            payload_hex=payload.hex(" "),
            payload_ascii=display_payload(payload),
            dangerous=True,
        )
    else:
        raise ValueError(f"unsupported single command: {name}")
    payload = ESC + ESC + command.encode("ascii") + CR_LF
    return CommandPreview(
        operation=f"single.{name}",
        effect=effect,
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_internal_query(name: str, value: str | None = None) -> CommandPreview:
    """Build read-only maintenance queries."""

    # Keep the public builder compatible with the standalone registry while
    # retaining the direct-file loading mode used by the offline test suite.
    try:
        from .queries import build_query
    except ImportError:
        build_query = None
    if build_query is not None:
        registered = build_query(name, value)
        return CommandPreview(
            operation=f"query.{registered.name}",
            effect=f"{registered.effect} {registered.response.layout}",
            payload_hex=registered.payload_hex,
            payload_ascii=registered.payload_ascii,
            requires_reset=False,
            dangerous=False,
        )

    documented = {
        "status": "WS",
        "buffer": "WB",
        "version": "WV",
        "identity": "IR",
    }
    fixed = {
        "system-version": "sv\r\n",
        "config": "config 0\r\n",
        "media-info": "showmi\r\n",
        "form-list": "objinquiry 0 0 1\r\n",
        "font-list": "objinquiry 0 0 3\r\n",
        "graphic-list": "objinquiry 0 0 2\r\n",
        "info": "info\r\n",
        "task-status": "taskstatus\r\n",
        "burn-status": "burnstatus\r\n",
        "last-state": "laststate 3\r\n",
    }
    if name in documented:
        payload = frame(documented[name])
        return CommandPreview(
            operation=f"query.{name}",
            effect="Read-only Toshiba query; no printer setting is changed.",
            payload_hex=payload.hex(" "),
            payload_ascii=display_payload(payload),
        )
    if name == "tph-info":
        if value not in {"1", "2"}:
            raise ValueError("tph-info requires value 1 or 2")
        command = f"tphinfo {value}\r\n"
    elif name in fixed:
        command = fixed[name]
    else:
        raise ValueError(f"unsupported internal query: {name}")
    payload = ESC + ESC + command.encode("ascii")
    return CommandPreview(
        operation=f"query.{name}",
        effect="Read-only Toshiba maintenance query; no printer setting is changed.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
    )


def read_internal_query(
    target: PrinterTarget, preview: CommandPreview, *, timeout: float, settle_delay: float
) -> dict[str, object]:
    payload = bytes.fromhex(preview.payload_hex)
    response_limit = MAX_RESPONSE
    try:
        from .queries import REGISTRY
    except ImportError:
        REGISTRY = None
    query_name = preview.operation.removeprefix("query.")
    if REGISTRY is not None:
        response_limit = REGISTRY.get(query_name).response_limit
    elif query_name not in {"status", "buffer", "version", "identity"}:
        response_limit = DIAGNOSTIC_MAX_RESPONSE
    response, response_truncated = exchange_limited(target, payload, timeout, response_limit)
    time.sleep(settle_delay)
    result = {
        "operation": preview.operation,
        "request": preview.payload_ascii,
        "response_hex": response.hex(" "),
        "response_text": response.decode("ascii", errors="replace"),
        "response_limit": response_limit,
        "response_truncated": response_truncated,
    }
    if REGISTRY is not None:
        result["response_description"] = REGISTRY.get(query_name).response.layout
    return result


def exchange_limited(target: PrinterTarget, payload: bytes, timeout: float, max_response: int) -> tuple[bytes, bool]:
    """Send one command and report whether the bounded response was truncated."""

    if max_response < 1:
        raise ValueError("max_response must be positive")

    chunks = bytearray()
    with socket.create_connection((str(target.host), target.port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(payload)
        while len(chunks) <= max_response:
            try:
                chunk = connection.recv(max_response + 1 - len(chunks))
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.extend(chunk)
    return bytes(chunks[:max_response]), len(chunks) > max_response


def exchange(target: PrinterTarget, payload: bytes, timeout: float) -> bytes:
    """Send exactly one command over a fresh B-FV4 socket."""

    response, _truncated = exchange_limited(target, payload, timeout, MAX_RESPONSE)
    return response


def parse_status(response: bytes) -> tuple[str, str, int]:
    if len(response) != 13 or response[:2] != b"\x01\x02" or response[-4:] != b"\x03\x04\r\n":
        raise ValueError(f"unexpected WS response ({len(response)} bytes)")
    body = response[2:-4]
    return body[:2].decode("ascii"), body[2:3].decode("ascii"), int(body[3:])


def parse_buffer(response: bytes) -> tuple[str, str, int, int, int]:
    if len(response) != 23 or response[:2] != b"\x01\x02" or response[-2:] != b"\r\n":
        raise ValueError(f"unexpected WB response ({len(response)} bytes)")
    body = response[2:-2]
    detail, status_type = body[:2].decode("ascii"), body[2:3].decode("ascii")
    remaining, length = int(body[3:7]), int(body[7:9])
    if length != 23:
        raise ValueError(f"WB response declares length {length}, expected 23")
    return detail, status_type, remaining, int(body[9:14]), int(body[14:19])


def parse_info(response: bytes) -> tuple[str, str]:
    if len(response) != 31:
        raise ValueError(f"unexpected IR response ({len(response)} bytes)")
    return response[:20].decode("ascii").rstrip(), response[20:].decode("ascii").rstrip()


def parse_version(response: bytes) -> tuple[str, str, str]:
    if len(response) != 27 or response[:2] != b"\x01\x02" or response[-4:] != b"\x03\x04\r\n":
        raise ValueError(f"unexpected WV response ({len(response)} bytes)")
    body = response[2:-4]
    return (
        body[:9].decode("ascii").rstrip(),
        body[9:16].decode("ascii").rstrip(),
        body[16:21].decode("ascii").rstrip(),
    )


def read_status(target: PrinterTarget, *, timeout: float, settle_delay: float) -> StatusSnapshot:
    """Read the four information blocks used by the Status page."""

    snapshot = StatusSnapshot(host=target.host, port=target.port)
    queries = {
        "status": frame("WS"),
        "buffer": frame("WB"),
        "version": frame("WV"),
        "info": frame("IR"),
    }
    for name, payload in queries.items():
        try:
            response = exchange(target, payload, timeout)
            snapshot.raw[name] = response.hex(" ")
            if name == "status" and response:
                detail, status_type, remaining = parse_status(response)
                snapshot.detail = detail
                snapshot.detail_name = STATUS_DETAILS.get(detail, "unknown")
                snapshot.status_type = status_type
                snapshot.remaining_count = remaining
            elif name == "buffer" and response:
                detail, _status_type, remaining, free, capacity = parse_buffer(response)
                if snapshot.detail is None:
                    snapshot.detail = detail
                    snapshot.detail_name = STATUS_DETAILS.get(detail, "unknown")
                snapshot.remaining_count = remaining
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
            elif not response:
                snapshot.errors[name] = "no response"
        except (OSError, ValueError) as error:
            snapshot.errors[name] = str(error)
        time.sleep(settle_delay)
    return snapshot


def apply_previews(
    target: PrinterTarget,
    commands: Iterable[CommandPreview],
    *,
    timeout: float,
    settle_delay: float,
    apply: bool,
    yes: bool,
) -> list[dict[str, object]]:
    previews = list(commands)
    print(json.dumps([item.model_dump(mode="json") for item in previews], indent=2), flush=True)
    if not apply:
        print("Preview only: use --apply --yes to transmit these bytes.", flush=True)
        return []
    if not yes:
        raise ValueError("writes require both --apply and --yes")
    results: list[dict[str, object]] = []
    for preview in previews:
        payload = bytes.fromhex(preview.payload_hex)
        response = exchange(target, payload, timeout)
        results.append(
            {
                "operation": preview.operation,
                "response_hex": response.hex(" "),
                "response_length": len(response),
            }
        )
        time.sleep(settle_delay)
    print(json.dumps(results, indent=2), flush=True)
    return results


def parse_target(args: argparse.Namespace) -> PrinterTarget:
    return PrinterTarget(host=args.host, port=args.port)


def add_write_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apply", action="store_true", help="transmit the previewed bytes")
    parser.add_argument("--yes", action="store_true", help="confirm the write together with --apply")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--settle-delay", type=float, default=0.75)


def add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("host", type=ipaddress.IPv4Address)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities = subparsers.add_parser("capabilities", help="show supported feature groups")
    capabilities.set_defaults(handler=lambda args: print(json.dumps(CAPABILITIES.as_dict(), indent=2)))

    status = subparsers.add_parser("status", help="read status, buffer, firmware and printer identity")
    add_target(status)
    status.add_argument("--timeout", type=float, default=1.5)
    status.add_argument("--settle-delay", type=float, default=0.75)

    try:
        from .queries import REGISTRY
    except ImportError:
        REGISTRY = None

        def query_names() -> tuple[str, ...]:
            return (
                "status",
                "buffer",
                "version",
                "identity",
                "system-version",
                "config",
                "media-info",
                "tph-info",
                "form-list",
                "font-list",
                "graphic-list",
                "info",
                "task-status",
                "burn-status",
                "last-state",
            )

    def show_query_list(args: argparse.Namespace) -> None:
        if REGISTRY is None:
            raise RuntimeError("maintenance registry is unavailable in direct-file mode")
        print(
            json.dumps(
                REGISTRY.describe(),
                indent=2,
            ),
            flush=True,
        )

    query_list = subparsers.add_parser("query-list", help="describe read-only maintenance queries")
    query_list.set_defaults(handler=show_query_list)

    query = subparsers.add_parser("query", help="run a read-only maintenance query")
    add_target(query)
    query.add_argument(
        "operation",
        choices=REGISTRY.names() if REGISTRY is not None else query_names(),
    )
    query.add_argument("value", nargs="?")
    query.add_argument("--timeout", type=float, default=1.5)
    query.add_argument("--settle-delay", type=float, default=0.75)

    lan = subparsers.add_parser("lan", help="preview/apply LAN settings")
    add_target(lan)
    add_write_flags(lan)
    lan.add_argument("--ip", type=ipaddress.IPv4Address)
    lan.add_argument("--gateway", type=ipaddress.IPv4Address)
    lan.add_argument("--subnet", type=ipaddress.IPv4Address)
    lan.add_argument("--dhcp", choices=("on", "off"))
    lan.add_argument("--client-id", help="DHCP client ID as hexadecimal bytes")
    lan.add_argument("--socket", choices=("on", "off"), dest="socket_enabled")
    lan.add_argument("--socket-port", type=int)

    parameter = subparsers.add_parser("tpcl-parameter", help="preview/apply TPCL ESC Z2;1")
    add_target(parameter)
    add_write_flags(parameter)
    for name, default, choices in (
        ("codepage", "8", tuple("0123456789ABCDEF")),
        ("zero-font", "0", ("0", "1")),
        ("baud", "2", tuple("0123456")),
        ("data-bits", "1", ("0", "1")),
        ("stop-bits", "0", ("0", "1")),
        ("parity", "0", ("0", "1", "2")),
        ("flow-control", "0", tuple("01234")),
        ("destination", "0", ("0", "5")),
        ("forward-feed", "0", ("0", "1")),
        ("control-code", "1", ("0", "1", "2")),
        ("feed-key", "0", ("0", "1")),
        ("head-check", "0", ("0", "1")),
        ("auto-calibration", "0", ("0", "1", "2")),
    ):
        parameter.add_argument(f"--{name}", default=default, choices=choices)
    parameter.add_argument("--euro-code", default="20", metavar="HH")

    fine = subparsers.add_parser("tpcl-fine", help="preview/apply TPCL ESC Z2;2 fine adjustment")
    add_target(fine)
    add_write_flags(fine)
    fine.add_argument("--x-direction", choices=("+", "-"), default="+")
    fine.add_argument("--x", type=int, default=0, metavar="TENTHS_MM")

    emulation = subparsers.add_parser("emulation", help="preview/apply an emulation selector")
    add_target(emulation)
    add_write_flags(emulation)
    emulation.add_argument("mode", choices=("D", "E", "I", "Z", "TPCL", "AUTO", "AUTO2"))
    emulation.add_argument("--add-arg", action="store_true")

    page = subparsers.add_parser("tpcl-page", help="preview/apply a TPCL parameter page")
    add_target(page)
    add_write_flags(page)
    page.add_argument("--protocol", choices=("setnvrr", "setnvrs"), default="setnvrr")
    page.add_argument("--count", type=int, required=True)
    page.add_argument("--body", required=True, help="parameter body, e.g. '20,1,8;21,1,0;'")
    page.add_argument("--add-arg", action="store_true")

    tpcl_general = subparsers.add_parser("tpcl-general", help="preview/apply a TPCL-General parameter update")
    add_target(tpcl_general)
    add_write_flags(tpcl_general)
    tpcl_general.add_argument(
        "parameters",
        nargs="+",
        metavar="CODE=VALUE",
        help="e.g. 20=0 21=0 28=15",
    )

    download_paths = subparsers.add_parser("download-paths", help="show supported printer-side filesystem paths")
    download_paths.set_defaults(handler=lambda args: print(json.dumps(DOWNLOAD_PATHS, indent=2), flush=True))

    download_header = subparsers.add_parser("download-header", help="preview a filesystem-download header")
    download_header.add_argument("page", choices=tuple(DOWNLOAD_PATHS))
    download_header.add_argument("--filename", required=True)
    download_header.add_argument("--size", type=int, required=True, dest="size_bytes")

    firmware = subparsers.add_parser("firmware", help="validate and optionally apply a .zip/.abin firmware package")
    add_target(firmware)
    add_write_flags(firmware)
    firmware.add_argument("--package", required=True, help="operator-supplied Toshiba firmware .zip or .abin")
    firmware.add_argument("--chunk-size", type=int, default=8192, metavar="BYTES")
    firmware.add_argument("--burn-wait", type=float, default=3.0, metavar="SECONDS")
    firmware.add_argument("--burn-timeout", type=float, default=300.0, metavar="SECONDS")
    firmware.add_argument("--write-timeout", type=float, default=60.0, metavar="SECONDS")
    firmware.add_argument("--force", action="store_true", help="allow retransmitting the same master version")

    single = subparsers.add_parser("single", help="preview/apply safe mapped SingleCommand operations")
    add_target(single)
    add_write_flags(single)
    single.add_argument(
        "operation",
        choices=(
            "media-calibration",
            "ribbon-calibration",
            "reboot",
            "self-test",
            "factory-reset",
            "reset-command",
            "reset",
            "wr-reset",
        ),
    )
    single.add_argument("value", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = make_parser().parse_args(argv)
    if args.command == "capabilities":
        args.handler(args)
        return
    if args.command == "download-paths":
        args.handler(args)
        return
    if args.command == "query-list":
        args.handler(args)
        return
    if args.command == "download-header":
        plan = build_download_plan(page=args.page, filename=args.filename, size_bytes=args.size_bytes)
        print(json.dumps(plan.model_dump(mode="json"), indent=2), flush=True)
        print(
            json.dumps(build_download_header(plan).model_dump(mode="json"), indent=2),
            flush=True,
        )
        print(
            "Preview only: raw file bytes are not transmitted by this command.",
            flush=True,
        )
        return
    if args.command == "firmware":
        from .firmware import apply_firmware_update, load_and_plan

        package, plan = load_and_plan(args.package, chunk_size=args.chunk_size)
        apply_firmware_update(
            parse_target(args),
            package,
            plan,
            timeout=args.timeout,
            write_timeout=args.write_timeout,
            burn_wait=args.burn_wait,
            burn_timeout=args.burn_timeout,
            apply=args.apply,
            yes=args.yes,
            force=args.force,
        )
        return
    if args.command == "status":
        print(
            json.dumps(
                read_status(parse_target(args), timeout=args.timeout, settle_delay=args.settle_delay).model_dump(
                    mode="json"
                ),
                indent=2,
            ),
            flush=True,
        )
        return
    if args.command == "query":
        preview = build_internal_query(args.operation, args.value)
        print(json.dumps(preview.model_dump(mode="json"), indent=2), flush=True)
        print(
            json.dumps(
                read_internal_query(
                    parse_target(args),
                    preview,
                    timeout=args.timeout,
                    settle_delay=args.settle_delay,
                ),
                indent=2,
            ),
            flush=True,
        )
        return
    target = parse_target(args)
    if args.command == "lan":
        settings = LanSettings(
            ip=args.ip,
            gateway=args.gateway,
            subnet=args.subnet,
            dhcp=None if args.dhcp is None else args.dhcp == "on",
            client_id=args.client_id,
            socket_enabled=None if args.socket_enabled is None else args.socket_enabled == "on",
            socket_port=args.socket_port,
        )
        apply_previews(
            target,
            build_lan_commands(settings),
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "tpcl-parameter":
        preview = build_parameter_command(
            codepage=args.codepage,
            zero_font=args.zero_font,
            baud=args.baud,
            data_bits=args.data_bits,
            stop_bits=args.stop_bits,
            parity=args.parity,
            flow_control=args.flow_control,
            destination=args.destination,
            forward_feed=args.forward_feed,
            control_code=args.control_code,
            feed_key=args.feed_key,
            euro_code=args.euro_code,
            head_check=args.head_check,
            auto_calibration=args.auto_calibration,
        )
        apply_previews(
            target,
            [preview],
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "tpcl-fine":
        preview = build_fine_adjustment_command(x_direction=args.x_direction, x_value=args.x)
        apply_previews(
            target,
            [preview],
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "emulation":
        preview = build_emulation_command(args.mode, add_arg=args.add_arg)
        apply_previews(
            target,
            [preview],
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "tpcl-page":
        preview = build_nv_parameter_command(
            protocol=args.protocol,
            count=args.count,
            body=args.body,
            add_arg=args.add_arg,
        )
        apply_previews(
            target,
            [preview],
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "tpcl-general":
        parameters: dict[int, str] = {}
        for item in args.parameters:
            if "=" not in item:
                raise ValueError(f"parameter must be CODE=VALUE: {item}")
            code_text, value = item.split("=", 1)
            try:
                code = int(code_text, 10)
            except ValueError as exc:
                raise ValueError(f"parameter code must be an integer: {item}") from exc
            if code in parameters:
                raise ValueError(f"duplicate TPCL-General code: {code}")
            parameters[code] = value
        apply_previews(
            target,
            [build_tpcl_general_command(parameters)],
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "single":
        reset_operations = {"self-test", "factory-reset", "reset-command", "reset", "wr-reset"}
        if args.operation in reset_operations and args.value is not None:
            raise ValueError("reset does not take a value")
        preview = build_single_command(args.operation, args.value)
        apply_previews(
            target,
            [preview],
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )


if __name__ == "__main__":
    main()
