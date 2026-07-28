import logging as log
from enum import IntEnum
import numpy as np
from dataclasses import dataclass
from strenum import StrEnum


MOVEMENT_PORT          = 30003
REALTIME_FEEDBACK_PORT = 30004
DASHBOARD_PORT         = 29999


class DobotError(IntEnum):
    FAIL_TO_GET             = -1
    COMMAND_ERROR           = -10000
    PARAMETER_NUM_ERROR     = -20000
    WRONG_PARAM_TYPE        = -30000
    FIRST_PARAM_INCORRECT   = -30001
    SECOND_PARAM_INCORRECT  = -30002
    PARAMETER_RANGE_INCORRECT = -40000
    FIRST_PARAM_RANGE       = -40001
    SECOND_PARAM_RANGE      = -40002


@dataclass
class IOPort:
    mode:     int
    distance: int
    index:    int
    status:   int

    def __post_init__(self):
        self.mode     = self._clamp(self.mode,     0, 1)
        self.distance = self._clamp(self.distance,  0, 100)
        self.index    = self._clamp(self.index,     1, 24)
        self.status   = self._clamp(self.status,    0, 1)

    @staticmethod
    def _clamp(val: int, lo: int, hi: int) -> int:
        return max(lo, min(val, hi))


class RobotMode(IntEnum):
    INIT        = 1
    BRAKE_OPEN  = 2
    RESERVED    = 3
    DISABLED    = 4
    ENABLE      = 5
    BACKDRIVE   = 6
    RUNNING     = 7
    RECORDING   = 8
    ERROR       = 9
    PAUSE       = 10
    JOG         = 11


class JointSelection(StrEnum):
    J1NEG = "j1-"
    J1POS = "j1+"
    J2NEG = "j2-"
    J2POS = "j2+"
    J3NEG = "j3-"
    J3POS = "j3+"
    J4NEG = "j4-"
    J4POS = "j4+"
    J5NEG = "j5-"
    J5POS = "j5+"


class RobotType(IntEnum):
    CR3    = 3
    CR3L   = 31
    CR5    = 5
    CR7    = 7
    CR10   = 10
    CR12   = 12
    CR16   = 16
    MG400  = 1
    M1PRO  = 2
    NOVA2  = 101
    NOVA5  = 103
    CR3V2  = 113
    CR5V2  = 115
    CR10V2 = 120


class URDF(StrEnum):
    M1PRO = "urdf/m1pro_description.urdf"


FeedbackType = np.dtype([
    ('len',                      np.int64),
    ('digital_input_bits',       np.uint64),
    ('digital_output_bits',      np.uint64),
    ('robot_mode',               np.uint64),
    ('time_stamp',               np.uint64),
    ('time_stamp_reserve_bit',   np.uint64),
    ('test_value',               np.uint64),
    ('test_value_keep_bit',      np.float64),
    ('speed_scaling',            np.float64),
    ('linear_momentum_norm',     np.float64),
    ('v_main',                   np.float64),
    ('v_robot',                  np.float64),
    ('i_robot',                  np.float64),
    ('i_robot_keep_bit1',        np.float64),
    ('i_robot_keep_bit2',        np.float64),
    ('tool_accelerometer_values', np.float64, (3,)),
    ('elbow_position',           np.float64, (3,)),
    ('elbow_velocity',           np.float64, (3,)),
    ('q_target',                 np.float64, (6,)),
    ('qd_target',                np.float64, (6,)),
    ('qdd_target',               np.float64, (6,)),
    ('i_target',                 np.float64, (6,)),
    ('m_target',                 np.float64, (6,)),
    ('q_actual',                 np.float64, (6,)),
    ('qd_actual',                np.float64, (6,)),
    ('i_actual',                 np.float64, (6,)),
    ('actual_TCP_force',         np.float64, (6,)),
    ('tool_vector_actual',       np.float64, (6,)),
    ('TCP_speed_actual',         np.float64, (6,)),
    ('TCP_force',                np.float64, (6,)),
    ('Tool_vector_target',       np.float64, (6,)),
    ('TCP_speed_target',         np.float64, (6,)),
    ('motor_temperatures',       np.float64, (6,)),
    ('joint_modes',              np.float64, (6,)),
    ('v_actual',                 np.float64, (6,)),
    ('hand_type',                np.byte,    (4,)),
    ('user',                     np.byte),
    ('tool',                     np.byte),
    ('run_queued_cmd',           np.byte),
    ('pause_cmd_flag',           np.byte),
    ('velocity_ratio',           np.byte),
    ('acceleration_ratio',       np.byte),
    ('jerk_ratio',               np.byte),
    ('xyz_velocity_ratio',       np.byte),
    ('r_velocity_ratio',         np.byte),
    ('xyz_acceleration_ratio',   np.byte),
    ('r_acceleration_ratio',     np.byte),
    ('xyz_jerk_ratio',           np.byte),
    ('r_jerk_ratio',             np.byte),
    ('brake_status',             np.byte),
    ('enable_status',            np.byte),
    ('drag_status',              np.byte),
    ('running_status',           np.byte),
    ('error_status',             np.byte),
    ('jog_status',               np.byte),
    ('robot_type',               np.byte),
    ('drag_button_signal',       np.byte),
    ('enable_button_signal',     np.byte),
    ('record_button_signal',     np.byte),
    ('reappear_button_signal',   np.byte),
    ('jaw_button_signal',        np.byte),
    ('six_force_online',         np.byte),
    ('reserve2',                 np.byte,    (82,)),
    ('m_actual',                 np.float64, (6,)),
    ('load',                     np.float64),
    ('center_x',                 np.float64),
    ('center_y',                 np.float64),
    ('center_z',                 np.float64),
    ('user_coords',              np.float64, (6,)),
    ('tool_coords',              np.float64, (6,)),
    ('trace_index',              np.float64),
    ('six_force_value',          np.float64, (6,)),
    ('target_quaternion',        np.float64, (4,)),
    ('actual_quaternion',        np.float64, (4,)),
    ('reserve3',                 np.byte,    (24,)),
])
