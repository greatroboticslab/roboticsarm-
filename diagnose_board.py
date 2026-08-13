"""
diagnose_board.py
==================
Raw serial diagnostic for the "board won't PONG" problem. This talks to the
port directly with pyserial -- no relay_controller.py, no app logic, no
DTR/RTS cleverness -- so we can see the actual bytes (or total silence)
coming off the board, which tells us whether this is a firmware/hardware
problem or an app-level one.

Usage:
    python diagnose_board.py COM3

What to look for:
  - Total silence, even during the open/reset window -> the chip isn't
    running any code at all (not booting). Points at power, a bad flash,
    or a dead/miswired board -- not a software issue on the PC side.
  - Garbled bytes during the open/reset window, then nothing -> the ROM
    bootloader banner printed (normal, wrong-baud garble) but the app
    never reached setup()'s "READY" print. Likely a reset/brownout loop,
    or the chip is stuck in the bootloader waiting for esptool instead of
    running the app.
  - Clean "READY" (or garbled-but-recognizable) followed by nothing after
    PING is sent -> app is running but not receiving/parsing your bytes.
    Points at something on the PC side (wrong port, another program
    holding it, line-ending issue).
  - Clean "READY" then clean "PONG" after PING -> the board is completely
    fine; the problem is isolated to relay_controller.py / the app.
"""

import sys
import time

import serial


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python diagnose_board.py <PORT>  e.g. COM3")
        return 1
    port = sys.argv[1]

    print(f"Opening {port} at 115200 baud (raw, no DTR/RTS manipulation)...")
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.2
    try:
        ser.open()
    except serial.SerialException as exc:
        print(f"FAILED to open port: {exc}")
        print("-> Port is wrong, or something else (PlatformIO monitor, "
              "another python process, Arduino IDE, etc.) already has it "
              "open. Close everything else and check the port name.")
        return 1

    print(f"Port open. DTR={ser.dtr} RTS={ser.rts} (whatever the OS default is)")
    print("Listening for 4 seconds for ANY boot activity...\n")

    deadline = time.time() + 4.0
    got_any_bytes = False
    buf = bytearray()
    while time.time() < deadline:
        chunk = ser.read(256)
        if chunk:
            got_any_bytes = True
            buf += chunk
        time.sleep(0.05)

    if buf:
        print(f"Received {len(buf)} bytes during boot window:")
        print("  raw :", buf)
        try:
            print("  text:", buf.decode("utf-8", errors="replace"))
        except Exception:
            pass
    else:
        print("Received NOTHING during the 4-second boot window.")
        print("-> The board never sent a single byte, not even a garbled ROM "
              "banner. Strongly suggests the chip isn't booting at all: "
              "check power (is it actually getting 5V/3.3V and not browning "
              "out), check the USB cable (some are charge-only, no data "
              "lines), and confirm this really is the board's port and not "
              "a different device.")

    print("\nSending PING...")
    ser.reset_input_buffer()
    ser.write(b"PING\r\n")
    ser.flush()

    deadline = time.time() + 2.0
    buf2 = bytearray()
    while time.time() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf2 += chunk
        time.sleep(0.05)

    if buf2:
        print(f"Received {len(buf2)} bytes after PING:")
        print("  raw :", buf2)
        try:
            print("  text:", buf2.decode("utf-8", errors="replace"))
        except Exception:
            pass
        if b"PONG" in buf2:
            print("\n*** PONG received. The board and firmware are fine at "
                  "the raw serial level. The problem is isolated to the app "
                  "/ relay_controller.py side (port sharing, timing, or a "
                  "leftover process holding the port). ***")
    else:
        print("Received NOTHING after PING.")
        if got_any_bytes:
            print("-> The board booted (we saw boot activity) but never "
                  "responded to PING. Either it's stuck in a reset loop "
                  "that restarts before it can process input (brownout -- "
                  "try USB power only, no external 5V), or something about "
                  "how the bytes are being sent isn't reaching it (unlikely "
                  "at this raw level, but try running this script again "
                  "right as you plug the board in).")
        else:
            print("-> Consistent with total silence above: the chip isn't "
                  "running code. This points at hardware (power/brownout, "
                  "bad solder joint on EN/GPIO0, dead board, or a USB cable "
                  "without data lines) rather than anything fixable in "
                  "Python.")

    ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
