"""Validated B-FV4 firmware package parsing and update transport.

The public tool accepts a firmware package supplied by the operator.  It does
not contain or download vendor firmware itself.  The transport mirrors the
documented community-tested setting-tool flow:

* validate every ``.abin`` header and payload CRC32;
* stream the complete selected images as raw bytes in bounded chunks;
* query ``burnstatus`` after the printer has finished writing flash;
* reboot and leave firmware mode only after a successful burn status.

No socket is opened while building a plan.  The write path is guarded by the
same explicit ``--apply --yes`` confirmation used by the other mutating CLI
commands.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import time
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Final
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from .core import PrinterTarget, StatusSnapshot

HEADER_SIZE: Final = 64
BLOCK_ALIGNMENT: Final = 131_072
DEFAULT_CHUNK_SIZE: Final = 8_192
DEFAULT_BURN_WAIT: Final = 3.0
DEFAULT_WRITE_TIMEOUT: Final = 60.0
MAGIC: Final = 0x46467841  # little-endian bytes: ``AxFF``

BYTE_BURN_STATUS: Final[bytes] = b"\x1b\x1bburnstatus\r\n"
BYTE_REBOOT_1: Final[bytes] = b"\x1b\x1breboot 1\r\n"
BYTE_EXIT: Final[bytes] = b"\x1b\x1bexit\r\n"

# The vendor header is a packed structure.  The eight bytes before the final
# CRC are reserved and are part of the checksum-covered header.
_HEADER = struct.Struct("<I4B6I HBBHH 16s 4x I")
assert _HEADER.size == HEADER_SIZE


class FirmwareError(ValueError):
    """A package or update cannot be safely processed."""


class FirmwareHeader(BaseModel):
    """The validated metadata in one Atmel-style ``.abin`` image."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    magic: str
    image_type: int = Field(ge=1, le=4)
    subtype: int = Field(ge=0, le=2)
    compressed: bool
    flash_address: int = Field(ge=0, le=0xFFFFFFFF)
    flash_backup_address: int = Field(ge=0, le=0xFFFFFFFF)
    memory_address: int = Field(ge=0, le=0xFFFFFFFF)
    size_bytes: int = Field(ge=0)
    uncompressed_size_bytes: int = Field(ge=0)
    data_crc32: str
    year: int = Field(ge=0, le=65535)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    company_id: int = Field(ge=0, le=65535)
    product_id: int = Field(ge=0, le=65535)
    version: str
    header_crc32: str


class FirmwareImageInfo(BaseModel):
    """Safe-to-print metadata for one package member."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str
    image_type: int
    subtype: int
    version: str
    flash_address: str
    flash_backup_address: str
    memory_address: str
    payload_bytes: int
    total_bytes: int
    data_crc32: str
    header_crc32: str
    sha256: str


class FirmwarePlan(BaseModel):
    """Offline update plan; raw firmware bytes are intentionally omitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: str
    package_sha256: str
    image_count: int = Field(ge=1)
    total_bytes: int = Field(ge=1)
    chunk_size: int = Field(ge=1, le=DEFAULT_CHUNK_SIZE)
    burn_status_command_hex: str
    post_success_command_hex: str
    images: tuple[FirmwareImageInfo, ...]
    effect: str
    status: str


class _FirmwareImage:
    """Internal validated image including bytes, never serialized in a plan."""

    def __init__(self, *, filename: str, raw: bytes, header: FirmwareHeader) -> None:
        self.filename = filename
        self.raw = raw
        self.header = header

    @property
    def info(self) -> FirmwareImageInfo:
        return FirmwareImageInfo(
            filename=self.filename,
            image_type=self.header.image_type,
            subtype=self.header.subtype,
            version=self.header.version,
            flash_address=f"0x{self.header.flash_address:08X}",
            flash_backup_address=f"0x{self.header.flash_backup_address:08X}",
            memory_address=f"0x{self.header.memory_address:08X}",
            payload_bytes=self.header.size_bytes,
            total_bytes=len(self.raw),
            data_crc32=self.header.data_crc32,
            header_crc32=self.header.header_crc32,
            sha256=hashlib.sha256(self.raw).hexdigest(),
        )


class FirmwarePackage:
    """A validated package loaded from a zip archive or one ``.abin`` file."""

    def __init__(self, *, source: Path, package_sha256: str, images: tuple[_FirmwareImage, ...]) -> None:
        if not images:
            raise FirmwareError("firmware package contains no .abin images")
        self.source = source
        self.package_sha256 = package_sha256
        self.images = images

    @classmethod
    def load(cls, path: str | Path) -> FirmwarePackage:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FirmwareError(f"firmware package does not exist: {source}")
        package_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        if source.suffix.lower() == ".zip":
            with ZipFile(source) as archive:
                members = [name for name in archive.namelist() if name.lower().endswith(".abin")]
                if not members:
                    raise FirmwareError("firmware zip contains no .abin images")
                images = tuple(_parse_image(name, archive.read(name)) for name in members)
        elif source.suffix.lower() == ".abin":
            images = (_parse_image(source.name, source.read_bytes()),)
        else:
            raise FirmwareError("firmware package must be a .zip or .abin file")
        return cls(source=source, package_sha256=package_sha256, images=images)

    def plan(self, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> FirmwarePlan:
        if not 1 <= chunk_size <= DEFAULT_CHUNK_SIZE:
            raise FirmwareError(f"chunk_size must be between 1 and {DEFAULT_CHUNK_SIZE}")
        master_versions = {
            image.header.version for image in self.images if image.header.image_type == 3 and image.header.subtype == 0
        }
        if len(master_versions) != 1:
            raise FirmwareError("package must contain exactly one type-3 master firmware version")
        return FirmwarePlan(
            package=str(self.source),
            package_sha256=self.package_sha256,
            image_count=len(self.images),
            total_bytes=sum(len(image.raw) for image in self.images),
            chunk_size=chunk_size,
            burn_status_command_hex=BYTE_BURN_STATUS.hex(" "),
            post_success_command_hex=(BYTE_REBOOT_1 + BYTE_EXIT).hex(" "),
            images=tuple(image.info for image in self.images),
            effect=(
                f"Stream {len(self.images)} validated .abin image(s), query burnstatus, "
                "then reboot and exit firmware mode only on success."
            ),
            status="offline validated plan; no firmware bytes transmitted",
        )

    @property
    def master_version(self) -> str:
        for image in self.images:
            if image.header.image_type == 3 and image.header.subtype == 0:
                return image.header.version
        raise FirmwareError("package has no type-3 master firmware image")


def _parse_image(filename: str, raw: bytes) -> _FirmwareImage:
    if len(raw) < HEADER_SIZE:
        raise FirmwareError(f"{filename}: image is shorter than the {HEADER_SIZE}-byte header")
    fields = _HEADER.unpack_from(raw)
    (
        magic,
        image_type,
        subtype,
        compression,
        _reserved,
        flash_address,
        flash_backup_address,
        memory_address,
        size_bytes,
        uncompressed_size_bytes,
        data_crc32,
        year,
        month,
        day,
        company_id,
        product_id,
        version_bytes,
        header_crc32,
    ) = fields
    if magic != MAGIC:
        raise FirmwareError(f"{filename}: invalid AxFF magic")
    if image_type not in {1, 2, 3, 4}:
        raise FirmwareError(f"{filename}: unsupported image type {image_type}")
    if subtype not in {0, 1, 2}:
        raise FirmwareError(f"{filename}: unsupported image subtype {subtype}")
    if compression != 0:
        raise FirmwareError(f"{filename}: compressed .abin images are not supported by this transport")
    if size_bytes != uncompressed_size_bytes:
        raise FirmwareError(f"{filename}: compressed-size metadata is inconsistent")
    if len(raw) != HEADER_SIZE + size_bytes:
        raise FirmwareError(
            f"{filename}: payload length {len(raw) - HEADER_SIZE} does not match header size {size_bytes}"
        )
    calculated_header_crc = zlib.crc32(raw[:60]) & 0xFFFFFFFF
    if calculated_header_crc != header_crc32:
        raise FirmwareError(f"{filename}: header CRC32 {calculated_header_crc:08X} != {header_crc32:08X}")
    payload_crc = zlib.crc32(raw[HEADER_SIZE:]) & 0xFFFFFFFF
    if payload_crc != data_crc32:
        raise FirmwareError(f"{filename}: payload CRC32 {payload_crc:08X} != {data_crc32:08X}")
    version = version_bytes.split(b"\0", 1)[0].decode("ascii", errors="strict")
    if not version:
        raise FirmwareError(f"{filename}: empty firmware version")
    header = FirmwareHeader(
        magic="AxFF",
        image_type=image_type,
        subtype=subtype,
        compressed=False,
        flash_address=flash_address,
        flash_backup_address=flash_backup_address,
        memory_address=memory_address,
        size_bytes=size_bytes,
        uncompressed_size_bytes=uncompressed_size_bytes,
        data_crc32=f"{data_crc32:08X}",
        year=year,
        month=month,
        day=day,
        company_id=company_id,
        product_id=product_id,
        version=version,
        header_crc32=f"{header_crc32:08X}",
    )
    return _FirmwareImage(filename=filename, raw=raw, header=header)


def _read_response(connection: socket.socket, *, timeout: float, limit: int = 512) -> bytes:
    connection.settimeout(timeout)
    response = bytearray()
    while len(response) < limit:
        try:
            chunk = connection.recv(limit - len(response))
        except TimeoutError:
            break
        if not chunk:
            break
        response.extend(chunk)
        if response.endswith(b"\r\n"):
            break
    return bytes(response)


def _burn_status_is_success(response: bytes) -> bool:
    text = response.decode("ascii", errors="replace").strip()
    if not text:
        raise FirmwareError("printer returned no burnstatus response")
    first = text.split(",", 1)[0].strip()
    try:
        code = int(first, 16)
    except ValueError as exc:
        raise FirmwareError(f"invalid burnstatus response: {text!r}") from exc
    return code != 0


def _validate_target(target: PrinterTarget, package: FirmwarePackage, *, timeout: float, force: bool) -> StatusSnapshot:
    from .core import read_status

    snapshot = read_status(target, timeout=timeout, settle_delay=0.25)
    if snapshot.errors:
        raise FirmwareError(f"preflight status errors: {json.dumps(snapshot.errors, sort_keys=True)}")
    if snapshot.detail_name != "ready":
        raise FirmwareError(f"printer is not ready: {snapshot.detail_name or snapshot.detail}")
    if snapshot.remaining_count not in {None, 0}:
        raise FirmwareError(f"printer reports {snapshot.remaining_count} pending item(s)")
    model = (snapshot.model_name or "") + " " + (snapshot.firmware_model or "")
    if "B-FV4" not in model.upper():
        raise FirmwareError(f"target does not identify as a B-FV4 printer: {model.strip()!r}")
    if not snapshot.firmware_version:
        raise FirmwareError("target did not report a firmware version")
    if snapshot.firmware_version == package.master_version and not force:
        raise FirmwareError(
            f"target already reports firmware {package.master_version}; use --force only to retransmit it"
        )
    return snapshot


def apply_firmware_update(
    target: PrinterTarget,
    package: FirmwarePackage,
    plan: FirmwarePlan,
    *,
    timeout: float,
    write_timeout: float = DEFAULT_WRITE_TIMEOUT,
    burn_wait: float,
    apply: bool,
    yes: bool,
    force: bool,
) -> dict[str, object] | None:
    """Print a plan and optionally perform the guarded firmware update."""

    print(json.dumps(plan.model_dump(mode="json"), indent=2), flush=True)
    if not apply:
        print("Preview only: use --apply --yes to transmit firmware bytes.", flush=True)
        return None
    if not yes:
        raise FirmwareError("firmware writes require both --apply and --yes")
    if burn_wait < 0:
        raise FirmwareError("burn_wait must be non-negative")
    if write_timeout <= 0:
        raise FirmwareError("write_timeout must be positive")

    snapshot = _validate_target(target, package, timeout=timeout, force=force)
    print(
        json.dumps(
            {
                "preflight": "passed",
                "host": str(target.host),
                "port": target.port,
                "current_firmware": snapshot.firmware_version,
                "target_firmware": package.master_version,
            },
            indent=2,
        ),
        flush=True,
    )

    sent = 0
    try:
        with socket.create_connection((str(target.host), target.port), timeout=timeout) as connection:
            # Flash writes can temporarily stop consuming TCP data while a
            # block is erased/programmed. The vendor tool uses a separate
            # write timeout; do not reuse the short query/connect timeout.
            connection.settimeout(write_timeout)
            for image in package.images:
                for offset in range(0, len(image.raw), plan.chunk_size):
                    chunk = image.raw[offset : offset + plan.chunk_size]
                    connection.sendall(chunk)
                    sent += len(chunk)
                    print(
                        f"transmit {image.filename}: {offset + len(chunk)}/{len(image.raw)} bytes "
                        f"({sent}/{plan.total_bytes} total)",
                        flush=True,
                    )
            if burn_wait:
                time.sleep(burn_wait)
            connection.sendall(BYTE_BURN_STATUS)
            burn_response = _read_response(connection, timeout=max(timeout, 1.5))
            success = _burn_status_is_success(burn_response)
            if not success:
                raise FirmwareError(f"printer rejected firmware burn: {burn_response!r}")
            connection.sendall(BYTE_REBOOT_1 + BYTE_EXIT)
    except (OSError, TimeoutError):
        # A failed raw stream can leave the printer's update channel locked.
        # Best-effort recovery is sent before the context closes; the caller
        # still receives the original transport error and must verify status.
        try:
            with socket.create_connection((str(target.host), target.port), timeout=timeout) as recovery:
                recovery.settimeout(timeout)
                recovery.sendall(BYTE_REBOOT_1 + BYTE_EXIT)
        except OSError:
            pass
        raise

    result = {
        "preflight": "passed",
        "transmitted_bytes": sent,
        "burn_status_response_hex": burn_response.hex(" "),
        "burn_status_response_text": burn_response.decode("ascii", errors="replace"),
        "reboot_sent": True,
        "target_firmware": package.master_version,
    }
    print(json.dumps(result, indent=2), flush=True)
    return result


def load_and_plan(path: str | Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> tuple[FirmwarePackage, FirmwarePlan]:
    """Load and validate a package, returning it with its offline plan."""

    package = FirmwarePackage.load(path)
    return package, package.plan(chunk_size=chunk_size)


__all__ = [
    "BLOCK_ALIGNMENT",
    "BYTE_BURN_STATUS",
    "DEFAULT_BURN_WAIT",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_WRITE_TIMEOUT",
    "FirmwareError",
    "FirmwareHeader",
    "FirmwareImageInfo",
    "FirmwarePackage",
    "FirmwarePlan",
    "apply_firmware_update",
    "load_and_plan",
]
