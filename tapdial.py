#!/usr/bin/env python3
"""
tapdial - send an alphanumeric page/SMS via the Telocator Alphanumeric Protocol (TAP).

Dials a paging terminal through a Hayes/AT modem and delivers one message to one
destination using the automatic-mode TAP handshake (remote-entry-device side).

Protocol behavior follows TAP_V1P8.md in this repository; section numbers in the
comments below refer to that file. That transcription is the authority; if this
program and the spec disagree, the spec is right.

Example:
    ./tapdial.py --port /dev/cu.usbserial-AB0NW409 --baud 1200 \\
        --dial 18005551234 --pager 5551234567 --message "hello from TAP"
"""

import argparse
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    sys.exit("This program requires pyserial.  Install it with:  pip3 install pyserial")


# --- ASCII control codes named by TAP (Appendix B) ---------------------------
STX = 0x02  # begins every block
ETX = 0x03  # block terminator: transaction ends in this block
EOT = 0x04  # disconnect
ACK = 0x06  # block / logon accepted
LF = 0x0A   # not part of TAP; tolerated around CR (sec 4.0)
CR = 0x0D   # ends every field and every protocol line
NAK = 0x15  # checksum / transmission error: resend last block
ETB = 0x17  # block terminator: transaction continues, last field complete
SUB = 0x1A  # transparency introducer
ESC = 0x1B  # automatic-mode logon, go-ahead <ESC>[p, disconnect
RS = 0x1E   # abandon this transaction, go to next
US = 0x1F   # block terminator: last field continues into next block

# Control bytes that MUST be made transparent if they appear in payload (sec 8).
MUST_ESCAPE = {CR, LF, ESC, STX, ETX, US, ETB, EOT, SUB}

# Symbolic names for readable session logging.
CTRL_NAMES = {
    0x02: "STX", 0x03: "ETX", 0x04: "EOT", 0x06: "ACK", 0x0A: "LF",
    0x0D: "CR", 0x15: "NAK", 0x16: "SYN", 0x17: "ETB", 0x1A: "SUB",
    0x1B: "ESC", 0x1E: "RS", 0x1F: "US", 0x7F: "DEL",
}

# Default timing / retry parameters (sec 7.0).
DEFAULT_TIMERS = {
    "t1": 2.0,   # interval between <CR>s awaiting ID= (step 3)
    "t2": 1.0,
    "t3": 10.0,  # terminal's response to logon and each block (steps 6,7,8)
    "t4": 4.0,
    "t5": 8.0,
    "n1": 3,     # <CR> transmissions awaiting ID= (step 3)
    "n2": 3,     # transaction resends before failure (step 8)
    "n3": 3,
}

PARITY_MAP = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
}


# --- session logging ---------------------------------------------------------
def render(data):
    """Render raw bytes with control codes shown symbolically, e.g. <STX>123<CR>."""
    out = []
    for b in data:
        b &= 0x7F  # TAP is a 7-bit protocol; ignore any parity bit
        if b in CTRL_NAMES:
            out.append("<%s>" % CTRL_NAMES[b])
        elif 0x20 <= b < 0x7F:
            out.append(chr(b))
        else:
            out.append("<%02X>" % b)
    return "".join(out)


def log(direction, data):
    """Print one side of the conversation. direction is 'TX', 'RX', or '..'."""
    if isinstance(data, str):
        text = data
    else:
        text = render(data)
    print("%s | %s" % (direction, text), flush=True)


# --- checksum (sec 5.0) ------------------------------------------------------
def tap_checksum(block_bytes):
    """
    Sum the 7-bit values of every byte in the block (STX through the terminator
    inclusive), keep the low 12 bits, and encode as three ASCII chars, high
    nibble first, each as 0x30 + nibble (range '0'..'?').
    """
    total = sum(b & 0x7F for b in block_bytes) & 0xFFF
    n1 = (total >> 8) & 0xF
    n2 = (total >> 4) & 0xF
    n3 = total & 0xF
    return bytes((0x30 + n1, 0x30 + n2, 0x30 + n3))


def escape_payload(text):
    """Apply transparency insertion (sec 8): SUB + (byte+0x40) for control bytes."""
    out = bytearray()
    for ch in text.encode("ascii", "replace"):
        if ch in MUST_ESCAPE:
            out.append(SUB)
            out.append((ch + 0x40) & 0x7F)
        else:
            out.append(ch & 0x7F)
    return bytes(out)


def build_block(pager_id, message):
    """
    Build a single-block, single-transaction TAP packet (sec 8):
        <STX> Field1 <CR> Field2 <CR> <ETX> <CHKSUM> <CR>
    Field 1 = pager ID, Field 2 = message.  Every field ends with <CR>, even if
    empty.  The checksum covers STX through ETX inclusive.
    """
    body = bytearray()
    body.append(STX)
    body += pager_id.encode("ascii", "replace")  # digits; no escaping needed
    body.append(CR)
    body += escape_payload(message)
    body.append(CR)
    body.append(ETX)
    chk = tap_checksum(body)
    body += chk
    body.append(CR)
    return bytes(body)


# --- serial / modem I/O ------------------------------------------------------
def read_until(ser, predicate, timeout, echo=True):
    """
    Read bytes until predicate(buffer_bytes) is true or timeout elapses.
    Returns the accumulated bytes.  Logged as it arrives.
    """
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        n = ser.in_waiting
        chunk = ser.read(n if n else 1)
        if chunk:
            buf += chunk
            if echo:
                log("RX", chunk)
            if predicate(bytes(buf)):
                return bytes(buf)
    return bytes(buf)


def send(ser, data, label="TX"):
    log(label, data)
    ser.write(data)
    ser.flush()


def modem_command(ser, cmd, expect, timeout, settle=0.4):
    """Send an AT command and wait for one of the expected result codes.

    Resets the input buffer first so stale bytes from a previous reply cannot
    desync parsing (a leftover result line, or the modem's echo of the command),
    then drains the rest of the result line after a match.  `expect` items must be
    whole result-code words (e.g. b"OK", b"CONNECT") -- never a bare digit like
    b"0", which would false-match a digit inside the echoed command or dial string.
    """
    ser.reset_input_buffer()
    send(ser, (cmd + "\r").encode("ascii"))
    got = read_until(ser, lambda b: any(e in b for e in expect), timeout)
    time.sleep(settle)
    n = ser.in_waiting
    if n:
        extra = ser.read(n)
        log("RX", extra)
        got += extra
    return got


def init_modem(ser, args):
    """Reset the modem to a known, TAP-friendly profile."""
    print("=== Initializing modem ===", flush=True)
    # ATZ  reset to a known profile.
    # E0   no command echo, so our AT text is not mixed into terminal output.
    # V1   verbose word result codes (OK / CONNECT / NO CARRIER ...).
    # M1   speaker on until carrier is up, so dialing is audible.
    # &M5  require an error-corrected (ARQ / V.42 / MNP) link.  Without error
    #      control this flaky dial-up path floods the TAP data phase with line
    #      noise; requiring ARQ keeps the 7-bit TAP byte stream clean.
    modem_command(ser, "ATZ", [b"OK"], 5.0)
    modem_command(ser, args.modem_init, [b"OK", b"ERROR"], 3.0)


def dial_and_handshake(ser, args, timers):
    """
    Dial, and complete TAP steps 3-4 (get the 'ID=' prompt).  The whole dial is
    retried up to --redials times because the far end intermittently fails to
    answer or connects without error control.  Returns once 'ID=' is seen.
    """
    for attempt in range(1, args.redials + 1):
        print("=== Dialing %s (attempt %d/%d) ==="
              % (args.dial, attempt, args.redials), flush=True)
        dialed = modem_command(
            ser, "ATDT" + args.dial,
            [b"CONNECT", b"BUSY", b"NO CARRIER", b"NO DIALTONE", b"ERROR"],
            args.dial_timeout, settle=0.8)
        if b"CONNECT" not in dialed:
            print("--- no CONNECT (%s); redialing ---"
                  % render(dialed).strip(), flush=True)
            time.sleep(2.0)
            continue
        # Give the far end a moment after carrier is up (step 2).
        time.sleep(1.0)

        # --- Step 3/4: send <CR> until the terminal answers with 'ID=' ---
        # Do NOT flush the input buffer: some terminals send 'ID=' the instant
        # carrier is up, so those bytes may already be waiting.
        print("=== TAP handshake ===", flush=True)
        for _ in range(timers["n1"] + 1):
            send(ser, bytes((CR,)))
            resp = read_until(ser, lambda b: b"ID=" in b, timers["t1"])
            if b"ID=" in resp:
                return  # handshake underway; caller continues at step 5A
        # Connected but no usable 'ID=' -- hang up and redial.
        print("--- connected but no 'ID=' prompt; hanging up and redialing ---",
              flush=True)
        hangup(ser)
        time.sleep(2.0)
    raise RuntimeError("Could not establish a TAP session after %d dial attempt(s)."
                       % args.redials)


# --- TAP session -------------------------------------------------------------
def run_session(ser, args, timers):
    init_modem(ser, args)
    dial_and_handshake(ser, args, timers)

    # --- Step 5A: automatic-mode logon  <ESC>PG1[password]<CR> ---
    logon = bytearray((ESC,))
    logon += b"PG1"
    if args.password:
        logon += args.password.encode("ascii", "replace")
    logon.append(CR)
    send(ser, bytes(logon))

    # --- Step 6: logon answer -- a message sequence ending in <ACK> ---
    resp = read_until(ser, ends_in_control, timers["t3"])
    verdict = final_control(resp)
    if verdict == NAK:
        raise RuntimeError("Logon rejected with <NAK> (step 6).")
    if verdict is None or verdict != ACK:
        # <ESC><EOT> or nothing -> forced disconnect / failure
        raise RuntimeError("Logon not accepted (step 6): %s" % render(resp))

    # --- Step 7: go-ahead  <ESC>[p<CR> ---
    resp = read_until(ser, lambda b: b"\x1b[p" in b or ESC in b and b"[p" in b,
                      timers["t3"])
    if b"\x1b[p" not in resp:
        raise RuntimeError("No go-ahead <ESC>[p from paging terminal (step 7).")

    # --- Step 8: send the transaction block, expect <ACK> (retry on <NAK>) ---
    block = build_block(args.pager, args.message)
    accepted = False
    rejected = None       # the terminal's message text if it rejects the page
    for attempt in range(timers["n2"] + 1):
        if attempt:
            print("=== Resending block (attempt %d) ===" % (attempt + 1), flush=True)
        send(ser, block)
        resp = read_until(ser, ends_in_control, timers["t3"])
        verdict = final_control(resp)
        if verdict == ACK:
            accepted = True
            break
        if verdict == NAK:
            continue  # checksum/transmission error: resend last block
        if verdict == RS:
            # Valid protocol outcome: the terminal accepted the block's framing but
            # is abandoning this transaction (typically an invalid pager ID).
            rejected = message_text(resp)
            break
        raise RuntimeError("Unexpected response to block (step 8): %s" % render(resp))
    if not accepted and rejected is None:
        raise RuntimeError("Block not accepted after %d attempts (all <NAK>)."
                           % (timers["n2"] + 1))

    # --- Step 9: disconnect  <EOT><CR> (sent after <ACK> or <RS>, per spec) ---
    send(ser, bytes((EOT, CR)))

    # --- Step 10: optional closing message sequence / terminal disconnect ---
    closing = read_until(ser, lambda b: (ESC in b and EOT in b), 5.0)

    if accepted:
        print("=== Page ACCEPTED by paging terminal: %s ==="
              % (message_text(resp) or "<ACK>"), flush=True)
        if message_text(closing):
            print("    closing: %s" % message_text(closing), flush=True)
    else:
        print("=== Page REJECTED by paging terminal: %s ==="
              % (rejected or "<RS>"), flush=True)
    return accepted


def ends_in_control(buf):
    """True once a completed response line ending in ACK/NAK/RS or <ESC><EOT> is seen."""
    return final_control(buf) is not None


def message_text(resp):
    """
    Extract the human-readable message-sequence text (response code + text) from a
    terminal response, dropping TAP control codes so an outcome like
    '511 Invalid Pager ID' can be printed plainly.
    """
    out = []
    for b in resp:
        b &= 0x7F
        if b in (CR, LF):
            out.append(" / ")
        elif 0x20 <= b < 0x7F:
            out.append(chr(b))
    return " ".join("".join(out).split()).strip(" /").strip()


def final_control(buf):
    """
    Return the terminating control byte of a paging-terminal response, or None if
    not yet complete.  Per sec 6/8 a response is a message sequence followed by
    one of <ACK> / <NAK> / <RS> (each optionally then <CR>), or <ESC><EOT>.
    """
    b = bytes(x & 0x7F for x in buf)
    if ESC in b:
        i = b.index(ESC)
        if EOT in b[i:]:
            return EOT  # forced disconnect
    # Scan for a standalone control terminator.
    for code in (ACK, NAK, RS):
        if bytes((code,)) in b:
            return code
    return None


def hangup(ser):
    """Drop the call (step 11): escape to command mode and hang up."""
    try:
        time.sleep(1.1)          # +++ guard time
        ser.write(b"+++")
        ser.flush()
        time.sleep(1.1)
        modem_command(ser, "ATH", [b"OK", b"NO CARRIER", b"0"], 5.0)
    except Exception:
        pass


# --- CLI ---------------------------------------------------------------------
def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Send an alphanumeric page/SMS via the TAP protocol over a modem.")
    p.add_argument("--port", required=True, help="serial device, e.g. /dev/cu.usbserial-AB0NW409")
    p.add_argument("--baud", required=True, type=int, help="serial (DTE) baud rate, e.g. 1200")
    p.add_argument("--dial", required=True, help="TAP access phone number to dial")
    p.add_argument("--pager", required=True, help="destination pager / SMS number (TAP Field 1)")
    p.add_argument("--message", required=True, help="message text (TAP Field 2)")
    p.add_argument("--password", default="", help="optional logon password (sec 5A); omit for none")
    p.add_argument("--framing", default="7E1", choices=["7E1", "8N1", "7O1", "8E1"],
                   help="serial framing (default 7E1 per TAP sec 2.0)")
    p.add_argument("--dial-timeout", type=float, default=60.0,
                   help="seconds to wait for CONNECT (default 60)")
    p.add_argument("--redials", type=int, default=3,
                   help="dial attempts before giving up (default 3)")
    p.add_argument("--modem-init", default="ATE0V1M1&M5",
                   help="modem setup string sent after ATZ (default 'ATE0V1M1&M5': "
                        "echo off, verbose, speaker on, require ARQ error control). "
                        "Use e.g. 'ATE0V1M1&M4' if the terminal has no error control.")
    return p.parse_args(argv)


def framing_params(framing):
    bits = serial.SEVENBITS if framing[0] == "7" else serial.EIGHTBITS
    parity = PARITY_MAP[framing[1]]
    return bits, parity


def main(argv):
    args = parse_args(argv)
    bytesize, parity = framing_params(args.framing)
    timers = dict(DEFAULT_TIMERS)

    print("tapdial: %s @ %d %s  ->  dial %s, pager %s"
          % (args.port, args.baud, args.framing, args.dial, args.pager), flush=True)

    ser = serial.Serial(
        port=args.port,
        baudrate=args.baud,
        bytesize=bytesize,
        parity=parity,
        stopbits=serial.STOPBITS_ONE,
        xonxoff=True,          # XON/XOFF both directions (sec 2.0)
        rtscts=False,
        timeout=0.2,
    )
    try:
        accepted = run_session(ser, args, timers)
        rc = 0 if accepted else 2   # 0 delivered, 2 rejected by terminal
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr, flush=True)
        rc = 1                       # 1 protocol/line/program failure
    finally:
        hangup(ser)
        ser.close()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
