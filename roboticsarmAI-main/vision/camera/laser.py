"""
[WIRED — pending two values you confirm] USB laser control via pyserial.

Most USB laser modules / relay boards enumerate as a plain serial port
and switch on/off via a short command. This implements that generic
pattern. Two things need confirming on your actual hardware before this
works as-is - both are called out below and live in vision/config.py:

  1. LASER_SERIAL_PORT - the OS-assigned port name.
     Windows: check Device Manager -> Ports (COM & LPT) with the laser
       plugged in (look for the COM number that appears/disappears when
       you plug/unplug it).
     Linux/Mac: run `ls /dev/tty*` before and after plugging in; the new
       entry (often /dev/ttyUSB0 or /dev/ttyACM0) is your port.

  2. LASER_ON_COMMAND / LASER_OFF_COMMAND - the exact bytes the device
     expects. b"1"/b"0" is implemented as the default guess (common for
     cheap relay-style modules); check any datasheet/manual that came
     with it if that doesn't work. Some devices instead expect e.g.
     b"ON\\n"/b"OFF\\n", or a single non-ASCII byte (e.g. b"\\x01") -
     easy to swap in vision/config.py once known.

If your hardware turns out NOT to be a simple serial on/off device (e.g.
it needs a specific vendor protocol, or shows up as a HID device instead
of a serial port), everything calling set_laser()/laser_on()/laser_off()
stays the same - only the body of _send_command() below would change.
"""

from __future__ import annotations

try:
    import serial
    _PYSERIAL_AVAILABLE = True
except ImportError:
    _PYSERIAL_AVAILABLE = False

from vision.config import LASER_SERIAL_PORT, LASER_BAUD_RATE, LASER_ON_COMMAND, LASER_OFF_COMMAND

_connection = None


def _require_pyserial():
    if not _PYSERIAL_AVAILABLE:
        raise ImportError("pyserial is not installed. Run: pip install pyserial")


def _get_connection():
    global _connection
    _require_pyserial()
    if _connection is None or not _connection.is_open:
        try:
            _connection = serial.Serial(LASER_SERIAL_PORT, LASER_BAUD_RATE, timeout=2)
        except serial.SerialException as e:
            raise RuntimeError(
                f"Could not open laser serial port '{LASER_SERIAL_PORT}': {e}\n"
                f"Confirm the port name in Device Manager (Windows) or "
                f"`ls /dev/tty*` (Linux/Mac), then update LASER_SERIAL_PORT "
                f"in vision/config.py."
            )
    return _connection


def _send_command(command: bytes) -> None:
    conn = _get_connection()
    conn.write(command)
    conn.flush()


def set_laser(robot, state: bool) -> None:
    """
    [WIRED — pending confirmed port/command] Turn the laser on/off.

    `robot` is accepted for interface consistency with how main.py calls
    other actuator functions (e.g. set_claw_dual_output), but isn't used
    here since the laser has its own independent USB serial connection,
    not a Dobot digital-output pin.
    """
    _send_command(LASER_ON_COMMAND if state else LASER_OFF_COMMAND)


def laser_on(robot=None) -> None:
    set_laser(robot, True)


def laser_off(robot=None) -> None:
    set_laser(robot, False)


def close() -> None:
    """Release the serial connection. Call on app shutdown."""
    global _connection
    if _connection is not None and _connection.is_open:
        _connection.close()
    _connection = None


if __name__ == "__main__":
    print(f"Testing laser on {LASER_SERIAL_PORT} @ {LASER_BAUD_RATE} baud ...")
    print("Turning ON for 2 seconds, then OFF.")
    import time
    laser_on()
    time.sleep(2)
    laser_off()
    close()
    print("Done. If nothing happened, check LASER_SERIAL_PORT / "
          "LASER_ON_COMMAND / LASER_OFF_COMMAND in vision/config.py.")
