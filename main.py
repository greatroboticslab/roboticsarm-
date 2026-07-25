import math
import threading
from time import sleep
from datetime import date
import tkinter as tk
from tkinter import messagebox, simpledialog

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from dobot_util import Dobot

from vision.config import PHOTO_STATION, NUM_VIEWS, VIEW_SETTLE_SECONDS, LIVE_FEED_FPS
from vision.camera.capture import (
    capture_station_frame,
    capture_wrist_frame,
    capture_frame,
    list_configured_cameras,
    frame_to_rgb,
    save_image,
    new_sample_id,
)
import requests
from vision.config import TOPIC_CAPTURE_COMMAND, TOPIC_ARM_MOVE_COMMAND, FOURDAI_API_URL
from vision.messaging.publisher import publish_captured, publish_capture_status
from vision.messaging.subscriber import subscribe

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# Global anchor for the matplotlib live tracking marker
live_dot = None
# Global anchor for the fixed photo-station marker (yellow dot)
photo_station_dot = None

# ---------------------------------------------------------------------------
# Arm operation lock — prevents two robot-motion sequences from running at
# once (e.g. clicking "Pickup & Photograph" while "Send to Robot" is still
# working through its queue). Both paths send commands over the same
# DobotSocketConnection, which is not designed for concurrent callers -
# interleaved sends/reads from two threads can corrupt command/response
# parsing. Scoped to the two sequence-level entry points (the pipeline and
# the queued point sender) rather than every single manual move, since
# those are the two places multi-step command sequences run unattended in
# a background thread.
# ---------------------------------------------------------------------------
arm_operation_lock = threading.Lock()


def try_start_arm_operation(op_name: str = "this operation") -> bool:
    """Attempt to claim exclusive arm access. Returns True if claimed;
    shows a warning and returns False if the arm is already busy."""
    if not arm_operation_lock.acquire(blocking=False):
        messagebox.showwarning(
            "Arm Busy",
            f"The arm is currently busy with another sequence.\n"
            f"Please wait for it to finish before starting {op_name}.")
        return False
    return True


def finish_arm_operation() -> None:
    """Release the arm operation lock. Safe to call even if not held."""
    try:
        arm_operation_lock.release()
    except RuntimeError:
        pass  # already released — avoid crashing cleanup paths on a double-release


# Ported directly from HongboRobot_ActualRobot_AI_Points.m
DRAWING_POINTS = np.array([
    [230, -30], [240, -30], [255, -30], [270, -30], [285, -30], [300, -30], [315, -30], [330, -30], [345, -30], [360, -30],
    [360, -20], [360, -5], [360, 10], [360, 25], [360, 40], [360, 55], [360, 70], [360, 85],
    [355, 90], [350, 95], [345, 100], [348, 105], [352, 110], [350, 115], [345, 120], [340, 125],
    [338, 130], [340, 135], [345, 140], [348, 145],
    [350, 140], [352, 130], [352, 115], [352, 95], [352, 75], [352, 55], [352, 35], [352, 15], [352, -5],
    [340, -5], [340, 10], [340, 30], [340, 50], [340, 70], [340, 90], [338, 105], [336, 115], [332, 120],
    [330, 118], [328, 110], [326, 98], [326, 80], [326, 60], [326, 40], [326, 20],
    [320, 20], [315, 22], [310, 25], [305, 32], [302, 40], [300, 52], [298, 40], [295, 32], [290, 25],
    [285, 22], [280, 20],
    [275, 20], [274, 35], [273, 50], [272, 70], [270, 90], [268, 110], [266, 120],
    [264, 110], [262, 95], [262, 70], [262, 45], [262, 20],
    [260, 20], [250, 20], [240, 20],
    [240, -5], [240, 15], [240, 35], [240, 55], [240, 75], [240, 95], [240, 115],
    [238, 125], [235, 130], [232, 135], [235, 140], [238, 145],
    [240, 140], [242, 130], [244, 115], [244, 95], [244, 75], [244, 55], [244, 35], [244, 15], [244, -5],
    [230, -5], [230, 10], [230, 30], [230, 50], [230, 70], [230, 85],
    [235, 90], [240, 95], [245, 100], [250, 105], [248, 110], [244, 115], [242, 120], [240, 125],
    [238, 130], [240, 135], [245, 140], [248, 145],
    [250, 140], [252, 130], [254, 115], [254, 95], [254, 75], [254, 55], [254, 35], [254, 15], [254, -5],
    [260, -5], [275, -5], [290, -5], [300, -5], [310, -5], [325, -5], [340, -5],
    [300, 10], [295, 15], [290, 22], [288, 32], [290, 42], [295, 50], [300, 55],
    [305, 50], [310, 42], [312, 32], [310, 22], [305, 15], [300, 10],
    [290, 60], [285, 65], [280, 70], [278, 80], [280, 90], [285, 98], [290, 102],
    [295, 104], [300, 105], [305, 104], [310, 102], [315, 98], [320, 90], [322, 80], [320, 70],
    [315, 65], [310, 60], [305, 58], [300, 57], [295, 58], [290, 60],
    [265, 30], [275, 40], [285, 50], [300, 65], [315, 50], [325, 40], [335, 30],
    [360, -30], [300, -30], [230, -30]
])


# --- NEW: Global Robot State ---
robot_data = {
    "joints": [0.0, 0.0, 0.0, 0.0],
    "cartesian": [0.0, 0.0, 0.0, 0.0],
    
}
is_jogging = False

def feedback_loop(robot_inst):
    """Thread function to constantly read Port 30004."""
    while True:
        try:
            data = robot_inst.feedback.get_feedback()
            if data is not None:
                robot_data["joints"]    = data[0]['q_actual'][:4].tolist()
                robot_data["cartesian"] = data[0]['tool_vector_actual'][:4].tolist()
        except Exception as e:
            # Log but don't crash — transient packet errors are expected
            print(f"[FEEDBACK WARNING]: {e}")
        sleep(0.02)  # 50Hz

# Add this line where you initialize your robot connection:


class RobotManager:
    def __init__(self, ip="192.168.1.6", urdf_path=None):
        self.ip = ip
        self.robot = Dobot(self.ip, urdf_file=urdf_path) #

    def boot_robot(self):
        """Dashboard handshake for physical robot initialization."""
        print("Booting robot...")
        self.robot.dashboard.clear_error()
        sleep(0.5)
        self.robot.dashboard.enable()
        print("Robot motors enabled.")

    def run_drawing(self):
        self.boot_robot()
        for i, pt in enumerate(DRAWING_POINTS):
            z_height = 245.0 if i == 0 or i == (len(DRAWING_POINTS) - 1) else 220.0
            # Ikinematics returns [[j1,j2,z,r]] — unpack with [0]
            joints = Ikinematics(pt[0], pt[1], z=z_height)[0]
            self.robot.movement.joint_mov_j(joints)
            print(f"Drawing point {i+1}/{len(DRAWING_POINTS)}: {pt}")
        self.robot.movement.sync()
        print("Drawing complete.")


# Global variables for state tracking
robot = None
ROBOT_CONNECTED = False



def initialize_robot(ip="192.168.1.6"):
    global robot, ROBOT_CONNECTED

    try:
        from dobot_util import Dobot
        print(f"Attempting to connect to robot at {ip}...")
        
        # 1. Establish Network Connection
        robot = Dobot(ip, logging=True)
        
        # 2. Bootup Handshake (Critical for Physical Robot)
        print("here2")
        robot.dashboard.clear_error()       # Clear existing alarms/faults
        sleep(0.3)
        
        robot.dashboard.continue_motion()   # Clear any active pause states
        sleep(0.3)
        
        print("Here3")
        robot.dashboard.enable()            # Power on the joints/motors
        sleep(3.0)                          # Give the motors time to fully engage
        
        ROBOT_CONNECTED = True
        print("Robot connected and enabled successfully!")
        
        # Start the background telemetry data-stream thread
        threading.Thread(target=feedback_loop, args=(robot,), daemon=True).start()
        
        # Set to Cartesian/User coordinate system mode
        # robot.dashboard.send_command("CoordinateL(0)")

        return True

    except Exception as e:
        print(f"Robot connection failed: {e}")
        print("Running in demo mode - robot commands will be simulated")
        robot = None
        ROBOT_CONNECTED = False
        return False

# Call the function immediately to maintain original behavior


# Calculate inverse kinematics for a 2-link planar arm
# --- CONFIGURATION TOGGLE ---
# False = Original Way (Checks X and Y values directly)
# True  = Newer Way (Checks calculated J1/J2 angles against degree limits)
STRICT_JOINT_CHECKING = True

def Ikinematics(x, y, z=200.0, r=0.0):
    L1 = 200.0  # Length of first arm segment
    L2 = 200.0  # Length of second arm segment

    # Physical Joint Limits for Dobot M1 Pro
    J1_MIN, J1_MAX = -85.0, 85.0
    J2_MIN, J2_MAX = -135.0, 135.0
    Z_MIN, Z_MAX = 5.0, 245.0

    # --- COORDINATE CHECK (Reverted Mode) ---
    if not STRICT_JOINT_CHECKING:
        # This will fail for any point where X or Y > 85
        if not (-85.0 <= x <= 85.0 and -135.0 <= y <= 135.0):
            raise ValueError(f"Target ({x}, {y}) blocked by X/Y coordinate check (reverted mode)")

    # Inverse kinematics calculations
    D = (x**2 + y**2 - L1**2 - L2**2) / (2 * L1 * L2)
    if abs(D) > 1:
        raise ValueError("Target position out of reach")
    D = max(-1,min(1,D))
    theta2 = math.atan2(math.sqrt(1 - D**2), D)
    theta1 = math.atan2(y, x) - math.atan2(L2 * math.sin(theta2), L1 + L2 * math.cos(theta2))

    # Convert radians to degrees
    j1 = math.degrees(theta1)
    j2 = math.degrees(theta2)

    if STRICT_JOINT_CHECKING:
        # check if orgional position is outside limits 
        if not (J1_MIN <= j1 <= J1_MAX and J2_MIN <= j2 <= J2_MAX):
            # try flipping the elbow
            theta2_alt = math.atan2(-math.sqrt(1- D**2),D)
            theta1_alt = math.atan2(y,x) - math.atan2(L2* math.sin(theta2_alt), L1 + L2 * math.cos(theta2_alt))
            j1_alt , j2_alt = math.degrees(theta1_alt) , math.degrees(theta2_alt)
            # checks if flipping the elbow works
            if ( J1_MIN <= j1_alt <= J1_MAX and J2_MIN <= j2_alt <= J2_MAX):
                j1 , j2 = j1_alt,j2_alt
            else:
                # if both fail raise a value error
                raise ValueError(f"No Valid joint configuations within limits for  ({x},{y})")
    # even though this is mainly for the new mode this also checks for old just in case 
    if not (J1_MIN <= j1 <= J1_MAX):
        raise ValueError(f"J1 angle ({j1:.1f}°) exceeds hardware limit")
    if not (J2_MIN <= j2 <= J2_MAX):
        raise ValueError(f"J2 angle ({j2:.1f}°) exceeds hardware limit")

    if not (Z_MIN <= z <= Z_MAX):
        raise ValueError(f"Z height ({z}) out of range")

    # FIX: Return a list containing the solution list to satisfy the 'sols[0]' unpacking
    return [[j1, j2, z, r]]

# Example Usage:
# If you want to use the Cathedral points (which are > 85), 
# you will need to set STRICT_JOINT_CHECKING = True at the top.

# ---- Robot Control Function ----



# --- MANUAL CONTROL STATE ---
m_x, m_y, m_z = 250.0, 0.0, 200.0 
m_claw = 0



def safe_move_to_point(x, y, z=200, r=0):
    """Non-blocking wrapper around move_to_point.
    Runs the move in a background thread so ensure_robot_enabled()'s
    re-enable polling loop never freezes the Tkinter main thread."""
    threading.Thread(target=move_to_point, args=(x, y, z, r), daemon=True).start()

def move_to_point(x, y, z=200, r=0):
    global m_x, m_y, m_z          # declare global so the assignment below persists
    if is_jogging:
        # messagebox must run on the main thread — schedule it there safely
        root.after(0, lambda: messagebox.showwarning(
            "Robot Busy", "Cannot send move command while jogging!"))
        return

    try:
        sols = Ikinematics(x, y, z, r)
        if not sols:
            print(f"Target ({x}, {y}) is unreachable.")
            return

        j1, j2, z_target, r_target = sols[0]

        if ROBOT_CONNECTED and robot:
            # Re-enable only if the robot has fallen out of ENABLE state.
            # On normal operation this is a fast no-op (mode is already 5).

            print(f"Moving to ({x},{y}) | J1={j1:.1f}° J2={j2:.1f}° Z={z_target:.1f}")
            move_error = robot.movement.joint_to_joint_move([j1, j2, z_target, r_target])
            if move_error is not None:
                print(f"[MOVE ERROR]: {move_error}")
                return   # do not sync position — robot did not move
            print(f"[MOVE SUCCESS]: ({x}, {y}, {z})")

        else:
            print(f"DEMO MODE: J1={j1:.1f}° J2={j2:.1f}° Z={z_target:.1f}")

        # Only reached if the move succeeded (or demo mode) — safe to sync
        m_x, m_y, m_z = x, y, z

    except Exception as e:
        print(f"Robot command failed: {e}")
# --- NEW: Jogging Handlers ---
# --- NEW AREA B: CONTINUOUS JOG HANDLERS ---
# --- REFINED AREA B ---
def handle_jog_press(axis_cmd):
    global is_jogging
    # Check 1: Is robot actually connected?
    # Check 2: Are we already jogging? (Prevents Windows key-repeat spam)
    if not ROBOT_CONNECTED or is_jogging or not manual_active.get():
        return
        
    # Get current joints from the background thread's latest data
    current_j = robot_data["joints"]
    
    # Send the safe command
    error = robot.movement.safe_move_jog(axis_cmd, current_j)
    
    if not error:
        is_jogging = True

def handle_jog_release(event):
    global is_jogging, m_x, m_y, m_z
    if ROBOT_CONNECTED:
        robot.movement.safe_move_jog("stop", [])
        is_jogging = False

        # --- SYNC FIX ---
        # m_x/m_y/m_z ("manual control state") previously only got updated
        # inside move_to_point(). Jogging bypasses move_to_point entirely
        # (it calls safe_move_jog directly), so after a keyboard jog these
        # stayed pointing at wherever the arm was *before* jogging. The next
        # button that reused them — e.g. "Manual Z" (handle_manual_z) or
        # any future move computed from m_x/m_y — would then jump the arm
        # back toward that stale pre-jog position instead of continuing
        # from where the jog actually left it.
        #
        # Fix: pull the robot's real, live telemetry (already tracked at
        # 50Hz by feedback_loop() into robot_data["cartesian"], the same
        # source the red tracking dot uses) and use it to resync m_x/m_y/m_z
        # the moment jogging stops.
        cart = robot_data.get("cartesian")
        if cart and len(cart) >= 3:
            m_x, m_y, m_z = cart[0], cart[1], cart[2]
            print(f"[JOG SYNC] Manual position resynced to actual pose: "
                  f"X={m_x:.1f} Y={m_y:.1f} Z={m_z:.1f}")



def handle_manual_z(dz):
    """Increments Z using the last known tracked position. Simple and safe."""
    if not manual_active.get():
        return
    global m_x, m_y, m_z
    m_z = max(5.0, min(245.0, m_z + dz))
    print(f"Manual Z: moving to Z={m_z:.1f}")
    safe_move_to_point(m_x, m_y, m_z)

# =====================================================================
# CLAW DUAL-OUTPUT CONFIGURATION & HANDLER
# =====================================================================
CONSTANT_PRESSURE_MODE = True  # False = Pulse Sequence Mode, True = Continuous Vacuum Pressure


def set_claw_dual_output(state):
    """
    Controls the claw via DO1 and DO2 on the dashboard queue (port 29999).
    Using DO (queue command) means the outputs are ordered and won't fire
    simultaneously. DO1 and DO2 tested as the working claw ports.
    state=1 → Active (grip), state=0 → Inactive (release).
    """
    state = 1 if int(state) >= 1 else 0
    mode_label = "[VACUUM LATCH]" if CONSTANT_PRESSURE_MODE else "[PULSE SEQUENCE]"
    state_label = "ACTIVE" if state == 1 else "INACTIVE"
    print(f"{mode_label} Claw → {state_label}")

    if not ROBOT_CONNECTED or not robot:
        print(f"DEMO MODE: Claw → {state_label}")
        return

    try:
        if CONSTANT_PRESSURE_MODE:
            # Sustained latch: hold DO1 or DO2 continuously
            if state == 1:
                robot.dashboard.set_digital_output(1, 0)  # Open OFF
                robot.dashboard.set_digital_output(2, 1)  # Close ON (latched)
            else:
                robot.dashboard.set_digital_output(1, 1)  # Open ON (latched)
                robot.dashboard.set_digital_output(2, 0)  # Close OFF
            sleep(0.5)
        else:
            # Pulse sequence: fire one direction then return to neutral
            if state == 1:
                robot.dashboard.set_digital_output(1, 1)  # Fire open
                robot.dashboard.set_digital_output(2, 0)
                sleep(0.5)
                robot.dashboard.set_digital_output(1, 0)  # Return to neutral
                robot.dashboard.set_digital_output(2, 1)
                sleep(0.5)
            else:
                robot.dashboard.set_digital_output(1, 1)  # Rest/open position
                robot.dashboard.set_digital_output(2, 0)
                sleep(0.5)
        print(f"[CLAW OK]: {state_label}")
    except Exception as e:
        print(f"[CLAW ERROR]: {e}")

        

def handle_manual_claw():
    """Toggles the unified claw state manually via UI button click."""
    if not manual_active.get():
        print("Manual Mode disabled.")
        return
    global m_claw

    m_claw = 1 if m_claw == 0 else 0

    # Update the button immediately so the UI feels responsive
    ui_text  = "ACTIVE"    if m_claw == 1 else "INACTIVE"
    ui_color = "green"     if m_claw == 1 else "darkorange"
    claw_overdrive_btn.config(text=f"Claw: {ui_text}", bg=ui_color)

    # Run the hardware command in a background thread so the sleep() calls
    # inside set_claw_dual_output do not freeze the Tkinter main thread
    threading.Thread(target=set_claw_dual_output, args=(m_claw,), daemon=True).start()

limit = 450
x = np.linspace(-limit, limit, 1000)
y = np.linspace(-limit, limit, 1000)
X, Y = np.meshgrid(x, y)

# Precompute constants
tan_85 = np.tan(np.radians(85))
tan_100 = np.tan(np.radians(100))

# Region definitions
r_squared = X**2 + Y**2
region1 = (153**2 <= r_squared) & (r_squared <= 400**2) & (np.abs(X) <= tan_85 * Y)
region2 = ((200 - X)**2 + (abs(200)/tan_85 - Y)**2 <= 200**2) & (Y <= -tan_100 * (X - 153) + 200/tan_85)
region3 = ((200 + X)**2 + (abs(200)/tan_85 - Y)**2 <= 200**2) & (Y <= tan_100 * (X + 153) + 200/tan_85)
final_region = region1 | (region2 | region3)

# Function to check if a point is inside the region
def is_inside(px, py):
    cond1 = (153**2 <= px**2 + py**2 <= 400**2) and (abs(px) <= tan_85 * py)
    cond2 = ((200 - px)**2 + (abs(200)/tan_85 - py)**2 <= 200**2) and (py <= -tan_100 * (px - 153) + 200/tan_85)
    cond3 = ((200 + px)**2 + (abs(200)/tan_85 - py)**2 <= 200**2) and (py <= tan_100 * (px + 153) + 200/tan_85)
    return cond1 or cond2 or cond3

# Lists to store valid and invalid points (now with z-values and claw state)
valid_points = []  # Will store tuples of (px, py, z, claw_state)
valid_scatters = []

# Global references for Joint Entry boxes
j1_entry = None
j2_entry = None
zj_entry = None

root = tk.Tk()
root.title("Robotic Arm Control")

# Initialize robot here — after root exists so any error dialogs can render,
# and before status_label so ROBOT_CONNECTED is set when the label is created.
initialize_robot("192.168.1.6")

# INITIALIZE HERE - This prevents the "Too early" error
global manual_active
manual_active = tk.BooleanVar(value=False)

# Create main container
main_container = tk.Frame(root)
main_container.pack(fill=tk.BOTH, expand=1)

# Create left side container for plot and manual input
left_container = tk.Frame(main_container)
left_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=1)

# Manual input frame (above the plot)
manual_frame = tk.Frame(left_container)
manual_frame.pack(fill=tk.X, padx=5, pady=5)


# --- MANUAL OVERDRIVE UI ---
overdrive_frame = tk.LabelFrame(manual_frame, text="Keyboard & Manual Overdrive", padx=10, pady=10)
overdrive_frame.pack(fill=tk.X, padx=10, pady=5)

# Safety Toggle (Must be ON for keys/buttons to work)
tk.Checkbutton(overdrive_frame, text="Enable Keyboard Control", variable=manual_active, 
               font=("Arial", 10, "bold"), fg="darkblue").grid(row=0, column=0, columnspan=3, pady=5)

# Z Control Buttons
tk.Button(overdrive_frame, text="Z Up (W)", width=10, command=lambda: handle_manual_z(10)).grid(row=1, column=0, padx=5)
tk.Button(overdrive_frame, text="Z Down (S)", width=10, command=lambda: handle_manual_z(-10)).grid(row=1, column=1, padx=5)

# Claw Toggle Button
claw_overdrive_btn = tk.Button(overdrive_frame, text="Claw: INACTIVE", width=15, bg="darkorange", fg="white",
                               command=handle_manual_claw)
claw_overdrive_btn.grid(row=1, column=2, padx=5)

# Main Title
tk.Label(manual_frame, text="Manual Control Interface", font=("Arial", 12, "bold")).pack(pady=5)

# --- ROW 1: MANUAL POINT INPUT (XYZ) ---
input_fields_frame = tk.Frame(manual_frame)
input_fields_frame.pack(pady=5)

# X input
x_frame = tk.Frame(input_fields_frame)
x_frame.pack(side=tk.LEFT, padx=10)
tk.Label(x_frame, text="X (mm):").pack()
x_manual_entry = tk.Entry(x_frame, width=8)
x_manual_entry.pack()

# Y input
y_frame = tk.Frame(input_fields_frame)
y_frame.pack(side=tk.LEFT, padx=10)
tk.Label(y_frame, text="Y (mm):").pack()
y_manual_entry = tk.Entry(y_frame, width=8)
y_manual_entry.pack()

# Z input
z_frame = tk.Frame(input_fields_frame)
z_frame.pack(side=tk.LEFT, padx=10)
tk.Label(z_frame, text="Z (mm):").pack()
z_manual_entry = tk.Entry(z_frame, width=8)
z_manual_entry.insert(0, "200")
z_manual_entry.pack()

# Claw control (XYZ Row)
claw_frame = tk.Frame(input_fields_frame)
claw_frame.pack(side=tk.LEFT, padx=10)
tk.Label(claw_frame, text="Claw:").pack()
claw_var = tk.IntVar(value=0)
claw_radio_frame = tk.Frame(claw_frame)
claw_radio_frame.pack()
tk.Radiobutton(claw_radio_frame, text="OFF", variable=claw_var, value=0).pack(side=tk.LEFT)
tk.Radiobutton(claw_radio_frame, text="ON", variable=claw_var, value=1).pack(side=tk.LEFT)

# Add Point Button (Inline with Row 1)
add_manual_button = tk.Button(input_fields_frame, text="Add Point", 
                              command=lambda: add_manual_point(), 
                              bg="lightgreen", padx=20)
add_manual_button.pack(side=tk.LEFT, padx=20)

# --- Visual Separator ---
tk.Frame(manual_frame, height=2, bd=1, relief=tk.SUNKEN).pack(fill=tk.X, padx=10, pady=10)

# --- ROW 2: MANUAL JOINT CONTROL (J1, J2, Z) ---
joint_fields_frame = tk.Frame(manual_frame)
joint_fields_frame.pack(pady=5)

# J1 Entry
f1 = tk.Frame(joint_fields_frame); f1.pack(side=tk.LEFT, padx=10)
tk.Label(f1, text="J1 (deg):").pack()
j1_entry = tk.Entry(f1, width=8); j1_entry.pack()

# J2 Entry
f2 = tk.Frame(joint_fields_frame); f2.pack(side=tk.LEFT, padx=10)
tk.Label(f2, text="J2 (deg):").pack()
j2_entry = tk.Entry(f2, width=8); j2_entry.pack()

# Z (Joint) Entry
f3 = tk.Frame(joint_fields_frame); f3.pack(side=tk.LEFT, padx=10)
tk.Label(f3, text="Z (mm):").pack()
zj_entry = tk.Entry(f3, width=8); zj_entry.insert(0, "200"); zj_entry.pack()

# Claw control (Joint Row)
claw_frame_j = tk.Frame(joint_fields_frame)
claw_frame_j.pack(side=tk.LEFT, padx=10)
tk.Label(claw_frame_j, text="Claw:").pack()
claw_var_j = tk.IntVar(value=0)
claw_radio_frame_j = tk.Frame(claw_frame_j)
claw_radio_frame_j.pack()
tk.Radiobutton(claw_radio_frame_j, text="OFF", variable=claw_var_j, value=0).pack(side=tk.LEFT)
tk.Radiobutton(claw_radio_frame_j, text="ON", variable=claw_var_j, value=1).pack(side=tk.LEFT)

# Move Joints Button (Inline with Row 2)
move_j_btn = tk.Button(joint_fields_frame, text="Move Joints", 
                       command=lambda: manual_joint_move(), 
                       bg="lightblue", padx=20)
move_j_btn.pack(side=tk.LEFT, padx=20)

# Create a graph to plot the valid region
fig, ax = plt.subplots(figsize=(4,4))
ax.set_title("Arm Valid Region")
fig.tight_layout()

#set x and y axis limits, with 100s interval ticks and 50s minor ticks
ax.set_xlim(-450, 450)
ax.set_ylim(-250, 450)
ax.set_xticks(np.arange(-400, 401, 100))
ax.set_yticks(np.arange(-300, 401, 100))
ax.grid(which='major', linestyle='-', linewidth=0.8)
ax.set_xticks(np.arange(-450, 451, 50), minor=True)
ax.set_yticks(np.arange(-300, 451, 50), minor=True)
ax.grid(which='minor', linestyle='--', linewidth=0.5)
ax.set_aspect('equal', 'box') 

# Setup the valid region plot in light grey
ax.contourf(X, Y, final_region, levels=[0.5, 1], colors=['lightgrey'], alpha=0.5)

# --- Photo station marker (yellow dot) ---
# Plot coordinates (px, py) and robot coordinates (x, y) are rotated
# relative to each other elsewhere in this file (see add_dobot_instructions:
# x = py, y = -px). Invert that here so the marker lands in the same spot
# on the plot that the arm will actually visit: px = -robot_y, py = robot_x.
_station_px = -PHOTO_STATION["y"]
_station_py = PHOTO_STATION["x"]
photo_station_dot = ax.scatter(
    _station_px, _station_py,
    color='yellow', edgecolors='black', s=140, marker='*',
    zorder=6, label="Photo Station"
)
ax.legend()

# Embed matplotlib figure into Tkinter
canvas = FigureCanvasTkAgg(fig, master=left_container)
canvas.draw()
canvas.get_tk_widget().pack(fill=tk.BOTH, expand=1)

# Frame for valid points list and buttons
frame = tk.Frame(main_container)
frame.pack(side=tk.RIGHT, fill=tk.Y)

# Connection status indicator
status_frame = tk.Frame(frame)
status_frame.pack(pady=5)
status_color = "green" if ROBOT_CONNECTED else "red"
status_text = "Robot Connected" if ROBOT_CONNECTED else "Demo Mode (No Robot)"
status_label = tk.Label(status_frame, text=status_text, fg=status_color, font=("Arial", 10, "bold"))
status_label.pack()

tk.Label(frame, text="Valid Points (FIFO)").pack(pady=(10,0))
points_listbox = tk.Listbox(frame, width=30, height=25)
points_listbox.pack(fill=tk.BOTH, expand=1)

# Function to add manual point
def add_manual_point():
    try:
        # Get values from input fields
        x_val = float(x_manual_entry.get())
        y_val = float(y_manual_entry.get())
        z_val = float(z_manual_entry.get())
        claw_state = claw_var.get()  # Get claw state from radio buttons
        
        # Validate z-value range
        if not (5.0 <= z_val <= 245.0):
            messagebox.showerror("Invalid Z-Value", "Z-value must be between 5 and 245 mm")
            return
        
        # Check if point is in valid region
        if is_inside(x_val, y_val):
            # Add valid point with its z-value and claw state
            valid_points.append((x_val, y_val, z_val, claw_state))
            scatter = ax.scatter(x_val, y_val, color='blue', s=50, marker='s')  # Blue square for manual points
            valid_scatters.append(scatter)
            
            claw_text = "ON" if claw_state == 1 else "OFF"
            points_listbox.insert(tk.END, f"{len(valid_points)}: ({x_val:.2f}, {y_val:.2f}, z={z_val:.1f}, claw={claw_text}) [Manual]")
            canvas.draw()
            
            # Clear input fields after successful addition
            x_manual_entry.delete(0, tk.END)
            y_manual_entry.delete(0, tk.END)
            z_manual_entry.delete(0, tk.END)
            z_manual_entry.insert(0, "200")  # Reset z to default
            # Keep claw setting as is (don't reset)
            
            print(f"Manual point added: ({x_val:.2f}, {y_val:.2f}, z={z_val:.1f}, claw={claw_text})")
        else:
            messagebox.showerror("Invalid Point", f"Point ({x_val:.2f}, {y_val:.2f}) is outside the valid region")
            
    except ValueError:
        messagebox.showerror("Invalid Input", "Please enter valid numeric values for X, Y, and Z coordinates")

# Function to remove first valid point (FIFO)
def remove_first_point():
    if valid_points:
        # Remove point from data
        valid_points.pop(0)
        # Remove scatter plot
        scatter = valid_scatters.pop(0)
        scatter.remove()
        # Remove from listbox
        points_listbox.delete(0)
        canvas.draw()

def add_dobot_instructions():
    if not valid_points:
        messagebox.showwarning("No Points", "No valid points to send to robot!")
        return

    if not try_start_arm_operation("sending the queued points"):
        return

    def process_next_point():
        if not valid_points:
            print("All points complete.")
            finish_arm_operation()
            return

        px, py, point_z, claw_state = valid_points[0]
        # Coordinate frame rotation to match physical desk orientation
        x = round(py, 2)
        y = -1 * round(px, 2)
        claw_text = "ON" if claw_state == 1 else "OFF"
        print(f"Sending point: x={px:.2f}, y={py:.2f}, z={point_z:.2f}, claw={claw_text}")

        def execute_point():
            """Runs in background thread — move, sync, claw, then schedule next."""
            try:
                # 1. Move to target
                move_to_point(x, y, point_z, 0)

                # 2. Block until the physical robot actually stops moving.
                #    This replaces the fixed 3-second delay and handles both
                #    short moves (no wasted wait) and long moves (no early fire).
                if ROBOT_CONNECTED and robot:
                    err = robot.movement.sync()
                    if err:
                        print(f"[SYNC WARNING]: {err}")

                # 3. Fire claw after confirmed arrival
                set_claw_dual_output(claw_state)
                print(f"Point complete: claw={claw_text}")

            except Exception as e:
                msg = str(e)
                print(f"Sequence failed: {msg}")
                finish_arm_operation()
                root.after(0, lambda msg=msg: messagebox.showerror(
                    "Robot Error", f"Point sequence failed: {msg}"))
                return

            # 4. Remove point from GUI on the main thread, then trigger next
            root.after(0, lambda: (remove_first_point(),
                                   root.after(100, process_next_point)))

        threading.Thread(target=execute_point, daemon=True).start()

    process_next_point()

def pickup_photograph_and_identify(pickup_x, pickup_y, pickup_z):
    """
    [WIRED merge / STUB hardware] Full capture pipeline:
    pick up object -> move to fixed photo station -> rotate J4 through
    NUM_VIEWS steps capturing station+wrist frames at each step ->
    hand the set off to the vision pipeline via MQTT.

    This function itself is fully wired into the arm's motion/claw
    control (real, tested code paths - move_to_point/Ikinematics/
    set_claw_dual_output). It calls into vision.camera.capture and
    vision.messaging.publisher, which are currently stubs that raise
    NotImplementedError - that's expected until cameras/broker are set
    up. The error is caught and reported through the GUI rather than
    crashing the app.
    """
    global robot

    # 1. Move to the object and grip it (reuses existing, tested logic)
    sols = Ikinematics(pickup_x, pickup_y, z=pickup_z)
    j1, j2, z_target, r_target = sols[0]
    if ROBOT_CONNECTED and robot:
        move_error = robot.movement.joint_to_joint_move([j1, j2, z_target, r_target])
        if move_error is not None:
            print(f"[PICKUP ERROR]: {move_error}")
            return
        robot.movement.sync()
    else:
        print(f"DEMO MODE: pickup at ({pickup_x}, {pickup_y}, {pickup_z})")
    set_claw_dual_output(1)  # grip

    # 2. Move to the fixed photo station (same marker shown as the yellow dot)
    station_sols = Ikinematics(PHOTO_STATION["x"], PHOTO_STATION["y"], z=PHOTO_STATION["z"])
    base_j1, base_j2, base_z, base_j4 = station_sols[0]
    if ROBOT_CONNECTED and robot:
        move_error = robot.movement.joint_to_joint_move([base_j1, base_j2, base_z, base_j4])
        if move_error is not None:
            print(f"[STATION MOVE ERROR]: {move_error}")
            return
        robot.movement.sync()
    else:
        print(f"DEMO MODE: moving to photo station {PHOTO_STATION}")

    # 3. Rotate J4 through NUM_VIEWS steps, capturing station + wrist frames
    sample_id = new_sample_id()
    step_deg = 360.0 / NUM_VIEWS
    views = []

    # Work over however many cameras are actually configured in
    # vision/config.py's CAMERAS dict, not just a hardcoded station+wrist
    # pair. If a camera is missing/unplugged, it's skipped rather than
    # aborting the whole sequence - once a camera fails, it's remembered
    # for the rest of THIS capture run so we don't waste time retrying a
    # known-dead camera on every single rotation step.
    cameras_to_use = list_configured_cameras()
    failed_cameras = set()

    for i in range(NUM_VIEWS):
        j4_target = base_j4 + (i * step_deg)
        if ROBOT_CONNECTED and robot:
            robot.movement.joint_to_joint_move([base_j1, base_j2, base_z, j4_target])
            robot.movement.sync()
        else:
            print(f"DEMO MODE: rotating to J4={j4_target:.1f} deg (view {i+1}/{NUM_VIEWS})")
        sleep(VIEW_SETTLE_SECONDS)

        pose_snapshot = {"j1": base_j1, "j2": base_j2, "z": base_z, "j4": j4_target}

        for camera_name in cameras_to_use:
            if camera_name in failed_cameras:
                continue
            try:
                frame = capture_frame(camera_name)
            except (RuntimeError, ImportError) as e:
                print(f"[CAPTURE SKIPPED] Camera '{camera_name}' unavailable — "
                      f"skipping it for the rest of this capture: {e}")
                failed_cameras.add(camera_name)
                continue
            image_path = save_image(frame, sample_id, camera_name, i)
            views.append({"source": camera_name, "view_index": i,
                          "image_path": image_path, "pose": pose_snapshot})

    if not views:
        raise RuntimeError(
            f"No cameras were available — capture produced zero images. "
            f"Configured cameras: {list(cameras_to_use.keys())}. Check "
            f"connections and run `python -m vision.camera.capture` (no .py)."
        )

    if failed_cameras:
        print(f"[CAPTURE COMPLETE WITH GAPS] Sample {sample_id}: "
              f"{len(views)} image(s) captured; cameras unavailable this "
              f"run: {sorted(failed_cameras)}")

    # 4. Hand off to the vision pipeline (raises NotImplementedError until
    #    an MQTT broker is configured)
    publish_captured(sample_id, views, PHOTO_STATION)
    print(f"[CAPTURE COMPLETE] sample_id={sample_id}, {len(views)} views published")
    return sample_id


def run_pickup_photograph_and_identify(pickup_x, pickup_y, pickup_z):
    """Background-thread wrapper so hardware/broker errors (missing
    camera, unplugged laser, no MQTT broker running, etc.) show a clean
    dialog instead of freezing or crashing the Tkinter main loop.

    BUGFIX: `except X as e:` bindings are deleted by Python the moment the
    except block ends (this is standard Python behavior, not a mistake in
    the original try/except). root.after(0, lambda: ...) schedules the
    lambda to run *later*, on a future pass through the Tkinter event
    loop - by which point `e` has already been deleted, causing
    `NameError: cannot access free variable 'e'`. Fix: read str(e) into a
    plain local variable *inside* the except block (before it's deleted),
    and have the lambda close over that string instead of over `e`.
    """
    def execute():
        if not try_start_arm_operation("the pickup & photograph pipeline"):
            return
        try:
            pickup_photograph_and_identify(pickup_x, pickup_y, pickup_z)
        except NotImplementedError as e:
            msg = str(e)
            root.after(0, lambda msg=msg: messagebox.showinfo(
                "Not Implemented Yet",
                f"Pipeline reached an unfinished piece:\n\n{msg}"))
        except (ImportError, RuntimeError, IOError, ConnectionError) as e:
            # These are now the expected real-world failure modes now that
            # camera/laser/MQTT are wired to real hardware/services rather
            # than stubs: missing dependency, camera not found, broker not
            # running, etc. Reported the same clean way rather than a raw
            # traceback dialog.
            msg = str(e)
            root.after(0, lambda msg=msg: messagebox.showwarning(
                "Pipeline Could Not Complete", msg))
        except Exception as e:
            msg = str(e)
            root.after(0, lambda msg=msg: messagebox.showerror(
                "Pipeline Error", f"Pickup/photograph sequence failed: {msg}"))
        finally:
            finish_arm_operation()

    threading.Thread(target=execute, daemon=True).start()


def run_automatic_capture_sequence(category: str, num_images: int,
                                    degrees_per_step: float,
                                    interval_seconds: float) -> None:
    """
    [WIRED] The full "no manual clicking" capture loop: from wherever the
    arm currently is, rotate J4 by `degrees_per_step` degrees, wait
    `interval_seconds` to settle, take a photo with a local camera, and
    repeat `num_images` times. Once done, registers the result through
    4DAI's own existing REST API (/collection/submission,
    /collection/images/upload) - the exact endpoints its manual "Submit"
    flow already used - so 4DAI needs zero code changes for this to work.

    Runs inline on whatever thread calls it - callers (the MQTT command
    handler below) are responsible for running it off the Tkinter main
    thread and holding arm_operation_lock, same convention as
    run_pickup_photograph_and_identify.
    """
    sample_id = new_sample_id()
    image_paths = []

    try:
        publish_capture_status("started", category=category,
                                sample_id=sample_id,
                                num_images=num_images,
                                degrees_per_step=degrees_per_step)

        base_joints = list(robot_data["joints"]) if robot_data["joints"] else [0.0, 0.0, 200.0, 0.0]
        base_j1, base_j2, base_z, base_j4 = (base_joints + [0.0, 0.0, 200.0, 0.0])[:4]

        cameras_to_use = list_configured_cameras()
        failed_cameras = set()

        for i in range(num_images):
            j4_target = base_j4 + (i * degrees_per_step)

            if ROBOT_CONNECTED and robot:
                move_error = robot.movement.joint_to_joint_move(
                    [base_j1, base_j2, base_z, j4_target])
                if move_error is not None:
                    raise RuntimeError(f"Move failed at image {i + 1}: {move_error}")
                robot.movement.sync()
            else:
                print(f"DEMO MODE: rotating to J4={j4_target:.1f} deg "
                      f"(image {i + 1}/{num_images})")

            sleep(interval_seconds)

            captured_this_step = False
            for camera_name in cameras_to_use:
                if camera_name in failed_cameras:
                    continue
                try:
                    frame = capture_frame(camera_name)
                except (RuntimeError, ImportError) as e:
                    print(f"[CAPTURE SKIPPED] '{camera_name}' unavailable: {e}")
                    failed_cameras.add(camera_name)
                    continue
                image_path = save_image(frame, sample_id, camera_name, i)
                image_paths.append(image_path)
                captured_this_step = True
                publish_capture_status("image", category=category,
                                        sample_id=sample_id, image_index=i,
                                        camera=camera_name)

            if not captured_this_step:
                print(f"[WARNING] No camera produced a frame for image {i + 1}")

        if not image_paths:
            raise RuntimeError("Automatic capture produced zero images - "
                                "check camera connections.")

        # Register through 4DAI's own REST API - same endpoints its
        # "Submit" button uses - so this sample lands in the correct
        # per-category collection and shows up in 4DAI's "View
        # Collections" page identically to a manual submission. 4DAI's
        # code is completely unmodified for this to work.
        values = {"predicted_label": category, "num_images": len(image_paths)}
        submit_response = requests.post(
            f"{FOURDAI_API_URL}/collection/submission",
            json={"category": category, "date": str(date.today()), "data": values},
            timeout=10,
        )
        submit_response.raise_for_status()
        fourdai_sample_id = submit_response.json()["sample_id"]

        for image_path in image_paths:
            with open(image_path, "rb") as image_file:
                upload_response = requests.post(
                    f"{FOURDAI_API_URL}/collection/images/upload",
                    files={"file": image_file},
                    data={"sample_id": fourdai_sample_id, "category": category},
                    timeout=10,
                )
            upload_response.raise_for_status()

        publish_capture_status("completed", category=category,
                                sample_id=fourdai_sample_id,
                                image_paths=image_paths, values=values)
        print(f"[AUTO CAPTURE COMPLETE] sample_id={fourdai_sample_id}, "
              f"{len(image_paths)} image(s) uploaded to 4DAI")

    except Exception as e:
        msg = str(e)
        print(f"[AUTO CAPTURE ERROR] {msg}")
        try:
            publish_capture_status("error", category=category, message=msg)
        except Exception as publish_err:
            print(f"[AUTO CAPTURE] Could not report error over MQTT: {publish_err}")


def _handle_capture_command(payload: dict) -> None:
    """MQTT handler for TOPIC_CAPTURE_COMMAND messages from 4DAI.
    Expected payload: {"category": str, "num_images": int,
    "degrees_per_step": float, "interval_seconds": float}."""
    category = payload.get("category", "uncategorized")
    num_images = int(payload.get("num_images", 5))
    degrees_per_step = float(payload.get("degrees_per_step", 5.0))
    interval_seconds = float(payload.get("interval_seconds", 5.0))

    if not try_start_arm_operation("an automatic capture sequence"):
        return
    try:
        run_automatic_capture_sequence(category, num_images,
                                        degrees_per_step, interval_seconds)
    finally:
        finish_arm_operation()


def start_capture_command_listener() -> None:
    """Background thread: subscribes to TOPIC_CAPTURE_COMMAND and runs
    each command as it arrives. Safe to call even if no MQTT broker is
    reachable yet - retries quietly rather than crashing the GUI, since
    this thread is not required for local/manual GUI operation."""
    def _run():
        while True:
            try:
                subscribe(TOPIC_CAPTURE_COMMAND, _handle_capture_command)
            except Exception as e:
                print(f"[MQTT] Capture command listener error, retrying in 5s: {e}")
                sleep(5)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# GENERIC REMOTE CONTROL — lets anything (4DAI's own "Arm Control" page,
# an external AI/automation script, a notebook, etc.) drive the arm over
# MQTT without needing direct access to this machine. Two message
# shapes, both published to TOPIC_ARM_MOVE_COMMAND:
#
#   {"jog": "J4+"}            - start jogging that axis (same as holding
#                                the arrow/W/S keys in the GUI)
#   {"jog": "stop"}           - stop jogging
#   {"j1": .., "j2": .., "j3": .., "j4": ..}
#                             - absolute joint move (any subset; missing
#                               joints hold their current position)
# =============================================================================

def _handle_move_command(payload: dict) -> None:
    if "jog" in payload:
        axis_cmd = payload["jog"]
        if not ROBOT_CONNECTED or not manual_active.get():
            print(f"[REMOTE JOG IGNORED] robot not connected or manual mode off: {axis_cmd}")
            return
        if axis_cmd == "stop":
            handle_jog_release(None)
        else:
            handle_jog_press(axis_cmd)
        return

    current = robot_data["joints"] if robot_data["joints"] else [0.0, 0.0, 200.0, 0.0]
    current = (list(current) + [0.0, 0.0, 200.0, 0.0])[:4]
    j1 = float(payload.get("j1", current[0]))
    j2 = float(payload.get("j2", current[1]))
    j3 = float(payload.get("j3", current[2]))
    j4 = float(payload.get("j4", current[3]))

    if not try_start_arm_operation("a remote move command"):
        return
    try:
        if ROBOT_CONNECTED and robot:
            move_error = robot.movement.joint_to_joint_move([j1, j2, j3, j4])
            if move_error is not None:
                print(f"[REMOTE MOVE ERROR]: {move_error}")
            else:
                robot.movement.sync()
        else:
            print(f"DEMO MODE: remote move to J1={j1:.1f} J2={j2:.1f} "
                  f"J3={j3:.1f} J4={j4:.1f}")
    finally:
        finish_arm_operation()


def start_move_command_listener() -> None:
    """Background thread: subscribes to TOPIC_ARM_MOVE_COMMAND for
    generic remote control (an AI model, external script, etc.)."""
    def _run():
        while True:
            try:
                subscribe(TOPIC_ARM_MOVE_COMMAND, _handle_move_command)
            except Exception as e:
                print(f"[MQTT] Move command listener error, retrying in 5s: {e}")
                sleep(5)

    threading.Thread(target=_run, daemon=True).start()


def dobot_error_reset():
    if ROBOT_CONNECTED and robot:
        try:
            robot.dashboard.clear_error()
            print("Robot errors cleared")
        except Exception as e:
            print(f"Failed to clear robot errors: {e}")
            messagebox.showerror("Robot Error", f"Failed to clear errors: {e}")
    else:
        print("DEMO MODE: Would clear robot errors")

def add_test_points_from_list():
    """Open a dialog to input a list of test points for testing purposes."""
    dialog = tk.Toplevel(root)
    dialog.title("Add Test Points")
    dialog.geometry("500x400")
    dialog.transient(root)
    dialog.grab_set()

    # Center the dialog
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() // 2) - (500 // 2)
    y = (dialog.winfo_screenheight() // 2) - (400 // 2)
    dialog.geometry(f"500x400+{x}+{y}")

    # Title
    tk.Label(dialog, text="Enter Test Points List", font=("Arial", 14, "bold")).pack(pady=10)

    # Instructions
    instructions = tk.Label(dialog, text="Enter a Python list of points.\nFormat: [(x, y, z, claw), (x, y, z, claw), ...]\nExample: [(100, 50, 200, 0), (150, 75, 180, 1)]", justify=tk.LEFT)
    instructions.pack(pady=5, padx=10)

    # Text area for input
    text_frame = tk.Frame(dialog)
    text_frame.pack(pady=10, padx=10, fill=tk.BOTH, expand=True)

    text_input = tk.Text(text_frame, height=10, width=50)
    text_input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    scrollbar = tk.Scrollbar(text_frame, command=text_input.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    text_input.config(yscrollcommand=scrollbar.set)

    # Example button
    def insert_example():
        example = """[(100, 50, 200, 0), (150, 75, 180, 1), (200, 100, 220, 0)]"""
        text_input.delete(1.0, tk.END)
        text_input.insert(1.0, example)

    tk.Button(dialog, text="Insert Example", command=insert_example).pack(pady=5)

    # Results label
    result_label = tk.Label(dialog, text="", fg="blue")
    result_label.pack(pady=5)

    def parse_and_add_points():
        input_text = text_input.get(1.0, tk.END).strip()

        if not input_text:
            messagebox.showwarning("Empty Input", "Please enter a list of points")
            return

        try:
            # Try to evaluate the input as a Python literal
            import ast
            points_list = ast.literal_eval(input_text)

            if not isinstance(points_list, list):
                messagebox.showerror("Invalid Format", "Input must be a list")
                return

            if len(points_list) == 0:
                messagebox.showwarning("Empty List", "The list is empty")
                return

            # Validate each point
            valid_count = 0
            invalid_count = 0

            for i, point in enumerate(points_list):
                try:
                    if len(point) != 4:
                        print(f"Point {i+1}: Invalid number of values (expected 4, got {len(point)})")
                        invalid_count += 1
                        continue

                    px, py, pz, claw = point

                    # Validate types and ranges
                    if not all(isinstance(coord, (int, float)) for coord in [px, py, pz]):
                        print(f"Point {i+1}: X, Y, Z must be numbers")
                        invalid_count += 1
                        continue

                    if not isinstance(claw, int) or claw not in [0, 1]:
                        print(f"Point {i+1}: Claw must be 0 (OFF) or 1 (ON)")
                        invalid_count += 1
                        continue

                    if not (5.0 <= pz <= 245.0):
                        print(f"Point {i+1}: Z-value must be between 5 and 245 mm")
                        invalid_count += 1
                        continue

                    if not is_inside(px, py):
                        print(f"Point {i+1}: ({px:.2f}, {py:.2f}) is outside valid region")
                        invalid_count += 1
                        continue

                    # Add valid point
                    valid_points.append((px, py, pz, claw))
                    scatter = ax.scatter(px, py, color='purple', s=50, marker='D')  # Purple diamond for test points
                    valid_scatters.append(scatter)

                    claw_text = "ON" if claw == 1 else "OFF"
                    points_listbox.insert(tk.END, f"{len(valid_points)}: ({px:.2f}, {py:.2f}, z={pz:.1f}, claw={claw_text}) [Test]")
                    valid_count += 1

                except Exception as e:
                    print(f"Point {i+1}: Error - {e}")
                    invalid_count += 1
                    continue

            # Update plot
            canvas.draw()

            # Show results
            result_text = f"Added {valid_count} valid points"
            if invalid_count > 0:
                result_text += f", {invalid_count} invalid points skipped"
            result_label.config(text=result_text)

            if valid_count > 0:
                print(f"Successfully added {valid_count} test points")
                if invalid_count > 0:
                    print(f"Skipped {invalid_count} invalid points")
            else:
                messagebox.showwarning("No Valid Points", "No valid points were added. Check the console for details.")

        except (ValueError, SyntaxError) as e:
            messagebox.showerror("Parse Error", f"Invalid Python syntax: {e}\n\nMake sure to use proper list format with brackets and parentheses.")

    # Buttons
    button_frame = tk.Frame(dialog)
    button_frame.pack(pady=10, fill=tk.X)

    tk.Button(button_frame, text="Add Points", command=parse_and_add_points, bg="lightgreen").pack(side=tk.LEFT, padx=10)
    tk.Button(button_frame, text="Cancel", command=dialog.destroy, bg="lightcoral").pack(side=tk.LEFT, padx=10)

    # Wait for dialog to close
    dialog.wait_window()

# Button to remove first point
remove_button = tk.Button(frame, text="Remove First Point (FIFO)", command=remove_first_point)
remove_button.pack(pady=5)

# Button to send coordinates to dobot
send_button = tk.Button(frame, text="Send Instructions", command=add_dobot_instructions, bg="lightgreen")
send_button.pack(pady=5)

# Button to reset error
error_button = tk.Button(frame, text="Clear Errors", command=dobot_error_reset, bg="orange")
error_button.pack(pady=5)

# Button to add test points from list
test_points_button = tk.Button(frame, text="Add Test Points", command=add_test_points_from_list, )
test_points_button.pack(pady=5)

# --- Vision pipeline entry point (yellow star = photo station on the plot) ---
def prompt_pickup_and_photograph():
    """Reuses the same X/Y/Z manual-entry fields as 'Add Point', but routes
    into the full pickup -> photo-station -> multi-view capture pipeline
    instead of just queueing a FIFO point."""
    try:
        x_val = float(x_manual_entry.get())
        y_val = float(y_manual_entry.get())
        z_val = float(z_manual_entry.get())
    except ValueError:
        messagebox.showerror("Invalid Input", "Enter numeric X, Y, Z in the manual fields above first.")
        return

    if not (5.0 <= z_val <= 245.0):
        messagebox.showerror("Invalid Z-Value", "Z-value must be between 5 and 245 mm")
        return
    if not is_inside(x_val, y_val):
        messagebox.showerror("Invalid Point", f"Point ({x_val:.2f}, {y_val:.2f}) is outside the valid region")
        return

    run_pickup_photograph_and_identify(x_val, y_val, z_val)

vision_button = tk.Button(frame, text="Pickup & Photograph (uses X/Y/Z above)",
                           command=prompt_pickup_and_photograph, bg="khaki")
vision_button.pack(pady=5)

# Custom dialog for Z-value and claw state

def get_point_settings(px, py):
    # Create the popup window
    dialog = tk.Toplevel(root)
    dialog.title("Point Settings")
    dialog.geometry("300x250")
    dialog.transient(root)
    dialog.grab_set()  # Forces user to interact with this window before the main one
    
    # Center the dialog on screen
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() // 2) - (300 // 2)
    y = (dialog.winfo_screenheight() // 2) - (250 // 2)
    dialog.geometry(f"300x250+{x}+{y}")
    
    # Initialize the result dictionary
    result = {'z': None, 'claw': 0}
    
    # CRITICAL: This variable tells the code when the "Add Point" button is clicked
    submitted = tk.BooleanVar(value=False)
    
    # UI Elements
    tk.Label(dialog, text=f"Settings for point:", font=("Arial", 10, "bold")).pack(pady=5)
    tk.Label(dialog, text=f"X: {px:.2f}, Y: {py:.2f}", font=("Arial", 9)).pack()
    
    # Z-value input
    tk.Label(dialog, text="\nZ-value (5-245 mm):").pack()
    z_entry = tk.Entry(dialog, width=15)
    z_entry.insert(0, "200") # Default height
    z_entry.pack()
    
    # Claw control
    tk.Label(dialog, text="\nClaw State:").pack()
    claw_var_inner = tk.IntVar(value=0)
    radio_frame = tk.Frame(dialog)
    radio_frame.pack()
    tk.Radiobutton(radio_frame, text="OFF", variable=claw_var_inner, value=0).pack(side=tk.LEFT)
    tk.Radiobutton(radio_frame, text="ON", variable=claw_var_inner, value=1).pack(side=tk.LEFT)
    
    # Internal function for the button click
    def add_point_clicked():
        try:
            z_val = float(z_entry.get())
            if 5.0 <= z_val <= 245.0:
                # SAVE the values into our result dictionary
                result['z'] = z_val
                result['claw'] = claw_var_inner.get()
                
                # Signal that we are done and close window
                submitted.set(True)
                dialog.destroy()
            else:
                messagebox.showerror("Invalid Z", "Z must be between 5 and 245")
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter a numeric Z-value")
    
    def cancel_clicked():
        # result['z'] remains None, so the point won't be saved
        submitted.set(True) # Set to true just to break the wait loop
        dialog.destroy()

    # Buttons
    btn_frame = tk.Frame(dialog)
    btn_frame.pack(pady=20)
    tk.Button(btn_frame, text="Add Point", command=add_point_clicked, bg="lightgreen", width=10).pack(side=tk.LEFT, padx=5)
    tk.Button(btn_frame, text="Cancel", command=cancel_clicked, bg="lightcoral", width=10).pack(side=tk.LEFT, padx=5)
    
    # CRITICAL: This pauses the main script until 'submitted' is set to True
    # Without this, the function returns result={'z':None} immediately.
    root.wait_variable(submitted)
    
    return result

# Event handler for clicking on the plot
def onclick(event):

    # --- ADD THIS CHECK AT THE VERY TOP ---
    global is_jogging
    if is_jogging:
        print("Click ignored: Robot is currently jogging.")
        return 
    # --------------------------------------

    if event.xdata is None or event.ydata is None:
        return
    px, py = event.xdata, event.ydata
    if is_inside(px, py):
        # Get point settings (z-value and claw state)
        settings = get_point_settings(px, py)
        
        if settings['z'] is not None:  # User didn't cancel
            # Add valid point with its z-value and claw state
            valid_points.append((px, py, settings['z'], settings['claw']))
            scatter = ax.scatter(px, py, color='green', s=50)
            valid_scatters.append(scatter)

            claw_text = "ON" if settings['claw'] == 1 else "OFF"
            points_listbox.insert(tk.END, f"{len(valid_points)}: ({px:.2f}, {py:.2f}, z={settings['z']:.1f}, claw={claw_text})")
        # If user cancelled, don't add the point
    else:
        # Add invalid point and remove after 1 second
        scatter = ax.scatter(px, py, color='red', s=50)
        canvas.draw()
        root.after(1000, lambda: remove_invalid_point(scatter))

    canvas.draw()

def remove_invalid_point(scatter):
    scatter.remove()
    canvas.draw()

def manual_joint_move():
    global robot, ROBOT_CONNECTED

    J1_MIN, J1_MAX = -85.0, 85.0
    J2_MIN, J2_MAX = -135.0, 135.0
    Z_MIN, Z_MAX = 5.0, 245.0
    J4_FIXED = -35.0

    try:
        j1 = float(j1_entry.get())
        j2 = float(j2_entry.get())
        z  = float(zj_entry.get())
        claw_state = claw_var_j.get()   # read the joint-row claw radio button

        if not (J1_MIN <= j1 <= J1_MAX and J2_MIN <= j2 <= J2_MAX and Z_MIN <= z <= Z_MAX):
            messagebox.showerror("Out of Range", "Joint values outside limits!")
            return

        def execute():
            if ROBOT_CONNECTED and robot:
                print(f"Moving to J1:{j1}° J2:{j2}° Z:{z}mm")
                move_error = robot.movement.joint_to_joint_move([j1, j2, z, J4_FIXED])
                if move_error is not None:
                    print(f"[JOINT MOVE ERROR]: {move_error}")
                    return
                print(f"[JOINT MOVE SUCCESS]: J1:{j1} J2:{j2} Z:{z}")
                # Apply claw state after move completes
                robot.movement.sync()
                set_claw_dual_output(claw_state)
            else:
                print(f"DEMO MODE: J1:{j1} J2:{j2} Z:{z} Claw:{'ON' if claw_state else 'OFF'}")

        # Run in background thread so ensure_robot_enabled and sync
        # don't block the Tkinter main thread
        threading.Thread(target=execute, daemon=True).start()

    except ValueError:
        messagebox.showerror("Input Error", "Please enter valid numbers for joints.")


def update_gui_from_feedback():
    """Refreshes the plot and labels with the robot's actual hardware position."""
    global live_dot
    
    if ROBOT_CONNECTED and "cartesian" in robot_data and robot_data["cartesian"] is not None:
        try:
            # 1. Get Cartesian X, Y, Z from the real-time hardware telemetry
            raw_x = robot_data["cartesian"][0]
            raw_y = robot_data["cartesian"][1]
            live_z = robot_data["cartesian"][2]
            
            # 2. Apply rotation matrix to align with your physical desk setup
            angle_deg = 90  # Change to -90, 180, etc. based on your setup
            theta = np.radians(angle_deg)
            rot_x = raw_x * np.cos(theta) - raw_y * np.sin(theta)
            rot_y = raw_x * np.sin(theta) + raw_y * np.cos(theta)
            
            # 3. Update the red tracking dot on the Matplotlib plot
            if live_dot is not None:
                live_dot.set_offsets(np.c_[rot_x, rot_y])
            else:
                live_dot = ax.scatter(rot_x, rot_y, color='red', s=100, zorder=5, label="Live Robot Pos")
                ax.legend()
                
            # 4. Update the text status label with X, Y, and Z
            if 'status_label' in globals() and status_label.winfo_exists():
                status_label.config(text=f"Robot Connected | X: {raw_x:.1f} | Y: {raw_y:.1f} | Z: {live_z:.1f}")
                
            fig.canvas.draw_idle() 
        except Exception as e:
            print(f"GUI telemetry loop warning: {e}")

    # Schedule this function to run again in 100ms
    root.after(100, update_gui_from_feedback)

# Connect the click event
fig.canvas.mpl_connect('button_press_event', onclick)

# Add instructions label
instructions = tk.Label(frame, text="Instructions:\n1. Click points on plot OR\n2. Use manual input (X,Y,Z) OR\n3. Use 'Add Test Points' for batch testing\n4. Send to robot",
                       justify=tk.LEFT, font=("Arial", 9), bg="lightyellow")
instructions.pack(pady=10)

# =============================================================================
# MongoDB gallery panel — browse captured samples/images stored in Mongo,
# plus a standalone "Capture Photo" button for a single quick snapshot
# (no arm movement, no multi-view rotation, no identification — just grab
# one frame from the station camera and log it).
# =============================================================================
gallery_frame = tk.Frame(main_container, width=280, bg="#f0f0f0", bd=1, relief=tk.SUNKEN)
# NOTE: Tkinter's side=RIGHT packing stacks outward in the order widgets
# are packed — whichever is packed FIRST ends up visually rightmost/
# outermost. `frame` (the control panel) was already packed earlier in
# this file, so without `before=frame` here, this new gallery panel would
# land sandwiched between the plot and the control buttons instead of
# sitting past them on the far right. `before=frame` forces the intended
# left-to-right reading order: plot | controls | gallery.
gallery_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=5, before=frame)
gallery_frame.pack_propagate(False)

# =============================================================================
# Live camera feed — continuous preview from any configured camera
# (vision.config.CAMERAS), not just a single snapshot. Runs as a
# self-rescheduling root.after() loop: each tick grabs one frame in a
# background thread (camera reads shouldn't block the GUI thread), then
# marshals the resulting PhotoImage back via root.after(0, ...) before
# scheduling the next tick. A busy-flag prevents ticks piling up if a
# camera read is slow. Works for however many cameras are configured -
# the dropdown is populated straight from CAMERAS, so adding a third/
# fourth camera in vision/config.py makes it selectable here with no
# other code changes.
# =============================================================================
tk.Label(gallery_frame, text="Live Camera Feed",
         font=("Arial", 11, "bold"), bg="#f0f0f0").pack(pady=(10, 5))

_camera_names = list(list_configured_cameras().keys()) or ["station"]
live_feed_camera_var = tk.StringVar(value=_camera_names[0])
tk.OptionMenu(gallery_frame, live_feed_camera_var, *_camera_names).pack(pady=2)

live_feed_button_row = tk.Frame(gallery_frame, bg="#f0f0f0")
live_feed_button_row.pack(pady=4)

live_feed_status_label = tk.Label(gallery_frame, text="Stopped",
                                   font=("Arial", 8), bg="#f0f0f0", fg="gray")
live_feed_status_label.pack()

live_feed_label = tk.Label(gallery_frame, bg="#222222", width=32, height=10,
                            text="Live feed will appear here", fg="white",
                            justify=tk.CENTER)
live_feed_label.pack(padx=8, pady=8)

live_feed_active = False
live_feed_busy = False


def _schedule_next_live_feed_tick():
    if live_feed_active:
        root.after(int(1000 / LIVE_FEED_FPS), _live_feed_tick)


def _live_feed_tick():
    global live_feed_busy
    if not live_feed_active:
        return
    if live_feed_busy:
        # Previous tick's camera read hasn't finished yet — skip this
        # tick rather than piling up threads.
        _schedule_next_live_feed_tick()
        return

    live_feed_busy = True
    camera_name = live_feed_camera_var.get()

    def grab():
        global live_feed_busy
        try:
            frame = capture_frame(camera_name)
            rgb = frame_to_rgb(frame)
        except Exception as e:
            msg = str(e)

            def on_error():
                global live_feed_busy
                live_feed_busy = False
                live_feed_status_label.config(text=f"Error: {msg}", fg="red")
                stop_live_feed()

            root.after(0, on_error)
            return

        def apply():
            global live_feed_busy
            try:
                if _PIL_AVAILABLE:
                    img = Image.fromarray(rgb)
                    img.thumbnail((260, 200))
                    photo = ImageTk.PhotoImage(img)
                    live_feed_label.image = photo  # keep a reference
                    live_feed_label.config(image=photo, text="")
                else:
                    live_feed_label.config(
                        text="Pillow not installed.\nRun: pip install Pillow")
            finally:
                live_feed_busy = False
                _schedule_next_live_feed_tick()

        root.after(0, apply)

    threading.Thread(target=grab, daemon=True).start()


def start_live_feed():
    global live_feed_active
    if live_feed_active:
        return
    if not _PIL_AVAILABLE:
        messagebox.showwarning("Pillow Required", "Run: pip install Pillow")
        return
    live_feed_active = True
    live_feed_status_label.config(text=f"Live — {live_feed_camera_var.get()}", fg="green")
    _schedule_next_live_feed_tick()


def stop_live_feed():
    global live_feed_active
    live_feed_active = False
    live_feed_status_label.config(text="Stopped", fg="gray")


tk.Button(live_feed_button_row, text="Start Feed", bg="lightgreen",
          command=start_live_feed).pack(side=tk.LEFT, padx=4)
tk.Button(live_feed_button_row, text="Stop Feed",
          command=stop_live_feed).pack(side=tk.LEFT, padx=4)

tk.Label(gallery_frame, text="Captured Objects",
         font=("Arial", 11, "bold"), bg="#f0f0f0").pack(pady=(10, 5))
tk.Label(gallery_frame, text="Browse captured samples in 4DAI's\n"
         "'View Collections' page — this app no\n"
         "longer keeps its own local copy.",
         font=("Arial", 8), bg="#f0f0f0", fg="gray",
         justify=tk.CENTER).pack(pady=(0, 10))


# --- KEYBOARD BINDINGS ---
# --- NEW AREA C: JOGGING BINDINGS ---

# J1 Control (Shoulder)
root.bind("<KeyPress-Up>",    lambda e: handle_jog_press("J1+"))
root.bind("<KeyRelease-Up>",  handle_jog_release)

root.bind("<KeyPress-Down>",  lambda e: handle_jog_press("J1-"))
root.bind("<KeyRelease-Down>", handle_jog_release)

# J2 Control (Elbow)
root.bind("<KeyPress-Left>",  lambda e: handle_jog_press("J2+"))
root.bind("<KeyRelease-Left>", handle_jog_release)

root.bind("<KeyPress-Right>", lambda e: handle_jog_press("J2-"))
root.bind("<KeyRelease-Right>", handle_jog_release)

# J3 Control (Z-Axis Height)
root.bind("<KeyPress-w>",     lambda e: handle_jog_press("J3+"))
root.bind("<KeyRelease-w>",   handle_jog_release)

root.bind("<KeyPress-s>",     lambda e: handle_jog_press("J3-"))
root.bind("<KeyRelease-s>",   handle_jog_release)

# J4 Control (Wrist rotation)
root.bind("<KeyPress-q>",     lambda e: handle_jog_press("J4+"))
root.bind("<KeyRelease-q>",   handle_jog_release)

root.bind("<KeyPress-e>",     lambda e: handle_jog_press("J4-"))
root.bind("<KeyRelease-e>",   handle_jog_release)



update_gui_from_feedback()

# Start listening for automatic-capture commands published by 4DAI (see
# vision/config.py TOPIC_CAPTURE_COMMAND). This is what lets 4DAI's GUI
# trigger a full "rotate + photograph" sequence with no manual
# button-clicking on the arm side, per the transcript's requirement.
start_capture_command_listener()

# Generic remote control - lets 4DAI's own Arm Control page, an external
# AI model, or any other MQTT publisher move the arm without touching
# this machine (see TOPIC_ARM_MOVE_COMMAND in vision/config.py).
start_move_command_listener()


def on_app_close():
    """Release camera handles, the laser's serial connection, and the
    MQTT client cleanly on exit rather than leaving them open/locked."""
    global live_feed_active
    live_feed_active = False  # stop the self-rescheduling live feed loop

    try:
        from vision.camera.capture import release_all
        release_all()
    except Exception as e:
        print(f"[CLEANUP] Camera release skipped: {e}")

    try:
        from vision.camera.laser import close as close_laser
        close_laser()
    except Exception as e:
        print(f"[CLEANUP] Laser close skipped: {e}")

    try:
        from vision.messaging.publisher import disconnect as disconnect_mqtt
        disconnect_mqtt()
    except Exception as e:
        print(f"[CLEANUP] MQTT disconnect skipped: {e}")

    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_app_close)
root.mainloop()



