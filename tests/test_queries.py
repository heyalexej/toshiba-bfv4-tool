"""Offline tests for the self-describing B-FV4 maintenance query layer.

The module under test is loaded from its file path under a private name so
the tests stay independent of ``sys.modules`` mutations performed by other
offline test modules in this suite.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "src" / "toshiba_bfv4_tool" / "queries.py"
SPEC = importlib.util.spec_from_file_location("toshiba_bfv4_tool_queries_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class StaticExchange:
    """Exchange callable returning a canned response or raising."""

    def __init__(self, response: bytes | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[bytes] = []

    def __call__(self, payload: bytes) -> bytes:
        self.requests.append(payload)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def test_registry_lists_documented_and_diagnostic_queries_separately() -> None:
    assert MODULE.REGISTRY.names(documented=True) == ("status", "buffer", "version", "identity")
    assert MODULE.REGISTRY.names(documented=False) == (
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
    assert MODULE.REGISTRY.names() == MODULE.REGISTRY.names(documented=True) + MODULE.REGISTRY.names(documented=False)


def test_registry_snapshot_order_is_the_documented_status_page_order() -> None:
    assert [query.name for query in MODULE.snapshot_queries()] == ["status", "buffer", "version", "identity"]


def test_registry_rejects_unknown_query_with_known_names() -> None:
    with pytest.raises(ValueError, match="known queries"):
        MODULE.REGISTRY.get("frobnicate")


def test_registry_describe_is_json_serializable_and_complete() -> None:
    description = MODULE.REGISTRY.describe()
    assert len(description) == len(MODULE.REGISTRY.specs)
    assert {entry["name"] for entry in description} == set(MODULE.REGISTRY.names())


def test_documented_builders_match_tpcl_wire_format() -> None:
    assert MODULE.build_query("status").payload == b"\x1bWS\x0a\x00"
    assert MODULE.build_query("buffer").payload == b"\x1bWB\x0a\x00"
    assert MODULE.build_query("version").payload == b"\x1bWV\x0a\x00"
    assert MODULE.build_query("identity").payload == b"\x1bIR\x0a\x00"


def test_diagnostic_builders_match_extended_wire_format() -> None:
    assert MODULE.build_query("system-version").payload == b"\x1b\x1bsv\r\n"
    assert MODULE.build_query("config").payload == b"\x1b\x1bconfig 0\r\n"
    assert MODULE.build_query("media-info").payload == b"\x1b\x1bshowmi\r\n"
    assert MODULE.build_query("tph-info", "2").payload == b"\x1b\x1btphinfo 2\r\n"
    assert MODULE.build_query("form-list").payload == b"\x1b\x1bobjinquiry 0 0 1\r\n"
    assert MODULE.build_query("font-list").payload == b"\x1b\x1bobjinquiry 0 0 3\r\n"
    assert MODULE.build_query("graphic-list").payload == b"\x1b\x1bobjinquiry 0 0 2\r\n"
    assert MODULE.build_query("info").payload == b"\x1b\x1binfo\r\n"
    assert MODULE.build_query("task-status").payload == b"\x1b\x1btaskstatus\r\n"
    assert MODULE.build_query("burn-status").payload == b"\x1b\x1bburnstatus\r\n"
    assert MODULE.build_query("last-state").payload == b"\x1b\x1blaststate 3\r\n"


def test_wire_query_exposes_hex_and_ascii_rendering() -> None:
    query = MODULE.build_query("status")
    assert query.payload_hex == "1b 57 53 0a 00"
    assert query.payload_ascii == r"\x1bWS\n\x00"
    assert MODULE.build_query("config").payload_ascii == r"\x1b\x1bconfig 0\r\n"


def test_wire_query_declares_read_only_effect_and_documentation_state() -> None:
    status = MODULE.build_query("status")
    assert status.documented is True
    assert "no printer setting is changed" in status.effect

    config = MODULE.build_query("config")
    assert config.documented is False
    assert "must not be retried" in config.effect


def test_every_specification_declares_its_failure_modes() -> None:
    for spec in MODULE.REGISTRY.specs:
        codes = {mode.code for mode in spec.failures}
        if spec.documented:
            assert MODULE.FailureCode.NO_RESPONSE in codes
            assert MODULE.FailureCode.UNEXPECTED_RESPONSE in codes
        else:
            assert MODULE.FailureCode.UNSUPPORTED in codes
            assert MODULE.FailureCode.NO_RESPONSE not in codes


def test_documented_response_specifications_declare_layout_and_example() -> None:
    for name in ("status", "buffer", "version", "identity"):
        response = MODULE.REGISTRY.get(name).response
        assert response.length_bytes is not None
        assert response.layout
        assert response.fields
        assert response.example_hex
        assert response.text_format is False


def test_tph_info_requires_a_whitelisted_value() -> None:
    with pytest.raises(ValueError, match="requires a value"):
        MODULE.build_query("tph-info")
    for bad in ("0", "3", "x"):
        with pytest.raises(ValueError, match="invalid value"):
            MODULE.build_query("tph-info", bad)


def test_queries_without_parameter_reject_values() -> None:
    for name in ("status", "config", "last-state"):
        with pytest.raises(ValueError, match="does not take a value"):
            MODULE.build_query(name, "1")


def test_status_parser_matches_documented_frame() -> None:
    response = bytes.fromhex("01 02 30 30 31 30 30 30 30 03 04 0d 0a")
    assert MODULE.parse_status_frame(response) == {
        "status_detail": "00",
        "status_name": "ready",
        "status_type": "1",
        "remaining_count": 0,
    }


def test_buffer_parser_matches_documented_frame() -> None:
    response = b"\x01\x02" + b"13" + b"3" + b"0012" + b"23" + b"01024" + b"02048" + b"\r\n"
    assert len(response) == 23
    assert MODULE.parse_buffer_frame(response) == {
        "status_detail": "13",
        "status_name": "no-paper",
        "status_type": "3",
        "remaining_count": 12,
        "response_length": 23,
        "buffer_free_kb": 1024,
        "buffer_capacity_kb": 2048,
    }


def test_version_parser_matches_documented_frame() -> None:
    response = b"\x01\x02" + b"23/08/18 " + b"B-FV4D " + b"V1.6 " + b"\x03\x04\r\n"
    assert MODULE.parse_version_frame(response) == {
        "firmware_creation_date": "23/08/18",
        "firmware_model": "B-FV4D",
        "firmware_version": "V1.6",
    }


def test_identity_parser_matches_documented_frame() -> None:
    response = b"B-FV4D".ljust(20) + b"SAMPLE00001".ljust(11)
    assert MODULE.parse_identity_frame(response) == {
        "model_name": "B-FV4D",
        "serial_number": "SAMPLE00001",
    }


def test_example_frames_from_the_registry_parse_successfully() -> None:
    for name, parser in MODULE.PARSERS.items():
        frame = bytes.fromhex(MODULE.REGISTRY.get(name).response.example_hex or "")
        fields = parser(frame)
        assert fields, name
        if "response_length" in fields:
            assert fields["response_length"] == MODULE.REGISTRY.get(name).response.length_bytes


def test_parsers_reject_frames_with_wrong_length_or_delimiters() -> None:
    good = bytes.fromhex("01 02 30 30 31 30 30 30 30 03 04 0d 0a")
    for bad in (good[:-1], good + b"\x00", b"\x02\x00" + good[2:], good[:-4] + b"\x03\x05\r\n"):
        with pytest.raises(ValueError):
            MODULE.parse_status_frame(bad)
    with pytest.raises(ValueError, match="declares length"):
        MODULE.parse_buffer_frame(b"\x01\x02" + b"001" + b"0000" + b"24" + b"01024" + b"02048" + b"\r\n")


def test_execute_query_success_for_every_documented_query() -> None:
    frames = {
        "status": bytes.fromhex("01 02 30 30 32 30 31 32 33 03 04 0d 0a"),
        "buffer": b"\x01\x02" + b"00" + b"1" + b"0000" + b"23" + b"01024" + b"02048" + b"\r\n",
        "version": b"\x01\x02" + b"23/08/18 " + b"B-FV4D " + b"V1.6 " + b"\x03\x04\r\n",
        "identity": b"B-FV4D".ljust(20) + b"2302H000418".ljust(11),
    }
    for name, frame in frames.items():
        exchange = StaticExchange(response=frame)
        outcome = MODULE.execute_query(name, exchange)
        assert outcome.ok is True, name
        assert outcome.error is None
        assert outcome.kind is not MODULE.QueryKind.DIAGNOSTIC
        assert outcome.fields, name
        assert exchange.requests == [MODULE.build_query(name).payload]


def test_execute_query_resolves_query_names_through_the_builder() -> None:
    exchange = StaticExchange(response=b"\x1b\x1bTASK OK\r\n")
    outcome = MODULE.execute_query("task-status", exchange)
    assert outcome.ok is True
    assert exchange.requests == [b"\x1b\x1btaskstatus\r\n"]


def test_execute_query_diagnostic_success_carries_text() -> None:
    exchange = StaticExchange(response=b"MODEL=B-FV4D FW=V1.6\r\n")
    outcome = MODULE.execute_query(MODULE.build_query("config"), exchange)
    assert outcome.ok is True
    assert outcome.documented is False
    assert outcome.response_text == "MODEL=B-FV4D FW=V1.6\r\n"
    assert outcome.fields == {"response_length": 22}


def test_execute_query_reports_timeout_without_retry_advice() -> None:
    exchange = StaticExchange(error=TimeoutError())
    outcome = MODULE.execute_query("status", exchange)
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.stage is MODULE.FailureStage.TRANSPORT
    assert outcome.error.code is MODULE.FailureCode.TIMEOUT
    assert "do not retry" in outcome.error.handling


def test_execute_query_reports_connection_errors() -> None:
    exchange = StaticExchange(error=ConnectionRefusedError("refused"))
    outcome = MODULE.execute_query("status", exchange)
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code is MODULE.FailureCode.CONNECTION_ERROR
    assert "refused" in outcome.error.message


def test_execute_query_maps_empty_answers_by_documentation_state() -> None:
    documented = MODULE.execute_query("status", StaticExchange(response=b""))
    assert documented.ok is False
    assert documented.error is not None
    assert documented.error.code is MODULE.FailureCode.NO_RESPONSE

    diagnostic = MODULE.execute_query("burn-status", StaticExchange(response=b""))
    assert diagnostic.ok is False
    assert diagnostic.error is not None
    assert diagnostic.error.code is MODULE.FailureCode.UNSUPPORTED
    assert "do not retry" in diagnostic.error.handling


def test_execute_query_reports_unexpected_documented_answers() -> None:
    outcome = MODULE.execute_query("status", StaticExchange(response=b"\x01\x02garbage"))
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.stage is MODULE.FailureStage.PARSE
    assert outcome.error.code is MODULE.FailureCode.UNEXPECTED_RESPONSE
    assert "unexpected WS response" in outcome.error.message


def test_failed_outcomes_keep_request_and_response_bytes() -> None:
    frame = b"\x01\x02xx"
    outcome = MODULE.execute_query("status", StaticExchange(response=frame))
    assert outcome.request_hex == "1b 57 53 0a 00"
    assert outcome.response_hex == frame.hex(" ")
