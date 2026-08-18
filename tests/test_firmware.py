"""Offline tests for the validated firmware package path."""

from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from pathlib import Path
from zipfile import ZipFile

import pytest

SCRIPT = Path(__file__).parents[1] / "src" / "toshiba_bfv4_tool" / "firmware.py"
SPEC = importlib.util.spec_from_file_location("toshiba_bfv4_tool_firmware", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

BYTE_BURN_STATUS = MODULE.BYTE_BURN_STATUS
BYTE_EXIT = MODULE.BYTE_EXIT
BYTE_REBOOT_1 = MODULE.BYTE_REBOOT_1
build_download_header = MODULE.build_download_header
FirmwareError = MODULE.FirmwareError
FirmwarePackage = MODULE.FirmwarePackage

HEADER = struct.Struct("<I4B6I HBBHH 16s 4x I")


def make_image(*, filename: str = "B-FV4-master.abin", image_type: int = 3, subtype: int = 0) -> bytes:
    payload = (b"community-test-firmware" * 20)[:431]
    header_without_crc = HEADER.pack(
        0x46467841,
        image_type,
        subtype,
        0,
        0xFF,
        0x00040000,
        0x00360000,
        0x20000000,
        len(payload),
        len(payload),
        zlib.crc32(payload) & 0xFFFFFFFF,
        2026,
        8,
        18,
        8,
        0,
        b"VTEST1\0",
        0,
    )
    return header_without_crc[:60] + struct.pack("<I", zlib.crc32(header_without_crc[:60]) & 0xFFFFFFFF) + payload


def test_valid_zip_is_planned_without_raw_bytes(tmp_path: Path) -> None:
    # The package contains both master and backup entries, as the official
    # setting workflow does for a normal firmware update.
    zip_path = tmp_path / "test-bfv4-firmware.zip"
    with ZipFile(zip_path, "w") as archive:
        archive.writestr("B-FV4-master.abin", make_image())
        archive.writestr("B-FV4-backup.abin", make_image(filename="B-FV4-backup.abin", subtype=1))
    package = FirmwarePackage.load(zip_path)
    plan = package.plan()
    assert plan.image_count == 2
    assert plan.total_bytes == 2 * len(make_image())
    assert bytes.fromhex(plan.download_header_hex) == build_download_header(image_count=2, total_bytes=plan.total_bytes)
    assert plan.burn_status_command_hex == BYTE_BURN_STATUS.hex(" ")
    assert plan.post_success_command_hex == (BYTE_REBOOT_1 + BYTE_EXIT).hex(" ")
    assert all("raw" not in item.model_dump_json() for item in plan.images)


def test_corrupt_payload_crc_is_rejected(tmp_path: Path) -> None:
    image = bytearray(make_image())
    image[-1] ^= 0xFF
    path = tmp_path / "bad.abin"
    path.write_bytes(image)
    with pytest.raises(FirmwareError, match="payload CRC32"):
        FirmwarePackage.load(path)


def test_compressed_image_is_rejected_explicitly(tmp_path: Path) -> None:
    image = bytearray(make_image())
    image[6] = 1
    path = tmp_path / "compressed.abin"
    # Recompute the header CRC after changing the compression flag.
    image[60:64] = struct.pack("<I", zlib.crc32(image[:60]) & 0xFFFFFFFF)
    path.write_bytes(image)
    with pytest.raises(FirmwareError, match="compressed"):
        FirmwarePackage.load(path)
