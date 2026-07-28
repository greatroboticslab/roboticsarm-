import socket
import logging as log
from typing import Optional, Tuple
from .types import DobotError, URDF

# Try importing ikpy; if not installed the Simulator class is simply unavailable
try:
    from ikpy.chain import Chain
    _IKPY_AVAILABLE = True
except ImportError:
    _IKPY_AVAILABLE = False


class DobotSocketConnection:
    """
    Low-level TCP/IP socket wrapper for a single Dobot port.

    Key fixes vs. the original implementation
    ------------------------------------------
    1. Greeting drain uses a *loop* so the entire welcome message is consumed
       regardless of its length.  The old single recv(1024) left leftover bytes
       in the buffer when newer firmware started sending a longer greeting,
       which caused every subsequent command's response to be mis-parsed.

    2. send_command appends '\\n'.  Newer DobotStudio Pro firmware versions
       expect a newline-terminated command string.  Adding it is harmless on
       older versions (they simply ignore trailing whitespace).

    3. __await_reply reads in a *loop* until it finds the ';' terminator, so
       a response that arrives in multiple TCP segments is reassembled
       correctly.  The old recv(1024) returned whatever arrived first.

    4. Response parsing locates the '{' … '}' block by character search
       instead of splitting on ',' — the old approach broke on multi-value
       responses such as "0,{J1,J2,J3,J4},GetAngle();" because split(',')
       tore the brace block apart.

    5. Unknown error IDs no longer raise an unhandled ValueError.  They are
       mapped to DobotError.FAIL_TO_GET and logged so the caller can decide
       what to do.
    """

    def __init__(self, ip: str, port: int, consume_greeting: bool = True):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(10.0)
        self.socket.connect((ip, port))

        if consume_greeting:
            # Drain the entire greeting message.
            # Older firmware sends ~512 bytes; newer firmware can send several
            # kilobytes.  We loop with a short timeout so we always read it all
            # without blocking permanently if nothing more arrives.
            try:
                self.socket.settimeout(2.0)
                while True:
                    chunk = self.socket.recv(4096)
                    if not chunk:
                        break  # connection closed unexpectedly
            except socket.timeout:
                pass  # silence after greeting is the expected exit condition
            finally:
                self.socket.settimeout(10.0)

        log.debug("Connection established on port %s", port)

    def send_command(self, cmd: str) -> Tuple[Optional[DobotError], str]:
        """
        Send *cmd* over the socket and return (error, return_value).

        No newline is appended — the Dobot TCP/IP protocol specification does
        not define a command terminator, and adding one causes some firmware
        versions to return -1 for every command.
        """
        raw_cmd = cmd.encode("utf-8")
        self.socket.sendall(raw_cmd)
        log.debug('Sent command: "%s"', cmd)
        return self._await_reply()

    def _await_reply(self) -> Tuple[Optional[DobotError], str]:
        """
        Read bytes until the response terminator (';') is received, then parse.

        Dobot response format:
            ErrorID,{value,...,valueN},CommandName(params...);

        Examples:
            0,{},EnableRobot();
            0,{5},RobotMode();
            0,{J1,J2,J3,J4},GetAngle();
            -10000,{},Mov(-500,100,200,150);
        """
        raw = b""
        try:
            while b";" not in raw:
                chunk = self.socket.recv(4096)
                if not chunk:
                    # Remote closed the connection mid-response
                    log.warning("Socket closed before response terminator received")
                    break
                raw += chunk
        except socket.timeout:
            log.warning("Timed out waiting for response terminator; parsing what we have")

        response = raw.decode("utf-8", errors="replace").strip()
        log.debug('Raw bytes received: %s', raw)
        log.debug('Decoded response:   "%s"', response)

        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: str) -> Tuple[Optional[DobotError], str]:
        """
        Parse a Dobot response string into (DobotError | None, return_value).

        The return_value is the content between '{' and '}', e.g.:
            "0,{5},RobotMode();"  →  (None, "5")
            "0,{},EnableRobot();" →  (None, "")
            "0,{J1,J2,J3,J4},GetAngle();" → (None, "J1,J2,J3,J4")
        """
        if not response:
            log.error("Empty response from robot")
            return (DobotError.FAIL_TO_GET, "")

        try:
            # Step 1: grab the error ID (everything before the first comma)
            first_comma = response.index(',')
            error_id = int(response[:first_comma].strip())

            # Step 2: grab the return-value block between '{' and '}'
            brace_open  = response.index('{', first_comma)
            brace_close = response.index('}', brace_open)
            return_value = response[brace_open + 1 : brace_close].strip()

        except (ValueError, IndexError) as exc:
            log.error("Failed to parse robot response '%s': %s", response, exc)
            return (DobotError.FAIL_TO_GET, "")

        if error_id == 0:
            return (None, return_value)

        # Map the numeric ID to a DobotError enum member.
        # If the firmware returns an ID we don't have in the enum yet, fall
        # back gracefully to FAIL_TO_GET instead of raising ValueError.
        try:
            return (DobotError(error_id), return_value)
        except ValueError:
            log.warning(
                "Unknown error ID %d in response '%s'; treating as FAIL_TO_GET",
                error_id, response
            )
            return (DobotError.FAIL_TO_GET, return_value)

    def close(self):
        try:
            if self.socket:
                self.socket.close()
        except Exception:
            pass

    def __del__(self):
        self.close()


class Simulator:
    """Optional IK simulator — only available when ikpy is installed."""

    def __init__(self, fn: URDF) -> None:
        if not _IKPY_AVAILABLE:
            raise ImportError(
                "ikpy is not installed.  Install it with: pip install ikpy"
            )
        self.chain = Chain.from_urdf_file(fn)

    def compute(self, target_position: list) -> list:
        return self.chain.inverse_kinematics(target_position)


def clamp(val, local_min, local_max):
    """Return *val* clamped to [local_min, local_max]."""
    log.info("%s clamped to range [%s, %s]", val, local_min, local_max)
    return max(local_min, min(val, local_max))
