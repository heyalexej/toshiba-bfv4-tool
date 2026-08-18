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
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class TpclParameterSettings(BaseModel):
    """Optional values for the TPCL ESC Z2;1 parameter page."""

    model_config = ConfigDict(extra="forbid")

    codepage: str | None = None
    zero_font: str | None = None
    baud: str | None = None
    data_bits: str | None = None
    stop_bits: str | None = None
    parity: str | None = None
    flow_control: str | None = None
    destination: str | None = None
    forward_feed: str | None = None
    control_code: str | None = None
    feed_key: str | None = None
    euro_code: str | None = None
    head_check: str | None = None
    auto_calibration: str | None = None

    @model_validator(mode="after")
    def validate_values(self) -> TpclParameterSettings:
        choices = {
            "codepage": set("0123456789ABCDEF"),
            "zero_font": {"0", "1"},
            "baud": set("0123456"),
            "data_bits": {"0", "1"},
            "stop_bits": {"0", "1"},
            "parity": {"0", "1", "2"},
            "flow_control": set("01234"),
            "destination": {"0", "5"},
            "forward_feed": {"0", "1"},
            "control_code": {"0", "1", "2"},
            "feed_key": {"0", "1"},
            "head_check": {"0", "1"},
            "auto_calibration": {"0", "1", "2"},
        }
        for name, allowed in choices.items():
            value = getattr(self, name)
            if value is not None and value not in allowed:
                raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
        if self.euro_code is not None:
            if len(self.euro_code) != 2 or any(char not in "0123456789ABCDEFabcdef" for char in self.euro_code):
                raise ValueError("euro_code must be two hexadecimal characters")
        return self


class FineAdjustmentSettings(BaseModel):
    """Optional values for the TPCL ESC Z2;2 fine adjustment page."""

    model_config = ConfigDict(extra="forbid")

    x_direction: Literal["+", "-"] | None = None
    x_value: int | None = Field(default=None, ge=0, le=995)


class SettingsBundle(BaseModel):
    """A validated, portable set of operator-authored printer settings."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    lan: LanSettings | None = None
    tpcl_parameter: TpclParameterSettings | None = None
    fine_adjustment: FineAdjustmentSettings | None = None
    tpcl_general: dict[int, str] = Field(default_factory=dict)


class PcSaveStartSettings(BaseModel):
    """Validated parameters for opening the TPCL PC-save mode."""

    model_config = ConfigDict(extra="forbid")

    identifier: int = Field(ge=1, le=99)
    drive: Literal[0, 1] = 0
    status_response: bool = False


class PcSaveCallSettings(BaseModel):
    """Validated parameters for calling a stored TPCL PC command stream."""

    model_config = ConfigDict(extra="forbid")

    identifier: int = Field(ge=1, le=99)
    drive: Literal[0, 1] = 0
    status_response: bool = False
    auto_call: bool = False


class BarcodeDataSettings(BaseModel):
    """Validated data for the TPCL ``RB`` barcode-data command."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    data: str = Field(min_length=1, max_length=2000)

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: str) -> str:
        if any(character in value for character in "\x00\r\n"):
            raise ValueError("barcode data may not contain NUL or line breaks")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("barcode data must be ASCII") from error
        return value


LINEAR_BARCODE_TYPES: Final = frozenset(
    {
        "0",  # JAN8/EAN8
        "5",  # JAN13/EAN13
        "6",  # UPC-E
        "7",
        "8",  # EAN13 add-on
        "9",
        "A",  # Code 128
        "C",  # Code 93
        "G",
        "H",  # UPC-E add-on
        "I",
        "J",  # EAN8 add-on
        "K",
        "L",
        "M",  # UPC-A add-on
        "N",  # UCC/EAN128
        "R",
        "S",  # customer barcode
        "U",  # POSTNET
        "V",  # RM4SCC
        "W",  # KIX
        "d",  # USPS Intelligent Mail (BV400 family)
    }
)


class LinearBarcodeFormatSettings(BaseModel):
    """Validated Toshiba ``XB`` fields for documented linear barcodes."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    x: int = Field(ge=0, le=9999)
    y: int = Field(ge=0, le=99999)
    barcode_type: str = "9"
    check_digit: int = Field(default=3, ge=1, le=5)
    module_width: int = Field(default=2, ge=1, le=15)
    rotation: Literal[0, 1, 2, 3] = 0
    height: int = Field(default=100, ge=0, le=1000)
    increment: int | None = Field(default=None, ge=-9999999999, le=9999999999)
    guard_bar_length: int = Field(default=0, ge=0, le=100)
    human_readable: Literal[0, 1] = 0
    zero_suppression: int = Field(default=0, ge=0, le=99)

    @field_validator("barcode_type")
    @classmethod
    def validate_barcode_type(cls, value: str) -> str:
        if value not in LINEAR_BARCODE_TYPES:
            allowed = ", ".join(sorted(LINEAR_BARCODE_TYPES))
            raise ValueError(f"barcode_type must be one of: {allowed}")
        return value


class IssueSettings(BaseModel):
    """Validated B-FV4 TPCL issue settings for the ``XS`` command."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(default=1, ge=1, le=9999)
    cut_interval: int = Field(default=0, ge=0, le=100)
    sensor: Literal[0, 1, 2, 3, 4] = 2
    issue_mode: Literal["C", "D", "E", "F", "G"] = "C"
    speed: Literal["1", "2", "3", "4", "5", "6", "7", "8", "9", "A", "B"] = "3"
    ribbon: Literal["0", "1", "2"] = "0"
    tag_rotation: Literal[0, 1, 2, 3] = 0
    status_response: bool = False


class Code128FormatSettings(BaseModel):
    """Validated TPCL format fields for a Code 128 barcode without data."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    x: int = Field(ge=0, le=9999)
    y: int = Field(ge=0, le=99999)
    module_width: int = Field(default=2, ge=1, le=15)
    rotation: Literal[0, 1, 2, 3] = 0
    height: int = Field(default=100, ge=0, le=1000)


class QrCodeFormatSettings(BaseModel):
    """Validated TPCL QR-code format fields without inline data."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    x: int = Field(ge=0, le=9999)
    y: int = Field(ge=0, le=99999)
    error_correction: Literal["L", "M", "Q", "H"] = "M"
    cell_width: int = Field(default=4, ge=0, le=52)
    mode: Literal["A", "M"] = "A"
    rotation: Literal[0, 1, 2, 3] = 0
    model: Literal[1, 2] | None = None
    mask: int | None = Field(default=None, ge=0, le=8)
    connection_number: int | None = Field(default=None, ge=1, le=16)
    connection_total: int | None = Field(default=None, ge=1, le=16)
    connection_xor: int | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def validate_qr_options(self) -> QrCodeFormatSettings:
        connection = (self.connection_number, self.connection_total, self.connection_xor)
        if any(value is not None for value in connection) and not all(value is not None for value in connection):
            raise ValueError("QR connection_number, connection_total and connection_xor must be supplied together")
        if self.mode == "A" and any(value is not None for value in (self.model, self.mask, *connection)):
            raise ValueError("QR model, mask and connection options require manual mode")
        return self


class DataMatrixFormatSettings(BaseModel):
    """Validated TPCL Data Matrix format fields without inline data."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    x: int = Field(ge=0, le=9999)
    y: int = Field(ge=0, le=99999)
    ecc_type: Literal["00", "01", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14", "20"] = "20"
    cell_width: int = Field(default=4, ge=0, le=99)
    format_id: int = Field(default=1, ge=1, le=6)
    rotation: Literal[0, 1, 2, 3] = 0
    cells_x: int | None = Field(default=None, ge=0, le=144)
    cells_y: int | None = Field(default=None, ge=0, le=144)

    @model_validator(mode="after")
    def validate_cells(self) -> DataMatrixFormatSettings:
        if (self.cells_x is None) != (self.cells_y is None):
            raise ValueError("Data Matrix cells_x and cells_y must be supplied together")
        return self


class Pdf417FormatSettings(BaseModel):
    """Validated TPCL PDF417 format fields without inline data."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    x: int = Field(ge=0, le=9999)
    y: int = Field(ge=0, le=99999)
    security_level: int = Field(default=0, ge=0, le=8)
    module_width: int = Field(default=2, ge=1, le=10)
    columns: int = Field(default=2, ge=1, le=30)
    rotation: Literal[0, 1, 2, 3] = 0
    bar_height: int = Field(default=20, ge=0, le=100)


class MaxiCodeFormatSettings(BaseModel):
    """Validated TPCL MaxiCode format fields without data."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    x: int = Field(ge=0, le=9999)
    y: int = Field(ge=0, le=99999)
    mode: int | None = Field(default=None, ge=0, le=9)
    connection_number: int | None = Field(default=None, ge=1, le=8)
    connection_total: int | None = Field(default=None, ge=1, le=8)
    zipper_contrast: Literal[0, 1, 2, 3] | None = None

    @model_validator(mode="after")
    def validate_connection(self) -> MaxiCodeFormatSettings:
        connection = (self.connection_number, self.connection_total)
        if any(value is not None for value in connection) and not all(value is not None for value in connection):
            raise ValueError("MaxiCode connection_number and connection_total must be supplied together")
        return self


class MaxiCodeDataSettings(BaseModel):
    """Validated fixed-width data for TPCL MaxiCode modes 2/3/4/6."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    mode: Literal[2, 3, 4, 6]
    postal_code: str | None = None
    postal_extension: str | None = None
    class_of_service: str | None = None
    country_code: str | None = None
    message: str | None = None
    primary: str | None = None
    secondary: str | None = None

    @model_validator(mode="after")
    def validate_data(self) -> MaxiCodeDataSettings:
        if self.mode in {2, 3}:
            if self.postal_code is None or self.class_of_service is None or self.country_code is None:
                raise ValueError("MaxiCode modes 2/3 require postal_code, class_of_service and country_code")
            if self.message is None:
                raise ValueError("MaxiCode modes 2/3 require message")
            postal_length = 5 if self.mode == 2 else 6
            if len(self.postal_code) != postal_length or not self.postal_code.isdigit():
                raise ValueError(f"MaxiCode mode {self.mode} postal_code must contain {postal_length} digits")
            if self.mode == 2 and (self.postal_extension is None or len(self.postal_extension) != 4):
                raise ValueError("MaxiCode mode 2 postal_extension must contain four digits")
            if self.mode == 2 and not self.postal_extension.isdigit():
                raise ValueError("MaxiCode mode 2 postal_extension must contain four digits")
            if self.mode == 3 and self.postal_extension is not None:
                raise ValueError("MaxiCode mode 3 does not accept postal_extension")
            for name, value in (("class_of_service", self.class_of_service), ("country_code", self.country_code)):
                if len(value) != 3 or not value.isdigit():
                    raise ValueError(f"MaxiCode {name} must contain three digits")
            if len(self.message) > 84:
                raise ValueError("MaxiCode message may contain at most 84 ASCII characters")
            self._validate_ascii(self.message)
        else:
            if self.primary is None or self.secondary is None:
                raise ValueError("MaxiCode modes 4/6 require primary and secondary")
            if len(self.primary) != 9 or len(self.secondary) > 84:
                raise ValueError("MaxiCode primary must be 9 characters and secondary at most 84 characters")
            self._validate_ascii(self.primary)
            self._validate_ascii(self.secondary)
        return self

    @staticmethod
    def _validate_ascii(value: str) -> None:
        if any(character in value for character in "\x00\r\n"):
            raise ValueError("MaxiCode data may not contain NUL or line breaks")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("MaxiCode data must be ASCII") from error


class Code128Settings(BaseModel):
    """Validated TPCL format fields for a Code 128 barcode."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    x: int = Field(ge=0, le=9999)
    y: int = Field(ge=0, le=99999)
    module_width: int = Field(default=2, ge=1, le=15)
    rotation: Literal[0, 1, 2, 3] = 0
    height: int = Field(default=100, ge=0, le=1000)
    data: str = Field(min_length=1, max_length=126)

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: str) -> str:
        if any(character in value for character in "\x00\r\n"):
            raise ValueError("barcode data may not contain NUL or line breaks")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("barcode data must be ASCII") from error
        return value


class QrCodeSettings(BaseModel):
    """Validated TPCL QR-code format fields for the B-FV4 family."""

    model_config = ConfigDict(extra="forbid")

    barcode_number: int = Field(ge=0, le=31)
    x: int = Field(ge=0, le=9999)
    y: int = Field(ge=0, le=99999)
    error_correction: Literal["L", "M", "Q", "H"] = "M"
    cell_width: int = Field(default=4, ge=0, le=52)
    mode: Literal["A", "M"] = "A"
    rotation: Literal[0, 1, 2, 3] = 0
    model: Literal[1, 2] | None = None
    mask: int | None = Field(default=None, ge=0, le=8)
    connection_number: int | None = Field(default=None, ge=1, le=16)
    connection_total: int | None = Field(default=None, ge=1, le=16)
    connection_xor: int | None = Field(default=None, ge=0, le=255)
    data: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_qr_options(self) -> QrCodeSettings:
        connection = (self.connection_number, self.connection_total, self.connection_xor)
        if any(value is not None for value in connection) and not all(value is not None for value in connection):
            raise ValueError("QR connection_number, connection_total and connection_xor must be supplied together")
        if self.mode == "A" and any(value is not None for value in (self.model, self.mask, *connection)):
            raise ValueError("QR model, mask and connection options require manual mode")
        if any(character in self.data for character in "\x00\r\n"):
            raise ValueError("QR data may not contain NUL or line breaks")
        try:
            self.data.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("QR data must be ASCII") from error
        return self


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
        "TPCLBarcodes (XB/RB/XS)",
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


def build_settings_commands(bundle: SettingsBundle) -> list[CommandPreview]:
    """Build the guarded command list represented by a settings bundle."""

    commands: list[CommandPreview] = []
    if bundle.lan is not None:
        commands.extend(build_lan_commands(bundle.lan))
    if bundle.tpcl_parameter is not None:
        values = bundle.tpcl_parameter.model_dump(exclude_none=True)
        commands.append(build_parameter_command(**values))
    if bundle.fine_adjustment is not None:
        values = bundle.fine_adjustment.model_dump(exclude_none=True)
        commands.append(build_fine_adjustment_command(**values))
    if bundle.tpcl_general:
        commands.append(build_tpcl_general_command(bundle.tpcl_general))
    if not commands:
        raise ValueError("settings bundle contains no settings to apply")
    return commands


def load_settings_bundle(path: str | Path) -> SettingsBundle:
    """Load and validate a JSON settings bundle from disk."""

    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"could not read settings bundle {source}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"settings bundle is not valid JSON: {error}") from error
    return SettingsBundle.model_validate(data)


def save_settings_bundle(path: str | Path, bundle: SettingsBundle) -> None:
    """Write a validated JSON settings bundle without contacting a printer."""

    destination = Path(path)
    try:
        destination.write_text(
            json.dumps(bundle.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise ValueError(f"could not write settings bundle {destination}: {error}") from error


def build_pc_save_start_command(identifier: int, *, drive: int = 0, status_response: bool = False) -> CommandPreview:
    """Build a preview for opening the TPCL PC-command save mode.

    This command is intentionally preview-only at the CLI: until a dedicated
    streaming path exists, transmitting ``XO`` would leave the printer waiting
    for a raw command body that this tool cannot safely provide.
    """

    settings = PcSaveStartSettings(identifier=identifier, drive=drive, status_response=status_response)
    payload = frame(f"XO;{settings.identifier:02d},{settings.drive},{int(settings.status_response)}")
    return CommandPreview(
        operation="pc-save.start",
        effect=(
            f"Open PC-command save mode for ID {settings.identifier:02d} on drive {settings.drive}; "
            "the printer will store subsequent TPCL bytes without executing them."
        ),
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_pc_save_terminate_command() -> CommandPreview:
    """Build a preview for terminating TPCL PC-command save mode."""

    payload = frame("XP")
    return CommandPreview(
        operation="pc-save.terminate",
        effect="Terminate PC-command save mode and return the printer to online operation.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_pc_save_call_command(
    identifier: int,
    *,
    drive: int = 0,
    status_response: bool = False,
    auto_call: bool = False,
) -> CommandPreview:
    """Build a guarded call for a previously saved TPCL command stream."""

    settings = PcSaveCallSettings(
        identifier=identifier,
        drive=drive,
        status_response=status_response,
        auto_call=auto_call,
    )
    call_mode = "L" if settings.auto_call else "M"
    payload = frame(f"XQ;{settings.identifier:02d},{settings.drive},{int(settings.status_response)},{call_mode}")
    return CommandPreview(
        operation="pc-save.call",
        effect=(
            f"Call saved PC command ID {settings.identifier:02d} from drive {settings.drive}; "
            + (
                "enable automatic call at printer power-on."
                if settings.auto_call
                else "leave power-on auto-call disabled."
            )
        ),
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        requires_reset=settings.auto_call,
        dangerous=True,
    )


def build_barcode_data_command(data: str, *, barcode_number: int = 0) -> CommandPreview:
    """Build the canonical TPCL ``RB`` data command for barcode fields."""

    settings = BarcodeDataSettings(barcode_number=barcode_number, data=data)
    payload = frame(f"RB{settings.barcode_number:02d};{settings.data}")
    return CommandPreview(
        operation="tpcl.barcode-data",
        effect=f"Load data into barcode slot {settings.barcode_number:02d}; no label is issued by this command.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_linear_barcode_format_command(
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    barcode_type: str = "9",
    check_digit: int = 3,
    module_width: int = 2,
    rotation: int = 0,
    height: int = 100,
    increment: int | None = None,
    guard_bar_length: int = 0,
    human_readable: int = 0,
    zero_suppression: int = 0,
) -> CommandPreview:
    """Build the documented linear-barcode ``XB`` format without data.

    The optional increment/skip tuple is emitted only when requested.  This
    keeps the default command in Toshiba's short form while still exposing
    the documented ``m nnnnnnnnnn, ooo, p, qq`` fields.
    """

    settings = LinearBarcodeFormatSettings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        barcode_type=barcode_type,
        check_digit=check_digit,
        module_width=module_width,
        rotation=rotation,
        height=height,
        increment=increment,
        guard_bar_length=guard_bar_length,
        human_readable=human_readable,
        zero_suppression=zero_suppression,
    )
    body = (
        f"XB{settings.barcode_number:02d};{settings.x:04d},{settings.y:05d},"
        f"{settings.barcode_type},{settings.check_digit},{settings.module_width:02d},"
        f"{settings.rotation},{settings.height:04d}"
    )
    if settings.increment is not None:
        sign = "+" if settings.increment >= 0 else "-"
        body += (
            f",{sign}{abs(settings.increment):010d},{settings.guard_bar_length:03d},"
            f"{settings.human_readable},{settings.zero_suppression:02d}"
        )
    payload = frame(body)
    return CommandPreview(
        operation="tpcl.barcode-linear-format",
        effect=f"Define linear barcode slot {settings.barcode_number:02d}; data is supplied separately by RB.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_linear_barcode_job(
    data: str,
    *,
    issue: IssueSettings | None = None,
    **format_values: object,
) -> list[CommandPreview]:
    """Build a canonical linear-barcode ``XB``/``RB``/optional ``XS`` job."""

    data_settings = BarcodeDataSettings(
        barcode_number=int(format_values.get("barcode_number", 0)),
        data=data,
    )
    commands = [
        build_linear_barcode_format_command(**format_values),
        build_barcode_data_command(data_settings.data, barcode_number=data_settings.barcode_number),
    ]
    if issue is not None:
        commands.append(build_issue_command(issue))
    return commands


def build_issue_command(settings: IssueSettings | None = None) -> CommandPreview:
    """Build the Toshiba B-FV4 TPCL label issue command ``XS``."""

    values = settings or IssueSettings()
    payload = frame(
        f"XS;I,{values.count:04d},{values.cut_interval:03d}{values.sensor}{values.issue_mode}"
        f"{values.speed}{values.ribbon}{values.tag_rotation}{int(values.status_response)}"
    )
    return CommandPreview(
        operation="tpcl.issue",
        effect=f"Issue {values.count} label(s) with mode {values.issue_mode} and sensor {values.sensor}.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_code128_format_command(
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    module_width: int = 2,
    rotation: int = 0,
    height: int = 100,
) -> CommandPreview:
    """Build a Code 128 ``XB`` format command without data or issue."""

    settings = Code128FormatSettings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        module_width=module_width,
        rotation=rotation,
        height=height,
    )
    payload = frame(
        f"XB{settings.barcode_number:02d};{settings.x:04d},{settings.y:05d},9,3,"
        f"{settings.module_width:02d},{settings.rotation},{settings.height:04d}"
    )
    return CommandPreview(
        operation="tpcl.barcode-code128-format",
        effect=f"Define Code 128 barcode slot {settings.barcode_number:02d}; data is supplied separately by RB.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_code128_job(
    data: str,
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    module_width: int = 2,
    rotation: int = 0,
    height: int = 100,
    issue: IssueSettings | None = None,
) -> list[CommandPreview]:
    """Build the canonical Code 128 format, data and issue sequence."""

    settings = Code128Settings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        module_width=module_width,
        rotation=rotation,
        height=height,
        data=data,
    )
    commands = [
        build_code128_format_command(
            barcode_number=settings.barcode_number,
            x=settings.x,
            y=settings.y,
            module_width=settings.module_width,
            rotation=settings.rotation,
            height=settings.height,
        ),
        build_barcode_data_command(settings.data, barcode_number=settings.barcode_number),
    ]
    if issue is not None:
        commands.append(build_issue_command(issue))
    return commands


def build_code128_command(
    data: str,
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    module_width: int = 2,
    rotation: int = 0,
    height: int = 100,
) -> CommandPreview:
    """Build a TPCL Code 128 format with inline data.

    This is the compact Toshiba-supported form. For a normal print workflow,
    prefer :func:`build_code128_job`, which emits ``XB`` then ``RB`` and,
    optionally, ``XS``.
    """

    settings = Code128Settings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        module_width=module_width,
        rotation=rotation,
        height=height,
        data=data,
    )
    payload = frame(
        f"XB{settings.barcode_number:02d};{settings.x:04d},{settings.y:05d},9,3,"
        f"{settings.module_width:02d},{settings.rotation},{settings.height:04d}={settings.data}"
    )
    return CommandPreview(
        operation="tpcl.barcode-code128",
        effect=(
            f"Define Code 128 barcode {settings.barcode_number:02d} at "
            f"({settings.x / 10:.1f}, {settings.y / 10:.1f}) mm; data is not printed until a label format uses it."
        ),
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_qr_code_command(
    data: str,
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    error_correction: str = "M",
    cell_width: int = 4,
    mode: str = "A",
    rotation: int = 0,
    model: int | None = None,
    mask: int | None = None,
    connection_number: int | None = None,
    connection_total: int | None = None,
    connection_xor: int | None = None,
) -> CommandPreview:
    """Build a TPCL QR format command for inline ASCII data.

    The builder covers the B-FV4 QR form (`ESC XB`) and deliberately does not
    accept field-link references or arbitrary binary/Kanji data. Those need a
    label-form/codepage-aware path rather than silently pretending that Python
    text is byte-identical to the printer's selected code page.
    """

    settings = QrCodeSettings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        error_correction=error_correction,
        cell_width=cell_width,
        mode=mode,
        rotation=rotation,
        model=model,
        mask=mask,
        connection_number=connection_number,
        connection_total=connection_total,
        connection_xor=connection_xor,
        data=data,
    )
    body = (
        f"XB{settings.barcode_number:02d};{settings.x:04d},{settings.y:05d},T,"
        f"{settings.error_correction},{settings.cell_width:02d},{settings.mode},{settings.rotation}"
    )
    if settings.model is not None:
        body += f",M{settings.model}"
    if settings.mask is not None:
        body += f",K{settings.mask}"
    if settings.connection_number is not None:
        body += f",J{settings.connection_number:02d}{settings.connection_total:02d}{settings.connection_xor:02X}"
    payload = frame(f"{body}={settings.data}")
    return CommandPreview(
        operation="tpcl.barcode-qr",
        effect=(
            f"Define QR code {settings.barcode_number:02d} at "
            f"({settings.x / 10:.1f}, {settings.y / 10:.1f}) mm; data is not printed until a label format uses it."
        ),
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_qr_code_format_command(
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    error_correction: str = "M",
    cell_width: int = 4,
    mode: str = "A",
    rotation: int = 0,
    model: int | None = None,
    mask: int | None = None,
    connection_number: int | None = None,
    connection_total: int | None = None,
    connection_xor: int | None = None,
) -> CommandPreview:
    """Build a QR ``XB`` format command without data or issue."""

    settings = QrCodeFormatSettings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        error_correction=error_correction,
        cell_width=cell_width,
        mode=mode,
        rotation=rotation,
        model=model,
        mask=mask,
        connection_number=connection_number,
        connection_total=connection_total,
        connection_xor=connection_xor,
    )
    body = (
        f"XB{settings.barcode_number:02d};{settings.x:04d},{settings.y:05d},T,"
        f"{settings.error_correction},{settings.cell_width:02d},{settings.mode},{settings.rotation}"
    )
    if settings.model is not None:
        body += f",M{settings.model}"
    if settings.mask is not None:
        body += f",K{settings.mask}"
    if settings.connection_number is not None:
        body += f",J{settings.connection_number:02d}{settings.connection_total:02d}{settings.connection_xor:02X}"
    payload = frame(body)
    return CommandPreview(
        operation="tpcl.barcode-qr-format",
        effect=f"Define QR code slot {settings.barcode_number:02d}; data is supplied separately by RB.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_qr_code_job(
    data: str,
    *,
    issue: IssueSettings | None = None,
    **format_values: object,
) -> list[CommandPreview]:
    """Build the canonical QR format, data and optional issue sequence."""

    data_settings = BarcodeDataSettings(
        barcode_number=int(format_values.get("barcode_number", 0)),
        data=data,
    )
    format_preview = build_qr_code_format_command(**format_values)
    commands = [
        format_preview,
        build_barcode_data_command(data_settings.data, barcode_number=data_settings.barcode_number),
    ]
    if issue is not None:
        commands.append(build_issue_command(issue))
    return commands


def build_data_matrix_format_command(
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    ecc_type: str = "20",
    cell_width: int = 4,
    format_id: int = 1,
    rotation: int = 0,
    cells_x: int | None = None,
    cells_y: int | None = None,
) -> CommandPreview:
    """Build a Toshiba Data Matrix ``XB`` format command."""

    settings = DataMatrixFormatSettings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        ecc_type=ecc_type,
        cell_width=cell_width,
        format_id=format_id,
        rotation=rotation,
        cells_x=cells_x,
        cells_y=cells_y,
    )
    body = (
        f"XB{settings.barcode_number:02d};{settings.x:04d},{settings.y:05d},Q,{settings.ecc_type},"
        f"{settings.cell_width:02d},{settings.format_id:02d},{settings.rotation}"
    )
    if settings.cells_x is not None:
        body += f",C{settings.cells_x:03d}{settings.cells_y:03d}"
    payload = frame(body)
    return CommandPreview(
        operation="tpcl.barcode-data-matrix-format",
        effect=f"Define Data Matrix slot {settings.barcode_number:02d}; data is supplied separately by RB.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_data_matrix_job(
    data: str,
    *,
    issue: IssueSettings | None = None,
    **format_values: object,
) -> list[CommandPreview]:
    """Build the canonical Data Matrix format, data and optional issue sequence."""

    data_settings = BarcodeDataSettings(
        barcode_number=int(format_values.get("barcode_number", 0)),
        data=data,
    )
    commands = [
        build_data_matrix_format_command(**format_values),
        build_barcode_data_command(data_settings.data, barcode_number=data_settings.barcode_number),
    ]
    if issue is not None:
        commands.append(build_issue_command(issue))
    return commands


def build_pdf417_format_command(
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    security_level: int = 0,
    module_width: int = 2,
    columns: int = 2,
    rotation: int = 0,
    bar_height: int = 20,
) -> CommandPreview:
    """Build a Toshiba PDF417 ``XB`` format command."""

    settings = Pdf417FormatSettings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        security_level=security_level,
        module_width=module_width,
        columns=columns,
        rotation=rotation,
        bar_height=bar_height,
    )
    payload = frame(
        f"XB{settings.barcode_number:02d};{settings.x:04d},{settings.y:05d},P,{settings.security_level:02d},"
        f"{settings.module_width:02d},{settings.columns:02d},{settings.rotation},{settings.bar_height:04d}"
    )
    return CommandPreview(
        operation="tpcl.barcode-pdf417-format",
        effect=f"Define PDF417 slot {settings.barcode_number:02d}; data is supplied separately by RB.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_pdf417_job(
    data: str,
    *,
    issue: IssueSettings | None = None,
    **format_values: object,
) -> list[CommandPreview]:
    """Build the canonical PDF417 format, data and optional issue sequence."""

    data_settings = BarcodeDataSettings(
        barcode_number=int(format_values.get("barcode_number", 0)),
        data=data,
    )
    commands = [
        build_pdf417_format_command(**format_values),
        build_barcode_data_command(data_settings.data, barcode_number=data_settings.barcode_number),
    ]
    if issue is not None:
        commands.append(build_issue_command(issue))
    return commands


def build_maxicode_format_command(
    *,
    barcode_number: int = 0,
    x: int = 0,
    y: int = 0,
    mode: int | None = None,
    connection_number: int | None = None,
    connection_total: int | None = None,
    zipper_contrast: int | None = None,
) -> CommandPreview:
    """Build a Toshiba MaxiCode ``XB`` format command."""

    settings = MaxiCodeFormatSettings(
        barcode_number=barcode_number,
        x=x,
        y=y,
        mode=mode,
        connection_number=connection_number,
        connection_total=connection_total,
        zipper_contrast=zipper_contrast,
    )
    body = f"XB{settings.barcode_number:02d};{settings.x:04d},{settings.y:05d},Z"
    if settings.mode is not None:
        body += f",{settings.mode}"
    if settings.connection_number is not None:
        body += f",J{settings.connection_number:02d}{settings.connection_total:02d}"
    if settings.zipper_contrast is not None:
        body += f",Z{settings.zipper_contrast}"
    payload = frame(body)
    return CommandPreview(
        operation="tpcl.barcode-maxicode-format",
        effect=f"Define MaxiCode slot {settings.barcode_number:02d}; data is supplied by the MaxiCode RB form.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_maxicode_data_command(
    *,
    barcode_number: int,
    mode: int,
    postal_code: str | None = None,
    postal_extension: str | None = None,
    class_of_service: str | None = None,
    country_code: str | None = None,
    message: str | None = None,
    primary: str | None = None,
    secondary: str | None = None,
) -> CommandPreview:
    """Build the fixed-width Toshiba MaxiCode ``RB`` data command."""

    settings = MaxiCodeDataSettings(
        barcode_number=barcode_number,
        mode=mode,
        postal_code=postal_code,
        postal_extension=postal_extension,
        class_of_service=class_of_service,
        country_code=country_code,
        message=message,
        primary=primary,
        secondary=secondary,
    )
    if settings.mode == 2:
        data = (
            f"{settings.postal_code}{settings.postal_extension}{settings.class_of_service}"
            f"{settings.country_code}{settings.message}"
        )
    elif settings.mode == 3:
        data = f"{settings.postal_code}{' ' * 3}{settings.class_of_service}{settings.country_code}{settings.message}"
    else:
        data = f"{settings.primary}{settings.secondary}"
    payload = frame(f"RB{settings.barcode_number:02d};{data}")
    return CommandPreview(
        operation="tpcl.barcode-maxicode-data",
        effect=f"Load fixed-width MaxiCode mode {settings.mode} data into slot {settings.barcode_number:02d}.",
        payload_hex=payload.hex(" "),
        payload_ascii=display_payload(payload),
        dangerous=True,
    )


def build_maxicode_job(
    *,
    issue: IssueSettings | None = None,
    format_values: Mapping[str, object],
    data_values: Mapping[str, object],
) -> list[CommandPreview]:
    """Build a canonical MaxiCode ``XB``/``RB``/optional ``XS`` job."""

    format_preview = build_maxicode_format_command(**format_values)
    data_preview = build_maxicode_data_command(**data_values)
    commands = [format_preview, data_preview]
    if issue is not None:
        commands.append(build_issue_command(issue))
    return commands


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


def add_issue_options(parser: argparse.ArgumentParser) -> None:
    """Add documented ``XS`` issue controls shared by barcode commands."""

    parser.add_argument("--count", type=int, default=1, metavar="0001-9999")
    parser.add_argument("--cut-interval", type=int, default=0, metavar="000-100")
    parser.add_argument("--sensor", type=int, choices=(0, 1, 2, 3, 4), default=2)
    parser.add_argument("--issue-mode", choices=("C", "D", "E", "F", "G"), default="C")
    parser.add_argument("--speed", choices=tuple("123456789AB"), default="3")
    parser.add_argument("--ribbon", choices=("0", "1", "2"), default="0")
    parser.add_argument("--tag-rotation", type=int, choices=(0, 1, 2, 3), default=0)
    parser.add_argument("--status-response", action="store_true")
    parser.add_argument(
        "--no-issue",
        action="store_true",
        help="emit only XB/RB and omit the XS label-issue command",
    )


def issue_settings_from_args(args: argparse.Namespace) -> IssueSettings | None:
    if args.no_issue:
        return None
    return IssueSettings(
        count=args.count,
        cut_interval=args.cut_interval,
        sensor=args.sensor,
        issue_mode=args.issue_mode,
        speed=args.speed,
        ribbon=args.ribbon,
        tag_rotation=args.tag_rotation,
        status_response=args.status_response,
    )


def add_optional_parameter_options(parser: argparse.ArgumentParser) -> None:
    for name in (
        "codepage",
        "zero-font",
        "baud",
        "data-bits",
        "stop-bits",
        "parity",
        "flow-control",
        "destination",
        "forward-feed",
        "control-code",
        "feed-key",
        "euro-code",
        "head-check",
        "auto-calibration",
    ):
        parser.add_argument(f"--{name}", default=None)


def parse_tpcl_general_values(items: Iterable[str]) -> dict[int, str]:
    parameters: dict[int, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"parameter must be CODE=VALUE: {item}")
        code_text, value = item.split("=", 1)
        try:
            code = int(code_text, 10)
        except ValueError as error:
            raise ValueError(f"parameter code must be an integer: {item}") from error
        if code in parameters:
            raise ValueError(f"duplicate TPCL-General code: {code}")
        parameters[code] = value
    return parameters


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

    settings_export = subparsers.add_parser(
        "settings-export", help="write a validated local settings bundle from supplied values"
    )
    settings_export.add_argument("path", type=Path)
    settings_export.add_argument("--ip", type=ipaddress.IPv4Address)
    settings_export.add_argument("--gateway", type=ipaddress.IPv4Address)
    settings_export.add_argument("--subnet", type=ipaddress.IPv4Address)
    settings_export.add_argument("--dhcp", choices=("on", "off"))
    settings_export.add_argument("--client-id")
    settings_export.add_argument("--socket", choices=("on", "off"), dest="socket_enabled")
    settings_export.add_argument("--socket-port", type=int)
    add_optional_parameter_options(settings_export)
    settings_export.add_argument("--x-direction", choices=("+", "-"))
    settings_export.add_argument("--x", type=int, dest="x_value", metavar="TENTHS_MM")
    settings_export.add_argument("--tpcl-general", action="append", default=[], metavar="CODE=VALUE")

    settings_apply = subparsers.add_parser("settings-apply", help="preview/apply a local settings bundle")
    add_target(settings_apply)
    add_write_flags(settings_apply)
    settings_apply.add_argument("--file", required=True, type=Path)

    pc_save_start = subparsers.add_parser("pc-save-start", help="preview opening TPCL PC-command save mode")
    pc_save_start.add_argument("--id", required=True, type=int, dest="identifier", metavar="01-99")
    pc_save_start.add_argument("--drive", choices=(0, 1), type=int, default=0)
    pc_save_start.add_argument("--status-response", action="store_true")

    pc_save_end = subparsers.add_parser("pc-save-end", help="preview terminating TPCL PC-command save mode")
    pc_save_end.set_defaults()

    pc_save_call = subparsers.add_parser("pc-save-call", help="preview/apply a stored TPCL PC command stream")
    add_target(pc_save_call)
    add_write_flags(pc_save_call)
    pc_save_call.add_argument("--id", required=True, type=int, dest="identifier", metavar="01-99")
    pc_save_call.add_argument("--drive", choices=(0, 1), type=int, default=0)
    pc_save_call.add_argument("--status-response", action="store_true")
    pc_save_call.add_argument(
        "--auto-call",
        action="store_true",
        help="enable automatic call at printer power-on; requires explicit --apply --yes",
    )

    barcode = subparsers.add_parser("barcode-code128", help="preview/apply a Code 128 XB/RB/XS print job")
    add_target(barcode)
    add_write_flags(barcode)
    add_issue_options(barcode)
    barcode.add_argument("--data", required=True, help="ASCII barcode data")
    barcode.add_argument("--number", type=int, default=0, dest="barcode_number", metavar="00-31")
    barcode.add_argument("--x", type=int, default=0, metavar="TENTHS_MM")
    barcode.add_argument("--y", type=int, default=0, metavar="TENTHS_MM")
    barcode.add_argument("--module-width", type=int, default=2, metavar="DOTS")
    barcode.add_argument("--rotation", type=int, choices=(0, 1, 2, 3), default=0)
    barcode.add_argument("--height", type=int, default=100, metavar="TENTHS_MM")

    linear = subparsers.add_parser("barcode", help="preview/apply a generic linear TPCL barcode job")
    add_target(linear)
    add_write_flags(linear)
    add_issue_options(linear)
    linear.add_argument("--data", required=True, help="ASCII barcode data")
    linear.add_argument("--type", default="9", dest="barcode_type", metavar="TYPE")
    linear.add_argument("--number", type=int, default=0, dest="barcode_number", metavar="00-31")
    linear.add_argument("--x", type=int, default=0, metavar="TENTHS_MM")
    linear.add_argument("--y", type=int, default=0, metavar="TENTHS_MM")
    linear.add_argument("--check-digit", type=int, default=3, choices=(1, 2, 3, 4, 5))
    linear.add_argument("--module-width", type=int, default=2, metavar="DOTS")
    linear.add_argument("--rotation", type=int, choices=(0, 1, 2, 3), default=0)
    linear.add_argument("--height", type=int, default=100, metavar="TENTHS_MM")
    linear.add_argument("--increment", type=int, metavar="SIGNED_VALUE")
    linear.add_argument("--guard-bar-length", type=int, default=0, metavar="000-100")
    linear.add_argument("--human-readable", type=int, choices=(0, 1), default=0)
    linear.add_argument("--zero-suppression", type=int, default=0, metavar="00-99")

    qr = subparsers.add_parser("qr", help="preview/apply a TPCL QR-code XB/RB/XS print job")
    add_target(qr)
    add_write_flags(qr)
    add_issue_options(qr)
    qr.add_argument("--data", required=True, help="ASCII QR data")
    qr.add_argument("--number", type=int, default=0, dest="barcode_number", metavar="00-31")
    qr.add_argument("--x", type=int, default=0, metavar="TENTHS_MM")
    qr.add_argument("--y", type=int, default=0, metavar="TENTHS_MM")
    qr.add_argument("--ecc", choices=("L", "M", "Q", "H"), default="M", dest="error_correction")
    qr.add_argument("--cell-width", type=int, default=4, metavar="DOTS")
    qr.add_argument("--mode", choices=("A", "M"), default="A")
    qr.add_argument("--rotation", type=int, choices=(0, 1, 2, 3), default=0)
    qr.add_argument("--model", type=int, choices=(1, 2, 3))
    qr.add_argument("--mask", type=int, choices=tuple(range(9)))
    qr.add_argument("--connection-number", type=int, metavar="01-16")
    qr.add_argument("--connection-total", type=int, metavar="01-16")
    qr.add_argument("--connection-xor", type=lambda value: int(value, 16), metavar="00-FF")

    data_matrix = subparsers.add_parser("data-matrix", help="preview/apply a TPCL Data Matrix XB/RB/XS job")
    add_target(data_matrix)
    add_write_flags(data_matrix)
    add_issue_options(data_matrix)
    data_matrix.add_argument("--data", required=True, help="ASCII Data Matrix data")
    data_matrix.add_argument("--number", type=int, default=0, dest="barcode_number", metavar="00-31")
    data_matrix.add_argument("--x", type=int, default=0, metavar="TENTHS_MM")
    data_matrix.add_argument("--y", type=int, default=0, metavar="TENTHS_MM")
    data_matrix.add_argument("--ecc", default="20", dest="ecc_type")
    data_matrix.add_argument("--cell-width", type=int, default=4, metavar="DOTS")
    data_matrix.add_argument("--format-id", type=int, default=1, choices=(1, 2, 3, 4, 5, 6))
    data_matrix.add_argument("--rotation", type=int, choices=(0, 1, 2, 3), default=0)
    data_matrix.add_argument("--cells-x", type=int)
    data_matrix.add_argument("--cells-y", type=int)

    pdf417 = subparsers.add_parser("pdf417", help="preview/apply a TPCL PDF417 XB/RB/XS job")
    add_target(pdf417)
    add_write_flags(pdf417)
    add_issue_options(pdf417)
    pdf417.add_argument("--data", required=True, help="ASCII PDF417 data")
    pdf417.add_argument("--number", type=int, default=0, dest="barcode_number", metavar="00-31")
    pdf417.add_argument("--x", type=int, default=0, metavar="TENTHS_MM")
    pdf417.add_argument("--y", type=int, default=0, metavar="TENTHS_MM")
    pdf417.add_argument("--security-level", type=int, default=0, choices=tuple(range(9)))
    pdf417.add_argument("--module-width", type=int, default=2, metavar="DOTS")
    pdf417.add_argument("--columns", type=int, default=2, metavar="01-30")
    pdf417.add_argument("--rotation", type=int, choices=(0, 1, 2, 3), default=0)
    pdf417.add_argument("--bar-height", type=int, default=20, metavar="0000-0100")

    maxicode = subparsers.add_parser("maxicode", help="preview/apply a TPCL MaxiCode XB/RB/XS job")
    add_target(maxicode)
    add_write_flags(maxicode)
    add_issue_options(maxicode)
    maxicode.add_argument("--mode", type=int, choices=(2, 3, 4, 6), required=True)
    maxicode.add_argument("--number", type=int, default=0, dest="barcode_number", metavar="00-31")
    maxicode.add_argument("--x", type=int, default=0, metavar="TENTHS_MM")
    maxicode.add_argument("--y", type=int, default=0, metavar="TENTHS_MM")
    maxicode.add_argument("--connection-number", type=int, metavar="01-08")
    maxicode.add_argument("--connection-total", type=int, metavar="01-08")
    maxicode.add_argument("--zipper-contrast", type=int, choices=(0, 1, 2, 3))
    maxicode.add_argument("--postal-code")
    maxicode.add_argument("--postal-extension")
    maxicode.add_argument("--class-of-service")
    maxicode.add_argument("--country-code")
    maxicode.add_argument("--message")
    maxicode.add_argument("--primary")
    maxicode.add_argument("--secondary")

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


def settings_bundle_from_args(args: argparse.Namespace) -> SettingsBundle:
    lan_values = {
        "ip": args.ip,
        "gateway": args.gateway,
        "subnet": args.subnet,
        "dhcp": None if args.dhcp is None else args.dhcp == "on",
        "client_id": args.client_id,
        "socket_enabled": None if args.socket_enabled is None else args.socket_enabled == "on",
        "socket_port": args.socket_port,
    }
    lan = LanSettings(**lan_values) if any(value is not None for value in lan_values.values()) else None

    parameter_names = (
        "codepage",
        "zero_font",
        "baud",
        "data_bits",
        "stop_bits",
        "parity",
        "flow_control",
        "destination",
        "forward_feed",
        "control_code",
        "feed_key",
        "euro_code",
        "head_check",
        "auto_calibration",
    )
    parameter_values = {name: getattr(args, name) for name in parameter_names}
    tpcl_parameter = TpclParameterSettings(**parameter_values) if any(parameter_values.values()) else None

    fine_values = {"x_direction": args.x_direction, "x_value": args.x_value}
    fine_adjustment = (
        FineAdjustmentSettings(**fine_values) if any(value is not None for value in fine_values.values()) else None
    )

    return SettingsBundle(
        lan=lan,
        tpcl_parameter=tpcl_parameter,
        fine_adjustment=fine_adjustment,
        tpcl_general=parse_tpcl_general_values(args.tpcl_general),
    )


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
    if args.command == "settings-export":
        bundle = settings_bundle_from_args(args)
        if (
            any(section is not None for section in (bundle.lan, bundle.tpcl_parameter, bundle.fine_adjustment))
            or bundle.tpcl_general
        ):
            build_settings_commands(bundle)
        save_settings_bundle(args.path, bundle)
        print(json.dumps(bundle.model_dump(mode="json"), indent=2), flush=True)
        return
    if args.command == "settings-apply":
        bundle = load_settings_bundle(args.file)
        apply_previews(
            parse_target(args),
            build_settings_commands(bundle),
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "pc-save-start":
        preview = build_pc_save_start_command(
            args.identifier,
            drive=args.drive,
            status_response=args.status_response,
        )
        print(json.dumps(preview.model_dump(mode="json"), indent=2), flush=True)
        print("Preview only: PC-save body transmission is intentionally disabled.", flush=True)
        return
    if args.command == "pc-save-end":
        preview = build_pc_save_terminate_command()
        print(json.dumps(preview.model_dump(mode="json"), indent=2), flush=True)
        print("Preview only: PC-save body transmission is intentionally disabled.", flush=True)
        return
    if args.command == "pc-save-call":
        apply_previews(
            parse_target(args),
            [
                build_pc_save_call_command(
                    args.identifier,
                    drive=args.drive,
                    status_response=args.status_response,
                    auto_call=args.auto_call,
                )
            ],
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "barcode-code128":
        apply_previews(
            parse_target(args),
            build_code128_job(
                args.data,
                barcode_number=args.barcode_number,
                x=args.x,
                y=args.y,
                module_width=args.module_width,
                rotation=args.rotation,
                height=args.height,
                issue=issue_settings_from_args(args),
            ),
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "qr":
        apply_previews(
            parse_target(args),
            build_qr_code_job(
                args.data,
                issue=issue_settings_from_args(args),
                barcode_number=args.barcode_number,
                x=args.x,
                y=args.y,
                error_correction=args.error_correction,
                cell_width=args.cell_width,
                mode=args.mode,
                rotation=args.rotation,
                model=args.model,
                mask=args.mask,
                connection_number=args.connection_number,
                connection_total=args.connection_total,
                connection_xor=args.connection_xor,
            ),
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "barcode":
        apply_previews(
            parse_target(args),
            build_linear_barcode_job(
                args.data,
                issue=issue_settings_from_args(args),
                barcode_number=args.barcode_number,
                x=args.x,
                y=args.y,
                barcode_type=args.barcode_type,
                check_digit=args.check_digit,
                module_width=args.module_width,
                rotation=args.rotation,
                height=args.height,
                increment=args.increment,
                guard_bar_length=args.guard_bar_length,
                human_readable=args.human_readable,
                zero_suppression=args.zero_suppression,
            ),
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "data-matrix":
        apply_previews(
            parse_target(args),
            build_data_matrix_job(
                args.data,
                issue=issue_settings_from_args(args),
                barcode_number=args.barcode_number,
                x=args.x,
                y=args.y,
                ecc_type=args.ecc_type,
                cell_width=args.cell_width,
                format_id=args.format_id,
                rotation=args.rotation,
                cells_x=args.cells_x,
                cells_y=args.cells_y,
            ),
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "pdf417":
        apply_previews(
            parse_target(args),
            build_pdf417_job(
                args.data,
                issue=issue_settings_from_args(args),
                barcode_number=args.barcode_number,
                x=args.x,
                y=args.y,
                security_level=args.security_level,
                module_width=args.module_width,
                columns=args.columns,
                rotation=args.rotation,
                bar_height=args.bar_height,
            ),
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
        )
        return
    if args.command == "maxicode":
        apply_previews(
            parse_target(args),
            build_maxicode_job(
                issue=issue_settings_from_args(args),
                format_values={
                    "barcode_number": args.barcode_number,
                    "x": args.x,
                    "y": args.y,
                    "mode": args.mode,
                    "connection_number": args.connection_number,
                    "connection_total": args.connection_total,
                    "zipper_contrast": args.zipper_contrast,
                },
                data_values={
                    "barcode_number": args.barcode_number,
                    "mode": args.mode,
                    "postal_code": args.postal_code,
                    "postal_extension": args.postal_extension,
                    "class_of_service": args.class_of_service,
                    "country_code": args.country_code,
                    "message": args.message,
                    "primary": args.primary,
                    "secondary": args.secondary,
                },
            ),
            timeout=args.timeout,
            settle_delay=args.settle_delay,
            apply=args.apply,
            yes=args.yes,
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
        parameters = parse_tpcl_general_values(args.parameters)
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
