"""Offline review tests for TPCL write paths of the Toshiba B-FV4 tool.

Complements ``tests/test_core.py`` by pinning the exact byte format, the
field validation and the preview/apply safety logic of the TPCL basic
parameters (``Z2;1``/``Z2;2``), the TPCL-General page, the emulation
selector and the reset commands.  See ``docs/tpcl.md`` for the documented
wire formats and open points.  Every test is offline: helpers that could
open a socket are monkeypatched so a regression can never reach a printer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

SCRIPT = Path(__file__).parents[1] / "src" / "toshiba_bfv4_tool" / "core.py"
SPEC = importlib.util.spec_from_file_location("toshiba_bfv4_tool_core_review", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

HOST = "192.0.2.10"  # RFC 5737 documentation address; never a real target
TARGET = MODULE.PrinterTarget(host=HOST)
TPCL_GENERAL_CANONICAL_ORDER = (20, 21, 22, 23, 24, 25, 26, 190, 27, 28, 29, 30, 32, 2000, 2002, 2003, 3000)


def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any socket transmission fail the test immediately."""

    def _fail(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("offline test must not transmit data to a printer")

    monkeypatch.setattr(MODULE, "exchange", _fail)


def preview_bytes(preview: object) -> bytes:
    return bytes.fromhex(preview.payload_hex.replace(" ", ""))


# ---------------------------------------------------------------------------
# TPCL basic parameters: ESC Z2;1
# ---------------------------------------------------------------------------


def test_z2_parameter_default_body_is_24_characters() -> None:
    preview = MODULE.build_parameter_command()
    assert preview.payload_ascii == "\\x1bZ2; 1, 802100000001000020000000\\n\\x00"
    body = preview.payload_ascii.split("Z2; 1, ", 1)[1].split("\\n", 1)[0]
    assert len(body) == 24
    assert preview_bytes(preview) == b"\x1bZ2; 1, 802100000001000020000000\n\x00"
    assert preview.requires_reset is True


def test_z2_parameter_places_two_char_euro_code_after_16_single_fields() -> None:
    preview = MODULE.build_parameter_command(
        codepage="F",
        control_code="2",
        euro_code="A0",
        head_check="1",
        auto_calibration="2",
    )
    assert preview.payload_ascii == "\\x1bZ2; 1, F021000000020000A0100020\\n\\x00"
    body = preview.payload_ascii.split("Z2; 1, ", 1)[1].split("\\n", 1)[0]
    assert body[16:18] == "A0"
    assert body[18] == "1"
    assert body[22] == "2"
    assert body[23] == "0"


def test_z2_parameter_accepts_lowercase_hex_euro_code() -> None:
    preview = MODULE.build_parameter_command(euro_code="2a")
    body = preview.payload_ascii.split("Z2; 1, ", 1)[1].split("\\n", 1)[0]
    assert body[16:18] == "2a"


def test_z2_parameter_rejects_invalid_field_lengths_and_hex() -> None:
    with pytest.raises(ValueError, match="single-character fields"):
        MODULE.build_parameter_command(codepage="12")
    with pytest.raises(ValueError, match="single-character fields"):
        MODULE.build_parameter_command(feed_key="01")
    with pytest.raises(ValueError, match="euro_code"):
        MODULE.build_parameter_command(euro_code="2")
    with pytest.raises(ValueError, match="euro_code"):
        MODULE.build_parameter_command(euro_code="2G")
    with pytest.raises(ValueError, match="euro_code"):
        MODULE.build_parameter_command(euro_code="202")


# ---------------------------------------------------------------------------
# TPCL fine adjustment: ESC Z2;2
# ---------------------------------------------------------------------------


def test_z2_fine_adjustment_default_body_is_neutral_33_characters() -> None:
    preview = MODULE.build_fine_adjustment_command()
    assert preview.payload_ascii == "\\x1bZ2; 2, +000+000+00+000+00+00-00+00000000\\n\\x00"
    body = preview.payload_ascii.split("Z2; 2, ", 1)[1].split("\\n", 1)[0]
    assert len(body) == 33
    assert preview.requires_reset is True


def test_z2_fine_adjustment_x_field_encodes_tenths_of_a_mm() -> None:
    preview = MODULE.build_fine_adjustment_command(x_direction="-", x_value=995)
    body = preview.payload_ascii.split("Z2; 2, ", 1)[1].split("\\n", 1)[0]
    assert body == "+000+000+00-995+00+00-00+00000000"
    assert preview_bytes(preview) == b"\x1bZ2; 2, +000+000+00-995+00+00-00+00000000\n\x00"


def test_z2_fine_adjustment_rejects_out_of_range_values_and_signs() -> None:
    with pytest.raises(ValueError, match="x_direction"):
        MODULE.build_fine_adjustment_command(x_direction="*")
    with pytest.raises(ValueError, match="x_value"):
        MODULE.build_fine_adjustment_command(x_value=-1)
    with pytest.raises(ValueError, match="x_value"):
        MODULE.build_fine_adjustment_command(x_value=996)
    MODULE.build_fine_adjustment_command(x_value=0)
    MODULE.build_fine_adjustment_command(x_direction="-", x_value=995)


# ---------------------------------------------------------------------------
# setnvrr/setnvrs transport envelopes
# ---------------------------------------------------------------------------


def test_nv_envelope_wire_format_without_and_with_arg_wrapper() -> None:
    plain = MODULE.build_nv_parameter_command(protocol="setnvrs", count=9999, body="31,2,65;")
    assert plain.payload_ascii == "\\x1b\\x1bsetnvrs 9999\\r\\n31,2,65;"
    assert plain.dangerous is True
    wrapped = MODULE.build_nv_parameter_command(protocol="setnvrr", count=0, body="20,1,8;", add_arg=True)
    assert wrapped.payload_ascii == "\\x1bArg\\x1b\\x1bsetnvrr 0\\r\\n20,1,8;\\x1b\\x1bexit\\r\\n"
    assert preview_bytes(wrapped) == b"\x1bArg\x1b\x1bsetnvrr 0\r\n20,1,8;\x1b\x1bexit\r\n"


def test_nv_envelope_validates_protocol_count_and_body() -> None:
    with pytest.raises(ValueError, match="setnvrr or setnvrs"):
        MODULE.build_nv_parameter_command(protocol="Z2", count=1, body="x;")
    with pytest.raises(ValueError, match="count"):
        MODULE.build_nv_parameter_command(protocol="setnvrr", count=-1, body="x;")
    with pytest.raises(ValueError, match="count"):
        MODULE.build_nv_parameter_command(protocol="setnvrr", count=10000, body="x;")
    for control in ("\x00", "\r", "\n"):
        with pytest.raises(ValueError, match="NUL or line breaks"):
            MODULE.build_nv_parameter_command(protocol="setnvrr", count=1, body=f"20,1{control}0;")
    with pytest.raises(UnicodeEncodeError):
        MODULE.build_nv_parameter_command(protocol="setnvrr", count=1, body="20,1,ä;")


# ---------------------------------------------------------------------------
# TPCL-General page
# ---------------------------------------------------------------------------


def test_tpcl_general_wrapper_matches_send_button_byte_stream() -> None:
    preview = MODULE.build_tpcl_general_command({20: "0", 21: "0", 28: "15"})
    assert preview.payload_ascii == (
        "\\x1bArg\\x1b\\x1bsetnvrr 3\\r\\n20,1,0;21,1,0;28,2,15;\\x1b\\x1breboot 1\\r\\n\\x1b\\x1bexit\\r\\n"
    )
    assert preview_bytes(preview) == (
        b"\x1bArg\x1b\x1bsetnvrr 3\r\n20,1,0;21,1,0;28,2,15;\x1b\x1breboot 1\r\n\x1b\x1bexit\r\n"
    )
    assert preview.operation == "parameter-page.tpcl-general"
    assert preview.requires_reset is True
    assert preview.dangerous is True


def test_tpcl_general_emits_canonical_order_and_byte_accurate_lengths() -> None:
    preview = MODULE.build_tpcl_general_command({3000: "1234567890", 20: "8"})
    assert preview.payload_ascii == (
        "\\x1bArg\\x1b\\x1bsetnvrr 2\\r\\n20,1,8;3000,10,1234567890;\\x1b\\x1breboot 1\\r\\n\\x1b\\x1bexit\\r\\n"
    )
    preview = MODULE.build_tpcl_general_command({27: "1", 190: "0", 26: "0"})
    assert "\\x1bsetnvrr 3\\r\\n26,1,0;190,1,0;27,1,1;" in preview.payload_ascii
    preview = MODULE.build_tpcl_general_command({code: "0" for code in TPCL_GENERAL_CANONICAL_ORDER})
    body = preview.payload_ascii.split("setnvrr 17\\r\\n", 1)[1].split("\\x1b", 1)[0]
    assert body == "".join(f"{code},1,0;" for code in TPCL_GENERAL_CANONICAL_ORDER)


def test_tpcl_general_field_validation() -> None:
    with pytest.raises(ValueError, match="at least one"):
        MODULE.build_tpcl_general_command({})
    with pytest.raises(ValueError, match="unsupported TPCL-General codes: \\[9999\\]"):
        MODULE.build_tpcl_general_command({9999: "1"})
    with pytest.raises(ValueError, match="non-empty strings"):
        MODULE.build_tpcl_general_command({20: ""})
    with pytest.raises(ValueError, match="NUL or line breaks"):
        MODULE.build_tpcl_general_command({20: "0\r"})
    with pytest.raises(ValueError, match="ASCII"):
        MODULE.build_tpcl_general_command({20: "ä"})
    with pytest.raises(ValueError, match="ASCII"):
        MODULE.build_tpcl_general_command({20: "\xff"})


def test_tpcl_general_code_registry_covers_documented_controls() -> None:
    assert MODULE.TPCL_GENERAL_CODES[20] == "image-char-code"
    assert MODULE.TPCL_GENERAL_CODES[21] == "image-zero-font"
    assert MODULE.TPCL_GENERAL_CODES[28] == "position-x-tenths-mm"
    assert MODULE.TPCL_GENERAL_CODES[190] == "control-reprint-after-error"
    assert MODULE.TPCL_GENERAL_CODES[3000] == "product-serial-number"
    assert TPCL_GENERAL_CANONICAL_ORDER == tuple(
        code
        for code in (20, 21, 22, 23, 24, 25, 26, 190, 27, 28, 29, 30, 32, 2000, 2002, 2003, 3000)
        if code in MODULE.TPCL_GENERAL_CODES
    )


# ---------------------------------------------------------------------------
# Emulation selector
# ---------------------------------------------------------------------------


def test_emulation_named_modes_encode_documented_selector_values() -> None:
    for mode, value in (("D", 65), ("E", 66), ("I", 73), ("Z", 90), ("TPCL", 69)):
        preview = MODULE.build_emulation_command(mode)
        assert preview.payload_ascii == f"\\x1b\\x1bsetnvrs 2\\r\\n31,2,{value};33,1,0;"
        assert preview_bytes(preview) == f"\x1b\x1bsetnvrs 2\r\n31,2,{value};33,1,0;".encode("ascii")
        assert preview.dangerous is True


def test_emulation_auto_modes_current_byte_format() -> None:
    # Current behaviour: AUTO/AUTO2 only emit the code-33 item.  docs/tpcl.md
    # records the open question about additional selector values 48/85.
    assert MODULE.build_emulation_command("AUTO").payload_ascii == "\\x1b\\x1bsetnvrs 1\\r\\n33,1,1;"
    assert MODULE.build_emulation_command("AUTO2").payload_ascii == "\\x1b\\x1bsetnvrs 1\\r\\n33,1,2;"


def test_emulation_add_arg_wraps_envelope_with_exit() -> None:
    preview = MODULE.build_emulation_command("D", add_arg=True)
    assert preview.payload_ascii == "\\x1bArg\\x1b\\x1bsetnvrs 2\\r\\n31,2,65;33,1,0;\\x1b\\x1bexit\\r\\n"
    assert preview_bytes(preview) == b"\x1bArg\x1b\x1bsetnvrs 2\r\n31,2,65;33,1,0;\x1b\x1bexit\r\n"


def test_emulation_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        MODULE.build_emulation_command("J")


# ---------------------------------------------------------------------------
# Reset commands
# ---------------------------------------------------------------------------


def test_reset_commands_use_tpcl_frame_with_double_escape_variants() -> None:
    reset = MODULE.build_reset_command()
    assert reset.payload_ascii == "\\x1bZ0\\n\\x00"
    assert reset.operation == "single.reset"
    assert reset.dangerous is True
    assert MODULE.build_single_command("wr-reset").payload_ascii == "\\x1bWR\\n\\x00"
    assert MODULE.build_single_command("reboot").payload_ascii == "\\x1b\\x1breboot 0\\r\\n"
    assert MODULE.build_single_command("reboot", "3").payload_ascii == "\\x1b\\x1breboot 3\\r\\n"
    assert MODULE.build_single_command("factory-reset").payload_ascii == "\\x1b\\x1bfacreset 0\\r\\n"
    assert MODULE.build_single_command("reset-command").payload_ascii == "\\x1b\\x1bresetcommand 0\\r\\n"
    assert MODULE.build_single_command("self-test").payload_ascii == "\\x1b\\x1bselftest 0\\r\\n"


def test_reset_value_validation_restricts_reboot_modes() -> None:
    with pytest.raises(ValueError, match="reboot value must be one of: 0, 1, 3"):
        MODULE.build_single_command("reboot", "2")
    with pytest.raises(ValueError, match="factory-reset value must be one of: 0"):
        MODULE.build_single_command("factory-reset", "1")
    with pytest.raises(ValueError, match="unsupported single command"):
        MODULE.build_single_command("power-off")


def test_frame_rejects_non_ascii_commands() -> None:
    assert MODULE.frame("WS") == b"\x1bWS\n\x00"
    with pytest.raises(ValueError, match="ASCII"):
        MODULE.frame("WÄ")


# ---------------------------------------------------------------------------
# Preview-only and --apply --yes safety logic
# ---------------------------------------------------------------------------


def test_preview_only_prints_bytes_and_never_transmits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    forbid_network(monkeypatch)
    preview = MODULE.build_reset_command()
    results = MODULE.apply_previews(TARGET, [preview], timeout=0.1, settle_delay=0.0, apply=False, yes=False)
    assert results == []
    out = capsys.readouterr().out
    assert "Preview only: use --apply --yes to transmit these bytes." in out
    assert '"operation": "single.reset"' in out
    assert preview.payload_hex in out


def test_apply_without_yes_aborts_before_transmission(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)
    with pytest.raises(ValueError, match="writes require both --apply and --yes"):
        MODULE.apply_previews(
            TARGET, [MODULE.build_reset_command()], timeout=0.1, settle_delay=0.0, apply=True, yes=False
        )


def test_apply_yes_transmits_exactly_the_previewed_bytes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    sent: list[tuple[object, bytes, float]] = []

    def fake_exchange(target: object, payload: bytes, timeout: float) -> bytes:
        sent.append((target, payload, timeout))
        return b"\x00"

    monkeypatch.setattr(MODULE, "exchange", fake_exchange)
    preview = MODULE.build_reset_command()
    results = MODULE.apply_previews(TARGET, [preview], timeout=0.25, settle_delay=0.0, apply=True, yes=True)
    assert sent == [(TARGET, b"\x1bZ0\n\x00", 0.25)]
    assert results == [{"operation": "single.reset", "response_hex": "00", "response_length": 1}]
    assert '"operation": "single.reset"' in capsys.readouterr().out


def test_cli_tpcl_general_apply_yes_sends_exact_wrapper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    sent: list[bytes] = []

    def fake_exchange(target: object, payload: bytes, timeout: float) -> bytes:
        sent.append(payload)
        return b""

    monkeypatch.setattr(MODULE, "exchange", fake_exchange)
    MODULE.main(["tpcl-general", HOST, "20=0", "21=0", "28=15", "--apply", "--yes", "--settle-delay", "0"])
    assert sent == [b"\x1bArg\x1b\x1bsetnvrr 3\r\n20,1,0;21,1,0;28,2,15;\x1b\x1breboot 1\r\n\x1b\x1bexit\r\n"]
    out = capsys.readouterr().out
    assert "Preview only" not in out
    assert '"operation": "parameter-page.tpcl-general"' in out


def test_cli_preview_only_never_transmits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    forbid_network(monkeypatch)
    MODULE.main(["emulation", HOST, "TPCL"])
    assert "Preview only: use --apply --yes to transmit these bytes." in capsys.readouterr().out


def test_cli_apply_without_yes_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)
    with pytest.raises(ValueError, match="writes require both --apply and --yes"):
        MODULE.main(["emulation", HOST, "TPCL", "--apply"])


def test_cli_tpcl_parameter_and_page_are_preview_only_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    forbid_network(monkeypatch)
    MODULE.main(["tpcl-parameter", HOST])
    MODULE.main(["tpcl-page", HOST, "--protocol", "setnvrr", "--count", "2", "--body", "20,1,8;21,1,0;"])
    out = capsys.readouterr().out
    assert out.count("Preview only: use --apply --yes to transmit these bytes.") == 2


def test_cli_rejects_malformed_tpcl_general_input(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)
    with pytest.raises(ValueError, match="CODE=VALUE"):
        MODULE.main(["tpcl-general", HOST, "20"])
    with pytest.raises(ValueError, match="integer"):
        MODULE.main(["tpcl-general", HOST, "twenty=0"])
    with pytest.raises(ValueError, match="duplicate"):
        MODULE.main(["tpcl-general", HOST, "20=0", "20=1"])
    with pytest.raises(ValueError, match="unsupported TPCL-General codes"):
        MODULE.main(["tpcl-general", HOST, "19=0"])


def test_cli_rejects_reset_value_and_unknown_reboot_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)
    with pytest.raises(ValueError, match="reset does not take a value"):
        MODULE.main(["single", HOST, "reset", "0"])
    with pytest.raises(ValueError, match="reset does not take a value"):
        MODULE.main(["single", HOST, "wr-reset", "1"])
    with pytest.raises(ValueError, match="reboot value must be one of: 0, 1, 3"):
        MODULE.main(["single", HOST, "reboot", "2", "--settle-delay", "0"])


def test_cli_rejects_unknown_emulation_mode_via_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)
    with pytest.raises(SystemExit):
        MODULE.main(["emulation", HOST, "J"])


def test_printer_target_validates_port_range() -> None:
    with pytest.raises(ValidationError):
        MODULE.PrinterTarget(host=HOST, port=0)
    with pytest.raises(ValidationError):
        MODULE.PrinterTarget(host=HOST, port=65536)
