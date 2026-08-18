# TPCL Write Paths — Technical Note

Quick reference for the tool's write paths (TPCL base parameters, fine
adjustment, TPCL-General, emulation, and reset), including verified byte
formats and open questions. Offline coverage: `tests/test_tpcl_review.py`.

## Byte formats

All TPCL commands use the `ESC + ASCII command + LF + NUL` (`frame`) envelope.

| Path | Bytes |
|---|---|
| Base parameters | `ESC Z2; 1, <24 characters> LF NUL` — 16 single-character fields, followed by a two-digit hexadecimal euro code, followed by 6 single-character fields; fields ignored by the B-FV4 are pinned to `0`; `requires_reset` |
| Fine adjustment | `ESC Z2; 2, <33 characters> LF NUL` — X coordinate in tenths of a millimeter with a sign and 3 digits, range 0–995 |
| Transport envelope | `ESC ESC setnvrr|setnvrs <count> CR LF <body>`; optionally with an `ESC Arg` prefix and `ESC ESC exit CR LF` trailer |
| TPCL-General | `ESC Arg` + `setnvrr <n>` envelope + `code,length,value;` items in fixed order (20, 21, 22, 23, 24, 25, 26, 190, 27, 28, 29, 30, 32, 2000, 2002, 2003, 3000) + `ESC ESC reboot 1 CR LF` + `ESC ESC exit CR LF`; `length` is the ASCII byte length of the value |
| Emulation (named) | `setnvrs 2` + `31,2,<value>;33,1,0;` with D=65, E=66, I=73, Z=90, TPCL=69 |
| Emulation (AUTO) | currently `setnvrs 1` + `33,1,1;` (AUTO) or `33,1,2;` (AUTO2) — see open question 1 |
| Reset | `ESC Z0 LF NUL` or `ESC WR LF NUL`; `ESC ESC reboot 0|1|3 CR LF`, `facreset 0`, `resetcommand 0`, `selftest 0` |

## Safety logic

All builders produce `CommandPreview` objects only. Transmission happens only
inside `apply_previews`, and only when `--apply` **and** `--yes` are both set.
`--apply` without `--yes` aborts before any socket write (`writes require both
--apply and --yes`); without either switch, the CLI prints only the exact
hex/ASCII preview. Tests verify both paths offline against a network-free stub.

## Open questions

1. **Emulation AUTO/AUTO2:** The process documentation also lists the selector
   values `AUTO=48` and `AUTO2=85`. The builder currently sends only the
   code-33 item (`33,1,1;`/`33,1,2;`) for AUTO/AUTO2, not a code-31 item. Verify
   on the individual test printer whether an additional
   `31,2,48;`/`31,2,85;` is required before applying the change. Until then,
   the tests pin the current behavior.
2. **`single media-calibration <value>`:** The value is copied without
   validation into `ESC ESC sc <value> CR LF` (unlike `ribbon-calibration`,
   which requires digits). Control characters would shape the preview bytes;
   read the preview carefully before every apply.
3. **TPCL-General values:** ASCII is enforced and NUL/CR/LF are rejected, but
   `;` and `,` are allowed in values and make the `code,length,value;` framing
   ambiguous for delimiter-based parsers. Until clarified on a test printer,
   use simple decimal values only.
4. **`dangerous` flag:** Informational only. The two-key gate (`--apply` +
   `--yes`) applies regardless; for example, `Z2;1`/`Z2;2` are not marked as
   `dangerous` but are still transmitted only with both switches.
