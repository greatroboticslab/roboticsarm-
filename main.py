import math
import threading
from time import sleep
import time
import os
import uuid
from datetime import date
import tkinter as tk
from tkinter import ttk
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
from vision.config import (
    TOPIC_CAPTURE_COMMAND,
    TOPIC_CAPTURE_COMMAND_SERVER_DEPENDENT,
    TOPIC_ARM_MOVE_COMMAND,
    FOURDAI_API_URL,
)
from vision.messaging.publisher import publish_captured, publish_capture_status
from vision.messaging.subscriber import subscribe
from vision.storage import mongo_client

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Server URL — where images/samples get uploaded and where the
# server-triggered capture endpoints live. Starts at whatever
# vision/config.py has configured (a local test address by default, see
# TEST_MODE there), but can be changed live from the "Server"
# tab without editing config.py or restarting the app.
# ---------------------------------------------------------------------------
SERVER_URL = FOURDAI_API_URL

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

# =============================================================================
# TABS
#   Tab 1 "Arm Control"       — nothing but driving the robot: plot, manual/
#                               joint controls, the point queue, and sending
#                               instructions to the arm.
#   Tab 2 "Camera"            — a bigger live webcam view, camera selection,
#                               the pickup+photograph vision pipeline, and a
#                               one-click "Capture Photo" that saves locally,
#                               logs to MongoDB, and uploads to the server.
#   Tab 3 "Server"            — the server URL (test-local by default),
#                               connection testing, and the server-
#                               triggered continuous-sweep automation.
#   Tab 4 "Database"          — a browser for what's stored in the local
#                               MongoDB (kept separate from "Server" so
#                               either can grow independently later).
# =============================================================================
notebook = ttk.Notebook(root)
notebook.pack(fill=tk.BOTH, expand=1)

# Make the tabs read clearly as clickable "browser tabs" (bigger, padded,
# an obvious highlight for whichever one is active) rather than the tiny
# default ttk tab strip.
_tab_style = ttk.Style()
try:
    _tab_style.theme_use("clam")
except tk.TclError:
    pass  # theme not available on this platform — fall back to default
_tab_style.configure("TNotebook.Tab", padding=[16, 8], font=("Arial", 10, "bold"))
_tab_style.map("TNotebook.Tab",
               background=[("selected", "#4a90d9"), ("!selected", "#d9d9d9")],
               foreground=[("selected", "white"), ("!selected", "black")])

tab_arm = tk.Frame(notebook)
tab_camera = tk.Frame(notebook)
tab_server = tk.Frame(notebook)
tab_database = tk.Frame(notebook)

# NOTE: widgets added to a Notebook via notebook.add(...) are already
# geometry-managed BY the notebook — do not also call .pack()/.grid() on
# these frames themselves. Doing so previously fought with the
# notebook's own show/hide-per-tab logic and made every tab's content
# render all at once regardless of which tab was selected, which is why
# clicking between tabs looked like it wasn't doing anything.
notebook.add(tab_arm, text="Arm Control")
notebook.add(tab_camera, text="Camera")
notebook.add(tab_server, text="Server")
notebook.add(tab_database, text="Database")

# Always boot straight into the Arm Control tab (demo mode banner and
# all), regardless of insertion order above.
notebook.select(tab_arm)

# Tab 1: Arm Control — everything below that packs into
# main_container/left_container/frame ends up on this tab only.
main_container = tab_arm

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

def photograph_at_current_position():
    """
    Capture-only: takes NO robot action at all — grabs one frame from
    every configured camera at whatever pose the arm is CURRENTLY in,
    tags each image with that live pose, and hands the set off to the
    vision/identification pipeline via MQTT.

    This replaces the old pickup -> move-to-photo-station -> rotate-J4
    pipeline. That version issued its own joint_to_joint_move commands
    mid-sequence based on the X/Y/Z typed into the manual entry boxes —
    a completely separate motion plan from whatever the manual jog keys
    or the point queue were doing. Even with the arm_operation_lock
    preventing the two from sending commands at the literal same instant,
    the pipeline's target pose and the arm's actual physical joint
    values (only updated live via the 50Hz feedback loop into
    robot_data) could still drift apart — e.g. if the manual entries were
    stale, or a previous jog left the arm somewhere the pipeline's
    Ikinematics call didn't account for. Taking the photo at the CURRENT
    position instead removes the motion entirely, so there's nothing
    left to desync: the pose recorded for the image IS the pose read
    straight from live feedback at capture time.
    """
    sample_id = new_sample_id()

    if ROBOT_CONNECTED and robot_data.get("joints"):
        pose_snapshot = {"joints": list(robot_data["joints"]),
                          "cartesian": list(robot_data.get("cartesian") or [])}
    else:
        pose_snapshot = {"joints": None, "cartesian": None,
                          "note": "demo mode — no live feedback available"}

    # Work over however many cameras are actually configured in
    # vision/config.py's CAMERAS dict. If a camera is missing/unplugged,
    # it's skipped rather than aborting the whole capture.
    cameras_to_use = list_configured_cameras()
    views = []
    failed_cameras = []

    for camera_name in cameras_to_use:
        try:
            frame = capture_frame(camera_name)
        except (RuntimeError, ImportError) as e:
            print(f"[CAPTURE SKIPPED] Camera '{camera_name}' unavailable: {e}")
            failed_cameras.append(camera_name)
            continue
        image_path = save_image(frame, sample_id, camera_name, 0)
        views.append({"source": camera_name, "view_index": 0,
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

    publish_captured(sample_id, views, pose_snapshot)
    print(f"[CAPTURE COMPLETE] sample_id={sample_id}, {len(views)} view(s) "
          f"published (no arm movement)")
    return sample_id


def run_photograph_at_current_position():
    """Background-thread wrapper so hardware/broker errors (missing
    camera, no MQTT broker running, etc.) show a clean dialog instead of
    freezing or crashing the Tkinter main loop.

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
        if not try_start_arm_operation("taking a photograph"):
            return
        try:
            photograph_at_current_position()
        except NotImplementedError as e:
            msg = str(e)
            root.after(0, lambda msg=msg: messagebox.showinfo(
                "Not Implemented Yet",
                f"Pipeline reached an unfinished piece:\n\n{msg}"))
        except (ImportError, RuntimeError, IOError, ConnectionError) as e:
            # These are the expected real-world failure modes now that
            # camera/MQTT are wired to real hardware/services rather than
            # stubs: missing dependency, camera not found, broker not
            # running, etc. Reported the same clean way rather than a raw
            # traceback dialog.
            msg = str(e)
            root.after(0, lambda msg=msg: messagebox.showwarning(
                "Photograph Could Not Complete", msg))
        except Exception as e:
            msg = str(e)
            root.after(0, lambda msg=msg: messagebox.showerror(
                "Photograph Error", f"Capture failed: {msg}"))
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
    run_photograph_at_current_position.
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
            f"{SERVER_URL}/collection/submission",
            json={"category": category, "date": str(date.today()), "data": values},
            timeout=10,
        )
        submit_response.raise_for_status()
        fourdai_sample_id = submit_response.json()["sample_id"]

        for image_path in image_paths:
            with open(image_path, "rb") as image_file:
                upload_response = requests.post(
                    f"{SERVER_URL}/collection/images/upload",
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


# =============================================================================
# SERVER-DEPENDENT VERSIONS — reimplementations of the pipeline above for the
# setup where 4DAI's server owns the camera entirely (browser webcam via
# Streamlit) instead of this machine reading a local OpenCV/UVC camera.
#
# These are kept side by side with the local-camera versions above (rather
# than replacing them) so both capture strategies remain available. Every
# function here that reimplements ("redoes") a same-named function above is
# suffixed `_server_dependent`; `run_continuous_sweep` has no local-camera
# counterpart so it keeps its own name.
# =============================================================================

def pickup_photograph_and_identify_server_dependent(pickup_x, pickup_y, pickup_z, category="uncategorized"):
    """
    Full capture pipeline: pick up object -> move to fixed photo station ->
    rotate J4 through NUM_VIEWS steps -> ask 4DAI to take a photo at each
    step.

    Camera hardware/capture is not handled on this machine at all here -
    every "take a photo now" moment is a REST call to 4DAI's
    `/collection/trigger-webcam-capture` endpoint (the same endpoint
    `run_continuous_sweep` below uses), and 4DAI owns the actual webcam,
    image storage, and Mongo record from there. This function only drives
    the arm and tells 4DAI when to snap each view.
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

    # 3. Rotate J4 through NUM_VIEWS steps, asking 4DAI to snap a photo at
    #    each step instead of grabbing a frame from a local camera.
    sample_id = str(uuid.uuid4())
    step_deg = 360.0 / NUM_VIEWS
    triggered = 0

    for i in range(NUM_VIEWS):
        j4_target = base_j4 + (i * step_deg)
        if ROBOT_CONNECTED and robot:
            robot.movement.joint_to_joint_move([base_j1, base_j2, base_z, j4_target])
            robot.movement.sync()
        else:
            print(f"DEMO MODE: rotating to J4={j4_target:.1f} deg (view {i+1}/{NUM_VIEWS})")
        sleep(VIEW_SETTLE_SECONDS)

        try:
            trigger_payload = {
                "category": category,
                "sample_id": sample_id,
                "image_index": i,
                "source": "robotic_arm_photo_station",
            }
            res = requests.post(
                f"{SERVER_URL}/collection/trigger-webcam-capture",
                json=trigger_payload,
                timeout=5,
            )
            if res.status_code == 200:
                triggered += 1
                print(f"[REMOTE TRIGGER] Sent capture trigger #{i} for category '{category}'")
            else:
                print(f"[REMOTE TRIGGER] Server returned status code {res.status_code}")
        except Exception as err:
            print(f"[TRIGGER ERROR] Could not reach website trigger endpoint: {err}")

        publish_capture_status("image_triggered", category=category,
                                sample_id=sample_id, image_index=i)

    if triggered == 0:
        raise ConnectionError(
            f"Could not reach the configured server at {SERVER_URL} for any of the "
            f"{NUM_VIEWS} view(s) — no capture triggers were sent. Check "
            f"that the server is running and that the URL set on the "
            f"'Server' tab is correct."
        )

    publish_capture_status("completed", category=category, sample_id=sample_id)
    print(f"[CAPTURE COMPLETE] sample_id={sample_id}, {triggered}/{NUM_VIEWS} view(s) triggered")
    return sample_id


def run_pickup_photograph_and_identify_server_dependent(pickup_x, pickup_y, pickup_z, category="uncategorized"):
    """Background-thread wrapper so errors (unreachable 4DAI server, no
    MQTT broker running, etc.) show a clean dialog instead of freezing or
    crashing the Tkinter main loop.

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
        if not try_start_arm_operation("the pickup & photograph pipeline (server-dependent)"):
            return
        try:
            pickup_photograph_and_identify_server_dependent(pickup_x, pickup_y, pickup_z, category)
        except (ImportError, RuntimeError, IOError, ConnectionError) as e:
            # Expected real-world failure modes: 4DAI unreachable, no MQTT
            # broker running, etc. Reported cleanly rather than a raw
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


def run_continuous_sweep(category="default", target_j1=0.0, target_j2=0.0, target_j3=200.0, target_j4=25.0):
    """
    Executes an arm sweep and sends photo capture triggers to
    the web server instead of using local OpenCV hardware cameras.
    """
    global robot
    print(f"[SWEEP START] Category: '{category}' | Target: ({target_j1}, {target_j2}, {target_j3}, {target_j4})")

    sample_id = f"sweep_{int(time.time())}"

    if ROBOT_CONNECTED and robot:
        robot.movement.sync()

    SWEEP_TRIGGER_DEGREES = 15.0
    image_index = 0
    last_capture_joints = [0.0, 0.0, 0.0, 0.0]
    arm_is_moving = True

    while arm_is_moving:
        if ROBOT_CONNECTED and robot:
            current_joints = [
                robot.get_j1_angle(),
                robot.get_j2_angle(),
                robot.get_j3_angle(),
                robot.get_j4_angle()
            ]
        else:
            current_joints = [target_j1, target_j2, target_j3, target_j4]
            arm_is_moving = False  # Single pass in simulation mode

        delta_j1 = abs(current_joints[0] - last_capture_joints[0])
        delta_j2 = abs(current_joints[1] - last_capture_joints[1])
        delta_j3 = abs(current_joints[2] - last_capture_joints[2])

        if delta_j1 >= SWEEP_TRIGGER_DEGREES or delta_j2 >= SWEEP_TRIGGER_DEGREES or delta_j3 >= SWEEP_TRIGGER_DEGREES or not arm_is_moving:

            # Send remote trigger request to website backend
            try:
                trigger_payload = {
                    "category": category,
                    "sample_id": sample_id,
                    "image_index": image_index,
                    "source": "robotic_arm_sweep"
                }

                res = requests.post(
                    f"{SERVER_URL}/collection/trigger-webcam-capture",
                    json=trigger_payload,
                    timeout=5
                )
                if res.status_code == 200:
                    print(f"[REMOTE TRIGGER] Sent capture trigger #{image_index} for category '{category}'")
                else:
                    print(f"[REMOTE TRIGGER] Server returned status code {res.status_code}")

            except Exception as err:
                print(f"[TRIGGER ERROR] Could not reach website trigger endpoint: {err}")

            publish_capture_status("image_triggered", category=category, sample_id=sample_id, image_index=image_index)
            image_index += 1
            last_capture_joints = list(current_joints)

        time.sleep(0.1)

    if ROBOT_CONNECTED and robot:
        robot.movement.sync()

    publish_capture_status("completed", category=category, sample_id=sample_id)
    print(f"[SWEEP COMPLETE] Sent {image_index} trigger request(s) for category '{category}'.")


def _handle_capture_command_server_dependent(payload: dict) -> None:
    """MQTT handler for TOPIC_CAPTURE_COMMAND_SERVER_DEPENDENT messages from
    4DAI (the continuous-sweep variant, keyed on target joint angles rather
    than a fixed image count/interval)."""
    category = payload.get("category", "uncategorized")

    # Target coordinates (defaulting to safe values if not provided)
    target_j1 = float(payload.get("target_j1", 0.0))
    target_j2 = float(payload.get("target_j2", 0.0))
    target_j3 = float(payload.get("target_j3", 200.0))
    target_j4 = float(payload.get("target_j4", 25.0))  # J4 fixed offset

    if not try_start_arm_operation("an automatic continuous sweep"):
        return
    try:
        run_continuous_sweep(category, target_j1, target_j2, target_j3, target_j4)
    finally:
        finish_arm_operation()


def start_capture_command_listener_server_dependent() -> None:
    """Background thread: subscribes to TOPIC_CAPTURE_COMMAND_SERVER_DEPENDENT
    and runs each command as it arrives, using the continuous-sweep handler
    above. Runs alongside (not instead of) start_capture_command_listener()
    — it listens on its own topic, so the two do not race each other."""
    def _run():
        while True:
            try:
                subscribe(TOPIC_CAPTURE_COMMAND_SERVER_DEPENDENT, _handle_capture_command_server_dependent)
            except Exception as e:
                print(f"[MQTT] Capture command listener (server-dependent) error, retrying in 5s: {e}")
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
tk.Label(tab_camera, text="Vision Pipeline", font=("Arial", 11, "bold")).pack(pady=(10, 2))
tk.Label(tab_camera, text="Takes a photo from every configured camera at\n"
         "the arm's CURRENT position (no movement) and\n"
         "hands the set off to the identification pipeline.",
         font=("Arial", 8), fg="gray", justify=tk.CENTER).pack()
vision_button = tk.Button(tab_camera, text="Take Photograph (current position)",
                           command=run_photograph_at_current_position, bg="khaki")
vision_button.pack(pady=5)

tk.Label(tab_server, text="Server Communication",
         font=("Arial", 12, "bold")).pack(pady=(10, 5))

# =====================================================================
# SERVER URL CONFIGURATION
# Editable at runtime — starts at whatever vision/config.py's TEST_MODE
# resolves to (a local test address by default), but can be pointed at
# any server IP without restarting the app.
# =====================================================================
server_frame = tk.LabelFrame(tab_server, text=" Server URL ", padx=10, pady=10)
server_frame.pack(fill=tk.X, padx=10, pady=5)

tk.Label(server_frame, text="Server / website address (test local IP by default):").pack(anchor=tk.W)
server_url_var = tk.StringVar(value=SERVER_URL)
server_url_entry = tk.Entry(server_frame, textvariable=server_url_var, width=40)
server_url_entry.pack(fill=tk.X, pady=2)

server_status_label = tk.Label(server_frame, text="Not tested yet", fg="gray")
server_status_label.pack(anchor=tk.W, pady=(2, 5))

def apply_server_url():
    """Commit the entry box's text as the live server URL used by every
    upload/trigger call in the app (Capture Photo, the vision pipeline,
    the sweep automation below)."""
    global SERVER_URL
    SERVER_URL = server_url_var.get().strip().rstrip("/")
    server_status_label.config(text=f"URL set to: {SERVER_URL} (not tested)", fg="gray")

def test_server_connection():
    apply_server_url()
    url = SERVER_URL

    def worker():
        try:
            res = requests.get(url, timeout=4)
            msg, color = f"Reachable (HTTP {res.status_code})", "green"
        except Exception as e:
            msg, color = f"Unreachable: {e}", "red"
        root.after(0, lambda: server_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()

server_btn_row = tk.Frame(server_frame)
server_btn_row.pack(fill=tk.X, pady=4)
tk.Button(server_btn_row, text="Save URL", command=apply_server_url,
          bg="lightblue").pack(side=tk.LEFT, padx=4)
tk.Button(server_btn_row, text="Test Connection", command=test_server_connection,
          bg="lightgreen").pack(side=tk.LEFT, padx=4)

# =====================================================================
# --- NEW: CONTINUOUS SWEEP UI PANEL (server-dependent capture) ---
# =====================================================================
sweep_ui_frame = tk.LabelFrame(tab_server, text=" Continuous Sweep Automation (Server-Triggered) ", padx=5, pady=5)
sweep_ui_frame.pack(fill=tk.X, pady=10, padx=5)

tk.Label(sweep_ui_frame, text="Category / Folder Name:").pack(anchor=tk.W)
category_entry = tk.Entry(sweep_ui_frame, width=20)
category_entry.insert(0, "testing")  # Default placeholder text
category_entry.pack(fill=tk.X, pady=2)

def trigger_ui_sweep():
    """Reads the category text box and starts the sweep if valid."""
    chosen_category = category_entry.get().strip()

    # Simple check: Make sure the text box isn't empty!
    if not chosen_category:
        messagebox.showwarning(
            "Missing Category",
            "Please enter a category name in the text box before starting the sweep!"
        )
        return

    # User entered a category name—launch the sweep!
    target_j1, target_j2, target_j3, target_j4 = 0.0, 0.0, 200.0, 25.0

    threading.Thread(
        target=run_continuous_sweep,
        args=(chosen_category, target_j1, target_j2, target_j3, target_j4),
        daemon=True
    ).start()

tk.Button(
    sweep_ui_frame,
    text="Start Sweep & Upload",
    command=trigger_ui_sweep,
    bg="lightblue",
    font=("Arial", 9, "bold")
).pack(fill=tk.X, pady=5)
# =====================================================================

# =====================================================================
# LOCAL MONGODB — browse what's actually been captured/stored locally.
# Uses the same "Collections" database 4DAI's own server points at (see
# vision/config.py MONGO_URI/MONGO_DB_NAME), so this is a read-only
# window into the same data, not a separate copy. Kept on its own
# "Database" tab (split from "Server") so there's room to grow either
# one independently later.
# =====================================================================
tk.Label(tab_database, text="Local Database",
         font=("Arial", 12, "bold")).pack(pady=(10, 5))

mongo_frame = tk.LabelFrame(tab_database, text=" Local MongoDB ", padx=10, pady=10)
mongo_frame.pack(fill=tk.BOTH, expand=1, padx=10, pady=10)

mongo_status_label = tk.Label(mongo_frame, text="Not tested yet", fg="gray")
mongo_status_label.pack(anchor=tk.W)

def test_mongo_connection():
    def worker():
        try:
            mongo_client.list_recent_samples(limit=1)
            msg, color = "Connected", "green"
        except Exception as e:
            msg, color = f"Unavailable: {e}", "red"
        root.after(0, lambda: mongo_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()

mongo_btn_row = tk.Frame(mongo_frame)
mongo_btn_row.pack(fill=tk.X, pady=(2, 6))
tk.Button(mongo_btn_row, text="Test Mongo Connection", command=test_mongo_connection,
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))

tk.Label(mongo_frame, text="Recent local captures (newest first) — double-click a row to view its photo(s):").pack(anchor=tk.W)
recent_samples_listbox = tk.Listbox(mongo_frame, height=12)
recent_samples_listbox.pack(fill=tk.BOTH, expand=1, pady=4)

# Listbox rows are plain text, but we need the actual sample _id behind
# each row to look up its images — kept in lockstep with the listbox's
# rows (same index = same row) and rebuilt every refresh.
_recent_sample_ids = []


def refresh_recent_samples():
    """Pull the most recent sample documents from local MongoDB and show
    them in the listbox. Safe to call even if MongoDB isn't running —
    shows the error in the list instead of crashing the GUI."""
    def worker():
        err = None
        try:
            samples = mongo_client.list_recent_samples(limit=30)
        except Exception as e:
            samples = None
            err = str(e)

        def apply():
            recent_samples_listbox.delete(0, tk.END)
            _recent_sample_ids.clear()
            if samples is None:
                recent_samples_listbox.insert(tk.END, f"Error: {err}")
                return
            if not samples:
                recent_samples_listbox.insert(tk.END, "(no samples captured yet)")
                return
            for s in samples:
                sid = s.get("_id", "?")
                sdate = s.get("date", "?")
                label = (s.get("data") or {}).get("predicted_label", "")
                recent_samples_listbox.insert(tk.END, f"{sdate}  |  {label}  |  {sid}")
                _recent_sample_ids.append(sid)

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def open_sample_photo_viewer(event=None):
    """Pop up a window showing every image logged against the
    double-clicked sample (one row per camera/view, scrollable if there
    are several)."""
    selection = recent_samples_listbox.curselection()
    if not selection:
        return
    idx = selection[0]
    if idx >= len(_recent_sample_ids):
        return  # clicked on a placeholder row like "(no samples yet)" or "Error: ..."
    sample_id = _recent_sample_ids[idx]

    if not _PIL_AVAILABLE:
        messagebox.showwarning("Pillow Required", "Run: pip install Pillow")
        return

    viewer = tk.Toplevel(root)
    viewer.title(f"Sample {sample_id}")
    viewer.geometry("760x600")
    loading_label = tk.Label(viewer, text="Loading images...", padx=20, pady=20)
    loading_label.pack()

    def worker():
        try:
            image_docs = mongo_client.get_images_for_sample(sample_id)
        except Exception as e:
            err = str(e)
            root.after(0, lambda: loading_label.config(
                text=f"Could not load images for this sample:\n{err}"))
            return

        def build_ui():
            loading_label.destroy()
            if not image_docs:
                tk.Label(viewer, text="No images found for this sample.",
                         padx=20, pady=20).pack()
                return

            canvas_frame = tk.Frame(viewer)
            canvas_frame.pack(fill=tk.BOTH, expand=1)
            scroll_canvas = tk.Canvas(canvas_frame)
            scrollbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL,
                                      command=scroll_canvas.yview)
            inner_frame = tk.Frame(scroll_canvas)
            inner_frame.bind("<Configure>", lambda e: scroll_canvas.configure(
                scrollregion=scroll_canvas.bbox("all")))
            scroll_canvas.create_window((0, 0), window=inner_frame, anchor="nw")
            scroll_canvas.configure(yscrollcommand=scrollbar.set)
            scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=1)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            viewer._photo_refs = []  # keep references so Tk doesn't garbage-collect them
            for doc in image_docs:
                path = doc.get("image_path", "")
                source = doc.get("source", "?")
                view_index = doc.get("view_index", "?")
                row = tk.Frame(inner_frame, pady=8)
                row.pack(fill=tk.X)
                tk.Label(row, text=f"{source} — view {view_index}",
                         font=("Arial", 9, "bold")).pack()
                try:
                    img = Image.open(path)
                    img.thumbnail((700, 525))
                    photo = ImageTk.PhotoImage(img)
                    viewer._photo_refs.append(photo)
                    tk.Label(row, image=photo).pack()
                except Exception as e:
                    tk.Label(row, text=f"Could not load '{path}': {e}",
                             fg="red", wraplength=680).pack()

        root.after(0, build_ui)

    threading.Thread(target=worker, daemon=True).start()


recent_samples_listbox.bind("<Double-Button-1>", open_sample_photo_viewer)

tk.Button(mongo_btn_row, text="Refresh", command=refresh_recent_samples,
          bg="lightblue").pack(side=tk.LEFT, padx=4)

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
# CAMERA TAB — live preview (full-size, not a small sidebar bubble), camera
# selection, and the "Capture Photo" action: grab one frame, save it
# locally, log it to the local MongoDB, and upload it to whatever server
# URL is configured on the "Server" tab.
# =============================================================================
gallery_frame = tk.Frame(tab_camera, bg="#f0f0f0")
gallery_frame.pack(fill=tk.BOTH, expand=1, padx=10, pady=10)

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
tk.Label(gallery_frame, text="Live Camera Feed (USB, all cameras at once)",
         font=("Arial", 11, "bold"), bg="#f0f0f0").pack(pady=(10, 5))

# Every camera in vision/config.py's CAMERAS dict is a plain USB/UVC
# webcam opened by index via OpenCV's cv2.VideoCapture (see
# vision/camera/capture.py) — no vendor SDK needed. Rather than showing
# one camera at a time behind a dropdown, each configured camera gets
# its own panel here and they all stream simultaneously, each on its own
# background thread/tick loop, so e.g. "station" and "wrist" (or any
# additional USB cameras you add to CAMERAS) are all live at once.
_camera_names = list(list_configured_cameras().keys()) or ["station"]

live_feed_panels = {}  # camera name -> {"image_label", "status_label", "busy"}

live_feeds_container = tk.Frame(gallery_frame, bg="#f0f0f0")
live_feeds_container.pack(fill=tk.BOTH, expand=1, padx=4, pady=4)

_feed_cols = 2 if len(_camera_names) > 1 else 1
for _feed_i, _cam_name in enumerate(_camera_names):
    _row, _col = divmod(_feed_i, _feed_cols)
    live_feeds_container.grid_columnconfigure(_col, weight=1)
    live_feeds_container.grid_rowconfigure(_row, weight=1)

    cam_panel = tk.Frame(live_feeds_container, bg="#f0f0f0", bd=1, relief=tk.GROOVE)
    cam_panel.grid(row=_row, column=_col, padx=4, pady=4, sticky="nsew")

    tk.Label(cam_panel, text=_cam_name.title(), font=("Arial", 10, "bold"),
             bg="#f0f0f0").pack()
    _status_lbl = tk.Label(cam_panel, text="Stopped", font=("Arial", 8),
                            bg="#f0f0f0", fg="gray")
    _status_lbl.pack()
    _img_lbl = tk.Label(cam_panel, bg="#222222", width=40, height=15,
                         text="Live feed will appear here", fg="white",
                         justify=tk.CENTER)
    _img_lbl.pack(padx=4, pady=4, fill=tk.BOTH, expand=1)

    live_feed_panels[_cam_name] = {
        "image_label": _img_lbl, "status_label": _status_lbl, "busy": False,
    }

live_feed_button_row = tk.Frame(gallery_frame, bg="#f0f0f0")
live_feed_button_row.pack(pady=4)

live_feed_active = False


def _schedule_next_live_feed_tick(camera_name, delay_ms=None):
    if live_feed_active:
        root.after(delay_ms or int(1000 / LIVE_FEED_FPS),
                   lambda: _live_feed_tick(camera_name))


def _live_feed_tick(camera_name):
    if not live_feed_active:
        return
    panel = live_feed_panels[camera_name]
    if panel["busy"]:
        # Previous tick's camera read hasn't finished yet — skip this
        # tick rather than piling up threads.
        _schedule_next_live_feed_tick(camera_name)
        return

    panel["busy"] = True

    def grab():
        try:
            frame = capture_frame(camera_name)
            rgb = frame_to_rgb(frame)
        except Exception as e:
            msg = str(e)

            def on_error():
                panel["busy"] = False
                panel["status_label"].config(text=f"Error: {msg}", fg="red")
                # A problem with ONE camera (unplugged, busy, etc.) should
                # not stop the others — keep retrying this one on its own
                # (slower) cadence instead of killing every feed.
                _schedule_next_live_feed_tick(camera_name, delay_ms=2000)

            root.after(0, on_error)
            return

        def apply():
            try:
                if _PIL_AVAILABLE:
                    img = Image.fromarray(rgb)
                    img.thumbnail((480, 360))
                    photo = ImageTk.PhotoImage(img)
                    panel["image_label"].image = photo  # keep a reference
                    panel["image_label"].config(image=photo, text="")
                    panel["status_label"].config(text="Live", fg="green")
                else:
                    panel["image_label"].config(
                        text="Pillow not installed.\nRun: pip install Pillow")
            finally:
                panel["busy"] = False
                _schedule_next_live_feed_tick(camera_name)

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
    for _cam_name in _camera_names:
        live_feed_panels[_cam_name]["status_label"].config(text="Starting...", fg="green")
        _schedule_next_live_feed_tick(_cam_name)


def stop_live_feed():
    global live_feed_active
    live_feed_active = False
    for panel in live_feed_panels.values():
        panel["status_label"].config(text="Stopped", fg="gray")


tk.Button(live_feed_button_row, text="Start All Feeds", bg="lightgreen",
          command=start_live_feed).pack(side=tk.LEFT, padx=4)
tk.Button(live_feed_button_row, text="Stop All Feeds",
          command=stop_live_feed).pack(side=tk.LEFT, padx=4)

tk.Label(gallery_frame, text="Capture Photo",
         font=("Arial", 11, "bold"), bg="#f0f0f0").pack(pady=(10, 5))

capture_status_label = tk.Label(gallery_frame, text="", bg="#f0f0f0", fg="gray",
                                 font=("Arial", 9), justify=tk.CENTER, wraplength=520)
capture_status_label.pack(pady=(0, 4))


def capture_photo_and_store():
    """
    One-click multi-camera snapshot (no arm movement, no rotation): grab
    one frame from EVERY USB camera configured in vision/config.py
    CAMERAS at once, save each to disk, log them all under a single
    sample in the local MongoDB (bringing back local-copy storage), and
    — if the server responds — upload each image too, through the same
    /collection/submission + /collection/images/upload endpoints the
    automatic capture sequence uses, against whatever SERVER_URL is
    currently configured on the "Server" tab.
    """
    camera_names = list(live_feed_panels.keys()) or list(list_configured_cameras().keys())
    capture_status_label.config(
        text=f"Capturing from {len(camera_names)} camera(s): {', '.join(camera_names)}...",
        fg="gray")

    def worker():
        sample_id = new_sample_id()
        saved_paths = {}   # camera name -> image path
        cam_errors = {}    # camera name -> error message

        for cam in camera_names:
            try:
                frame = capture_frame(cam)
                saved_paths[cam] = save_image(frame, sample_id, cam, 0)
            except Exception as e:
                cam_errors[cam] = str(e)

        mongo_ok = False
        if saved_paths:
            try:
                mongo_client.save_sample(sample_id, str(date.today()),
                                          {"predicted_label": "manual_snapshot",
                                           "cameras": list(saved_paths.keys()),
                                           "num_images": len(saved_paths)})
                for cam, path in saved_paths.items():
                    mongo_client.save_image_record(new_sample_id(), sample_id, path, cam, 0)
                mongo_ok = True
            except Exception as e:
                print(f"[MONGO] Could not log capture locally: {e}")

        uploaded = 0
        if SERVER_URL and saved_paths:
            try:
                submit_response = requests.post(
                    f"{SERVER_URL}/collection/submission",
                    json={"category": "manual_snapshot", "date": str(date.today()),
                          "data": {"predicted_label": "manual_snapshot",
                                   "num_images": len(saved_paths)}},
                    timeout=5,
                )
                submit_response.raise_for_status()
                remote_sample_id = submit_response.json()["sample_id"]
                for cam, path in saved_paths.items():
                    try:
                        with open(path, "rb") as image_file:
                            upload_response = requests.post(
                                f"{SERVER_URL}/collection/images/upload",
                                files={"file": image_file},
                                data={"sample_id": remote_sample_id, "category": "manual_snapshot"},
                                timeout=10,
                            )
                        upload_response.raise_for_status()
                        uploaded += 1
                    except Exception as e:
                        print(f"[UPLOAD] Could not upload '{cam}' image: {e}")
            except Exception as e:
                print(f"[UPLOAD] Could not create submission on server ({SERVER_URL}): {e}")

        def report():
            parts = [f"Saved {len(saved_paths)}/{len(camera_names)} camera(s)"]
            if cam_errors:
                parts.append("Errors: " + ", ".join(
                    f"{c} ({m})" for c, m in cam_errors.items()))
            parts.append("MongoDB: OK" if mongo_ok else "MongoDB: failed")
            parts.append(f"Upload: {uploaded}/{len(saved_paths)}" if saved_paths
                         else "Upload: skipped")
            color = "green" if (mongo_ok or uploaded) else "orange"
            capture_status_label.config(text=" | ".join(parts), fg=color)
            try:
                refresh_recent_samples()
            except NameError:
                pass  # Database tab not built yet — harmless
        root.after(0, report)

    threading.Thread(target=worker, daemon=True).start()


tk.Button(gallery_frame, text="Capture Photo — All Cameras (Save + Upload)", bg="khaki",
          font=("Arial", 10, "bold"),
          command=capture_photo_and_store).pack(pady=6)

tk.Label(gallery_frame, text="Captures are saved locally, logged to the local\n"
         "MongoDB (browse them on the 'Database' tab), and\n"
         "uploaded to the server URL configured on the\n"
         "'Server' tab.",
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
refresh_recent_samples()

# Start listening for automatic-capture commands published by 4DAI (see
# vision/config.py TOPIC_CAPTURE_COMMAND). This is what lets 4DAI's GUI
# trigger a full "rotate + photograph" sequence with no manual
# button-clicking on the arm side, per the transcript's requirement.
start_capture_command_listener()

# Also start the server-dependent (continuous-sweep) capture listener, on
# its own topic (TOPIC_CAPTURE_COMMAND_SERVER_DEPENDENT) so it doesn't
# collide with the listener above.
start_capture_command_listener_server_dependent()

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



