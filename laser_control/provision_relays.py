"""
provision_relays.py
====================
One-shot / pre-flight provisioning for the robotic-arm rig's ESP32
Generic GPIO Output Controller (same firmware family as 3DAI's
lib_3dai/provision_relays.py — ported over and adapted for this rig).

WHY THIS EXISTS
----------------
The ESP32 firmware (ESP32_Laser_Control) hard-codes no pins: every relay
channel is assigned at runtime with a serial ``CONFIG`` command, and the
PWM laser output is assigned separately with ``LASER CONFIG``. The board
only rejects a *conflicting* pin AFTER you try to send it:

    ERR PIN_IN_USE     -> CONFIG on a pin another relay channel already owns
    ERR PIN_IS_RELAY    -> LASER CONFIG on a pin a relay channel already owns
    ERR PIN_IS_LASER    -> CONFIG on the pin the PWM laser already owns

The GUI's "Laser Channels" section lets you type any pin into any of the
4 channel rows and only tells you it collided *after* you click Configure
and the board says no — and it never shows you what's already occupying a
pin from a previous session. That's exactly the failure mode described:
one channel rejected as "it is a relay" (LASER CONFIG hit a pin a relay
channel already has) and the rest rejected as "in use" (two or more relay
rows pointing at the same GPIO).

This script mirrors 3DAI's approach: define the ENTIRE pin plan for the
rig (all 4 relay channels + the PWM laser pin, if used) in one place,
validate it for internal conflicts *before* touching the board, and only
then push it. It also gives you a `--status` mode to read back what the
board currently thinks is configured, and a `--factory-reset` mode to
wipe stale channel config left over from a previous session (the usual
reason "in use" shows up even though nothing you're doing looks wrong).

SAFETY
------
- Importing this module does nothing to hardware. No port is opened and
  no command is sent at import time.
- Default action is a DRY RUN: it only prints what it would send. Pass
  --commit to actually open the port and configure the board.
- Every relay channel is provisioned with SAFE OFF — this script refuses
  to provision a channel as SAFE ON, since a laser-driving relay must
  never come up energized on boot.
- This script never sends SET — it only writes channel config (and,
  optionally, the LASER CONFIG pin/freq/maxduty). Turning anything ON is
  a separate, deliberate act done elsewhere (the GUI's ON/OFF buttons).

Usage
-----
    # Show the full plan and flag any conflicts (no hardware touched):
    python -m laser_control.provision_relays

    # Read back what the board currently has configured, incl. the pin
    # the current plan would collide with (use this FIRST when you get
    # "in use" / "is a relay" and don't know why):
    python -m laser_control.provision_relays --status --port COM3

    # Wipe all existing channel config on the board (does NOT touch the
    # PWM laser config — that's never persisted by the firmware anyway):
    python -m laser_control.provision_relays --factory-reset --port COM3

    # Actually push the validated plan:
    python -m laser_control.provision_relays --commit --port COM3
    python -m laser_control.provision_relays --commit   # auto-detect port
"""

from dataclasses import dataclass
from typing import Optional


# ── ESP32 pin-safety constants (same silicon rules as the firmware) ────────
# GPIO6-11 are bonded to the on-board SPI flash; driving them hangs the boot.
# GPIO34-39 are input-only silicon and cannot be outputs (firmware enforces
# this too, but we want to fail loud, locally, before ever opening the port).
_SPI_FLASH_PINS = range(6, 12)      # 6,7,8,9,10,11
_INPUT_ONLY_PINS = range(34, 40)    # 34..39


@dataclass(frozen=True)
class RelayChannel:
    """One provisioned relay output channel on the rig."""
    ch: int              # 1-based channel number (matches firmware, 1-4 here)
    pin: int             # ESP32 GPIO number
    label: str           # what this channel drives, for humans
    active_high: bool = True   # relay energizes on HIGH
    safe_on: bool = False      # boot/idle state; MUST stay False on this rig


# ── THIS RIG'S PIN MAP ──────────────────────────────────────────────────────
# Order matches the GUI's Laser Channels rows EXACTLY (Ch1=25, Ch2=26,
# Ch3=19, Ch4=18) so the channel NUMBER each pin is stored under agrees with
# what the app sends. A stale/mismatched channel-to-pin mapping (e.g. pin 25
# stored under a different channel number than the GUI's "Ch1" row expects)
# is what causes ERR PIN_IN_USE even when the physical pins themselves are
# correct -- this ordering removes that ambiguity going forward.
RELAY_CHANNELS: list[RelayChannel] = [
    RelayChannel(ch=1, pin=25, label="Laser 1 relay (active-HIGH)", active_high=True),
    RelayChannel(ch=2, pin=26, label="Laser 2 relay (active-HIGH)", active_high=True),
    RelayChannel(ch=3, pin=19, label="Laser 3 relay (active-HIGH)", active_high=True),
    RelayChannel(ch=4, pin=18, label="Laser 4 relay (active-HIGH)", active_high=True),
]

# The 5th laser: the bigger, PWM-dimmable laser, in addition to the 4
# relay-switched ones, wired to its own TTL/PWM pin (the "top" panel in the
# Laser tab). Confirmed wiring: GPIO21.
LASER_PWM_PIN: Optional[int] = 21
LASER_PWM_FREQ_HZ = 1000
LASER_PWM_MAXDUTY_PCT = 100


def validate_channels(channels: list[RelayChannel], laser_pwm_pin: Optional[int] = None) -> None:
    """Raise ValueError if the plan is unsafe or internally inconsistent.

    Pure, no I/O. This is what turns "board rejected it, guess why" into a
    clear message before a single byte goes out the serial port. Checks:
      - each relay pin is a legal, safe GPIO
      - no channel number or pin is duplicated across relay channels
      - the PWM laser pin (if any) doesn't collide with a relay pin
        (this is the exact cause of "rejected... because it is a relay")
    """
    seen_ch: dict[int, RelayChannel] = {}
    seen_pin: dict[int, RelayChannel] = {}
    for c in channels:
        if c.pin in _SPI_FLASH_PINS:
            raise ValueError(
                f"CH {c.ch}: GPIO{c.pin} is an SPI-flash pin (6-11); it would "
                f"hang the ESP32 on boot. Refusing to provision."
            )
        if c.pin in _INPUT_ONLY_PINS:
            raise ValueError(
                f"CH {c.ch}: GPIO{c.pin} is input-only (34-39) and cannot drive "
                f"an output."
            )
        if not (0 <= c.pin <= 39):
            raise ValueError(f"CH {c.ch}: GPIO{c.pin} out of range 0-39.")
        if c.safe_on:
            raise ValueError(
                f"CH {c.ch}: safe_on=True is not allowed on this rig; outputs "
                f"must boot de-energized (SAFE OFF)."
            )
        if c.ch in seen_ch:
            raise ValueError(f"Duplicate channel number {c.ch}.")
        if c.pin in seen_pin:
            raise ValueError(
                f"GPIO{c.pin} assigned to both CH {seen_pin[c.pin].ch} and "
                f"CH {c.ch}. This is the 'ERR PIN_IN_USE' you were hitting on "
                f"the board — fix it here before sending anything."
            )
        seen_ch[c.ch] = c
        seen_pin[c.pin] = c

    if laser_pwm_pin is not None:
        if laser_pwm_pin in seen_pin:
            owner = seen_pin[laser_pwm_pin]
            raise ValueError(
                f"LASER_PWM_PIN={laser_pwm_pin} is the same pin as CH {owner.ch} "
                f"({owner.label}). This is the 'ERR PIN_IS_RELAY' you were "
                f"hitting on the top panel — the board refuses to PWM a pin a "
                f"relay channel already owns (PWMing a relay coil can damage "
                f"it). Pick a different, unused GPIO for the PWM laser, or set "
                f"LASER_PWM_PIN = None if this rig doesn't actually have a "
                f"separate PWM laser."
            )
        if laser_pwm_pin in _SPI_FLASH_PINS or laser_pwm_pin in _INPUT_ONLY_PINS or not (0 <= laser_pwm_pin <= 39):
            raise ValueError(f"LASER_PWM_PIN={laser_pwm_pin} is not a usable output GPIO.")


def config_command(c: RelayChannel) -> str:
    """Return the exact firmware CONFIG line for a channel (no I/O)."""
    pol = "HIGH" if c.active_high else "LOW"
    safe = "ON" if c.safe_on else "OFF"
    return f"CONFIG {c.ch} PIN {c.pin} POL {pol} SAFE {safe}"


def print_plan(channels: list[RelayChannel], laser_pwm_pin: Optional[int]) -> None:
    print("Planned ESP32 relay provisioning (SAFE=OFF on every channel):\n")
    for c in channels:
        print(f"  {config_command(c):<45}  # {c.label}")
    if laser_pwm_pin is not None:
        print(
            f"\n  LASER CONFIG PIN {laser_pwm_pin} FREQ {LASER_PWM_FREQ_HZ} "
            f"MAXDUTY {LASER_PWM_MAXDUTY_PCT}"
        )
    else:
        print("\n  (No PWM laser pin configured — LASER CONFIG will not be sent.)")
    print(
        "\nDRY RUN: nothing was sent. Re-run with --commit to configure the "
        "board.\nThis only writes channel config; it does not turn any output ON."
    )


def commit(channels: list[RelayChannel], laser_pwm_pin: Optional[int], port: Optional[str]) -> int:
    """Open the serial port and send CONFIG for each channel (+ LASER CONFIG
    if set). Returns a process exit code."""
    from laser_control.relay_controller import RelayController

    if port is None:
        print("No --port given; scanning for an ESP32...")
        port = RelayController.find_esp32()
        if port is None:
            print("ERROR: no ESP32 found on any serial port.")
            return 2
        print(f"Found ESP32 on {port}.")

    rc = RelayController(port)
    if not rc.connect():
        print(f"ERROR: could not connect / no PONG on {port}.")
        if rc.last_error:
            print(f"        {rc.last_error}")
        return 2

    try:
        ok = True
        for c in channels:
            sent, raw = rc.configure_channel_verbose(
                c.ch, pin=c.pin, active_high=c.active_high, safe_on=c.safe_on
            )
            detail = "" if sent else f"  -> {' | '.join(raw)}"
            print(f"  {config_command(c):<45}  -> {'OK' if sent else 'FAILED'}{detail}")
            ok = ok and sent

        if laser_pwm_pin is not None:
            sent, raw = rc.laser_config_verbose(
                pin=laser_pwm_pin, freq_hz=LASER_PWM_FREQ_HZ, max_duty_pct=LASER_PWM_MAXDUTY_PCT
            )
            detail = "" if sent else f"  -> {' | '.join(raw)}"
            print(f"  LASER CONFIG PIN {laser_pwm_pin} ...{'':<20}  -> {'OK' if sent else 'FAILED'}{detail}")
            ok = ok and sent

        # Belt-and-suspenders: drive every relay to its safe (OFF) state now.
        rc.safe_all()
        print("\nAll relay channels driven to SAFE (OFF).")
        rc.print_status()
        return 0 if ok else 1
    finally:
        rc.disconnect()


def show_status(port: Optional[str]) -> int:
    """Read back the board's current channel table + laser status, no writes.
    Run this FIRST when the board is rejecting things and you don't know
    what it already thinks is configured (e.g. left over from a previous
    session, or from someone else's test)."""
    from laser_control.relay_controller import RelayController

    if port is None:
        port = RelayController.find_esp32()
        if port is None:
            print("ERROR: no ESP32 found on any serial port.")
            return 2
    rc = RelayController(port)
    if not rc.connect():
        print(f"ERROR: could not connect / no PONG on {port}.")
        if rc.last_error:
            print(f"        {rc.last_error}")
        return 2
    try:
        rc.print_status()
        laser_line = rc.laser_status()
        print(laser_line if laser_line else "LASER STATUS: (no response)")
        return 0
    finally:
        rc.disconnect()


def factory_reset(port: Optional[str]) -> int:
    """Wipe ALL existing relay channel config on the board. Use this when
    'in use' errors don't make sense against the plan you're looking at —
    it usually means the board still remembers channels from an earlier
    session/pin-map that this plan didn't account for."""
    from laser_control.relay_controller import RelayController

    if port is None:
        port = RelayController.find_esp32()
        if port is None:
            print("ERROR: no ESP32 found on any serial port.")
            return 2
    rc = RelayController(port)
    if not rc.connect():
        print(f"ERROR: could not connect / no PONG on {port}.")
        if rc.last_error:
            print(f"        {rc.last_error}")
        return 2
    try:
        ok = rc.factory_reset()
        print("Factory reset:", "OK" if ok else "FAILED")
        rc.print_status()
        return 0 if ok else 1
    finally:
        rc.disconnect()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true",
                         help="Actually open the serial port and send CONFIG (default: dry run).")
    parser.add_argument("--status", action="store_true",
                         help="Read back the board's current channel + laser state (no writes).")
    parser.add_argument("--factory-reset", action="store_true",
                         help="Erase ALL existing relay channel config on the board.")
    parser.add_argument("--port", default=None,
                         help="Serial port (e.g. COM5). If omitted, the ESP32 is auto-detected.")
    args = parser.parse_args(argv)

    # Validate the pin map before anything else, dry run included — this is
    # the step that catches both bugs described (duplicate relay pins, and
    # the PWM laser pin colliding with a relay pin) before touching hardware.
    validate_channels(RELAY_CHANNELS, LASER_PWM_PIN)

    if args.status:
        return show_status(args.port)
    if args.factory_reset:
        return factory_reset(args.port)
    if args.commit:
        return commit(RELAY_CHANNELS, LASER_PWM_PIN, args.port)

    print_plan(RELAY_CHANNELS, LASER_PWM_PIN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
