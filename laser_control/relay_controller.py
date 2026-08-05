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

    # ── Connection management ──────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Open the serial port and wait for the device to be ready.

        Returns True on success, False if the port cannot be opened or the
        device does not respond to PING.
        """
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=self.timeout,
            )
            # The ESP32 resets when the serial port is opened (DTR toggle).
            # Wait for it to boot and print READY.
            time.sleep(self.connect_delay)
            self._serial.reset_input_buffer()
            return self.ping()
        except serial.SerialException as exc:
            print(f"[RelayController] Could not open {self.port}: {exc}")
            return False

    def disconnect(self) -> None:
        """Close the serial port."""
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

        self._serial.reset_input_buffer()
        self._serial.write((cmd.strip() + "\n").encode())

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
        lines = self._send(cmd)
        return any(ln.startswith("OK") for ln in lines)

    # ── Commands ──────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Return True if the device responds with PONG."""
        lines = self._send("PING")
        return any("PONG" in ln for ln in lines)

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

        Returns True on success.
        """
        pol  = "HIGH" if active_high else "LOW"
        safe = "ON"   if safe_on     else "OFF"
        return self._send_expecting_ok(
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
        """
        return self._send_expecting_ok(
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