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


def test_pc_save_commands_are_exact_and_auto_call_is_explicit() -> None:
    start = MODULE.build_pc_save_start_command(7, drive=1, status_response=True)
    assert start.payload_ascii == "\\x1bXO;07,1,1\\n\\x00"
    assert start.dangerous is True

    terminate = MODULE.build_pc_save_terminate_command()
    assert terminate.payload_ascii == "\\x1bXP\\n\\x00"

    call = MODULE.build_pc_save_call_command(7, drive=1)
    assert call.payload_ascii == "\\x1bXQ;07,1,0,M\\n\\x00"
    assert call.requires_reset is False

    auto_call = MODULE.build_pc_save_call_command(7, auto_call=True)
    assert auto_call.payload_ascii == "\\x1bXQ;07,0,0,L\\n\\x00"
    assert auto_call.requires_reset is True


def test_pc_save_identifiers_and_drives_are_validated() -> None:
    with pytest.raises(ValueError):
        MODULE.build_pc_save_call_command(0)
    with pytest.raises(ValueError):
        MODULE.build_pc_save_start_command(100)
    with pytest.raises(ValueError):
        MODULE.build_pc_save_call_command(1, drive=2)


def test_code128_builder_matches_tpcl_xb_format() -> None:
    preview = MODULE.build_code128_command("ABC123", barcode_number=7, x=12, y=345, rotation=1, height=80)
    assert preview.payload_ascii == "\\x1bXB07;0012,00345,9,3,02,1,0080=ABC123\\n\\x00"
    assert preview.dangerous is True


def test_code128_builder_rejects_framing_controls_and_non_ascii() -> None:
    with pytest.raises(ValueError, match="line breaks"):
        MODULE.build_code128_command("ABC\n123")
    with pytest.raises(ValueError, match="ASCII"):
        MODULE.build_code128_command("ä")
    with pytest.raises(ValueError, match="data"):
        MODULE.build_code128_command("")


def test_canonical_code128_job_uses_xb_rb_and_xs() -> None:
    commands = MODULE.build_code128_job(
        "ABC123",
        barcode_number=7,
        x=12,
        y=345,
        rotation=1,
        height=80,
        issue=MODULE.IssueSettings(
            count=2,
            cut_interval=10,
            sensor=1,
            issue_mode="D",
            speed="A",
            tag_rotation=2,
            status_response=True,
        ),
    )
    assert [command.payload_ascii for command in commands] == [
        "\\x1bXB07;0012,00345,9,3,02,1,0080\\n\\x00",
        "\\x1bRB07;ABC123\\n\\x00",
        "\\x1bXS;I,0002,0101DA021\\n\\x00",
    ]


def test_linear_barcode_builder_supports_documented_optional_fields() -> None:
    preview = MODULE.build_linear_barcode_format_command(
        barcode_number=4,
        barcode_type="V",
        increment=-12,
        guard_bar_length=7,
        human_readable=1,
        zero_suppression=2,
    )
    assert preview.payload_ascii == "\\x1bXB04;0000,00000,V,3,02,0,0100,-0000000012,007,1,02\\n\\x00"

    commands = MODULE.build_linear_barcode_job("TRACK", barcode_number=4, barcode_type="V", issue=None)
    assert commands[1].payload_ascii == "\\x1bRB04;TRACK\\n\\x00"


def test_qr_builder_matches_automatic_tpcl_xb_format() -> None:
    preview = MODULE.build_qr_code_command("https://example.invalid", barcode_number=3, x=15, y=125)
    assert preview.payload_ascii == ("\\x1bXB03;0015,00125,T,M,04,A,0=https://example.invalid\\n\\x00")


def test_qr_builder_matches_manual_model_mask_and_connection() -> None:
    preview = MODULE.build_qr_code_command(
        "PAYLOAD",
        mode="M",
        error_correction="H",
        model=2,
        mask=8,
        connection_number=2,
        connection_total=2,
        connection_xor=0xAF,
    )
    assert preview.payload_ascii == "\\x1bXB00;0000,00000,T,H,04,M,0,M2,K8,J0202AF=PAYLOAD\\n\\x00"


def test_qr_builder_validates_manual_options_and_data() -> None:
    with pytest.raises(ValueError, match="manual mode"):
        MODULE.build_qr_code_command("data", model=2)
    with pytest.raises(ValueError, match="Input should be 1 or 2"):
        MODULE.build_qr_code_command("data", mode="M", model=3, error_correction="H")
    with pytest.raises(ValueError, match="supplied together"):
        MODULE.build_qr_code_command("data", mode="M", connection_number=1)
    with pytest.raises(ValueError, match="line breaks"):
        MODULE.build_qr_code_command("data\r")


def test_qr_data_matrix_and_pdf417_jobs_are_canonical() -> None:
    qr = MODULE.build_qr_code_job("PAYLOAD", barcode_number=3, x=15, y=125)
    assert [command.payload_ascii for command in qr] == [
        "\\x1bXB03;0015,00125,T,M,04,A,0\\n\\x00",
        "\\x1bRB03;PAYLOAD\\n\\x00",
    ]

    matrix = MODULE.build_data_matrix_job("DM", barcode_number=2, cells_x=10, cells_y=12)
    assert [command.payload_ascii for command in matrix] == [
        "\\x1bXB02;0000,00000,Q,20,04,01,0,C010012\\n\\x00",
        "\\x1bRB02;DM\\n\\x00",
    ]

    pdf417 = MODULE.build_pdf417_job("PDF", barcode_number=1)
    assert [command.payload_ascii for command in pdf417] == [
        "\\x1bXB01;0000,00000,P,00,02,02,0,0020\\n\\x00",
        "\\x1bRB01;PDF\\n\\x00",
    ]


def test_maxicode_mode_two_uses_fixed_width_rb_data() -> None:
    preview = MODULE.build_maxicode_data_command(
        barcode_number=5,
        mode=2,
        postal_code="12345",
        postal_extension="6789",
        class_of_service="001",
        country_code="840",
        message="ABC",
    )
    assert preview.payload_ascii == "\\x1bRB05;123456789001840ABC\\n\\x00"


def test_barcode_jobs_reject_non_ascii_or_incomplete_2d_options() -> None:
    with pytest.raises(ValueError, match="ASCII"):
        MODULE.build_linear_barcode_job("ä")
    with pytest.raises(ValueError, match="cells_x and cells_y"):
        MODULE.build_data_matrix_job("DM", cells_x=10)
    with pytest.raises(ValueError, match="postal_extension"):
        MODULE.build_maxicode_data_command(
            barcode_number=0,
            mode=2,
            postal_code="12345",
            postal_extension="123",
            class_of_service="001",
            country_code="840",
            message="ABC",
        )
