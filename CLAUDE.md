# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

`tapsend` is intended to implement a sender for the **Telocator Alphanumeric Protocol
(TAP)** — the 1980s/90s dial-up protocol for injecting alphanumeric pages into a
paging terminal. The role being built is the **remote entry device** (the caller), not
the paging terminal (the answerer).

At present the repository contains only `TAP_V1P8.md`, a faithful Markdown
transcription of the TAP v1.8 specification (Feb 1997). There is no build system,
tests, or git repository yet. When adding code, establish those and document the
build/test/run commands here.

**`TAP_V1P8.md` is the authoritative reference for all protocol behavior.** It is long;
the summary below exists so you can implement or review without re-reading the whole
file, but consult it for anything subtle (continuation across blocks, response-code
text parsing, the anomaly list in §4.0).

## Protocol facts an implementation must get right

These are the details most likely to cause silent interop bugs. Section numbers refer
to `TAP_V1P8.md`.

- **Serial framing (§2.0):** 7 data bits, even parity, 1 start, 1 stop; XON/XOFF both
  directions; no echo in full duplex. Default 300 baud. Because it is 7-bit even
  parity, checksums are computed on **7-bit values** (mask each byte with `& 0x7F`).
  Some systems use 8 data bits / no parity (§4.0) — make parity configurable.

- **Checksum (§5.0):** arithmetic sum of the 7-bit values of **every character in the
  block including `STX` and the `ETX`/`ETB`/`US` terminator**, take the low **12 bits**,
  then emit **3 ASCII chars** each carrying 4 bits, high nibble first, each encoded as
  `0x30 + nibble` (range `0x30`–`0x3F`, i.e. `0123456789:;<=>?`). Worked example in the
  file: `<STX>123<CR>ABC<CR><ETX>` → `17;`.

- **Block structure (§8):** a block is ≤256 chars (≤250 info + 3 control + 3 checksum),
  starts with `<STX>`, ends with `<terminator><CHKSUM><CR>`. Each field ends with
  `<CR>`, **even an empty field**. Paging transactions are typically 2 fields: Field 1 =
  Pager ID, Field 2 = message.

- **Block terminators select continuation semantics (§8):**
  - `<ETX>` — the transaction ends in this block.
  - `<ETB>` — transaction continues; the last field in this block is complete.
  - `<US>`  — the last field itself continues into the next block; **no `<CR>`** is
    emitted after the partial field, and the field's terminating `<CR>` appears in
    whatever block the field finally ends.

- **Transparency insertion (§8):** to send a control byte that would collide with the
  protocol, emit `SUB (0x1A)` followed by `(byte + 0x40)`. The bytes that **must** be
  escaped when they appear in payload: `CR LF ESC STX ETX US ETB EOT SUB`.

- **Call flow (§3.0, sample in Appendix C):** send `<CR>` until `ID=`; log on with
  `<ESC>PG1<CR>` (automatic mode; `PG`=paging service, `1`=terminal type); read logon
  message sequence ending in `<ACK>`; wait for go-ahead `<ESC>[p<CR>` (`p` is
  lowercase); send transaction blocks; disconnect with `<EOT><CR>`.

- **Per-block responses (§8):** each block is answered with a message sequence followed
  by one of `<ACK>` (next block), `<NAK>` (resend), `<RS>` (abandon this transaction —
  typically an invalid pager ID), or `<ESC><EOT>` (terminal disconnects). Since rev 1.6
  a message sequence with a numeric response code is required, but older terminals omit
  it or send bare `<CR><code><CR>` — parse defensively.

- **Response codes (Appendix A):** 3-digit code + space + human text. First digit is the
  class: `1xx` informational, `2xx` success (e.g. `211` page sent), `5xx` failure. Codes
  **110, 214, 517** carry machine-readable text (version `M.m`, or a decimal char-limit)
  — the rest is display text. In a multi-line sequence, only the first line carries the
  code, and a continuation line that happens to start with a number is indented one
  space to disambiguate.

- **Timers/retries (§7.0), all configurable:** t1=2s (CR interval), t2=1s, t3=10s
  (terminal responses), t4=4s (device sends next transaction), t5=8s; n1=n2=n3=3.

- **Line endings (§4.0):** treat `<CR>`, `<LF>`, `<CR><LF>`, `<LF><CR>` all as
  end-of-line when reading from the terminal; ignore an `<LF>` adjacent to a `<CR>`.

## Running against real hardware (`tapsend.py`)

`tapsend.py` is the remote-entry-device implementation. Send one page with:

```bash
./tapsend.py --port /dev/cu.usbserial-AB0NW409 --baud 2400 \
    --dial <access#> --pager <sms#> --message "text"
```

Exit codes: `0` delivered (terminal `<ACK>`), `2` rejected by the terminal (e.g. a
`511 Invalid Pager ID` with `<RS>`), `1` protocol/line/program failure.

Findings from bringing this up on a **USR Courier** modem (verified end-to-end against
a live TAP access number, which returns `110 1.8` and rejects pager `5551212` with
`511 Invalid Pager ID`):

- **Use 2400 baud, not 1200** — this Courier connects at 1200 then immediately drops
  (`1200/ARQ` → `NO CARRIER`). 2400 is stable.
- **The link must be error-corrected.** Without ARQ the dial-up path floods the TAP
  data phase with `?~?~` line noise and the byte stream is unusable. The default
  `--modem-init ATE0V1M1&M5` forces an ARQ link (`&M5`). Fall back to `&M4` only if a
  terminal has no error control.
- **Modem command hygiene matters** (see `modem_command`): reset the input buffer
  before each AT command and drain the full result line after, or leftover bytes /
  command echo desync the next command. Match whole result words (`OK`, `CONNECT`),
  never a bare digit like `0` — it false-matches digits inside the echoed command or
  the dial string.
- After `CONNECT`, do **not** flush the input buffer before step 3 — the terminal may
  have already sent `ID=`.

## Working with the spec file

`TAP_V1P8.md` uses literal control-code notation like `<STX>`, `<CR>`, `<ESC>[p` in
prose and code fences — these are notation, **not** literal angle-bracket text on the
wire. When quoting the spec in tests or comments, cite section numbers so the reference
stays checkable against the file.
