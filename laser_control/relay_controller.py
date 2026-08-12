"""
relay_controller.py
====================
Python client for the ESP32 Generic GPIO Output Controller firmware.

Usage (basic):
    from relay_controller import RelayController

    with RelayController("COM3") as rc:           # or "/dev/ttyUSB0" on Linux
        rc.configure_channel(1, pin=18)           # default: active-HIGH, safe=OFF
        rc.set_channel(1, True)                   # ON
        print(rc.get_channel(1))
        rc.safe_all()

Usage (no context manager):
    rc = RelayController("/dev/ttyUSB0")
    rc.connect()
    ...
    rc.disconnect()

Dependencies:
    pip install pyserial
"""

import time
import threading
import serial
import serial.tools.list_ports
from dataclasses import dataclass
from typing import Optional


# ── Data model ─────────────────────────────────────────────────────────────

@dataclass
class ChannelStatus:
    """Represents the state of one output channel."""
    ch: int                        # 1-based channel number
    configured: bool
    pin: Optional[int] = None
    active_high: Optional[bool] = None
    safe_on: Optional[bool] = None
    state: Optional[bool] = None   # Current logical state (ON=True, OFF=False)

    def __str__(self) -> str:
        if not self.configured:
            return f"CH {self.ch}: UNCONFIGURED"
        pol  = "HIGH" if self.active_high else "LOW"
        safe = "ON"   if self.safe_on     else "OFF"
        st   = "ON"   if self.state       else "OFF"
        return (f"CH {self.ch}: PIN={self.pin} POL={pol} "
                f"SAFE={safe} STATE={st}")


# ── Controller class ───────────────────────────────────────────────────────

class RelayController:
    """
    Serial interface to the ESP32 GPIO controller firmware.

    Parameters
    ----------
    port : str
        Serial port identifier, e.g. "COM3" or "/dev/ttyUSB0".
    baud : int
        Baud rate — must match firmware (default 115200).
    timeout : float
        Seconds to wait for the first response line (default 2.0).
    inter_line_timeout : float
        Seconds to wait between lines for multi-line responses (default 0.15).
    connect_delay : float
        Seconds to wait after opening the port for the ESP32 to reset (default 1.5).
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        timeout: float = 2.0,
        inter_line_timeout: float = 0.15,
        connect_delay: float = 1.5,
    ):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.inter_line_timeout = inter_line_timeout
        self.connect_delay = connect_delay
        self._serial: Optional[serial.Serial] = None
        # main.py drives this controller from multiple threads at once —
        # a 1-second PING heartbeat (to satisfy the firmware's host-silence
        # watchdog) runs continuously in the background alongside whatever
        # thread a button click spins up (Configure/SET/STATUS/REMOVE/LASER
        # ...). Without serializing access, two threads' write()/readline()
        # calls can interleave on the same serial port — e.g. one thread's
        # reset_input_buffer() discarding bytes the board already sent in
        # response to a *different* thread's command — which shows up as a
        # spurious "board sent no response" timeout with no board-side
        # cause at all. Every command goes through _send(), so a single
        # lock there covers every public method.
        self._lock = threading.Lock()
        # Set by connect() on failure so callers (e.g. the GUI) can show the
        # *actual* reason instead of a generic "no PING response" for every
        # failure mode — see connect() for what gets stored here and why.
        self.last_error: Optional[str] = None

    # ── Connection management ──────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Open the serial port and wait for the device to be ready.

        Returns True on success, False if the port cannot be opened or the
        device does not respond to PING. On False, self.last_error holds
        the actual reason (see below) — check it if a generic "no PING
        response" message isn't enough to tell what's actually wrong.
        """
        self.last_error = None
        try:
            # Create the serial object WITHOUT opening it immediately
            self._serial = serial.Serial()
            self._serial.port = self.port
            self._serial.baudrate = self.baud
            self._serial.timeout = self.timeout

            # Now open the port safely
            self._serial.open()
            # CRITICAL FIX: Disable DTR and RTS so the ESP32 doesn't get trapped in its bootloader
            self._serial.dtr = False
            self._serial.rts = False
            
            
            
        except serial.SerialException as exc:
            # The port itself couldn't be opened — wrong/stale COM number,
            # already held open by another process (a leftover python.exe,
            # Arduino Serial Monitor, PuTTY, ...), or Windows hasn't
            # finished re-enumerating the device yet right after a replug.
            self.last_error = f"Could not open {self.port}: {exc}"
            print(f"[RelayController] {self.last_error}")
            return False

        try:
            # The ESP32 resets when the serial port is opened (DTR toggle).
            # Wait for it to boot and print READY.
            time.sleep(self.connect_delay)
            self._serial.reset_input_buffer()
            if self.ping():
                return True
            # Port opened fine and stayed open, but the board never sent
            # PONG within the timeout. Most likely causes: the board is
            # sitting in its UART bootloader instead of running the
            # sketch (needs a press of the physical EN/reset button — a
            # brief USB unplug doesn't always clear this if the board is
            # also powered from an external 5V rail through the relay
            # terminal block, since it never actually loses power), or
            # it's still finishing a slow boot.
            self.last_error = (
                "Port opened, but the board never responded to PING. If a "
                "USB unplug/replug didn't help, try pressing the physical "
                "EN/reset button on the board itself — a USB-only power "
                "cycle won't reset it if it's also getting 5V from "
                "elsewhere on the board."
            )
            return False
        except serial.SerialException as exc:
            # A genuine I/O error mid-ping (not a timeout) — e.g. the OS
            # driver hiccuping right after the device re-enumerated. This
            # used to be silently swallowed and reported as the same
            # generic "no PING response" as a real timeout, which hid
            # what was actually going on.
            self.last_error = f"I/O error while pinging {self.port}: {exc}"
            print(f"[RelayController] {self.last_error}")
            return False

    def disconnect(self) -> None:
        """Close the serial port."""
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()

    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def __enter__(self) -> "RelayController":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ── Low-level I/O ─────────────────────────────────────────────────────

    def _send(self, cmd: str) -> list[str]:
        """
        Send a command and return all response lines as a list of strings.

        Uses readline() with a generous first-line timeout and a short
        inter-line timeout, so both single-line (OK/ERR) and multi-line
        (STATUS, HELP) responses are captured correctly.
        """
        if not self.is_connected():
            raise ConnectionError("Not connected — call connect() first.")

        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write((cmd.strip() + "\n").encode())
            self._serial.flush()  # CRITICAL FIX: Force Windows to send the command immediately

            lines: list[str] = []


            # First line: use the full timeout so the device has time to respond.
            self._serial.timeout = self.timeout
            first = self._serial.readline().decode(errors="replace").strip()
            if first:
                lines.append(first)

            # Subsequent lines: short inter-line timeout.
            self._serial.timeout = self.inter_line_timeout
            while True:
                line = self._serial.readline().decode(errors="replace").strip()
                if not line:
                    break
                lines.append(line)

            return lines

    def _send_expecting_ok(self, cmd: str) -> bool:
        """Send a command and return True if the response contains 'OK'."""
        ok, _ = self._send_expecting_ok_verbose(cmd)
        return ok

    def _send_expecting_ok_verbose(self, cmd: str) -> tuple:
        """Same as _send_expecting_ok, but also returns the board's raw
        response lines so callers can surface the *actual* rejection
        reason (e.g. 'ERR PIN_IN_USE') instead of a generic guess.
        Returns (ok: bool, raw_lines: list[str])."""
        lines = self._send(cmd)
        ok = any(ln.startswith("OK") for ln in lines)
        return ok, lines

    # ── Commands ──────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Send a PING command and check for a PONG response."""
        if not self.is_connected():
            return False

        try:
            with self._lock:
                # Clear any lingering junk in the buffer
                self._serial.reset_input_buffer()
                
                # Send PING with standard newline
                self._serial.write(b"PING\n")
                self._serial.flush()

                # Read lines until timeout
                start_time = time.time()
                while time.time() - start_time < self.timeout:
                    if self._serial.in_waiting > 0:
                        line = self._serial.readline().decode("utf-8", errors="ignore").strip()
                        if line:
                            print(f"[ESP32 Says]: {line}")
                            if "PONG" in line or "READY" in line:
                                return True
                    time.sleep(0.05)
                
                print("[RelayController] Ping timed out: No response received from ESP32.")
                return False
                
        except Exception as e:
            print(f"[RelayController] Ping error: {e}")
            return False

    def configure_channel(
        self,
        ch: int,
        pin: int,
        active_high: bool = True,
        safe_on: bool = False,
    ) -> bool:
        """
        Configure (or re-configure) an output channel.

        Parameters
        ----------
        ch          : 1-based channel number (1 – 16).
        pin         : GPIO pin number on the ESP32.
        active_high : True → relay energises on HIGH; False → on LOW.
        safe_on     : True → safe state is ON; False → OFF.

        Returns True on success. Use configure_channel_verbose() if you
        need the board's actual reason when this returns False.
        """
        ok, _ = self.configure_channel_verbose(ch, pin, active_high, safe_on)
        return ok

    def configure_channel_verbose(
        self,
        ch: int,
        pin: int,
        active_high: bool = True,
        safe_on: bool = False,
    ) -> tuple:
        """Same as configure_channel(), but returns (ok, raw_response_lines)
        so the caller can see the board's actual rejection reason."""
        pol  = "HIGH" if active_high else "LOW"
        safe = "ON"   if safe_on     else "OFF"
        return self._send_expecting_ok_verbose(
            f"CONFIG {ch} PIN {pin} POL {pol} SAFE {safe}"
        )

    def set_channel(self, ch: int, state: bool) -> bool:
        """
        Drive channel ch ON (True) or OFF (False).
        Returns True on success.
        """
        return self._send_expecting_ok(f"SET {ch} {'ON' if state else 'OFF'}")

    def get_channel(self, ch: int) -> Optional[ChannelStatus]:
        """
        Query a single channel.
        Returns a ChannelStatus or None if the response could not be parsed.
        """
        lines = self._send(f"GET {ch}")
        for line in lines:
            status = _parse_status_line(line)
            if status is not None:
                return status
        return None

    def status(self) -> list[ChannelStatus]:
        """
        Query all configured channels.
        Returns a list of ChannelStatus objects (may be empty).
        """
        lines = self._send("STATUS")
        results = []
        for line in lines:
            status = _parse_status_line(line)
            if status is not None:
                results.append(status)
        return results

    def safe_all(self) -> bool:
        """Drive all channels to their configured safe states. Returns True on success."""
        return self._send_expecting_ok("SAFE")

    def remove_channel(self, ch: int) -> bool:
        """
        Remove a channel's configuration (drives safe state first).
        Returns True on success.
        """
        return self._send_expecting_ok(f"REMOVE {ch}")

    def factory_reset(self) -> bool:
        """Erase all channel configuration from flash. Returns True on success."""
        lines = self._send("FACTORY")
        return any("OK" in ln for ln in lines)

    def help(self) -> str:
        """Return the firmware's built-in help text as a string."""
        return "\n".join(self._send("HELP"))

    # ── Laser PWM control ──────────────────────────────────────────────────
    #
    # The laser is a high-power (5.5 W, 455 nm) output driven by PWM on its
    # TTL wire. The firmware enforces the safety model; these are thin wrappers.
    # Typical sequence:  laser_config(...) -> laser_arm() -> laser_set(pct) ...
    #                    -> laser_off() -> laser_disarm().
    # While firing, keep sending commands (or a periodic laser_status) so the
    # firmware watchdog does not auto-disarm the laser.

    def laser_config(self, pin: int, freq_hz: int = 1000, max_duty_pct: int = 100) -> bool:
        """Configure the laser PWM pin, frequency, and hard duty ceiling.

        Leaves the laser disarmed at 0%. Returns True on success.
        Use laser_config_verbose() if you need the board's actual reason
        when this returns False.
        """
        ok, _ = self.laser_config_verbose(pin=pin, freq_hz=freq_hz, max_duty_pct=max_duty_pct)
        return ok

    def laser_config_verbose(self, pin: int, freq_hz: int = 1000, max_duty_pct: int = 100) -> tuple:
        """Same as laser_config(), but returns (ok, raw_response_lines) so
        the caller can see the board's actual rejection reason (e.g.
        'ERR PIN_IN_USE', 'ERR BAD_PIN') instead of guessing why."""
        return self._send_expecting_ok_verbose(
            f"LASER CONFIG PIN {pin} FREQ {freq_hz} MAXDUTY {max_duty_pct}"
        )

    def laser_arm(self) -> bool:
        """Arm the laser. Nonzero duty is refused until armed. Returns True on OK."""
        return self._send_expecting_ok("LASER ARM")

    def laser_disarm(self) -> bool:
        """Disarm the laser: forces 0% and blocks further firing. Returns True on OK."""
        return self._send_expecting_ok("LASER DISARM")

    def laser_set(self, duty_pct: int) -> bool:
        """Set laser duty 0-100 (clamped to the configured max; requires ARM).

        Returns True on success. False if not armed, over the max, or out of range.
        """
        return self._send_expecting_ok(f"LASER SET {duty_pct}")

    def laser_freq(self, freq_hz: int) -> bool:
        """Change the laser PWM/modulation frequency. Returns True on OK."""
        return self._send_expecting_ok(f"LASER FREQ {freq_hz}")

    def laser_off(self) -> bool:
        """Immediately set laser duty to 0% (stays armed). Returns True on OK."""
        return self._send_expecting_ok("LASER OFF")

    def laser_status(self) -> Optional[str]:
        """Return the raw firmware LASER STATUS line, or None if no response."""
        for line in self._send("LASER STATUS"):
            if line.upper().startswith("LASER"):
                return line
        return None

    # ── Convenience helpers ────────────────────────────────────────────────

    def on(self, ch: int) -> bool:
        """Shorthand for set_channel(ch, True)."""
        return self.set_channel(ch, True)

    def off(self, ch: int) -> bool:
        """Shorthand for set_channel(ch, False)."""
        return self.set_channel(ch, False)

    def pulse(self, ch: int, duration: float = 0.5) -> bool:
        """
        Turn channel ON, wait duration seconds, then turn it OFF.
        Returns True if both SET commands succeeded.
        """
        ok = self.set_channel(ch, True)
        time.sleep(duration)
        ok &= self.set_channel(ch, False)
        return ok

    def print_status(self) -> None:
        """Print a human-readable status table to stdout."""
        channels = self.status()
        if not channels:
            print("No channels configured.")
            return
        print(f"{'CH':<4} {'PIN':<5} {'POL':<5} {'SAFE':<6} {'STATE':<6}")
        print("-" * 30)
        for c in channels:
            if c.configured:
                pol  = "HIGH" if c.active_high else "LOW"
                safe = "ON"   if c.safe_on     else "OFF"
                st   = "ON"   if c.state       else "OFF"
                print(f"{c.ch:<4} {c.pin:<5} {pol:<5} {safe:<6} {st:<6}")
            else:
                print(f"{c.ch:<4} {'—':<5} {'—':<5} {'—':<6} UNCONFIGURED")

    # ── Port discovery ─────────────────────────────────────────────────────

    @staticmethod
    def list_ports() -> list[str]:
        """Return a list of available serial port names on this machine."""
        return [p.device for p in serial.tools.list_ports.comports()]

    @staticmethod
    def find_esp32(baud: int = 115200) -> Optional[str]:
        """
        Scan all available serial ports and return the first one that
        responds to PING.  Returns None if no ESP32 is found.
        """
        for port in RelayController.list_ports():
            try:
                rc = RelayController(port, baud=baud, timeout=1.0, connect_delay=1.5)
                if rc.connect():
                    rc.disconnect()
                    return port
            except Exception:
                pass
        return None


# ── Response parsing ───────────────────────────────────────────────────────

def _parse_status_line(line: str) -> Optional[ChannelStatus]:
    """
    Parse a firmware status line into a ChannelStatus.

    Expected formats:
        CH 1 PIN 18 POL HIGH SAFE OFF STATE ON
        CH 2 UNCONFIGURED
    """
    line = line.strip()
    if not line.upper().startswith("CH "):
        return None

    tokens = line.upper().split()
    try:
        ch = int(tokens[1])
    except (IndexError, ValueError):
        return None

    if len(tokens) >= 3 and tokens[2] == "UNCONFIGURED":
        return ChannelStatus(ch=ch, configured=False)

    # Expect: CH <ch> PIN <pin> POL <pol> SAFE <safe> STATE <state>
    try:
        pin        = int(tokens[3])
        active_high = tokens[5] == "HIGH"
        safe_on    = tokens[7] == "ON"
        state      = tokens[9] == "ON"
        return ChannelStatus(
            ch=ch,
            configured=True,
            pin=pin,
            active_high=active_high,
            safe_on=safe_on,
            state=state,
        )
    except (IndexError, ValueError):
        return None


# ── Example / quick test ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    PORT = sys.argv[1] if len(sys.argv) > 1 else None

    if PORT is None:
        print("Scanning for ESP32...")
        PORT = RelayController.find_esp32()
        if PORT is None:
            print("No ESP32 found. Pass port as argument: python relay_controller.py COM3")
            sys.exit(1)
        print(f"Found device on {PORT}")

    print(f"Connecting to {PORT}...")
    with RelayController(PORT) as rc:
        if not rc.is_connected():
            print("Failed to connect.")
            sys.exit(1)

        print("Connected.\n")

        # ── Configure five channels on typical safe GPIO pins ────────────────
        # Adjust pin numbers to match your wiring.
        channel_pins = {1: 18, 2: 19, 3: 21, 4: 22, 5: 23}
        for ch, pin in channel_pins.items():
            ok = rc.configure_channel(ch, pin=pin, active_high=True, safe_on=False)
            print(f"CONFIG ch{ch} pin{pin}: {'OK' if ok else 'FAILED'}")

        print()
        rc.print_status()

        print("\nTurning channel 1 ON...")
        rc.on(1)
        time.sleep(1)

        print("Pulsing channel 2 (0.5 s)...")
        rc.pulse(2, 0.5)

        print("Driving all channels to safe state...")
        rc.safe_all()

        print("\nFinal status:")
        rc.print_status()