import socket
import logging as log
from typing import Optional
from .util import DobotSocketConnection, Simulator, clamp
from .types import (
    DobotError, IOPort, RobotMode, JointSelection, URDF,
    MOVEMENT_PORT, DASHBOARD_PORT, REALTIME_FEEDBACK_PORT, FeedbackType,
)

import numpy as np


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Dobot:
    """
    Top-level entry point.  Instantiating this class opens three TCP connections:
        dashboard  → port 29999  (setting / query commands)
        movement   → port 30003  (queued motion commands)
        feedback   → port 30004  (1440-byte real-time telemetry stream)
    """

    def __init__(
        self,
        ip: str,
        urdf_file: Optional[URDF] = None,
        is_cr: bool = False,
        logging: bool = False,
        log_name: str = "output.log",
        log_level=log.DEBUG,
    ):
        if logging:
            log.basicConfig(filename=log_name, level=log_level)

        self.movement  = Movement(ip, urdf_file)
        self.feedback  = Feedback(ip)
        self.dashboard = Dashboard(ip)


# ---------------------------------------------------------------------------
# Movement (port 30003)
# ---------------------------------------------------------------------------

class Movement(DobotSocketConnection):

    # Joint safety limits for the M1 Pro
    SAFE_LIMITS = {
        "J1": (-83.0,  83.0),
        "J2": (-128.0, 128.0),
        "J3": (7.0,    243.0),   # vertical Z-axis (mm)
        "J4": (-358.0, 358.0),
    }

    def __init__(self, ip: str, urdf_file: Optional[URDF] = None):
        super().__init__(ip, MOVEMENT_PORT)
        self.simulator = Simulator(urdf_file) if urdf_file else None

    # ------------------------------------------------------------------
    # Digital output (queued)
    # ------------------------------------------------------------------

    def set_digital_output_queued(self, index: int, val: int) -> Optional[DobotError]:
        index = clamp(index, 1, 20)
        val   = clamp(val,   0,  1)
        if index >= 17:
            tool_index = index - 16
            cmd = f"ToolDO({tool_index}, {val})"
        else:
            cmd = f"DO({index}, {val})"
        opt_error, _ = self.send_command(cmd)
        if opt_error is not None:
            log.error("Queued DO error DO(%d)=%d: %s", index, val, opt_error)
        return opt_error

    # ------------------------------------------------------------------
    # Joint motion commands
    # ------------------------------------------------------------------

    def joint_mov_j(self, joints: list) -> Optional[DobotError]:
        """JointMovJ — move to target joint angles in joint-interpolation mode."""
        inner = ', '.join(map(str, joints))
        opt_error, _ = self.send_command(f"JointMovJ({inner})")
        return opt_error

    def joint_to_joint_move(self, joints: list) -> Optional[DobotError]:
        """Alias of joint_mov_j kept for backward compatibility."""
        return self.joint_mov_j(joints)

    def move_joint(self, joints: list) -> Optional[DobotError]:
        """MovJ — move to Cartesian target via joint interpolation."""
        inner = ', '.join(str(j) for j in joints)
        opt_error, _ = self.send_command(f"MovJ({inner})")
        return opt_error

    def move_joint_io(
        self,
        x: float, y: float, z: float,
        rx: float, ry: float, rz: float,
        io_ports: list,
    ) -> Optional[DobotError]:
        """MovJIO."""
        cmd = f"MovJIO({x}, {y}, {z}, {rx}, {ry}, {rz}"
        for p in io_ports:
            cmd += f",{{{p.mode}, {p.distance}, {p.index}, {p.status}}}"
        cmd += ")"
        opt_error, _ = self.send_command(cmd)
        return opt_error

    # ------------------------------------------------------------------
    # Linear motion commands
    # ------------------------------------------------------------------

    def move_linear(self, points: list) -> Optional[DobotError]:
        """MovL."""
        inner = ', '.join(str(p) for p in points)
        opt_error, _ = self.send_command(f"MovL({inner})")
        return opt_error

    def move_linear_io(
        self,
        x: float, y: float, z: float,
        rx: float, ry: float, rz: float,
        io_ports: list,
    ) -> Optional[DobotError]:
        """MovLIO."""
        cmd = f"MovLIO({x}, {y}, {z}, {rx}, {ry}, {rz}"
        for p in io_ports:
            cmd += f",{{{p.mode}, {p.distance}, {p.index}, {p.status}}}"
        cmd += ")"
        opt_error, _ = self.send_command(cmd)
        return opt_error

    # ------------------------------------------------------------------
    # Arc
    # ------------------------------------------------------------------

    def move_arc(
        self,
        x: float, y: float, z: float, rx: float, ry: float, rz: float,
        x2: float, y2: float, z2: float, rx2: float, ry2: float, rz2: float,
    ) -> Optional[DobotError]:
        opt_error, _ = self.send_command(
            f"Arc({x}, {y}, {z}, {rx}, {ry}, {rz}, {x2}, {y2}, {z2}, {rx2}, {ry2}, {rz2})"
        )
        return opt_error

    # ------------------------------------------------------------------
    # Relative motion
    # ------------------------------------------------------------------

    def relative_move_joint(
        self,
        offx: float, offy: float, offz: float,
        offrx: float, offry: float, offrz: float,
        user_index: int,
    ) -> Optional[DobotError]:
        """RelMovJUser."""
        user_index = clamp(user_index, 0, 9)
        opt_error, _ = self.send_command(
            f"RelMovJUser({offx}, {offy}, {offz}, {offrx}, {offry}, {offrz}, {user_index})"
        )
        return opt_error

    def relative_linear_joint(
        self,
        offx: float, offy: float, offz: float,
        offrx: float, offry: float, offrz: float,
        user_index: int,
    ) -> Optional[DobotError]:
        """RelMovLUser."""
        user_index = clamp(user_index, 0, 9)
        opt_error, _ = self.send_command(
            f"RelMovLUser({offx}, {offy}, {offz}, {offrx}, {offry}, {offrz}, {user_index})"
        )
        return opt_error

    def relative_joint_motion(
        self,
        off1: float, off2: float, off3: float,
        off4: float, off5: float, off6: float,
    ) -> Optional[DobotError]:
        """RelJointMovJ."""
        opt_error, _ = self.send_command(
            f"RelJointMovJ({off1}, {off2}, {off3}, {off4}, {off5}, {off6})"
        )
        return opt_error

    # ------------------------------------------------------------------
    # Jogging
    # ------------------------------------------------------------------

    def move_jog(self, joint: JointSelection) -> Optional[DobotError]:
        """MoveJog with a typed JointSelection."""
        opt_error, _ = self.send_command(f"MoveJog({joint})")
        return opt_error

    def safe_move_jog(self, cmd: str, current_joints: list) -> Optional[DobotError]:
        """
        MoveJog with boundary checking.

        Sends a stop command ("MoveJog()") instead of the requested jog if the
        requested axis is already at its limit in the requested direction.
        Pass cmd="stop" or cmd="" to send a stop unconditionally.
        """
        if not cmd or cmd.lower() == "stop":
            opt_error, _ = self.send_command("MoveJog()")
            return opt_error

        # Parse e.g. "J1+" → axis_key="J1", direction="+"
        axis_key  = cmd[:2].upper()
        direction = cmd[2] if len(cmd) > 2 else "+"
        axis_idx  = int(axis_key[1]) - 1

        low, high = self.SAFE_LIMITS.get(axis_key, (-999, 999))

        if current_joints and len(current_joints) > axis_idx:
            current_val = current_joints[axis_idx]
            if (direction == "+" and current_val >= high) or \
               (direction == "-" and current_val <= low):
                log.warning(
                    "Safety trigger: %s at %.2f, jog blocked.", axis_key, current_val
                )
                self.send_command("MoveJog()")   # force stop
                return None
        else:
            log.warning(
                "Jog skipped — live telemetry not yet available (joints=%s)",
                current_joints,
            )
            return None

        opt_error, _ = self.send_command(f"MoveJog({cmd})")
        return opt_error

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync(self) -> Optional[DobotError]:
        """
        Block until all queued motion commands have completed.
        Essential when you need to guarantee the robot has reached its target
        before firing a claw, reading a sensor, or queuing the next move.
        """
        opt_error, _ = self.send_command("Sync()")
        return opt_error


# ---------------------------------------------------------------------------
# Dashboard (port 29999)
# ---------------------------------------------------------------------------

class Dashboard(DobotSocketConnection):

    def __init__(self, ip: str):
        super().__init__(ip, DASHBOARD_PORT)

    # ------------------------------------------------------------------
    # Robot control
    # ------------------------------------------------------------------

    def turn_on(self) -> Optional[DobotError]:
        opt_error, _ = self.send_command("PowerOn()")
        return opt_error

    def enable(self) -> Optional[DobotError]:
        """
        EnableRobot() — called with no parameters so the robot uses whatever
        payload was last configured in DobotStudio Pro.

        If your application needs to specify the load weight explicitly, call
        enable_with_load() instead.
        """
        opt_error, _ = self.send_command("EnableRobot()")
        return opt_error

    def enable_with_load(
        self,
        weight: float = 0.0,
        cx: float = 0.0,
        cy: float = 0.0,
        cz: float = 0.0,
    ) -> Optional[DobotError]:
        """
        EnableRobot(load, centerX, centerY, centerZ).

        Use this if EnableRobot() alone returns an error after a firmware
        update — some versions require explicit load parameters.
        """
        opt_error, _ = self.send_command(
            f"EnableRobot({weight}, {cx}, {cy}, {cz})"
        )
        return opt_error

    def disable(self) -> Optional[DobotError]:
        opt_error, _ = self.send_command("DisableRobot()")
        return opt_error

    def reset(self) -> Optional[DobotError]:
        opt_error, _ = self.send_command("ResetRobot()")
        return opt_error

    def emergency_stop(self) -> Optional[DobotError]:
        opt_error, _ = self.send_command("EmergencyStop()")
        return opt_error

    def clear_error(self) -> Optional[DobotError]:
        """
        Clear robot alarms.
        After clearing, call continue_script() to restart the motion queue
        if it was paused by the alarm.
        """
        opt_error, _ = self.send_command("ClearError()")
        return opt_error

    def pause(self) -> Optional[DobotError]:
        opt_error, _ = self.send_command("Pause()")
        return opt_error

    def continue_motion(self) -> Optional[DobotError]:
        """Continue() — resumes motion paused by Pause()."""
        opt_error, _ = self.send_command("Continue()")
        return opt_error

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    def robot_mode(self):
        """
        Returns the current RobotMode enum value, or a DobotError on failure.

        RobotMode values (most relevant):
            4  = DISABLED
            5  = ENABLE  (idle, ready for motion)
            7  = RUNNING
            9  = ERROR   (has uncleared alarms)
            10 = PAUSE
        """
        opt_error, ret_val = self.send_command("RobotMode()")
        if opt_error:
            return opt_error
        try:
            return RobotMode(int(ret_val))
        except (ValueError, TypeError) as exc:
            log.error("Could not parse RobotMode response '%s': %s", ret_val, exc)
            return DobotError.FAIL_TO_GET

    def get_error_id(self):
        opt_error, ret_val = self.send_command("GetErrorID()")
        log.info("Error IDs: %s", ret_val)
        return opt_error

    def get_angle(self):
        """GetAngle() — returns joint positions as a list of floats, or DobotError."""
        opt_error, ret_val = self.send_command("GetAngle()")
        if opt_error:
            return opt_error
        try:
            return [float(v.strip()) for v in ret_val.split(',') if v.strip()]
        except ValueError:
            return DobotError.FAIL_TO_GET

    def get_pose(self, user: int = 0, tool: int = 0):
        """GetPose() — returns Cartesian pose [X, Y, Z, R], or DobotError."""
        opt_error, ret_val = self.send_command(f"GetPose(User={user},Tool={tool})")
        if opt_error:
            return opt_error
        try:
            return [float(v.strip()) for v in ret_val.split(',') if v.strip()]
        except ValueError:
            return DobotError.FAIL_TO_GET

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def get_digital_input(self, index: int):
        index = clamp(index, 1, 32)
        opt_error, ret_val = self.send_command(f"DI({index})")
        try:
            return int(ret_val)
        except (ValueError, TypeError):
            return opt_error

    def set_digital_output(self, index: int, val: int) -> Optional[DobotError]:
        """
        Queued DO command via the dashboard port (29999).

        DO() / ToolDO() are queue commands — they enter the dashboard command
        queue and fire in the order they were sent.  This is intentional: the
        claw pulse sequence in set_claw_dual_output relies on this ordering
        together with sleep() calls to produce a clean two-step signal.

        DOExecute() / ToolDOExecute() are the immediate variants but can
        return -1 in states where the robot isn't fully accepting instant IO.
        Keeping DO() here matches the original tested behaviour.
        """
        index = clamp(index, 1, 20)
        val   = clamp(val,   0,  1)
        if index >= 17:
            tool_index = index - 16
            opt_error, _ = self.send_command(f"ToolDO({tool_index}, {val})")
        else:
            opt_error, _ = self.send_command(f"DO({index}, {val})")
        return opt_error

    # ------------------------------------------------------------------
    # Speed / acceleration settings
    # ------------------------------------------------------------------

    def set_linear_accel(self, rate: int) -> Optional[DobotError]:
        rate = clamp(rate, 1, 100)
        opt_error, _ = self.send_command(f"AccL({rate})")
        return opt_error

    def set_joint_accel(self, rate: int) -> Optional[DobotError]:
        rate = clamp(rate, 1, 100)
        opt_error, _ = self.send_command(f"AccJ({rate})")
        return opt_error

    def set_linear_velocity(self, rate: int) -> Optional[DobotError]:
        rate = clamp(rate, 1, 100)
        opt_error, _ = self.send_command(f"SpeedL({rate})")
        return opt_error

    def set_joint_velocity(self, rate: int) -> Optional[DobotError]:
        rate = clamp(rate, 1, 100)
        opt_error, _ = self.send_command(f"SpeedJ({rate})")
        return opt_error

    def set_speedfactor(self, ratio: int) -> Optional[DobotError]:
        ratio = clamp(ratio, 1, 100)
        opt_error, _ = self.send_command(f"SpeedFactor({ratio})")
        return opt_error

    def set_arc_params(self, index: int) -> Optional[DobotError]:
        index = clamp(index, 0, 9)
        opt_error, _ = self.send_command(f"Arch({index})")
        return opt_error

    def set_continuous_path(self, ratio: int) -> Optional[DobotError]:
        ratio = clamp(ratio, 1, 100)
        opt_error, _ = self.send_command(f"CP({ratio})")
        return opt_error

    def set_user(self, index: int) -> Optional[DobotError]:
        index = clamp(index, 0, 9)
        opt_error, _ = self.send_command(f"User({index})")
        return opt_error

    def set_tool(self, index: int) -> Optional[DobotError]:
        index = clamp(index, 0, 9)
        opt_error, _ = self.send_command(f"Tool({index})")
        return opt_error

    def set_payload(self, weight: float, inertia: float = 0.0) -> Optional[DobotError]:
        opt_error, _ = self.send_command(f"SetPayLoad({weight}, {inertia})")
        return opt_error

    # ------------------------------------------------------------------
    # Script control
    # ------------------------------------------------------------------

    def run_script(self, name: str) -> Optional[DobotError]:
        opt_error, _ = self.send_command(f"RunScript({name})")
        return opt_error

    def stop_script(self) -> Optional[DobotError]:
        opt_error, _ = self.send_command("StopScript()")
        return opt_error

    def pause_script(self) -> Optional[DobotError]:
        opt_error, _ = self.send_command("PauseScript()")
        return opt_error

    def continue_script(self) -> Optional[DobotError]:
        opt_error, _ = self.send_command("ContinueScript()")
        return opt_error


# ---------------------------------------------------------------------------
# Feedback (port 30004)
# ---------------------------------------------------------------------------

class Feedback(DobotSocketConnection):
    """
    Real-time telemetry stream on port 30004.

    The controller streams 1440-byte packets continuously from the moment the
    connection is opened (every 8 ms).  consume_greeting=False is passed to
    the parent so we never try to drain it as a greeting — that would
    misalign the 1440-byte packet boundary and corrupt every subsequent read.
    """

    PACKET_SIZE = 1440

    def __init__(self, ip: str):
        super().__init__(ip, REALTIME_FEEDBACK_PORT, consume_greeting=False)
        self.socket.settimeout(0.5)

    def get_feedback(self):
        """
        Read exactly one 1440-byte packet and return it as a numpy structured
        array, or None if no complete packet is available yet.
        """
        try:
            data = b""
            while len(data) < self.PACKET_SIZE:
                chunk = self.socket.recv(self.PACKET_SIZE - len(data))
                if not chunk:
                    return None
                data += chunk
            if len(data) == self.PACKET_SIZE:
                return np.frombuffer(data, dtype=FeedbackType)
        except socket.timeout:
            return None
        except Exception as exc:
            log.warning("Feedback read error: %s", exc)
            return None

        return None



