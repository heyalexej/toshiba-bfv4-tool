"""Offline protocol tests for the Toshiba B-FV4 community tool."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "src" / "toshiba_bfv4_tool" / "core.py"
SPEC = importlib.util.spec_from_file_location("toshiba_bfv4_tool", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_ip_command_matches_toshiba_wire_format() -> None:
    settings = MODULE.LanSettings(ip="192.0.2.67")
    preview = MODULE.build_lan_commands(settings)[0]
    assert preview.payload_ascii == "\\x1bIP; 2, 192, 000, 002, 067\\n\\x00"
    assert preview.payload_hex == "1b 49 50 3b 20 32 2c 20 31 39 32 2c 20 30 30 30 2c 20 30 30 32 2c 20 30 36 37 0a 00"


def test_socket_command_uses_five_digit_port() -> None:
    settings = MODULE.LanSettings(socket_enabled=True, socket_port=9100)
    preview = MODULE.build_lan_commands(settings)[0]
    assert preview.payload_ascii == "\\x1bIS; 1, 09100\\n\\x00"


def test_dhcp_client_id_is_padded_with_ff() -> None:
    settings = MODULE.LanSettings(dhcp=True, client_id="1256CD")
    preview = MODULE.build_lan_commands(settings)[0]
    assert "IH; 1, 1256CD" in preview.payload_ascii
    assert preview.payload_ascii.endswith("FFFFFFFFFFFFFFFFFFFFFFFFFF\\n\\x00")


def test_parameter_command_has_documented_reset_requirement() -> None:
    preview = MODULE.build_parameter_command()
    assert preview.payload_ascii.startswith("\\x1bZ2; 1, ")
    assert preview.payload_ascii.endswith("\\n\\x00")
    assert preview.requires_reset is True


def test_fine_adjustment_command_has_x_coordinate_in_tenths_of_a_mm() -> None:
    preview = MODULE.build_fine_adjustment_command(x_direction="-", x_value=15)
    assert "Z2; 2, +000+000+00-015" in preview.payload_ascii
    assert preview.requires_reset is True


def test_single_command_uses_double_escape_and_crlf() -> None:
    preview = MODULE.build_single_command("media-calibration")
    assert preview.payload_ascii == "\\x1b\\x1bmc\\r\\n"


def test_media_calibration_value_is_ascii_digits_only() -> None:
    preview = MODULE.build_single_command("media-calibration", "12")
    assert preview.payload_ascii == "\\x1b\\x1bsc 12\\r\\n"
    with pytest.raises(ValueError, match="ASCII digits"):
        MODULE.build_single_command("media-calibration", "1;reboot")
    with pytest.raises(ValueError, match="ASCII digits"):
        MODULE.build_single_command("media-calibration", "１２")


def test_single_command_builds_factory_reset() -> None:
    preview = MODULE.build_single_command("factory-reset")
    assert preview.payload_ascii == "\\x1b\\x1bfacreset 0\\r\\n"


def test_single_command_builds_reboot_modes() -> None:
    preview = MODULE.build_single_command("reboot", "3")
    assert preview.payload_ascii == "\\x1b\\x1breboot 3\\r\\n"


def test_internal_query_builds_config_command() -> None:
    preview = MODULE.build_internal_query("config")
    assert preview.payload_ascii == "\\x1b\\x1bconfig 0\\r\\n"
    assert preview.dangerous is False


def test_emulation_command_builds_tpcl_selection_body() -> None:
    preview = MODULE.build_emulation_command("TPCL")
    assert preview.payload_ascii == "\\x1b\\x1bsetnvrs 2\\r\\n31,2,69;33,1,0;"


def test_parameter_page_special_envelope_contains_exit_command() -> None:
    preview = MODULE.build_nv_parameter_command(protocol="setnvrr", count=1, body="20;", add_arg=True)
    assert preview.payload_ascii == "\\x1bArg\\x1b\\x1bsetnvrr 1\\r\\n20;\\x1b\\x1bexit\\r\\n"


def test_download_paths_match_supported_basic_and_font_destinations() -> None:
    assert MODULE.DOWNLOAD_PATHS["font-ttec"] == "/FS/FONT/TTEC/TTF/"
    assert MODULE.DOWNLOAD_PATHS["font-bitmap"] == "/FS/FONT/BITMAP/"
    assert MODULE.DOWNLOAD_PATHS["font-ttf"] == "/FS/FONT/TTF/"
    assert MODULE.DOWNLOAD_PATHS["basic-main"] == "/FS/FORM/E/CODE/"
    assert MODULE.DOWNLOAD_PATHS["basic-data"] == "/FS/FORM/E/"
    assert MODULE.DOWNLOAD_PATHS["general"] is None


def test_download_header_builds_cp_envelope() -> None:
    plan = MODULE.build_download_plan(page="basic-main", filename="MAIN.BAS", size_bytes=123)
    preview = MODULE.build_download_header(plan)
    assert preview.payload_ascii == "\\x1b\\x1bcp /FS/FORM/E/CODE/MAIN.BAS 123\\r\\n"
    transfer = MODULE.build_download_transfer(plan, b"x" * 123)
    assert transfer.startswith(MODULE.BYTE_SPECIAL + bytes.fromhex(preview.payload_hex))
    assert transfer.endswith(MODULE.BYTE_EXIT)
    assert b"x" * 123 in transfer


def test_general_download_is_raw_bytes_without_path_or_header() -> None:
    plan = MODULE.build_download_plan(page="general", filename="anything.bin", size_bytes=1)
    assert plan.destination is None
    assert MODULE.build_download_transfer(plan, b"x") == b"x"


def test_tpcl_general_send_builds_wrapper_and_order() -> None:
    preview = MODULE.build_tpcl_general_command({28: "15", 20: "0"})
    assert preview.payload_ascii == (
        "\\x1bArg\\x1b\\x1bsetnvrr 2\\r\\n20,1,0;28,2,15;\\x1b\\x1breboot 1\\r\\n\\x1b\\x1bexit\\r\\n"
    )


def test_download_plan_rejects_path_traversal_and_wrong_basic_filename() -> None:
    with pytest.raises(ValueError):
        MODULE.build_download_plan(page="basic-data", filename="../x", size_bytes=1)
    with pytest.raises(ValueError):
        MODULE.build_download_plan(page="basic-main", filename="OTHER.BAS", size_bytes=1)


def test_capability_manifest_distinguishes_family_optional_pages() -> None:
    assert "LAN" in MODULE.CAPABILITIES.bfv4d_relevant
    assert "WLAN" in MODULE.CAPABILITIES.family_optional


def test_status_response_parser_matches_live_shape() -> None:
    response = bytes.fromhex("01 02 30 30 31 30 30 30 30 03 04 0d 0a")
    assert MODULE.parse_status(response) == ("00", "1", 0)
    assert MODULE.STATUS_DETAILS["36"] == "reserved"


def test_internal_diagnostic_query_uses_large_limit_and_reports_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[int] = []

    def fake_exchange_limited(target: object, payload: bytes, timeout: float, max_response: int) -> tuple[bytes, bool]:
        captured.append(max_response)
        return b"report", True

    monkeypatch.setattr(MODULE, "exchange_limited", fake_exchange_limited)
    result = MODULE.read_internal_query(
        MODULE.PrinterTarget(host="192.0.2.10"),
        MODULE.build_internal_query("form-list"),
        timeout=0.1,
        settle_delay=0.0,
    )
    assert captured == [MODULE.DIAGNOSTIC_MAX_RESPONSE]
    assert result["response_truncated"] is True
    assert result["response_limit"] == MODULE.DIAGNOSTIC_MAX_RESPONSE


def test_settings_bundle_round_trips_and_builds_partial_commands(tmp_path: Path) -> None:
    path = tmp_path / "printer-settings.json"
    bundle = MODULE.SettingsBundle(
        lan=MODULE.LanSettings(socket_enabled=True, socket_port=9100),
        tpcl_general={20: "0", 28: "15"},
    )
    MODULE.save_settings_bundle(path, bundle)
    loaded = MODULE.load_settings_bundle(path)
    previews = MODULE.build_settings_commands(loaded)
    assert loaded == bundle
    assert [preview.operation for preview in previews] == [
        "lan.socket",
        "parameter-page.tpcl-general",
    ]
    assert "20,1,0;28,2,15;" in previews[1].payload_ascii


def test_empty_settings_bundle_cannot_be_applied() -> None:
    with pytest.raises(ValueError, match="contains no settings"):
        MODULE.build_settings_commands(MODULE.SettingsBundle())


def test_settings_bundle_validates_tpcl_parameter_choices() -> None:
    with pytest.raises(ValueError, match="codepage must be one of"):
        MODULE.TpclParameterSettings(codepage="Z")
    with pytest.raises(ValueError, match="euro_code must be two hexadecimal"):
        MODULE.TpclParameterSettings(euro_code="GG")
