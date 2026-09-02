# tapsend

Send a single alphanumeric page (SMS) to a pager or cell number by dialing a paging
terminal over a modem and speaking the **Telocator Alphanumeric Protocol (TAP)**.

`tapsend.py` plays the *remote entry device* (the caller) side of TAP v1.8: it dials
the access number, performs the automatic-mode logon, delivers one message to one
destination, and prints the entire serial session as it happens. Protocol behavior
follows `TAP_V1P8.md` in this repository, which is the authoritative reference.

## Requirements

- Python 3 and [pyserial](https://pypi.org/project/pyserial/):

  ```bash
  pip3 install pyserial
  ```

- A Hayes/AT-compatible modem on a serial port, connected to a phone line that can
  reach a TAP paging terminal (an "access number").

## Usage

```bash
./tapsend.py --port <serial-device> --baud <rate> \
    --dial <access-number> --pager <destination> --message "text"
```

Example (the setup verified against a USR Courier):

```bash
./tapsend.py --port /dev/cu.usbserial-AB0NW409 --baud 2400 \
    --dial 18005551234 --pager 5551234567 --message "hello from TAP"
```

### Arguments

| Argument         | Required | Default        | Description |
|------------------|----------|----------------|-------------|
| `--port`         | yes      | —              | Serial device, e.g. `/dev/cu.usbserial-AB0NW409` |
| `--baud`         | yes      | —              | DTE baud rate (see notes below) |
| `--dial`         | yes      | —              | TAP access phone number to dial |
| `--pager`        | yes      | —              | Destination pager / SMS number (TAP Field 1) |
| `--message`      | yes      | —              | Message text (TAP Field 2) |
| `--password`     | no       | *(none)*       | Logon password, if the terminal requires one |
| `--framing`      | no       | `7E1`          | Serial framing: `7E1`, `8N1`, `7O1`, or `8E1` |
| `--dial-timeout` | no       | `60`           | Seconds to wait for `CONNECT` |
| `--redials`      | no       | `3`            | Dial attempts before giving up |
| `--modem-init`   | no       | `ATE0V1M1&M5`  | Modem setup string sent after `ATZ` |

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Delivered — terminal answered the block with `<ACK>` |
| `2`  | Rejected by the terminal (e.g. `511 Invalid Pager ID`, answered with `<RS>`) |
| `1`  | Protocol, line, or program failure |

## What it prints

Every byte in and out of the modem is logged, with control codes shown symbolically
(`<STX>`, `<CR>`, `<ACK>`, …) so the handshake is readable. A successful run ends with:

```
=== Page ACCEPTED by paging terminal: 213 Message Accepted ===
```

and a rejection ends with:

```
=== Page REJECTED by paging terminal: 511 Invalid Pager ID ===
```

## Notes from real hardware (USR Courier)

These were verified end-to-end against a live TAP access number, which returns
`110 1.8` and rejects pager `5551212` with `511 Invalid Pager ID`.

- **Prefer 2400 baud over 1200.** This Courier connects at 1200 then immediately drops
  (`1200/ARQ` → `NO CARRIER`); 2400 is stable.
- **Keep the link error-corrected.** The default `--modem-init ATE0V1M1&M5` forces an
  ARQ (V.42/MNP) connection. Without error control the dial-up path floods the TAP data
  phase with `?~?~` line noise and the byte stream is unusable. Fall back to `&M4`
  (or `&M0`) only if a terminal has no error control — expect occasional stray noise
  bytes in that case.
- `&M5` **requires** ARQ (it does not disable it); that is why a good connection reports
  `CONNECT 2400/ARQ`.

## Protocol reference

`TAP_V1P8.md` is a faithful transcription of the TAP v1.8 specification (Feb 1997).
Control-code notation in that file (`<STX>`, `<CR>`, `<ESC>[p`, …) is *notation*, not
literal angle-bracket text on the wire. See `CLAUDE.md` for a distilled summary of the
protocol details the implementation must get right.
