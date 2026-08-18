"""Self-describing read-only query layer for Toshiba B-FV4 printers.

Every read-only query the tool supports lives in one registry.  Each entry
describes its own wire format, response layout, and failure behaviour, so a
caller can inspect a query before sending it.  Builders turn registry entries
into exact byte sequences; :func:`execute_query` runs a query through an
injected exchange callable, which keeps this module fully offline testable
and free of any network access of its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

ESC: Final[bytes] = b"\x1b"
LF_NUL: Final[bytes] = b"\x0a\x00"
CR_LF: Final[bytes] = b"\x0d\x0a"
SOH_STX: Final[bytes] = b"\x01\x02"
ETX_EOT_CRLF: Final[bytes] = b"\x03\x04\r\n"
DEFAULT_RESPONSE_LIMIT: Final[int] = 512
DIAGNOSTIC_RESPONSE_LIMIT: Final[int] = 64 * 1024

#: Transports a request through a single printer connection and returns the
#: raw response bytes.  Implementations own connection handling and timeouts.
Exchange: TypeAlias = Callable[[bytes], bytes]


class QueryKind(StrEnum):
    """Functional group of a query."""

    STATUS = "status"
    BUFFER = "buffer"
    FIRMWARE = "firmware"
    IDENTITY = "identity"
    DIAGNOSTIC = "diagnostic"


class WireTransport(StrEnum):
    """Framing used on the wire.

    ``TPCL`` frames a command as ``ESC <command> LF NUL``; ``EXTENDED``
    frames it as ``ESC ESC <command> CR LF``.
    """

    TPCL = "tpcl-frame"
    EXTENDED = "extended-frame"


class FailureStage(StrEnum):
    TRANSPORT = "transport"
    RESPONSE = "response"
    PARSE = "parse"


class FailureCode(StrEnum):
    CONNECTION_ERROR = "connection-error"
    TIMEOUT = "timeout"
    NO_RESPONSE = "no-response"
    UNSUPPORTED = "unsupported"
    UNEXPECTED_RESPONSE = "unexpected-response"


class ResponseField(BaseModel):
    """One fixed-width field of a documented response layout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    width: int | None = None


class ResponseSpec(BaseModel):
    """Description of the bytes a printer is expected to answer with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    length_bytes: int | None = None
    layout: str
    begins_with_hex: str | None = None
    ends_with_hex: str | None = None
    fields: tuple[ResponseField, ...] = ()
    example_hex: str | None = None
    text_format: bool = False


class FailureMode(BaseModel):
    """A failure condition a query can produce and how to react to it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: FailureCode
    condition: str
    handling: str


class ParameterSpec(BaseModel):
    """An argument a query command takes on the wire."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    choices: tuple[str, ...]
    description: str


class QuerySpec(BaseModel):
    """A self-describing registry entry for one read-only query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: QueryKind
    transport: WireTransport
    command: str
    summary: str
    documented: bool
    response: ResponseSpec
    failures: tuple[FailureMode, ...]
    parameter: ParameterSpec | None = None
    response_limit: int = Field(default=DEFAULT_RESPONSE_LIMIT, ge=1, le=DIAGNOSTIC_RESPONSE_LIMIT)


class WireQuery(BaseModel):
    """A built query: the exact bytes plus everything needed to judge it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: QueryKind
    transport: WireTransport
    documented: bool
    command: str
    summary: str
    effect: str
    payload_hex: str
    payload_ascii: str
    response: ResponseSpec
    failures: tuple[FailureMode, ...]
    response_limit: int

    @property
    def payload(self) -> bytes:
        return bytes.fromhex(self.payload_hex)


class QueryFailure(BaseModel):
    """Structured error report for one failed query."""

    model_config = ConfigDict(extra="forbid")

    stage: FailureStage
    code: FailureCode
    message: str
    handling: str


class QueryOutcome(BaseModel):
    """Result of one executed query, successful or not."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: QueryKind
    documented: bool
    ok: bool
    request_hex: str
    response_hex: str = ""
    response_text: str | None = None
    response_truncated: bool = False
    fields: dict[str, str | int] = Field(default_factory=dict)
    error: QueryFailure | None = None


TIMEOUT_MODE: Final = FailureMode(
    code=FailureCode.TIMEOUT,
    condition="The printer does not answer within the receive timeout.",
    handling="Treat the timeout as an abort; do not retry with changed parameters.",
)
CONNECTION_MODE: Final = FailureMode(
    code=FailureCode.CONNECTION_ERROR,
    condition="The TCP connection cannot be established or breaks during the exchange.",
    handling="Verify reachability and the socket port before the next query.",
)
NO_RESPONSE_MODE: Final = FailureMode(
    code=FailureCode.NO_RESPONSE,
    condition="A documented query returns zero bytes.",
    handling="Check the printer state and the connection before querying again.",
)
UNSUPPORTED_MODE: Final = FailureMode(
    code=FailureCode.UNSUPPORTED,
    condition="A maintenance query returns zero bytes.",
    handling="Treat the query as unsupported by this firmware; do not retry it and do not assume support.",
)
UNEXPECTED_RESPONSE_MODE: Final = FailureMode(
    code=FailureCode.UNEXPECTED_RESPONSE,
    condition="The response does not match the documented layout.",
    handling="Keep the raw bytes for analysis; do not retry with changed parameters.",
)

DOCUMENTED_FAILURES: Final = (TIMEOUT_MODE, CONNECTION_MODE, NO_RESPONSE_MODE, UNEXPECTED_RESPONSE_MODE)
DIAGNOSTIC_FAILURES: Final = (TIMEOUT_MODE, CONNECTION_MODE, UNSUPPORTED_MODE)

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


def _documented_spec(
    *,
    name: str,
    kind: QueryKind,
    command: str,
    summary: str,
    response: ResponseSpec,
) -> QuerySpec:
    return QuerySpec(
        name=name,
        kind=kind,
        transport=WireTransport.TPCL,
        command=command,
        summary=summary,
        documented=True,
        response=response,
        failures=DOCUMENTED_FAILURES,
    )


def _diagnostic_spec(
    *,
    name: str,
    command: str,
    summary: str,
    parameter: ParameterSpec | None = None,
) -> QuerySpec:
    return QuerySpec(
        name=name,
        kind=QueryKind.DIAGNOSTIC,
        transport=WireTransport.EXTENDED,
        command=command,
        summary=summary,
        documented=False,
        response=ResponseSpec(
            length_bytes=None,
            layout=(
                "Free-form ASCII text of unspecified length; the documented "
                "B-FV4 LAN interface does not define this response."
            ),
            fields=(
                ResponseField(name="response-text", description="Maintenance report text as returned by the printer"),
            ),
            example_hex=None,
            text_format=True,
        ),
        failures=DIAGNOSTIC_FAILURES,
        parameter=parameter,
        response_limit=DIAGNOSTIC_RESPONSE_LIMIT,
    )


SPECS: Final[tuple[QuerySpec, ...]] = (
    _documented_spec(
        name="status",
        kind=QueryKind.STATUS,
        command="WS",
        summary="Current printer status block with the remaining count.",
        response=ResponseSpec(
            length_bytes=13,
            layout=(
                "SOH STX | status detail (2 digits) | status type (1 digit) | "
                "remaining count (4 digits) | ETX EOT CR LF"
            ),
            begins_with_hex="01 02",
            ends_with_hex="03 04 0d 0a",
            fields=(
                ResponseField(
                    name="status-detail", description="Two-digit code from the documented status table", width=2
                ),
                ResponseField(name="status-type", description="One-digit query type", width=1),
                ResponseField(name="remaining-count", description="Remaining quantity as decimal digits", width=4),
            ),
            example_hex=(b"\x01\x02" + b"001" + b"0000" + b"\x03\x04\r\n").hex(" "),
        ),
    ),
    _documented_spec(
        name="buffer",
        kind=QueryKind.BUFFER,
        command="WB",
        summary="Printer status plus receive-buffer capacity figures.",
        response=ResponseSpec(
            length_bytes=23,
            layout=(
                "SOH STX | status detail (2) | status type (1) | remaining count (4) | "
                "response length (2, always 23) | buffer free KB (5) | buffer capacity KB (5) | CR LF"
            ),
            begins_with_hex="01 02",
            ends_with_hex="0d 0a",
            fields=(
                ResponseField(
                    name="status-detail", description="Two-digit code from the documented status table", width=2
                ),
                ResponseField(name="status-type", description="One-digit query type", width=1),
                ResponseField(name="remaining-count", description="Remaining quantity as decimal digits", width=4),
                ResponseField(name="response-length", description="Decimal response length, always 23", width=2),
                ResponseField(name="buffer-free-kb", description="Free receive-buffer space in kilobytes", width=5),
                ResponseField(name="buffer-capacity-kb", description="Receive-buffer capacity in kilobytes", width=5),
            ),
            example_hex=(b"\x01\x02" + b"001" + b"0000" + b"23" + b"01024" + b"02048" + b"\r\n").hex(" "),
        ),
    ),
    _documented_spec(
        name="version",
        kind=QueryKind.FIRMWARE,
        command="WV",
        summary="Firmware creation date, model and version.",
        response=ResponseSpec(
            length_bytes=27,
            layout="SOH STX | firmware creation date (9) | firmware model (7) | firmware version (5) | ETX EOT CR LF",
            begins_with_hex="01 02",
            ends_with_hex="03 04 0d 0a",
            fields=(
                ResponseField(
                    name="firmware-creation-date", description="Firmware creation date, space padded", width=9
                ),
                ResponseField(name="firmware-model", description="Printer model, space padded", width=7),
                ResponseField(name="firmware-version", description="Firmware version, space padded", width=5),
            ),
            example_hex=(b"\x01\x02" + b"23/08/18 " + b"B-FV4D " + b"V1.0 " + b"\x03\x04\r\n").hex(" "),
        ),
    ),
    _documented_spec(
        name="identity",
        kind=QueryKind.IDENTITY,
        command="IR",
        summary="Stored model name and serial number.",
        response=ResponseSpec(
            length_bytes=31,
            layout="model name (20, space padded) | serial number (11, space padded)",
            fields=(
                ResponseField(name="model-name", description="Model name, space padded", width=20),
                ResponseField(name="serial-number", description="Serial number, space padded", width=11),
            ),
            example_hex=(b"B-FV4D".ljust(20) + b"SAMPLE00001").hex(" "),
        ),
    ),
    _diagnostic_spec(name="system-version", command="sv", summary="Maintenance system version report."),
    _diagnostic_spec(name="config", command="config 0", summary="Maintenance configuration report."),
    _diagnostic_spec(name="media-info", command="showmi", summary="Maintenance media information report."),
    _diagnostic_spec(
        name="tph-info",
        command="tphinfo {value}",
        summary="Maintenance thermal print-head report.",
        parameter=ParameterSpec(
            name="value",
            choices=("1", "2"),
            description="Report variant requested from the printer.",
        ),
    ),
    _diagnostic_spec(name="form-list", command="objinquiry 0 0 1", summary="Inventory of stored forms."),
    _diagnostic_spec(name="font-list", command="objinquiry 0 0 3", summary="Inventory of stored fonts."),
    _diagnostic_spec(name="graphic-list", command="objinquiry 0 0 2", summary="Inventory of stored graphics."),
    _diagnostic_spec(name="info", command="info", summary="General maintenance information report."),
    _diagnostic_spec(name="task-status", command="taskstatus", summary="Current maintenance task status."),
    _diagnostic_spec(name="burn-status", command="burnstatus", summary="Maintenance burn status report."),
    _diagnostic_spec(
        name="last-state",
        command="laststate 3",
        summary="Last recorded printer state at detail level 3.",
    ),
)


@dataclass(frozen=True)
class QueryRegistry:
    """Lookup and self-description over the supported read-only queries."""

    specs: tuple[QuerySpec, ...]

    def get(self, name: str) -> QuerySpec:
        for spec in self.specs:
            if spec.name == name:
                return spec
        known = ", ".join(spec.name for spec in self.specs)
        raise ValueError(f"unknown query: {name!r}; known queries: {known}")

    def names(self, *, documented: bool | None = None) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs if documented is None or spec.documented is documented)

    def snapshot(self) -> tuple[QuerySpec, ...]:
        """The documented queries in canonical snapshot order."""

        return tuple(spec for spec in self.specs if spec.documented)

    def describe(self) -> list[dict[str, object]]:
        """Machine-readable dump of every registry entry."""

        return [spec.model_dump(mode="json") for spec in self.specs]


REGISTRY: Final = QueryRegistry(specs=SPECS)


def _display_payload(payload: bytes) -> str:
    """Render control bytes visibly while leaving printable ASCII untouched."""

    return (
        payload.decode("ascii", errors="backslashreplace")
        .replace("\x1b", r"\x1b")
        .replace("\r", r"\r")
        .replace("\n", r"\n")
        .replace("\x00", r"\x00")
    )


def _frame(transport: WireTransport, command: str) -> bytes:
    try:
        text = command.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("query command must be ASCII") from exc
    if transport is WireTransport.TPCL:
        return ESC + text + LF_NUL
    return ESC + ESC + text + CR_LF


def build_query(name: str, value: str | None = None) -> WireQuery:
    """Build the exact wire bytes for one registered read-only query."""

    spec = REGISTRY.get(name)
    command = spec.command
    if spec.parameter is None:
        if value is not None:
            raise ValueError(f"query {name!r} does not take a value")
    else:
        choices = ", ".join(spec.parameter.choices)
        if value is None:
            raise ValueError(f"query {name!r} requires a value: {choices}")
        if value not in spec.parameter.choices:
            raise ValueError(f"invalid value for {name!r}: {value!r}; expected one of: {choices}")
        command = command.format(value=value)
    payload = _frame(spec.transport, command)
    effect = (
        "Read-only query from the documented B-FV4 LAN interface; no printer setting is changed."
        if spec.documented
        else (
            "Read-only maintenance query; no printer setting is changed. An empty answer means the "
            "query is not supported by this firmware and must not be retried."
        )
    )
    return WireQuery(
        name=spec.name,
        kind=spec.kind,
        transport=spec.transport,
        documented=spec.documented,
        command=command,
        summary=spec.summary,
        effect=effect,
        payload_hex=payload.hex(" "),
        payload_ascii=_display_payload(payload),
        response=spec.response,
        failures=spec.failures,
        response_limit=spec.response_limit,
    )


def snapshot_queries() -> tuple[WireQuery, ...]:
    """Build the four documented queries in canonical snapshot order."""

    return tuple(build_query(spec.name) for spec in REGISTRY.snapshot())


def parse_status_frame(response: bytes) -> dict[str, str | int]:
    """Parse the 13-byte WS response into named fields."""

    if len(response) != 13 or response[:2] != SOH_STX or response[-4:] != ETX_EOT_CRLF:
        raise ValueError(f"unexpected WS response ({len(response)} bytes)")
    body = response[2:-4]
    detail = body[:2].decode("ascii")
    return {
        "status_detail": detail,
        "status_name": STATUS_DETAILS.get(detail, "unknown"),
        "status_type": body[2:3].decode("ascii"),
        "remaining_count": int(body[3:]),
    }


def parse_buffer_frame(response: bytes) -> dict[str, str | int]:
    """Parse the 23-byte WB response into named fields."""

    if len(response) != 23 or response[:2] != SOH_STX or response[-2:] != CR_LF:
        raise ValueError(f"unexpected WB response ({len(response)} bytes)")
    body = response[2:-2]
    detail = body[:2].decode("ascii")
    declared = int(body[7:9])
    if declared != 23:
        raise ValueError(f"WB response declares length {declared}, expected 23")
    return {
        "status_detail": detail,
        "status_name": STATUS_DETAILS.get(detail, "unknown"),
        "status_type": body[2:3].decode("ascii"),
        "remaining_count": int(body[3:7]),
        "response_length": declared,
        "buffer_free_kb": int(body[9:14]),
        "buffer_capacity_kb": int(body[14:19]),
    }


def parse_version_frame(response: bytes) -> dict[str, str | int]:
    """Parse the 27-byte WV response into named fields."""

    if len(response) != 27 or response[:2] != SOH_STX or response[-4:] != ETX_EOT_CRLF:
        raise ValueError(f"unexpected WV response ({len(response)} bytes)")
    body = response[2:-4]
    return {
        "firmware_creation_date": body[:9].decode("ascii").rstrip(),
        "firmware_model": body[9:16].decode("ascii").rstrip(),
        "firmware_version": body[16:21].decode("ascii").rstrip(),
    }


def parse_identity_frame(response: bytes) -> dict[str, str | int]:
    """Parse the 31-byte IR response into named fields."""

    if len(response) != 31:
        raise ValueError(f"unexpected IR response ({len(response)} bytes)")
    return {
        "model_name": response[:20].decode("ascii").rstrip(),
        "serial_number": response[20:].decode("ascii").rstrip(),
    }


PARSERS: Final[dict[str, Callable[[bytes], dict[str, str | int]]]] = {
    "status": parse_status_frame,
    "buffer": parse_buffer_frame,
    "version": parse_version_frame,
    "identity": parse_identity_frame,
}


def execute_query(query: WireQuery | str, exchange: Exchange) -> QueryOutcome:
    """Run one read-only query through an injected exchange callable.

    The callable owns the connection and its timeouts.  Every failure path is
    reported as a :class:`QueryFailure` instead of an exception so callers
    can decide how to proceed without parsing error strings.
    """

    wire = query if isinstance(query, WireQuery) else build_query(query)
    handling = {mode.code: mode.handling for mode in wire.failures}

    def failure(stage: FailureStage, code: FailureCode, message: str, response_hex: str = "") -> QueryOutcome:
        return QueryOutcome(
            name=wire.name,
            kind=wire.kind,
            documented=wire.documented,
            ok=False,
            request_hex=wire.payload_hex,
            response_hex=response_hex,
            error=QueryFailure(stage=stage, code=code, message=message, handling=handling.get(code, "")),
        )

    try:
        response = exchange(wire.payload)
    except TimeoutError as error:
        detail = f": {error}" if str(error) else ""
        return failure(FailureStage.TRANSPORT, FailureCode.TIMEOUT, f"receive timed out{detail}")
    except OSError as error:
        return failure(FailureStage.TRANSPORT, FailureCode.CONNECTION_ERROR, str(error) or "connection failed")

    if not response:
        code = FailureCode.NO_RESPONSE if wire.documented else FailureCode.UNSUPPORTED
        return failure(FailureStage.RESPONSE, code, "printer returned zero bytes", response.hex(" "))

    if wire.kind is QueryKind.DIAGNOSTIC:
        return QueryOutcome(
            name=wire.name,
            kind=wire.kind,
            documented=wire.documented,
            ok=True,
            request_hex=wire.payload_hex,
            response_hex=response.hex(" "),
            response_text=response.decode("ascii", errors="replace"),
            fields={"response_length": len(response)},
        )

    parser = PARSERS.get(wire.name)
    if parser is None:
        return failure(FailureStage.PARSE, FailureCode.UNEXPECTED_RESPONSE, f"no parser registered for {wire.name!r}")
    try:
        fields = parser(response)
    except ValueError as error:
        return failure(FailureStage.PARSE, FailureCode.UNEXPECTED_RESPONSE, str(error), response.hex(" "))
    return QueryOutcome(
        name=wire.name,
        kind=wire.kind,
        documented=wire.documented,
        ok=True,
        request_hex=wire.payload_hex,
        response_hex=response.hex(" "),
        fields=fields,
    )


__all__ = [
    "DEFAULT_RESPONSE_LIMIT",
    "DOCUMENTED_FAILURES",
    "DIAGNOSTIC_RESPONSE_LIMIT",
    "DIAGNOSTIC_FAILURES",
    "Exchange",
    "FailureCode",
    "FailureMode",
    "FailureStage",
    "ParameterSpec",
    "PARSERS",
    "QueryFailure",
    "QueryKind",
    "QueryOutcome",
    "QueryRegistry",
    "QuerySpec",
    "REGISTRY",
    "ResponseField",
    "ResponseSpec",
    "SPECS",
    "STATUS_DETAILS",
    "WireQuery",
    "WireTransport",
    "build_query",
    "execute_query",
    "parse_buffer_frame",
    "parse_identity_frame",
    "parse_status_frame",
    "parse_version_frame",
    "snapshot_queries",
]
