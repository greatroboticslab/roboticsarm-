import math
import threading
from time import sleep
import time
import os
import shutil
import json
import re
import uuid
from datetime import date, datetime
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox, simpledialog, filedialog

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from dobot_util import Dobot

from vision.config import PHOTO_STATION, NUM_VIEWS, VIEW_SETTLE_SECONDS, LIVE_FEED_FPS
from vision.config import JSON_LOG_DIR, JSON_LOG_FILENAME
from vision.camera.capture import (
    capture_station_frame,
    capture_wrist_frame,
    capture_frame,
    list_configured_cameras,
    frame_to_rgb,
    save_image,
    new_sample_id,
    assign_camera,
    remove_camera_assignment,
    list_camera_indices,
    is_camera_available,
    list_camera_device_names,
    set_flush_stale_frames,
    get_camera_settings,
    set_camera_settings,
    capture_frames_multi,
    probe_camera_modes,
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
from vision.storage import mongo_client, object_catalog, excel_export, json_logger, attribute_schema, session_manager, query_safety, package_export, storage_location
from vision.storage.capture_pipeline import record_capture
from vision.services import rotation_coordinator
from vision.config import DATA_AUTHORITY_MODE

# Optional local-LLM (langchain-mongodb agent toolkit via Ollama) natural-
# language Mongo query — see vision/services/mongo_nlp_agent.py's module
# docstring. Imported here (not inline in the Database tab code) so it's a
# single, obvious place to swap out later. Only used inside the "Database"
# tab's "Ask" box, and only when Ollama + the toolkit are actually
# available — the standard Objects/Images/Inventory browser never touches
# this or any LLM.
from vision.services import mongo_nlp_agent
from vision.config import NLP_AGENT_MODEL, NL_QUERY_FIELD_SAMPLE_SIZE

# [WIRED] Middleman mode (Virtual tab). Business logic
# (networking/protocol/session/photo-transfer) lives entirely in these
# modules — main.py only wires callables into them + reflects status in
# the UI. See each module's docstring for the split.
from vision.net_utils import get_local_ip
from vision.services.middleman_physical_side import PhysicalSideController
from vision.services.middleman_other_side import OtherSideController
from vision.config import MQTT_BROKER_HOST, MQTT_BROKER_PORT
from laser_control import channel_assignments
from vision.services import hard_deck

from laser_control import RelayController
try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def resolve_image_path(path: str) -> str:
    """
    Best-effort resolution for an image path pulled out of Mongo.

    Paths written from this session on are already absolute (see
    vision.camera.capture.save_image / vision.services.photo_transfer.
    save_photo_bundle_files), so this is normally a no-op. It exists for
    situations that otherwise show up as a bare "No such file or
    directory" with no indication of why:

      1. The absolute path was baked in at capture time by a DIFFERENT
         copy of this app than the one running right now — e.g. images
         captured from a previous unzip/clone of this repo, or from
         before vision.storage.storage_location existed, or the storage
         location was reconfigured since. The path's images/... suffix
         is usually still intact even though its prefix no longer
         exists; re-anchor that suffix under the CURRENT storage
         location (vision.storage.storage_location.get_storage_root())
         and check there before giving up. This is the common case in
         practice — it's what makes photos from an earlier session (or
         an earlier delivered zip) keep working after re-extracting a
         fresh copy of the app.
      2. Genuinely old data with a RELATIVE path stored (pre-dates the
         absolute-path fix entirely) — retry it relative to this repo's
         own root as a last resort.
      3. The images/ folder was moved to a different machine entirely
         (e.g. after Export Package + Import Package elsewhere) —
         nothing can fully recover from that automatically, but the
         caller gets back the best-guess path so the resulting error at
         least shows a path that makes it obvious what to check.
    """
    if os.path.isabs(path) and os.path.exists(path):
        return path
    if os.path.exists(path):
        return os.path.abspath(path)

    # Case 1: re-anchor "images/..." under the CURRENT storage location.
    # Handles both Windows (\) and POSIX (/) separators in the stored
    # path, regardless of which platform captured it. The suffix after
    # "images/" already starts with "objects"/"middleman"/"imported" —
    # whichever of the three roots the image actually came from — so
    # rejoining it under get_storage_root() + "images" handles all three
    # in one shot rather than needing a separate branch per root.
    normalized = path.replace("\\", "/")
    marker = "/images/"
    idx = normalized.rfind(marker)
    if idx != -1:
        suffix_parts = normalized[idx + len(marker):].split("/")
        candidate = os.path.join(storage_location.get_storage_root(), "images", *suffix_parts)
        if os.path.exists(candidate):
            return candidate

    # Case 2: legacy relative path, tried against this repo's own root.
    candidate = os.path.join(_REPO_ROOT, path)
    if os.path.exists(candidate):
        return candidate

    return path  # doesn't exist anywhere we know to look — let the caller's
                 # own error handling report exactly what was tried

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
_jog_autocapture_after_id = None   # pending root.after() id for the repeating
                                    # 1s-while-held jog capture tick, or None
_jog_autocapture_sample_id = None  # images/<this>/ folder shared by every
                                    # frame captured during one press-to-release
                                    # jog gesture, or None while not jogging


def current_arm_position() -> dict | None:
    """
    Best-effort snapshot of the arm's live Cartesian pose, for stamping
    onto a capture as its "position" attribute (position_x/y/z/r — see
    vision.storage.attribute_schema and capture_pipeline.
    build_object_data()). Returns None — which capture_pipeline then
    stores as null, per column — whenever a real position genuinely
    isn't available, rather than reporting a stale/meaningless [0,0,0,0]:

      - the robot was never connected this run (ROBOT_CONNECTED False,
        e.g. DEMO MODE or the Remote Control side of a middleman split,
        which has no local arm at all), or
      - feedback_loop() hasn't received its first Port 30004 packet yet
        (robot_data["cartesian"] still at its untouched startup default).

    Called right at the moment a capture is being recorded (see
    run_manual_snapshot() and run_data_collection_rotation_local()) so
    the stored position reflects wherever the arm actually was for that
    specific photo/sequence, not wherever it ends up afterward.
    """
    if not ROBOT_CONNECTED:
        return None
    cartesian = robot_data.get("cartesian")
    if not cartesian or len(cartesian) < 3:
        return None
    if cartesian == [0.0, 0.0, 0.0, 0.0]:
        return None  # indistinguishable from "no packet received yet" — treat as unavailable
    x, y, z = cartesian[0], cartesian[1], cartesian[2]
    r = cartesian[3] if len(cartesian) > 3 else None
    return {"x": x, "y": y, "z": z, "r": r}


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
m_j4 = 0.0
m_claw = 0


def sync_manual_position_from_feedback(reason: str = "") -> None:
    """
    Resyncs the "manual control state" globals (m_x, m_y, m_z, m_j4) to the
    robot's real, live telemetry (robot_data["cartesian"]/["joints"], the
    same 50Hz feed the red tracking dot uses).

    WHY THIS EXISTS: m_x/m_y/m_z/m_j4 only get updated automatically inside
    move_to_point() (used by the click-a-point-on-the-plot flow and the
    queued-points sender). Several other code paths move the arm directly
    via robot.movement.joint_to_joint_move() instead - the automatic
    capture sequence, the pickup+photograph pipeline, remote MQTT move
    commands, and the manual J1/J2/Z/J4 "Move Joints" button - and none of
    those touched these globals at all. Left unsynced, the NEXT manual
    action that reuses them (e.g. "Manual Z", clicking a new point, or
    sending the point queue - all of which used to hardcode J4 back to 0
    regardless of the arm's actual wrist angle) would start from a stale
    pre-move position instead of where the arm actually is now - i.e. the
    position tracking silently drifts out of sync with the real robot and
    subsequent "manual" moves jump/snap to positions and wrist angles that
    were never actually commanded ("making up moves"). This mirrors the
    same fix already applied to jog release (handle_jog_release) so every
    direct-move code path stays consistent.
    """
    global m_x, m_y, m_z, m_j4
    cart = robot_data.get("cartesian")
    if cart and len(cart) >= 3:
        m_x, m_y, m_z = cart[0], cart[1], cart[2]
    joints = robot_data.get("joints")
    if joints and len(joints) >= 4:
        m_j4 = joints[3]
    if reason and (cart or joints):
        print(f"[POSITION SYNC] Manual position resynced after {reason}: "
              f"X={m_x:.1f} Y={m_y:.1f} Z={m_z:.1f} J4={m_j4:.1f}")



# =====================================================================
# AUTO-CAPTURE ON ARM MOVEMENT
# =====================================================================
# Two independent toggles (checkboxes live on the Camera tab, built
# further down):
#   - auto_capture_on_move_var:  every PROGRAMMATIC move (rotation
#     sequences, the pickup+photograph pipeline, remote MQTT move
#     commands, the manual "Move Joints" button — i.e. every direct
#     robot.movement.joint_to_joint_move() call site, all routed through
#     _dispatch_joint_move() below) fires one capture right as the move
#     is issued and a second one 1 second later.
#   - auto_capture_on_jog_var: continuous keyboard/button jogging (see
#     handle_jog_press/handle_jog_release) fires one capture on first
#     press, one on release, and one every 1 second for as long as the
#     key/button stays held.
# Both are deliberately declared as tk.BooleanVar()s further down (after
# `root = tk.Tk()` exists — a Tk Variable needs a live root) but are
# referenced here by name only inside function bodies, which Python
# resolves at CALL time, not at def time, so the forward reference is
# safe: by the time a move/jog actually happens the GUI (and these
# vars) already exist.
#
# Captures taken here are deliberately NOT run through
# run_manual_snapshot()/record_capture() (Mongo write + server upload
# per image): jogging can fire this every second for as long as a key
# is held, and a network round trip per frame would fall further and
# further behind. Instead this is a lightweight, local-disk-only save
# (capture_frame + save_image, same primitives everything else in this
# file uses) — good enough to have a visual record of "what did the
# scene/arm look like around this move", filterable/deletable later by
# its "robot_arm_moving_..."/"manual_jog_moving_..." sample-id prefix,
# without adding load to Mongo or the network on every single move.
# =====================================================================

def capture_movement_snapshot(sample_id: str, label: str) -> None:
    """Grabs one frame from every currently configured camera, saves it
    to disk (via the same capture_frame()/save_image() primitives every
    other capture path uses), AND records it into Mongo/CSV/JSON via
    capture_pipeline.record_capture() — same as a manual snapshot, minus
    the server upload step (record_capture() itself never touches the
    network; upload is a separate step run_manual_snapshot() does after
    it, which this intentionally skips). That keeps this fast enough to
    fire once a second while jogging, while still making these captures
    show up (named/labeled `label`, newest-first sortable) on the
    Database tab like any other capture — a disk-only save with no DB
    record wasn't visible anywhere in the app, which looked like "auto-
    capture isn't doing anything" even though files existed on disk.
    Runs in its own background thread so a slow/unavailable camera never
    blocks the move that triggered it. Best-effort per camera: one
    camera failing (unplugged, busy, etc.) doesn't stop the others."""
    def worker():
        pairs = []
        for cam in list(list_configured_cameras().keys()):
            try:
                for view_index, (suffix, frame) in enumerate(capture_frames_multi(cam)):
                    path = save_image(frame, sample_id, cam, view_index)
                    pairs.append((cam, path))
            except Exception as e:
                print(f"[AUTO CAPTURE] '{cam}' unavailable for {sample_id}: {e}")
        if not pairs:
            return
        try:
            record_capture(
                name=label,
                image_paths_by_source=pairs,
                category="auto_capture",
                position=current_arm_position(),
                export_excel=False,  # this can fire every second while
                                      # jogging — refresh the Excel report
                                      # on a normal manual/pipeline capture
                                      # instead of on every single one of these
            )
        except Exception as e:
            print(f"[AUTO CAPTURE] record_capture failed for {sample_id}: {e}")
    threading.Thread(target=worker, daemon=True).start()


def trigger_arm_move_autocapture(reason: str = "") -> None:
    """Call this right when a programmatic move is ISSUED (not after it
    completes — see _dispatch_joint_move below). Fires one capture now
    and, if the toggle is still on a second from now, one more — both
    saved under the SAME sample_id (images/robot_arm_moving_<ts>/) so
    the two frames from one move event stay grouped together. No-op if
    the "Auto-capture on arm movement" checkbox (Camera tab) is off, or
    before that checkbox has been built yet (startup/demo-mode moves)."""
    try:
        if not auto_capture_on_move_var.get():
            return
    except NameError:
        return  # Camera tab not built yet — nothing to toggle against
    sample_id = f"robot_arm_moving_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    print(f"[AUTO CAPTURE] arm move started ({reason}) — capturing '{sample_id}'")
    capture_movement_snapshot(sample_id, "robot_arm_moving")
    root.after(1000, lambda: capture_movement_snapshot(sample_id, "robot_arm_moving"))


def _dispatch_joint_move(joints: list, reason: str = "arm move"):
    """Single choke point for every DIRECT robot.movement.joint_to_joint_move()
    call in this file (the rotation sequences, the pickup+photograph
    pipeline, remote MQTT moves, and the manual "Move Joints" button —
    see sync_manual_position_from_feedback()'s docstring for the full
    list of call sites, all updated to go through this). Fires the
    "arm moving" auto-capture (see trigger_arm_move_autocapture above)
    right as the move is sent, then issues the move itself — so adding
    any other future cross-cutting move behavior only needs to change
    this one function instead of every call site individually."""
    trigger_arm_move_autocapture(reason)
    return robot.movement.joint_to_joint_move(joints)


def safe_move_to_point(x, y, z=200, r=0):
    """Non-blocking wrapper around move_to_point.
    Runs the move in a background thread so ensure_robot_enabled()'s
    re-enable polling loop never freezes the Tkinter main thread."""
    threading.Thread(target=move_to_point, args=(x, y, z, r), daemon=True).start()

def move_to_point(x, y, z=200, r=0):
    """Returns None on success/demo-mode-simulated, or an error string
    (unreachable target, hard-deck rejection, or a real hardware move
    error) on failure/rejection — callers (point-queue send, the Move
    Joints button, _handle_move_command's remote path indirectly via
    the same hard-deck check) can surface this instead of it being a
    silent failure."""
    global m_x, m_y, m_z, m_j4    # declare global so the assignment below persists
    if is_jogging:
        # messagebox must run on the main thread — schedule it there safely
        root.after(0, lambda: messagebox.showwarning(
            "Robot Busy", "Cannot send move command while jogging!"))
        return "Robot is currently jogging"

    try:
        sols = Ikinematics(x, y, z, r)
        if not sols:
            msg = f"Target ({x}, {y}) is unreachable."
            print(msg)
            return msg

        j1, j2, z_target, r_target = sols[0]

        floor = _effective_hard_deck_z()
        if floor is not None and z_target < floor:
            msg = (f"Move rejected: target Z {z_target:.1f} is below the height floor "
                   f"({floor:.1f}).")
            print(f"[HARD DECK] {msg}")
            return msg

        if ROBOT_CONNECTED and robot:
            # Re-enable only if the robot has fallen out of ENABLE state.
            # On normal operation this is a fast no-op (mode is already 5).

            print(f"Moving to ({x},{y}) | J1={j1:.1f}° J2={j2:.1f}° Z={z_target:.1f}")
            move_error = _dispatch_joint_move([j1, j2, z_target, r_target], reason="move_to_point")
            if move_error is not None:
                print(f"[MOVE ERROR]: {move_error}")
                return str(move_error)   # do not sync position — robot did not move
            print(f"[MOVE SUCCESS]: ({x}, {y}, {z})")

        else:
            print(f"DEMO MODE: J1={j1:.1f}° J2={j2:.1f}° Z={z_target:.1f}")

        # Only reached if the move succeeded (or demo mode) — safe to sync
        m_x, m_y, m_z, m_j4 = x, y, z, r_target
        return None

    except Exception as e:
        msg = f"Robot command failed: {e}"
        print(msg)
        return msg
# --- NEW: Jogging Handlers ---
# --- NEW AREA B: CONTINUOUS JOG HANDLERS ---
# --- REFINED AREA B ---
def _jog_autocapture_tick(sample_id: str) -> None:
    """Reschedules itself every 1s for as long as jogging is still active
    AND the toggle is still on - see _start_jog_autocapture below."""
    global _jog_autocapture_after_id
    if not is_jogging or not auto_capture_on_jog_var.get():
        _jog_autocapture_after_id = None
        return
    capture_movement_snapshot(sample_id, "manual_jog_moving")
    _jog_autocapture_after_id = root.after(1000, lambda: _jog_autocapture_tick(sample_id))


def _start_jog_autocapture() -> None:
    """Call right when a jog actually starts (is_jogging just became
    True). Captures one frame immediately, then kicks off the repeating
    1s tick above for as long as the key/button stays held. No-op if the
    "Auto-capture while jogging" checkbox (Camera tab) is off, or before
    it's been built yet (startup)."""
    global _jog_autocapture_after_id, _jog_autocapture_sample_id
    try:
        if not auto_capture_on_jog_var.get():
            return
    except NameError:
        return  # Camera tab not built yet
    _jog_autocapture_sample_id = f"manual_jog_moving_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    print(f"[AUTO CAPTURE] jog started — capturing '{_jog_autocapture_sample_id}'")
    capture_movement_snapshot(_jog_autocapture_sample_id, "manual_jog_moving")
    _jog_autocapture_after_id = root.after(1000, lambda: _jog_autocapture_tick(_jog_autocapture_sample_id))


def _stop_jog_autocapture() -> None:
    """Call right when a jog ends (release, or any early-return path that
    stops it). Cancels the pending repeating tick and takes one final
    capture under the same sample_id the press/hold captures used, so a
    press-hold-release gesture's photos all land in one folder."""
    global _jog_autocapture_after_id, _jog_autocapture_sample_id
    if _jog_autocapture_after_id is not None:
        try:
            root.after_cancel(_jog_autocapture_after_id)
        except Exception:
            pass
        _jog_autocapture_after_id = None
    if _jog_autocapture_sample_id is not None:
        try:
            if auto_capture_on_jog_var.get():
                capture_movement_snapshot(_jog_autocapture_sample_id, "manual_jog_moving")
        except NameError:
            pass
        _jog_autocapture_sample_id = None


def handle_jog_press(axis_cmd, _via_remote=False):
    global is_jogging

    mode = control_mode_var.get()

    if not _via_remote:
        if mode == "middleman_other":
            # Redirect to the connected Physical Side instead of local
            # hardware. send_move() itself refuses (returns False) unless
            # this instance is currently the active controller.
            if other_side_controller is not None:
                other_side_controller.send_move({"jog": axis_cmd})
            return

        if mode == "middleman_physical" and _physical_side_locked_out():
            return  # a remote controller is currently active — local jog locked out

    # Check 1: Is robot actually connected?
    # Check 2: Are we already jogging? (Prevents Windows key-repeat spam)
    # Check 3: Is an automated sequence (rotation capture, pickup+photograph,
    # point queue, ...) currently driving the arm? arm_operation_lock is held
    # for the duration of those, so a stray jog key during one doesn't
    # collide with the sequence's own moves. (The socket-level lock in
    # dobot_util/util.py already stops any concurrent access from corrupting
    # command/response parsing, but jogging mid-sequence would still send the
    # arm somewhere the sequence doesn't expect - so it's blocked here too.)
    if not ROBOT_CONNECTED or is_jogging or not manual_active.get() \
            or arm_operation_lock.locked():
        return
        
    # Get current joints from the background thread's latest data
    current_j = robot_data["joints"]
    
    # Send the safe command
    error = robot.movement.safe_move_jog(axis_cmd, current_j)
    
    if not error:
        is_jogging = True
        _start_jog_autocapture()

def handle_jog_release(event, _via_remote=False):
    global is_jogging, m_x, m_y, m_z, m_j4

    mode = control_mode_var.get()

    if not _via_remote:
        if mode == "middleman_other":
            if other_side_controller is not None:
                other_side_controller.send_move({"jog": "stop"})
            return

        if mode == "middleman_physical" and _physical_side_locked_out():
            return

    if ROBOT_CONNECTED:
        robot.movement.safe_move_jog("stop", [])
        is_jogging = False
        _stop_jog_autocapture()

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
        joints = robot_data.get("joints")
        if joints and len(joints) >= 4:
            m_j4 = joints[3]



def handle_manual_z(dz):
    """Increments Z using the last known tracked position. Simple and safe."""
    if not manual_active.get():
        return
    global m_x, m_y, m_z
    m_z = max(5.0, min(245.0, m_z + dz))
    print(f"Manual Z: moving to Z={m_z:.1f}")
    # BUGFIX: previously called safe_move_to_point(m_x, m_y, m_z) with no
    # 4th argument, which defaults to r=0 — meaning every single Z nudge
    # silently snapped J4 back to 0, regardless of where the wrist
    # actually was (set via the Move Joints row, a queued point, jogging,
    # etc). Passing m_j4 now holds whatever J4 is currently tracked
    # instead of clobbering it.
    safe_move_to_point(m_x, m_y, m_z, m_j4)

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
valid_points = []  # Will store tuples of (px, py, z, claw_state, j4)
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
#   Tab 1 "Arm"       — nothing but driving the physical robot directly:
#                       the height floor / hard deck, plot, manual/joint
#                       controls, the point queue, and sending instructions
#                       to the arm. No control-mode/Middleman UI here — see
#                       "Virtual".
#   Tab 2 "Virtual"   — Control Mode (Demo / Physical Manual /
#                       Middleman (Robot Side) / Remote Control) and every
#                       Middleman-specific panel (remote queue, discovery/
#                       connect dropdown, remote capture, remote laser
#                       toggles). Split out of the Arm tab so "driving this
#                       machine directly" and "being/using a remote
#                       controller" aren't competing for the same screen.
#   Tab 3 "Camera"    — the working "Capture Photo" flow (save + log to
#                       MongoDB + upload) at the top, live webcam view, and
#                       camera selection. No laser controls here — see
#                       "Laser".
#   Tab 4 "Laser"     — the ESP32 laser controller: connect/disconnect,
#                       PWM configure/arm/fire, and the per-channel relay
#                       toggles. Split out of "Camera" so it reads as its
#                       own subsystem rather than a camera accessory.
#   Tab 5 "Server"    — the server URL (test-local by default), connection
#                       testing, and the server-triggered continuous-sweep
#                       automation.
#   Tab 6 "Database"  — a browser for what's stored in the local MongoDB
#                       (kept separate from "Server" so either can grow
#                       independently later).
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

def make_scrollable_tab(page: tk.Frame) -> tk.Frame:
    """
    Wraps a notebook page in a canvas + BOTH scrollbars (vertical for
    tabs that have grown taller than the window, horizontal for wide
    rows — e.g. the Cleanup/Merge forms' side-by-side fields) and
    returns an inner Frame to actually build tab content in.

    Every `tab_xxx` variable is reassigned to this inner frame (not the
    raw notebook page — see right after this function's call sites
    below) so every existing `tk.Whatever(tab_xxx, ...)` call site
    elsewhere in this file automatically becomes scrollable with zero
    other changes needed; the notebook page itself (`tab_xxx_page`) is
    still what gets passed to notebook.add()/notebook.select(), since
    those need the actual registered tab widget, not its inner content
    frame.

    The inner frame's width AND height each track whichever is LARGER —
    the visible canvas dimension, or the content's own natural size —
    so a tab whose content comfortably fits still looks normal
    (children packed with fill=tk.BOTH/expand=1 stretch to fill the
    full visible area, same as before this wrapper existed — this is
    what keeps e.g. the Camera tab's live-feed panels from getting
    vertically squashed down to their bare minimum height), while
    content that's genuinely bigger than the window in either
    direction becomes scrollable instead of being clipped.
    """
    canvas = tk.Canvas(page, highlightthickness=0)
    vbar = tk.Scrollbar(page, orient=tk.VERTICAL, command=canvas.yview)
    hbar = tk.Scrollbar(page, orient=tk.HORIZONTAL, command=canvas.xview)
    inner = tk.Frame(canvas)

    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)

    def _sync_scrollregion(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _sync_inner_size(event):
        canvas.itemconfig(
            window_id,
            width=max(event.width, inner.winfo_reqwidth()),
            height=max(event.height, inner.winfo_reqheight()),
        )

    inner.bind("<Configure>", _sync_scrollregion)
    canvas.bind("<Configure>", _sync_inner_size)

    canvas.grid(row=0, column=0, sticky="nsew")
    vbar.grid(row=0, column=1, sticky="ns")
    hbar.grid(row=1, column=0, sticky="ew")
    page.grid_rowconfigure(0, weight=1)
    page.grid_columnconfigure(0, weight=1)

    # Mouse wheel scrolling — vertical by default, Shift+wheel for
    # horizontal — bound/unbound as the pointer enters/leaves THIS
    # tab's canvas specifically, so it doesn't fight with whichever tab
    # is active and doesn't hijack wheel events meant for a nested
    # Listbox/Text/Canvas (Tk still dispatches to the widget directly
    # under the cursor first). <Button-4>/<Button-5> cover Linux, which
    # doesn't send <MouseWheel> at all.
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_shift_mousewheel(event):
        canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_enter(_event):
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Shift-MouseWheel>", _on_shift_mousewheel)
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

    def _on_leave(_event):
        canvas.unbind_all("<MouseWheel>")
        canvas.unbind_all("<Shift-MouseWheel>")
        canvas.unbind_all("<Button-4>")
        canvas.unbind_all("<Button-5>")

    canvas.bind("<Enter>", _on_enter)
    canvas.bind("<Leave>", _on_leave)

    return inner


tab_arm_page = tk.Frame(notebook)
tab_virtual_page = tk.Frame(notebook)
tab_camera_page = tk.Frame(notebook)
tab_laser_page = tk.Frame(notebook)
tab_server_page = tk.Frame(notebook)
tab_database_page = tk.Frame(notebook)
tab_data_collection_page = tk.Frame(notebook)
tab_sync_storage_page = tk.Frame(notebook)

# NOTE: widgets added to a Notebook via notebook.add(...) are already
# geometry-managed BY the notebook — do not also call .pack()/.grid() on
# these frames themselves. Doing so previously fought with the
# notebook's own show/hide-per-tab logic and made every tab's content
# render all at once regardless of which tab was selected, which is why
# clicking between tabs looked like it wasn't doing anything.
notebook.add(tab_arm_page, text="Arm")
notebook.add(tab_virtual_page, text="Virtual")
notebook.add(tab_camera_page, text="Camera")
notebook.add(tab_laser_page, text="Laser")
notebook.add(tab_server_page, text="Server")
notebook.add(tab_database_page, text="Database")
notebook.add(tab_data_collection_page, text="Data Collection")
notebook.add(tab_sync_storage_page, text="Sync & Storage")

# Always boot straight into the Arm tab (demo mode banner and all),
# regardless of insertion order above.
notebook.select(tab_arm_page)

# Every tab wrapped in a scroll-in-both-directions canvas (see
# make_scrollable_tab() above) — tab_arm/tab_virtual/etc. below now
# refer to each page's INNER scrollable frame, not the raw notebook
# page, so every `tk.Whatever(tab_arm, ...)` call site further down in
# this file automatically becomes scrollable with no other changes.
tab_arm = make_scrollable_tab(tab_arm_page)
tab_virtual = make_scrollable_tab(tab_virtual_page)
tab_camera = make_scrollable_tab(tab_camera_page)
tab_laser = make_scrollable_tab(tab_laser_page)
tab_server = make_scrollable_tab(tab_server_page)
tab_database = make_scrollable_tab(tab_database_page)
tab_data_collection = make_scrollable_tab(tab_data_collection_page)
tab_sync_storage = make_scrollable_tab(tab_sync_storage_page)

# Tab 1: Arm — everything below that packs into main_container/
# left_container/frame ends up on this tab only. Control Mode/Middleman
# UI is parented on tab_virtual instead (see below) so it lands on the
# "Virtual" tab.
main_container = tab_arm

# =====================================================================
# [WIRED] CONTROL MODE — Demo / Physical Manual / Middleman (Physical
# Side) / Middleman (Other Side). See vision/services/middleman_*.py
# for the actual networking/protocol logic — this section is UI +
# thin glue only, per the "reduce creep" split agreed on.
#
# Default on startup (never auto-selected for either Middleman mode):
#   no robot detected -> Demo
#   robot connected    -> Physical Manual
# =====================================================================
control_mode_var = tk.StringVar(value="physical_manual" if ROBOT_CONNECTED else "demo")

tk.Label(tab_virtual, text="Virtual / Remote Control", font=("Arial", 12, "bold")).pack(pady=(10, 0))
tk.Label(tab_virtual, text="Choose how this machine is driven: purely local (Demo /\n"
         "Physical Manual — see the 'Arm' tab for jogging and the point\n"
         "queue) or over the network via Middleman: Middleman (Robot Side)\n"
         "if this machine IS the robot being remote-driven, or Remote\n"
         "Control if this machine is doing the driving.",
         font=("Arial", 8), fg="gray", justify=tk.CENTER).pack(pady=(2, 8))

# Parented on tab_virtual (not main_container/tab_arm) — Control Mode and
# every Middleman panel live on the "Virtual" tab, separate from the Arm
# tab's direct hard-deck/point-control UI.
control_mode_frame = tk.LabelFrame(tab_virtual, text=" Control Mode ", padx=10, pady=8)
control_mode_frame.pack(fill=tk.X, padx=10, pady=(8, 0))

control_mode_radio_row = tk.Frame(control_mode_frame)
control_mode_radio_row.pack(fill=tk.X)

# Display labels only — the underlying mode values ("middleman_physical",
# "middleman_other") and all the Python identifiers/module names built on
# them (physical_side_controller, middleman_physical_side.py, etc.) are
# left as-is; only what's shown on screen changed:
#   "Middleman — Physical Side"  -> "Middleman (Robot Side)"
#   "Middleman — Other Side"     -> "Remote Control"
for _mode_value, _mode_label in [
    ("demo", "Demo"),
    ("physical_manual", "Physical Manual"),
    ("middleman_physical", "Middleman (Robot Side)"),
    ("middleman_other", "Remote Control"),
]:
    tk.Radiobutton(control_mode_radio_row, text=_mode_label, variable=control_mode_var,
                   value=_mode_value, command=lambda: on_control_mode_changed(),
                   font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 12))

control_mode_status_label = tk.Label(control_mode_frame, text="", fg="gray",
                                      font=("Arial", 9), justify=tk.LEFT, wraplength=760)
control_mode_status_label.pack(anchor=tk.W, pady=(4, 0))

# Always-visible connectivity info: this machine's own IP (relevant if
# it ends up running Middleman — Physical Side) and the MQTT broker
# it's actually talking through (nothing connects to the IP directly —
# see vision/net_utils.py's docstring).
_this_machine_ip = get_local_ip(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
tk.Label(control_mode_frame,
         text=f"This machine's IP: {_this_machine_ip}   |   MQTT broker: {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}",
         fg="gray", font=("Arial", 8)).pack(anchor=tk.W, pady=(2, 0))

# =====================================================================
# [WIRED] HEIGHT FLOOR ("HARD DECK") — a Z value the end-effector is
# never allowed to move below, local-only settable (never remotely),
# enforced inside move_to_point() and _handle_move_command() (the two
# funnels every planned/absolute move already goes through), plus a
# reactive jog watchdog in update_gui_from_feedback() for continuous
# jogging, which has no single predictable target to check in advance.
#
# Two tiers, per the agreed design:
#   Base floor   - always enforced, every mode.
#   Remote floor - optional, must be >= base floor. Enforced ADDITIONALLY
#                  (as the stricter of the two) only while a Middleman —
#                  Physical Side controller is actively driving this
#                  machine. A remote-initiated move that violates
#                  whichever floor is in effect is rejected AND an
#                  explicit error is sent back to the controller that
#                  sent it (see middleman_physical_side.py's
#                  _publish_error) — never just silently ignored.
# =====================================================================
hard_deck_state = hard_deck.load()  # {"base_hard_deck_z": float|None, "remote_hard_deck_z": float|None}
_HARD_DECK_STEP = 1.0  # matches "raise/lower by 1 at a time" for calibration


def _being_remote_controlled() -> bool:
    """True while this machine is Middleman — Physical Side AND a
    remote controller is currently active (not just queued/idle)."""
    return (control_mode_var.get() == "middleman_physical"
            and physical_side_controller is not None
            and physical_side_controller.queue.snapshot()["active"] is not None)


def _effective_hard_deck_z():
    """Returns the currently-enforced floor, or None if no floor is set
    at all. remote_hard_deck_z (if set) only applies while actually
    being remote-controlled, and only ever as the stricter of the two
    (it's validated >= base at set-time, so this is just "prefer it
    when active")."""
    base = hard_deck_state.get("base_hard_deck_z")
    remote = hard_deck_state.get("remote_hard_deck_z")
    if _being_remote_controlled() and remote is not None:
        return remote
    return base


J4_SAFE_LIMITS = (-358.0, 358.0)  # matches dobot_util's Movement.SAFE_LIMITS["J4"]


def _normalize_j4_target(j4_target: float) -> float:
    """
    BUGFIX (root cause of rotation-capture moves "returning a -1"):
    every rotation loop (run_automatic_capture_sequence,
    run_data_collection_rotation_local, and the server-dependent pickup
    pipeline) computes each step as `base_j4 + i * degrees_per_step` with
    no bounds check at all, unlike the manual "Move Joints" row (which
    validates against J4_SAFE_LIMITS and shows a clear "Out of Range"
    dialog *before* ever contacting the robot - see manual_joint_move()).
    A rotation sweep of more than a few steps, or one that starts from a
    J4 already partway toward a limit, very quickly commands a J4 target
    outside +/-358 degrees. The robot firmware rejects that JointMovJ
    with its own error code, but that code isn't one of the ones
    dobot_util/types.py's DobotError enum knows about, so
    _parse_response()'s fallback (see dobot_util/util.py) reports it as
    the generic DobotError.FAIL_TO_GET - which prints/displays as a bare
    "-1" with no indication the real problem was simply an out-of-range
    wrist angle.

    Fix: since a wrist rotation of exactly 360 degrees returns the joint
    to the same physical orientation, any target outside the safe range
    has an equivalent, in-range target reached by adding/subtracting
    whole 360-degree turns - so wrap it back into range here instead of
    sending an invalid command and finding out only via a cryptic -1.
    """
    lo, hi = J4_SAFE_LIMITS
    span = hi - lo  # 716 degrees - the joint's full mechanical travel
    if span <= 0:
        return j4_target
    while j4_target > hi:
        j4_target -= 360.0
    while j4_target < lo:
        j4_target += 360.0
    # Should be unreachable given the joint's >360-degree travel (any
    # angle has an equivalent within one 360-degree turn of center), but
    # guard anyway rather than silently sending something still invalid.
    if not (lo <= j4_target <= hi):
        j4_target = max(lo, min(hi, j4_target))
    return j4_target


def _hard_deck_violation(z_value, context: str):
    """Shared check for every direct robot.movement.joint_to_joint_move
    call site in the file (the pipeline sequences and the raw joint-space
    Move Joints panel bypass move_to_point()/_handle_move_command()'s own
    checks entirely, so they need this called explicitly before each
    move). Returns an error string if z_value violates the effective
    floor, else None."""
    floor = _effective_hard_deck_z()
    if floor is not None and z_value < floor:
        msg = f"{context}: target Z {z_value:.1f} is below the height floor ({floor:.1f})."
        print(f"[HARD DECK] {msg}")
        return msg
    return None


hard_deck_frame = tk.LabelFrame(main_container, text=" Height Floor / Hard Deck (local only) ", padx=10, pady=8)
hard_deck_frame.pack(fill=tk.X, padx=10, pady=(8, 0))

tk.Label(hard_deck_frame,
         text="Jog Z close to the surface, then \"Set Base Floor\" to lock in the current height as a "
              "limit no move can go below. Optionally set a stricter Remote Floor used only while "
              "being remote-controlled via Middleman.",
         fg="gray", font=("Arial", 8), wraplength=760, justify=tk.LEFT).pack(anchor=tk.W)

hard_deck_status_label = tk.Label(hard_deck_frame, text="", font=("Arial", 9))
hard_deck_status_label.pack(anchor=tk.W, pady=(4, 4))


def _refresh_hard_deck_status_label():
    base = hard_deck_state.get("base_hard_deck_z")
    remote = hard_deck_state.get("remote_hard_deck_z")
    base_text = f"{base:.1f}" if base is not None else "not set"
    remote_text = f"{remote:.1f}" if remote is not None else "not set (uses base)"
    hard_deck_status_label.config(text=f"Base floor: {base_text}    |    Remote floor: {remote_text}")


_refresh_hard_deck_status_label()

hard_deck_nudge_row = tk.Frame(hard_deck_frame)
hard_deck_nudge_row.pack(anchor=tk.W)


def _hard_deck_nudge_z(delta: float):
    """Small step move on Z only, holding current X/Y/J4 — the
    calibration workflow: nudge down/up by _HARD_DECK_STEP at a time
    until close to the real floor, then lock it in below."""
    if not ROBOT_CONNECTED:
        messagebox.showwarning("No Robot", "Connect the robot first to calibrate a height floor.")
        return
    target_z = m_z + delta
    error = move_to_point(m_x, m_y, target_z, m_j4)
    if error:
        messagebox.showwarning("Move Rejected", error)


tk.Button(hard_deck_nudge_row, text=f"Z \u2212{_HARD_DECK_STEP:g}", width=6,
          command=lambda: _hard_deck_nudge_z(-_HARD_DECK_STEP)).pack(side=tk.LEFT, padx=2)
tk.Button(hard_deck_nudge_row, text=f"Z +{_HARD_DECK_STEP:g}", width=6,
          command=lambda: _hard_deck_nudge_z(_HARD_DECK_STEP)).pack(side=tk.LEFT, padx=2)

hard_deck_buttons_row = tk.Frame(hard_deck_frame)
hard_deck_buttons_row.pack(anchor=tk.W, pady=(6, 0))


def _set_hard_deck(tier: str):
    """tier is 'base' or 'remote'. Reads the CURRENT live Z (not a typed
    value) so the floor always matches a position the robot has
    actually reached, per the described calibration workflow."""
    if not ROBOT_CONNECTED or "cartesian" not in robot_data or robot_data["cartesian"] is None:
        messagebox.showwarning("No Robot", "Connect the robot first to calibrate a height floor.")
        return
    current_z = robot_data["cartesian"][2]

    base = hard_deck_state.get("base_hard_deck_z")
    remote = hard_deck_state.get("remote_hard_deck_z")

    if tier == "base":
        if remote is not None and current_z > remote:
            messagebox.showwarning(
                "Base Floor Too High",
                f"Base floor ({current_z:.1f}) would be above the existing remote floor "
                f"({remote:.1f}) \u2014 remote floor must always be \u2265 base floor. "
                f"Clear or raise the remote floor first.")
            return
        base = current_z
    else:
        if base is not None and current_z < base:
            messagebox.showwarning(
                "Remote Floor Too Low",
                f"Remote floor must be \u2265 the base floor ({base:.1f}). "
                f"Current position ({current_z:.1f}) is below that.")
            return
        remote = current_z

    hard_deck_state["base_hard_deck_z"] = base
    hard_deck_state["remote_hard_deck_z"] = remote
    hard_deck.save(base, remote)
    _refresh_hard_deck_status_label()


def _clear_hard_deck(tier: str):
    if not messagebox.askyesno("Clear Floor", f"Clear the {tier} height floor? This removes a safety limit."):
        return
    if tier == "base":
        hard_deck_state["base_hard_deck_z"] = None
    else:
        hard_deck_state["remote_hard_deck_z"] = None
    hard_deck.save(hard_deck_state["base_hard_deck_z"], hard_deck_state["remote_hard_deck_z"])
    _refresh_hard_deck_status_label()


tk.Button(hard_deck_buttons_row, text="Set Base Floor \u2190 Current Z", bg="lightblue",
          command=lambda: _set_hard_deck("base")).pack(side=tk.LEFT, padx=2)
tk.Button(hard_deck_buttons_row, text="Clear Base", bg="salmon",
          command=lambda: _clear_hard_deck("base")).pack(side=tk.LEFT, padx=2)
tk.Button(hard_deck_buttons_row, text="Set Remote Floor \u2190 Current Z", bg="lightblue",
          command=lambda: _set_hard_deck("remote")).pack(side=tk.LEFT, padx=(16, 2))
tk.Button(hard_deck_buttons_row, text="Clear Remote", bg="salmon",
          command=lambda: _clear_hard_deck("remote")).pack(side=tk.LEFT, padx=2)

# --- Middleman (Robot Side) controls (shown/used only in that mode) ---
middleman_physical_frame = tk.Frame(control_mode_frame)
middleman_physical_queue_label = tk.Label(middleman_physical_frame, text="", fg="gray",
                                           font=("Arial", 8), justify=tk.LEFT, wraplength=760)
middleman_physical_queue_label.pack(anchor=tk.W)
middleman_physical_disconnect_all_btn = tk.Button(
    middleman_physical_frame, text="Disconnect All / Clear Queue", bg="salmon")
middleman_physical_disconnect_all_btn.pack(anchor=tk.W, pady=(2, 0))

# --- Remote Control controls (shown/used only in that mode) ---
middleman_other_frame = tk.Frame(control_mode_frame)

middleman_other_top_row = tk.Frame(middleman_other_frame)
middleman_other_top_row.pack(fill=tk.X)
tk.Label(middleman_other_top_row, text="Robot Side:", font=("Arial", 9)).pack(side=tk.LEFT)
middleman_other_selected_ip = tk.StringVar(value="")
middleman_other_dropdown = ttk.Combobox(middleman_other_top_row, textvariable=middleman_other_selected_ip,
                                         width=32, state="readonly")
middleman_other_dropdown.pack(side=tk.LEFT, padx=4)
middleman_other_connect_btn = tk.Button(middleman_other_top_row, text="Connect")
middleman_other_connect_btn.pack(side=tk.LEFT, padx=4)
middleman_other_disconnect_btn = tk.Button(middleman_other_top_row, text="Disconnect")
middleman_other_disconnect_btn.pack(side=tk.LEFT, padx=4)
middleman_other_capture_btn = tk.Button(middleman_other_top_row, text="Capture Now", bg="khaki")
middleman_other_capture_btn.pack(side=tk.LEFT, padx=4)

# Per-channel laser toggles — this rig's lasers are 4 individually
# relay-switched outputs (see the Laser Channels panel on the Physical
# Side machine), not one on/off laser, so this mirrors that instead of
# a single Laser On/Off pair. Names here are generic ("Ch 1".."Ch 4")
# since the Other Side has no visibility into the Physical Side's local
# channel names — only the physical machine (which configured them)
# knows that mapping.
middleman_other_laser_row = tk.Frame(middleman_other_frame)
middleman_other_laser_row.pack(fill=tk.X, pady=(4, 0))
tk.Label(middleman_other_laser_row, text="Lasers:", font=("Arial", 9)).pack(side=tk.LEFT)
middleman_other_laser_buttons = {}  # channel(1-4) -> (on_btn, off_btn)
for _ch in range(1, 5):
    tk.Label(middleman_other_laser_row, text=f"Ch{_ch}", font=("Arial", 8)).pack(side=tk.LEFT, padx=(8, 2))
    _on_btn = tk.Button(middleman_other_laser_row, text="ON", bg="lightgreen", width=4)
    _on_btn.pack(side=tk.LEFT)
    _off_btn = tk.Button(middleman_other_laser_row, text="OFF", bg="salmon", width=4)
    _off_btn.pack(side=tk.LEFT, padx=(0, 2))
    middleman_other_laser_buttons[_ch] = (_on_btn, _off_btn)

# -----------------------------------------------------------------------
# Middleman glue. All actual networking/protocol logic lives in
# vision/services/middleman_physical_side.py and middleman_other_side.py
# — everything below is: (a) small executor callables that wrap this
# machine's already-existing hardware calls, and (b) UI plumbing.
#
# NOTE on scope: this wires jog moves, laser on/off, and single-shot
# "capture all configured cameras at current position" through the
# middleman link (the primitives explicitly agreed on). The point-queue
# ("Add Point" -> batch "Send to robot") flow is NOT redirected here —
# it only queues points locally regardless of mode; extending it to
# middleman is a natural follow-up but wasn't part of this pass.
# -----------------------------------------------------------------------
physical_side_controller = None   # PhysicalSideController, once this mode is entered
other_side_controller = None      # OtherSideController, once this mode is entered
middleman_other_dropdown_ip_by_label = {}  # display label -> physical_side_ip


def _physical_side_locked_out() -> bool:
    """True while a remote controller is actively driving this machine
    in Middleman — Physical Side mode (local jog/manual is blocked)."""
    if physical_side_controller is None:
        return False
    return physical_side_controller.queue.snapshot()["active"] is not None


def _middleman_laser_executor(channel, state: bool) -> None:
    """Laser executor handed to PhysicalSideController. Per the board
    photos, this rig's lasers are relay-switched (individually
    configured on the "Laser Channels" panel on the "Laser" tab, using
    laser_ctl's generic multi-channel CONFIG/SET commands) — not one
    PWM-dimmable laser, so this is channel-addressed rather than a
    single on/off. channel=None means "all channels currently
    configured" (used by the timeout-safety path, which needs to kill
    every laser at once, not guess which one channel was actually in
    use).

    Requires the channels to already be configured locally first (on
    the "Laser" tab's laser_channels_frame) — this cannot configure
    them remotely, same as the single-laser panel it replaces for this
    hardware."""
    if laser_ctl is None:
        print("[MIDDLEMAN] Laser command ignored — ESP32 not connected locally on this Physical Side.")
        return
    if channel is not None:
        channels = [int(channel)]
    else:
        channels = [int(ch_str) for ch_str in laser_channel_assignments.keys()]
    if not channels:
        print("[MIDDLEMAN] Laser command ignored — no laser channels configured locally yet.")
        return
    for ch in channels:
        laser_ctl.set_channel(ch, state)


def _middleman_capture_executor(object_id: str = None):
    """Capture executor handed to PhysicalSideController: grabs one frame
    from every locally-configured camera at the arm's current position.
    Mirrors the existing 'Capture Photo — All Cameras' flow, minus the
    local save/upload (photo_transfer.py handles relaying + the Other
    Side's local save instead).

    object_id: passed straight through from the incoming capture-request
    payload (see PhysicalSideController's docstring). None = an ad hoc
    single "Capture Now" press, mint a fresh id as before. A real value
    means this is one step of a rotation sequence the Other Side is
    coordinating — reuse it as sample_id so every step's bundle carries
    the same id and the Other Side's rotation_coordinator groups them
    into one object instead of each becoming its own.
    """
    sample_id = object_id or new_sample_id()
    frames = []
    for camera_name in list_configured_cameras():
        try:
            frame = capture_frame(camera_name)
            frames.append((camera_name, 0, frame))
        except Exception as e:
            print(f"[MIDDLEMAN] Capture failed for camera '{camera_name}': {e}")
    values = {"num_images": len(frames)}
    return sample_id, frames, values


def _middleman_telemetry_provider() -> dict:
    return {
        "robot_connected": ROBOT_CONNECTED,
        "joints": robot_data.get("joints"),
        "cartesian": robot_data.get("cartesian"),
    }


def _on_physical_control_status_change(status: dict) -> None:
    def apply():
        active = status.get("active")
        queue = status.get("queue", [])
        queue_text = f"Active controller: {active or 'none'}"
        if queue:
            queue_text += f"  |  Queued: {', '.join(queue)}"
        middleman_physical_queue_label.config(text=queue_text)
        if control_mode_var.get() == "middleman_physical" and physical_side_controller is not None:
            lock_note = (" Local controls locked out while a remote controller is active."
                         if active else " No remote controller — local controls available.")
            control_mode_status_label.config(
                text=f"Middleman (Robot Side). Listening as {physical_side_controller.ip}.{lock_note}",
                fg="blue")
    root.after(0, apply)


def _on_discovery_update(discovered: dict) -> None:
    def apply():
        middleman_other_dropdown_ip_by_label.clear()
        labels = []
        for ip, info in sorted(discovered.items()):
            label = f"{info.get('name', '?')} ({ip})"
            middleman_other_dropdown_ip_by_label[label] = ip
            labels.append(label)
        middleman_other_dropdown["values"] = labels
    root.after(0, apply)


def _on_telemetry_update(data: dict) -> None:
    """Mirrors the Physical Side's live position onto this machine's own
    plot while in Remote Control mode (internal mode value:
    middleman_other). Reuses the exact same dot + blitting path as the
    local telemetry loop (update_gui_from_feedback, see the tracking-lag
    fix) rather than a separate slower
    code path \u2014 live_dot is otherwise idle on a controller-only machine
    since update_gui_from_feedback only drives it from LOCAL robot
    telemetry, gated on this machine's own ROBOT_CONNECTED."""
    def apply():
        global live_dot
        if control_mode_var.get() != "middleman_other":
            return  # stale telemetry from a since-abandoned connection

        cartesian = data.get("cartesian")
        if not cartesian:
            return
        try:
            raw_x, raw_y, raw_z = cartesian[0], cartesian[1], cartesian[2]
        except (TypeError, IndexError, KeyError):
            return

        angle_deg = 90  # same rotation as update_gui_from_feedback, for a
                         # display that matches what Physical Manual mode
                         # would show for the same real position
        theta = np.radians(angle_deg)
        rot_x = raw_x * np.cos(theta) - raw_y * np.sin(theta)
        rot_y = raw_x * np.sin(theta) + raw_y * np.cos(theta)

        if live_dot is None:
            live_dot = ax.scatter(rot_x, rot_y, color='red', s=100, zorder=5, label="Live Robot Pos")
            ax.legend()
            canvas.draw()  # establishes the legend; also refreshes _plot_background
        else:
            live_dot.set_offsets(np.c_[rot_x, rot_y])
            if _plot_background[0] is not None:
                fig.canvas.restore_region(_plot_background[0])
                ax.draw_artist(live_dot)
                fig.canvas.blit(ax.bbox)
            else:
                fig.canvas.draw_idle()

        if 'status_label' in globals() and status_label.winfo_exists():
            remote_connected = data.get("robot_connected", False)
            conn_note = "remote robot connected" if remote_connected else "remote in demo / no robot"
            status_label.config(
                text=f"Middleman remote | X: {raw_x:.1f} | Y: {raw_y:.1f} | Z: {raw_z:.1f} ({conn_note})")

    root.after(0, apply)


def _on_other_control_status_update(status: dict) -> None:
    def apply():
        if other_side_controller is None or control_mode_var.get() != "middleman_other":
            return
        active = status.get("active")
        is_me = active == other_side_controller.controller_id
        role_text = "You are the ACTIVE controller." if is_me else f"Waiting in queue (active: {active or 'none'})."
        control_mode_status_label.config(
            text=f"Remote Control, connected to {middleman_other_selected_ip.get()}. {role_text}",
            fg="blue")
    root.after(0, apply)


def _on_photo_received(saved_paths: list) -> None:
    def apply():
        current = control_mode_status_label.cget("text")
        control_mode_status_label.config(text=f"{current}  |  Received {len(saved_paths)} photo(s), saved locally.")
        try:
            refresh_objects_list()
            refresh_images_list()
        except NameError:
            pass  # Database tab not built yet — harmless
    root.after(0, apply)


def _on_middleman_error_received(message: str) -> None:
    """A command this instance sent (almost always a move, most notably a
    hard-deck rejection) was refused by the Physical Side. Shown as a
    popup rather than just a status-line update — a rejected move is
    safety-relevant and easy to miss as passive text, same reasoning as
    the existing 'Robot Busy' warning elsewhere in this file."""
    def apply():
        messagebox.showwarning("Command Rejected by Robot", message)
    root.after(0, apply)


def _middleman_other_connect():
    ip = middleman_other_dropdown_ip_by_label.get(middleman_other_selected_ip.get())
    if not ip:
        messagebox.showwarning("No robot selected", "Pick a robot (Robot Side) from the dropdown first.")
        return
    if other_side_controller is not None:
        other_side_controller.connect(ip)


def _middleman_other_disconnect():
    if other_side_controller is not None:
        other_side_controller.disconnect()


def _middleman_other_capture():
    if other_side_controller is not None:
        if not other_side_controller.request_capture():
            messagebox.showinfo("Not active", "You're not the active controller yet (queued or not connected).")


def _middleman_other_laser(channel: int, state: bool):
    if other_side_controller is not None:
        if not other_side_controller.send_laser(channel, state):
            messagebox.showinfo("Not active", "You're not the active controller yet (queued or not connected).")


def _middleman_physical_disconnect_all():
    if physical_side_controller is not None:
        physical_side_controller.disconnect_all()


def on_control_mode_changed():
    global physical_side_controller, other_side_controller
    mode = control_mode_var.get()

    # Tear down whichever middleman role is no longer selected.
    if physical_side_controller is not None and mode != "middleman_physical":
        physical_side_controller.stop()
        physical_side_controller = None
        middleman_physical_frame.pack_forget()
    if other_side_controller is not None and mode != "middleman_other":
        other_side_controller.stop()
        other_side_controller = None
        middleman_other_frame.pack_forget()

    if mode == "demo":
        control_mode_status_label.config(
            text="Demo — all actions simulated, no hardware touched.", fg="gray")

    elif mode == "physical_manual":
        if ROBOT_CONNECTED:
            control_mode_status_label.config(
                text="Physical Manual — driving the local robot directly.", fg="green")
        else:
            control_mode_status_label.config(
                text="Physical Manual selected, but no robot is connected — acting as Demo.",
                fg="orange")

    elif mode == "middleman_physical":
        middleman_physical_frame.pack(fill=tk.X, pady=(6, 0))
        if physical_side_controller is None:
            try:
                candidate = PhysicalSideController(
                    robot_connected_provider=lambda: ROBOT_CONNECTED,
                    move_executor=_handle_move_command,
                    laser_executor=_middleman_laser_executor,
                    capture_executor=_middleman_capture_executor,
                    telemetry_provider=_middleman_telemetry_provider,
                    on_control_status_change=_on_physical_control_status_change,
                    on_log=print,
                )
                candidate.start()
                physical_side_controller = candidate
            except Exception as e:
                control_mode_status_label.config(
                    text=f"Middleman (Robot Side) failed to start: {e}", fg="red")
                return
        control_mode_status_label.config(
            text=f"Middleman (Robot Side). Listening as {physical_side_controller.ip}. "
                 f"No remote controller yet.", fg="blue")

    elif mode == "middleman_other":
        middleman_other_frame.pack(fill=tk.X, pady=(6, 0))
        if other_side_controller is None:
            try:
                candidate = OtherSideController(
                    on_discovery_update=_on_discovery_update,
                    on_telemetry_update=_on_telemetry_update,
                    on_control_status_update=_on_other_control_status_update,
                    on_photo_received=_on_photo_received,
                    on_error_received=_on_middleman_error_received,
                    on_log=print,
                )
                candidate.start_discovery()
                other_side_controller = candidate
            except Exception as e:
                control_mode_status_label.config(
                    text=f"Remote Control failed to start: {e}", fg="red")
                return
        control_mode_status_label.config(
            text="Remote Control. Select a robot (Robot Side) above, then Connect.", fg="blue")


middleman_other_connect_btn.config(command=_middleman_other_connect)
middleman_other_disconnect_btn.config(command=_middleman_other_disconnect)
middleman_other_capture_btn.config(command=_middleman_other_capture)
for _ch, (_on_btn, _off_btn) in middleman_other_laser_buttons.items():
    _on_btn.config(command=lambda ch=_ch: _middleman_other_laser(ch, True))
    _off_btn.config(command=lambda ch=_ch: _middleman_other_laser(ch, False))
middleman_physical_disconnect_all_btn.config(command=_middleman_physical_disconnect_all)

# Reflect the startup default (set on control_mode_var above) in the
# status label immediately, without requiring the user to click a
# radio button first.
on_control_mode_changed()

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

# J4 (wrist rotation) Jog Buttons — click-and-hold equivalent of the Q/E
# keyboard jog bindings, for mouse-only use. Reuses the same continuous
# jog press/release handlers as every other axis, so behavior (and the
# "Enable Keyboard Control" safety gate) is identical to the keys.
j4_minus_btn = tk.Button(overdrive_frame, text="J4 Left (Q)", width=10)
j4_minus_btn.grid(row=2, column=0, padx=5, pady=(4, 0))
j4_minus_btn.bind("<ButtonPress-1>", lambda e: handle_jog_press("J4+"))
j4_minus_btn.bind("<ButtonRelease-1>", handle_jog_release)

j4_plus_btn = tk.Button(overdrive_frame, text="J4 Right (E)", width=10)
j4_plus_btn.grid(row=2, column=1, padx=5, pady=(4, 0))
j4_plus_btn.bind("<ButtonPress-1>", lambda e: handle_jog_press("J4-"))
j4_plus_btn.bind("<ButtonRelease-1>", handle_jog_release)

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

# J4 input — leave blank to hold whatever J4 is currently tracked
# (m_j4) rather than snapping the wrist back to 0 on every position move.
j4_frame = tk.Frame(input_fields_frame)
j4_frame.pack(side=tk.LEFT, padx=10)
tk.Label(j4_frame, text="J4 (deg):").pack()
j4_manual_entry = tk.Entry(j4_frame, width=8)
j4_manual_entry.pack()

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

# J4 (wrist rotation) Entry — leave blank to hold the arm's current J4
f4 = tk.Frame(joint_fields_frame); f4.pack(side=tk.LEFT, padx=10)
tk.Label(f4, text="J4 (deg):").pack()
j4_entry = tk.Entry(f4, width=8); j4_entry.pack()

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

# --- Blitting cache for the live tracking dot ---
# The workspace plot (contourf background + grid + saved-point scatters)
# is expensive to redraw in full — doing that on every telemetry tick
# (previously via fig.canvas.draw_idle() at 10Hz) is what actually caused
# the tracking dot to visibly lag behind the real robot, especially under
# any other GUI/thread load. Fix: snapshot everything static ONCE into
# _plot_background, then each tick only blits the moved dot back onto
# that cached snapshot — no re-render of the expensive stuff.
#
# _plot_background is auto-recaptured on ANY full canvas.draw() anywhere
# else in this file (new saved-point markers, etc.) via the draw_event
# hook below, so other code paths that add static plot content don't
# need to know blitting exists — they just keep calling canvas.draw()
# as before and the cache quietly stays correct.
_plot_background = [None]


def _capture_plot_background(event=None):
    _plot_background[0] = fig.canvas.copy_from_bbox(ax.bbox)


fig.canvas.mpl_connect('draw_event', _capture_plot_background)
_capture_plot_background()

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

        # J4 is optional — leave blank to hold whatever J4 is currently
        # tracked (m_j4) when this point is actually sent, rather than
        # forcing a specific wrist angle.
        j4_text = j4_manual_entry.get().strip()
        j4_val = float(j4_text) if j4_text else None
        if j4_val is not None and not (-358.0 <= j4_val <= 358.0):
            messagebox.showerror("Invalid J4-Value", "J4 must be between -358 and 358 degrees")
            return

        # Validate z-value range
        if not (5.0 <= z_val <= 245.0):
            messagebox.showerror("Invalid Z-Value", "Z-value must be between 5 and 245 mm")
            return
        
        # Check if point is in valid region
        if is_inside(x_val, y_val):
            # Add valid point with its z-value, claw state, and J4 (None = hold current J4 when sent)
            valid_points.append((x_val, y_val, z_val, claw_state, j4_val))
            scatter = ax.scatter(x_val, y_val, color='blue', s=50, marker='s')  # Blue square for manual points
            valid_scatters.append(scatter)
            
            claw_text = "ON" if claw_state == 1 else "OFF"
            j4_text_display = f"{j4_val:.1f}" if j4_val is not None else "current"
            points_listbox.insert(tk.END, f"{len(valid_points)}: ({x_val:.2f}, {y_val:.2f}, z={z_val:.1f}, claw={claw_text}, J4={j4_text_display}) [Manual]")
            canvas.draw()
            
            # Clear input fields after successful addition
            x_manual_entry.delete(0, tk.END)
            y_manual_entry.delete(0, tk.END)
            z_manual_entry.delete(0, tk.END)
            z_manual_entry.insert(0, "200")  # Reset z to default
            j4_manual_entry.delete(0, tk.END)
            # Keep claw setting as is (don't reset)
            
            print(f"Manual point added: ({x_val:.2f}, {y_val:.2f}, z={z_val:.1f}, claw={claw_text}, J4={j4_text_display})")
        else:
            messagebox.showerror("Invalid Point", f"Point ({x_val:.2f}, {y_val:.2f}) is outside the valid region")
            
    except ValueError:
        messagebox.showerror("Invalid Input", "Please enter valid numeric values for X, Y, Z, and J4")

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

        px, py, point_z, claw_state, point_j4 = valid_points[0]
        # Coordinate frame rotation to match physical desk orientation
        x = round(py, 2)
        y = -1 * round(px, 2)
        claw_text = "ON" if claw_state == 1 else "OFF"
        # BUGFIX: every queued point used to be sent with move_to_point's
        # J4 argument hardcoded to 0 — meaning sending the queue would
        # silently snap the wrist to 0° on the very first point and hold
        # it there for every point after, regardless of what J4 was set
        # to (via the Move Joints row, jogging, or a previous point).
        # A point can now carry its own J4 (set in the Add Point row or
        # the click-a-point dialog); if it didn't specify one, hold
        # whatever J4 is currently tracked instead of forcing it to 0.
        j4_target = point_j4 if point_j4 is not None else m_j4
        j4_display = f"{point_j4:.1f}" if point_j4 is not None else f"current ({m_j4:.1f})"
        print(f"Sending point: x={px:.2f}, y={py:.2f}, z={point_z:.2f}, "
              f"claw={claw_text}, J4={j4_display}")

        def execute_point():
            """Runs in background thread — move, sync, claw, then schedule next.
            Aborts the rest of the queue (rather than plowing ahead) if a
            move is rejected — e.g. a hard-deck violation — since later
            points were planned assuming this one actually happened."""
            try:
                if control_mode_var.get() == "middleman_other":
                    # Redirect to the connected Physical Side instead of
                    # local hardware — same IK solve move_to_point would
                    # do locally, just sent as an absolute joint command
                    # (with the claw riding along) over the existing
                    # move-relay protocol instead of executed here.
                    sols = Ikinematics(x, y, point_z, j4_target)
                    if not sols:
                        raise RuntimeError(f"Target ({x}, {y}) is unreachable.")
                    j1, j2, z_target, r_target = sols[0]
                    if other_side_controller is None or not other_side_controller.send_move(
                            {"j1": j1, "j2": j2, "j3": z_target, "j4": r_target, "claw": claw_state}):
                        raise RuntimeError("Not the active remote controller (queued or not connected).")
                    print(f"Point complete (relayed): claw={claw_text}")
                else:
                    # 1. Move to target
                    error = move_to_point(x, y, point_z, j4_target)
                    if error:
                        raise RuntimeError(error)

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
                    "Robot Error", f"Point sequence failed: {msg}\n\nRemaining queued points were NOT sent."))
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
        hard_deck_error = _hard_deck_violation(base_z, "Capture rotation sequence")
        if hard_deck_error:
            raise RuntimeError(hard_deck_error)

        cameras_to_use = list_configured_cameras()
        failed_cameras = set()

        for i in range(num_images):
            j4_target = _normalize_j4_target(base_j4 + (i * degrees_per_step))

            if ROBOT_CONNECTED and robot:
                move_error = _dispatch_joint_move(
                    [base_j1, base_j2, base_z, j4_target],
                    reason=f"automatic capture rotation step {i + 1}/{num_images}")
                if move_error is not None:
                    raise RuntimeError(f"Move failed at image {i + 1}: {move_error}")
                robot.movement.sync()
                sync_manual_position_from_feedback("automatic capture rotation step")
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
        # Pass our own already-structured, timestamp-sortable id through
        # as a hint - the server honors it if given (see
        # server-4dai/Server/main.py's submission()) instead of minting
        # its own random one, so the folder it creates on upload matches
        # this same sortable id rather than diverging from it.
        local_sample_id = new_sample_id()
        submit_response = requests.post(
            f"{SERVER_URL}/collection/submission",
            json={"category": category, "date": str(date.today()), "data": values,
                  "sample_id": local_sample_id},
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

def _handle_move_command(payload: dict):
    """Returns None if accepted (incl. jog and demo-mode-simulated), or
    an error string (hard-deck rejection, busy, real hardware move
    error) — PhysicalSideController relays a non-None return back to
    whoever sent the command as an explicit error (see
    middleman_physical_side.py's _publish_error); the legacy generic
    remote listener (start_move_command_listener) ignores the return
    value, unchanged from before."""
    if "jog" in payload:
        axis_cmd = payload["jog"]
        if not ROBOT_CONNECTED or not manual_active.get():
            print(f"[REMOTE JOG IGNORED] robot not connected or manual mode off: {axis_cmd}")
            return None  # not an error to report back — just a no-op in demo/disabled state
        if axis_cmd == "stop":
            handle_jog_release(None, _via_remote=True)
        else:
            handle_jog_press(axis_cmd, _via_remote=True)
        return None

    current = robot_data["joints"] if robot_data["joints"] else [0.0, 0.0, 200.0, 0.0]
    current = (list(current) + [0.0, 0.0, 200.0, 0.0])[:4]
    j1 = float(payload.get("j1", current[0]))
    j2 = float(payload.get("j2", current[1]))
    j3 = float(payload.get("j3", current[2]))
    j4 = float(payload.get("j4", current[3]))

    floor = _effective_hard_deck_z()
    if floor is not None and j3 < floor:
        msg = f"Move rejected: target Z {j3:.1f} is below the height floor ({floor:.1f})."
        print(f"[HARD DECK] {msg}")
        return msg

    if not try_start_arm_operation("a remote move command"):
        return "Robot is busy with another operation"
    try:
        if ROBOT_CONNECTED and robot:
            move_error = _dispatch_joint_move([j1, j2, j3, j4], reason="remote MQTT move command")
            if move_error is not None:
                print(f"[REMOTE MOVE ERROR]: {move_error}")
                return str(move_error)
            robot.movement.sync()
            sync_manual_position_from_feedback("remote MQTT move command")
        else:
            print(f"DEMO MODE: remote move to J1={j1:.1f} J2={j2:.1f} "
                  f"J3={j3:.1f} J4={j4:.1f}")

        # Optional claw command riding along with the move — lets a
        # queued point's pick/place action (see add_dobot_instructions)
        # be relayed in one message instead of needing a second topic.
        if "claw" in payload:
            set_claw_dual_output(payload["claw"])

        return None
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
    hard_deck_error = _hard_deck_violation(z_target, "Pipeline pickup move")
    if hard_deck_error:
        print(f"[PICKUP ABORTED]: {hard_deck_error}")
        return
    if ROBOT_CONNECTED and robot:
        move_error = _dispatch_joint_move([j1, j2, z_target, r_target], reason="pipeline pickup move")
        if move_error is not None:
            print(f"[PICKUP ERROR]: {move_error}")
            return
        robot.movement.sync()
        sync_manual_position_from_feedback("pipeline pickup move")
    else:
        print(f"DEMO MODE: pickup at ({pickup_x}, {pickup_y}, {pickup_z})")
    set_claw_dual_output(1)  # grip

    # 2. Move to the fixed photo station (same marker shown as the yellow dot)
    station_sols = Ikinematics(PHOTO_STATION["x"], PHOTO_STATION["y"], z=PHOTO_STATION["z"])
    base_j1, base_j2, base_z, base_j4 = station_sols[0]
    hard_deck_error = _hard_deck_violation(base_z, "Pipeline photo-station move")
    if hard_deck_error:
        print(f"[STATION MOVE ABORTED]: {hard_deck_error}")
        return
    if ROBOT_CONNECTED and robot:
        move_error = _dispatch_joint_move([base_j1, base_j2, base_z, base_j4], reason="pipeline photo-station move")
        if move_error is not None:
            print(f"[STATION MOVE ERROR]: {move_error}")
            return
        robot.movement.sync()
        sync_manual_position_from_feedback("pipeline photo-station move")
    else:
        print(f"DEMO MODE: moving to photo station {PHOTO_STATION}")

    # 3. Rotate J4 through NUM_VIEWS steps, asking 4DAI to snap a photo at
    #    each step instead of grabbing a frame from a local camera.
    sample_id = new_sample_id()
    step_deg = 360.0 / NUM_VIEWS
    triggered = 0

    for i in range(NUM_VIEWS):
        j4_target = _normalize_j4_target(base_j4 + (i * step_deg))
        if ROBOT_CONNECTED and robot:
            _dispatch_joint_move([base_j1, base_j2, base_z, j4_target],
                                  reason=f"pipeline view {i+1}/{NUM_VIEWS} rotation")
            robot.movement.sync()
            sync_manual_position_from_feedback(f"pipeline view {i+1}/{NUM_VIEWS} rotation")
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

    sample_id = f"sweep_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

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
                    # Accept either 4 values (x, y, z, claw — J4 held at
                    # whatever's currently tracked when sent) or 5 (x, y,
                    # z, claw, j4) so existing pasted test-point lists
                    # don't break after adding J4 support here.
                    if len(point) not in (4, 5):
                        print(f"Point {i+1}: Invalid number of values (expected 4 or 5, got {len(point)})")
                        invalid_count += 1
                        continue

                    if len(point) == 5:
                        px, py, pz, claw, j4_val = point
                    else:
                        px, py, pz, claw = point
                        j4_val = None

                    # Validate types and ranges
                    if not all(isinstance(coord, (int, float)) for coord in [px, py, pz]):
                        print(f"Point {i+1}: X, Y, Z must be numbers")
                        invalid_count += 1
                        continue

                    if not isinstance(claw, int) or claw not in [0, 1]:
                        print(f"Point {i+1}: Claw must be 0 (OFF) or 1 (ON)")
                        invalid_count += 1
                        continue

                    if j4_val is not None:
                        if not isinstance(j4_val, (int, float)):
                            print(f"Point {i+1}: J4 must be a number")
                            invalid_count += 1
                            continue
                        if not (-358.0 <= j4_val <= 358.0):
                            print(f"Point {i+1}: J4 must be between -358 and 358 degrees")
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
                    valid_points.append((px, py, pz, claw, j4_val))
                    scatter = ax.scatter(px, py, color='purple', s=50, marker='D')  # Purple diamond for test points
                    valid_scatters.append(scatter)

                    claw_text = "ON" if claw == 1 else "OFF"
                    j4_text_display = f"{j4_val:.1f}" if j4_val is not None else "current"
                    points_listbox.insert(tk.END, f"{len(valid_points)}: ({px:.2f}, {py:.2f}, z={pz:.1f}, claw={claw_text}, J4={j4_text_display}) [Test]")
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
#
# Standard MongoDB access (this whole section, all three sub-tabs below)
# is the DEFAULT and never touches an LLM. The optional "Ask" box further
# down is a secondary layer on top — see vision/services/mongo_nlp_agent.py.
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
            mongo_client.list_recent_objects(limit=1)
            msg, color = "Connected", "green"
        except Exception as e:
            msg, color = f"Unavailable: {e}", "red"
        root.after(0, lambda: mongo_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()

mongo_btn_row = tk.Frame(mongo_frame)
mongo_btn_row.pack(fill=tk.X, pady=(2, 6))
tk.Button(mongo_btn_row, text="Test Mongo Connection", command=test_mongo_connection,
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))

# --- Three collections, three sub-tabs: Log (objects, default view),
# Images, and Inventory (object_catalog — see vision/storage/
# object_catalog.py). Same underlying data as the plan describes; each
# sub-tab is just a different read query against it.
db_subtabs = ttk.Notebook(mongo_frame)
db_subtabs.pack(fill=tk.BOTH, expand=1, pady=4)

tab_objects_log = tk.Frame(db_subtabs)
tab_images_log = tk.Frame(db_subtabs)
tab_inventory = tk.Frame(db_subtabs)
db_subtabs.add(tab_objects_log, text="Objects (Log)")
db_subtabs.add(tab_images_log, text="Images")
db_subtabs.add(tab_inventory, text="Inventory")

# ---- Objects (Log) sub-tab -------------------------------------------------
tk.Label(tab_objects_log,
         text="Recent captures (newest first) — double-click a row to see its images + attributes:"
         ).pack(anchor=tk.W, pady=(4, 0))

# Standard (non-NLP) query filter bar. This is the "always available,
# no LLM required" way to narrow the Objects list — the "Ask" box
# elsewhere on this tab is a secondary, optional path on top of the
# same find_objects() this calls directly. Dropdown values are pulled
# from what's actually in Mongo (distinct_object_categories/colors), so
# this never drifts from attribute_schema.json or goes stale.
obj_filter_frame = tk.Frame(tab_objects_log)
obj_filter_frame.pack(fill=tk.X, pady=(2, 0))

tk.Label(obj_filter_frame, text="Category:").pack(side=tk.LEFT)
obj_filter_category = ttk.Combobox(obj_filter_frame, width=12, state="readonly")
obj_filter_category.pack(side=tk.LEFT, padx=(2, 8))

tk.Label(obj_filter_frame, text="Color:").pack(side=tk.LEFT)
obj_filter_color = ttk.Combobox(obj_filter_frame, width=12, state="readonly")
obj_filter_color.pack(side=tk.LEFT, padx=(2, 8))

tk.Label(obj_filter_frame, text="Name contains:").pack(side=tk.LEFT)
obj_filter_name = tk.Entry(obj_filter_frame, width=14)
obj_filter_name.pack(side=tk.LEFT, padx=(2, 8))

tk.Label(obj_filter_frame, text="Object ID (exact):").pack(side=tk.LEFT)
obj_filter_object_id = tk.Entry(obj_filter_frame, width=14)
obj_filter_object_id.pack(side=tk.LEFT, padx=(2, 8))

obj_filter_today_only = tk.BooleanVar(value=False)
tk.Checkbutton(obj_filter_frame, text="Captured today only",
               variable=obj_filter_today_only).pack(side=tk.LEFT, padx=(0, 8))

# ---- Advanced Query: raw MongoDB filter syntax, for anything the
# dropdowns above don't cover — size, position, freeform attributes,
# session_id, captured_at ranges, $or/$in across several fields, etc.
# Runs through query_safety.validate_filter() before ever reaching
# pymongo (same whitelist the old NLP layer's generated filters used),
# so a typo or a deliberately hostile filter can't smuggle in $where or
# a write operator — it just gets rejected with a clear error instead.
obj_adv_query_frame = tk.Frame(tab_objects_log)
obj_adv_query_frame.pack(fill=tk.X, pady=(2, 0))
tk.Label(obj_adv_query_frame, text="Advanced Query (MongoDB filter, JSON):").pack(anchor=tk.W)
obj_adv_query_entry = tk.Entry(obj_adv_query_frame, width=70)
obj_adv_query_entry.pack(side=tk.LEFT, fill=tk.X, expand=1, padx=(0, 4))


def _insert_obj_example_today():
    """Fills the Advanced Query box with a working, ready-to-run example
    — filters to whatever session_id today's date resolves to, which is
    the simplest correct "today" query (see find_objects()'s docstring
    for the equivalent captured_at-range version, for arbitrary time
    windows rather than a whole calendar day)."""
    obj_adv_query_entry.delete(0, tk.END)
    example = json.dumps({"session_id": session_manager.today_session_id()})
    obj_adv_query_entry.insert(0, example)


tk.Button(obj_adv_query_frame, text="Example: Today",
          command=_insert_obj_example_today).pack(side=tk.LEFT, padx=(0, 8))

tk.Label(obj_adv_query_frame, text="Sort:").pack(side=tk.LEFT)
obj_sort_order = ttk.Combobox(obj_adv_query_frame, width=11, state="readonly",
                               values=["Newest first", "Oldest first"])
obj_sort_order.set("Newest first")
obj_sort_order.pack(side=tk.LEFT)

tk.Label(tab_objects_log,
         text='Fields: data.name, data.category, data.color, data.size, '
              'data.position_x, data.position_y, data.position_z, '
              'data.reserved_1/2/3, data.attributes.<key>, session_id, captured_at. '
              'Operators: $eq $ne $gt $gte $lt $lte $in $nin $and $or $nor $not $regex $exists. '
              'Example: {"data.size": "large", "data.position_x": {"$gt": 10}}',
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT
         ).pack(anchor=tk.W, padx=2)

objects_listbox = tk.Listbox(tab_objects_log, height=12, selectmode=tk.EXTENDED)
objects_listbox.pack(fill=tk.BOTH, expand=1, pady=4)

# Listbox rows are plain text, but we need the actual object _id behind
# each row to look it up — kept in lockstep with the listbox's rows
# (same index = same row) and rebuilt every refresh.
_recent_object_ids = []


def _populate_object_filter_dropdowns():
    """Fill the Category/Color dropdowns from whatever values actually
    exist in Mongo right now. Safe to call even if Mongo's unreachable —
    just leaves the dropdowns empty (the "Any" default still works)."""
    def worker():
        try:
            categories = mongo_client.distinct_object_categories()
            colors = mongo_client.distinct_object_colors()
        except Exception:
            categories, colors = [], []

        def apply():
            obj_filter_category["values"] = ["(any)"] + categories
            obj_filter_category.set("(any)")
            obj_filter_color["values"] = ["(any)"] + colors
            obj_filter_color.set("(any)")

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def _build_object_filter() -> dict:
    """Turn the filter bar widgets into a Mongo filter dict using only
    known, hardcoded field names — the same safety rule find_objects()'s
    docstring calls out (never pass raw user text straight in as part of
    the filter). The one bit of free text (Name contains) goes in as the
    *value* of a fixed field's $regex, with special characters escaped
    via re.escape so it's always a literal substring match, never an
    attacker-controlled regex pattern.

    The Advanced Query box, if non-empty, is parsed as JSON, validated
    through query_safety (whitelisted operators only — raises
    QueryValidationError on anything else, e.g. $where), and merged in
    on top of the dropdown-built filter — its keys win on collision.
    Raises ValueError (invalid JSON) or query_safety.QueryValidationError
    (disallowed operator) so the caller can show a clear message instead
    of silently ignoring a bad query.
    """
    mongo_filter = {}
    category = obj_filter_category.get()
    if category and category != "(any)":
        mongo_filter["data.category"] = category
    color = obj_filter_color.get()
    if color and color != "(any)":
        mongo_filter["data.color"] = color
    name_text = obj_filter_name.get().strip()
    if name_text:
        mongo_filter["data.name"] = {"$regex": re.escape(name_text), "$options": "i"}
    object_id_text = obj_filter_object_id.get().strip()
    if object_id_text:
        # Exact match, not a regex — this is the same random object_id
        # generated at capture time (see capture_pipeline.record_capture)
        # and shown in the detail viewer's read-only "Object ID" field,
        # meant for jumping straight back to one specific capture rather
        # than a fuzzy search.
        mongo_filter["_id"] = object_id_text
    if obj_filter_today_only.get():
        mongo_filter["session_id"] = session_manager.today_session_id()

    advanced_text = obj_adv_query_entry.get().strip()
    if advanced_text:
        try:
            advanced_filter = json.loads(advanced_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Advanced Query isn't valid JSON: {e}")
        query_safety.validate_filter(advanced_filter)
        mongo_filter.update(advanced_filter)

    return mongo_filter


def refresh_objects_list():
    """Pull object documents from local MongoDB and show them in the
    listbox — filtered by the filter bar above if any filter is set,
    otherwise the same "most recent 30" view as before. Safe to call
    even if MongoDB isn't running — shows the error in the list instead
    of crashing the GUI."""
    def worker():
        err = None
        try:
            ascending = (obj_sort_order.get() == "Oldest first")
            mongo_filter = _build_object_filter()
            if mongo_filter:
                objects = mongo_client.find_objects(mongo_filter, limit=100, sort_ascending=ascending)
            else:
                objects = mongo_client.list_recent_objects(limit=30, sort_ascending=ascending)
        except Exception as e:
            objects = None
            err = str(e)

        def apply():
            objects_listbox.delete(0, tk.END)
            _recent_object_ids.clear()
            if objects is None:
                objects_listbox.insert(tk.END, f"Error: {err}")
                return
            if not objects:
                objects_listbox.insert(tk.END, "(no objects match this filter)")
                return
            for o in objects:
                oid = o.get("_id", "?")
                odate = o.get("date", "?")
                name = (o.get("data") or {}).get("name", "")
                objects_listbox.insert(tk.END, f"{odate}  |  {name}  |  {oid}")
                _recent_object_ids.append(oid)

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def clear_object_filters():
    obj_filter_category.set("(any)")
    obj_filter_color.set("(any)")
    obj_filter_name.delete(0, tk.END)
    obj_filter_object_id.delete(0, tk.END)
    obj_filter_today_only.set(False)
    obj_adv_query_entry.delete(0, tk.END)
    refresh_objects_list()


def show_object_detail(object_id: str, viewer_title_prefix: str = "Object"):
    """Pop up a window showing one object's fixed + freeform attributes
    (EDITABLE — see the Save Changes button below) AND every image
    logged against it, oldest-first — the "click a recent capture, see
    and edit its images and attributes" view. Shared by the Objects
    (Log), Images, Inventory, and Data Collection ("Today's Captures")
    tabs' double-click handlers so there's exactly one viewer
    implementation instead of several near-duplicates."""
    viewer = tk.Toplevel(root)
    viewer.title(f"{viewer_title_prefix} {object_id}")
    viewer.geometry("800x700")
    loading_label = tk.Label(viewer, text="Loading...", padx=20, pady=20)
    loading_label.pack()

    def worker():
        try:
            obj = mongo_client.get_object(object_id)
            image_docs = mongo_client.get_images_for_object(object_id)
            # Oldest-first within the viewer regardless of whatever order
            # Mongo happened to return them in — matches "sort images in
            # time order". Docs without a captured_at (shouldn't happen
            # for anything captured through this pipeline, but a
            # defensive fallback) sort first via datetime.min.
            image_docs.sort(key=lambda d: d.get("captured_at") or datetime.min)
            err = None
        except Exception as e:
            obj, image_docs, err = None, None, str(e)

        def build_ui():
            loading_label.destroy()
            if err is not None:
                tk.Label(viewer, text=f"Could not load object:\n{err}",
                         fg="red", padx=20, pady=20).pack()
                return
            if obj is None:
                tk.Label(viewer, text=f"No object found with id {object_id}.",
                         fg="red", padx=20, pady=20).pack()
                return

            # --- Attributes panel (fixed columns + freeform "why") — EDITABLE ---
            attrs_frame = tk.LabelFrame(viewer, text=" Attributes (editable) ", padx=8, pady=8)
            attrs_frame.pack(fill=tk.X, padx=10, pady=(10, 4))
            data = (obj or {}).get("data") or {}

            # Object ID is deliberately shown (and copy-able via normal
            # text selection) even though it's not one of
            # attribute_schema's fixed columns, and stays read-only —
            # it's how you search for this exact object again later
            # (e.g. Advanced Query {"_id": "<this>"} on the Objects
            # tab), not something that makes sense to "edit".
            id_row = tk.Frame(attrs_frame)
            id_row.pack(fill=tk.X)
            tk.Label(id_row, text="Object ID:", font=("Arial", 9, "bold"),
                     width=14, anchor="w").pack(side=tk.LEFT)
            id_entry = tk.Entry(id_row, font=("Courier", 9))
            id_entry.insert(0, object_id)
            id_entry.config(state="readonly")
            id_entry.pack(side=tk.LEFT, fill=tk.X, expand=1)

            # Every fixed column is always shown, even when this object
            # doesn't have a value for it yet — an empty, editable Entry
            # (or a Combobox for a boolean-typed column) rather than
            # silently omitting the row, so filling in a missing value
            # is just "type into the blank field and Save", right here,
            # no separate Attribute Review trip required.
            labels = attribute_schema.display_labels()
            schema_types = {c["key"]: c.get("type", "string")
                             for c in attribute_schema.load_schema().get("fixed_columns", [])}
            field_widgets = {}
            for key in attribute_schema.fixed_column_keys():
                value = data.get(key)
                is_missing = value in (None, "")
                row = tk.Frame(attrs_frame)
                row.pack(fill=tk.X, pady=1)
                tk.Label(row, text=f"{labels.get(key, key)}:", font=("Arial", 9, "bold"),
                         width=14, anchor="w",
                         fg="#b35900" if is_missing else "black").pack(side=tk.LEFT)
                if schema_types.get(key) == "boolean":
                    widget = ttk.Combobox(row, width=37, state="readonly",
                                           values=["", "true", "false", attribute_schema.UNKNOWN])
                    widget.set("" if is_missing else str(value))
                else:
                    widget = tk.Entry(row, width=40)
                    if not is_missing:
                        widget.insert(0, str(value))
                widget.pack(side=tk.LEFT, fill=tk.X, expand=1)
                field_widgets[key] = widget

            freeform = data.get(attribute_schema.freeform_key()) or {}
            tk.Label(attrs_frame, text="Freeform attributes (JSON, editable):",
                     font=("Arial", 9, "bold"), anchor="w").pack(fill=tk.X, pady=(6, 0))
            freeform_text = tk.Text(attrs_frame, height=4, font=("Courier", 8))
            freeform_text.insert("1.0", json.dumps(freeform, indent=2) if freeform else "{}")
            freeform_text.pack(fill=tk.X)

            save_status_label = tk.Label(attrs_frame, text="", fg="gray")
            save_status_label.pack(anchor=tk.W, pady=(4, 0))

            def save_attribute_changes():
                new_data = {}
                for key, widget in field_widgets.items():
                    raw_value = widget.get().strip()
                    if raw_value == "":
                        new_data[key] = None
                        continue
                    if schema_types.get(key) == "number":
                        try:
                            new_data[key] = float(raw_value) if "." in raw_value else int(raw_value)
                        except ValueError:
                            new_data[key] = raw_value  # keep what was typed rather than losing it
                    else:
                        new_data[key] = raw_value

                try:
                    freeform_value = json.loads(freeform_text.get("1.0", tk.END).strip() or "{}")
                except json.JSONDecodeError as e:
                    messagebox.showerror("Invalid JSON", f"Freeform attributes aren't valid JSON: {e}")
                    return
                new_data[attribute_schema.freeform_key()] = freeform_value

                try:
                    mongo_client.update_object_data(object_id, new_data)
                except Exception as e:
                    save_status_label.config(text=f"Save failed: {e}", fg="red")
                    return

                save_status_label.config(text="Saved.", fg="green")
                try:
                    refresh_objects_list()
                    refresh_images_list()
                    refresh_inventory_list()
                    refresh_dc_today_list()
                except NameError:
                    pass

            tk.Button(attrs_frame, text="Save Changes", command=save_attribute_changes,
                      bg="lightgreen").pack(anchor=tk.W, pady=(6, 0))

            # --- Images panel ---
            if not image_docs:
                tk.Label(viewer, text="No images found for this object.",
                         padx=20, pady=10).pack()
                return
            if not _PIL_AVAILABLE:
                tk.Label(viewer, text="Install Pillow to view images: pip install Pillow",
                         fg="red", padx=20, pady=10).pack()
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
                raw_path = doc.get("image_path", "")
                path = resolve_image_path(raw_path)
                source = doc.get("source", "?")
                view_index = doc.get("view_index", "?")
                when = doc.get("captured_at", "")
                row = tk.Frame(inner_frame, pady=8)
                row.pack(fill=tk.X)
                tk.Label(row, text=f"{source} — view {view_index}  ({when})",
                         font=("Arial", 9, "bold")).pack()
                try:
                    img = Image.open(path)
                    img.thumbnail((700, 525))
                    photo = ImageTk.PhotoImage(img)
                    viewer._photo_refs.append(photo)
                    tk.Label(row, image=photo).pack()
                except Exception as e:
                    hint = ("" if path == raw_path else f"\n(tried: {path})")
                    tk.Label(row, text=f"Could not load '{raw_path}': {e}{hint}",
                             fg="red", wraplength=680).pack()

        root.after(0, build_ui)

    threading.Thread(target=worker, daemon=True).start()

def open_object_detail_viewer(event=None):
    """Objects (Log) tab double-click handler."""
    selection = objects_listbox.curselection()
    if not selection:
        return
    idx = selection[0]
    if idx >= len(_recent_object_ids):
        return  # clicked a placeholder row like "(no objects yet)" or "Error: ..."
    show_object_detail(_recent_object_ids[idx])


objects_listbox.bind("<Double-Button-1>", open_object_detail_viewer)


def delete_selected_objects():
    """Objects tab's "Delete Selected" button — multi-select (the
    listbox is selectmode=EXTENDED, so ctrl/shift-click work) delete of
    whole captures. Same underlying mongo_client.delete_object() as the
    single-object delete this replaced on the Data Collection tab's
    "Today's Captures" list — that list is view-only now; this is
    where capture deletion actually lives."""
    selection = objects_listbox.curselection()
    if not selection:
        return
    object_ids = [_recent_object_ids[i] for i in selection if i < len(_recent_object_ids)]
    if not object_ids:
        return  # selected only a placeholder row like "(no objects...)" or "Error: ..."
    if not messagebox.askyesno(
            "Delete selected",
            f"Delete {len(object_ids)} object(s) and their image records from MongoDB?\n\n"
            f"This does NOT delete the image files from disk, and does NOT change the "
            f"Inventory 'times seen' count — only the CSV/JSON logs keep a record that "
            f"these captures happened at all."):
        return

    def worker():
        errors = []
        for object_id in object_ids:
            try:
                mongo_client.delete_object(object_id)
            except Exception as e:
                errors.append(f"{object_id}: {e}")

        def apply():
            if errors:
                messagebox.showerror("Some deletes failed", "\n".join(errors))
            refresh_objects_list()
            try:
                refresh_images_list()
                refresh_inventory_list()
                refresh_dc_today_list()
            except NameError:
                pass

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


obj_buttons_frame = tk.Frame(tab_objects_log)
obj_buttons_frame.pack(anchor=tk.W, pady=4)
tk.Button(obj_buttons_frame, text="Apply Filters", command=refresh_objects_list,
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(obj_buttons_frame, text="Clear Filters", command=clear_object_filters
          ).pack(side=tk.LEFT, padx=(0, 4))
tk.Button(obj_buttons_frame, text="Refresh", command=refresh_objects_list,
          bg="lightblue").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(obj_buttons_frame, text="Delete Selected", command=delete_selected_objects,
          bg="salmon").pack(side=tk.LEFT)
root.after(0, _populate_object_filter_dropdowns)

# ---- Images sub-tab ---------------------------------------------------
tk.Label(tab_images_log,
         text="Recent photos (newest first, across all objects):").pack(anchor=tk.W, pady=(4, 0))

# Standard (non-NLP) filter bar for the Images view. "Captured today
# only" is the literal "photos taken today" example query — checking it
# calls mongo_client.find_images({"session_id": session_manager.today_session_id()}),
# the same session_id pattern find_images()'s own docstring recommends
# over a raw captured_at datetime range for whole-day queries.
img_filter_frame = tk.Frame(tab_images_log)
img_filter_frame.pack(fill=tk.X, pady=(2, 0))

tk.Label(img_filter_frame, text="Source:").pack(side=tk.LEFT)
img_filter_source = ttk.Combobox(img_filter_frame, width=12, state="readonly")
img_filter_source.pack(side=tk.LEFT, padx=(2, 8))

img_filter_today_only = tk.BooleanVar(value=False)
tk.Checkbutton(img_filter_frame, text="Captured today only",
               variable=img_filter_today_only).pack(side=tk.LEFT, padx=(0, 8))

# ---- Advanced Query: raw MongoDB filter syntax — same safety model as
# the Objects tab's Advanced Query box (see its comment for details).
img_adv_query_frame = tk.Frame(tab_images_log)
img_adv_query_frame.pack(fill=tk.X, pady=(2, 0))
tk.Label(img_adv_query_frame, text="Advanced Query (MongoDB filter, JSON):").pack(anchor=tk.W)
img_adv_query_entry = tk.Entry(img_adv_query_frame, width=70)
img_adv_query_entry.pack(side=tk.LEFT, fill=tk.X, expand=1, padx=(0, 4))


def _insert_img_example_today():
    """Same idea as the Objects tab's Example: Today button — see
    find_images()'s docstring for why session_id is the simplest
    correct "today" filter."""
    img_adv_query_entry.delete(0, tk.END)
    example = json.dumps({"session_id": session_manager.today_session_id()})
    img_adv_query_entry.insert(0, example)


tk.Button(img_adv_query_frame, text="Example: Today",
          command=_insert_img_example_today).pack(side=tk.LEFT, padx=(0, 8))

tk.Label(img_adv_query_frame, text="Sort:").pack(side=tk.LEFT)
img_sort_order = ttk.Combobox(img_adv_query_frame, width=11, state="readonly",
                               values=["Newest first", "Oldest first"])
img_sort_order.set("Newest first")
img_sort_order.pack(side=tk.LEFT)

tk.Label(tab_images_log,
         text='Fields: source, view_index, object_id, session_id, captured_at. '
              'Operators: $eq $ne $gt $gte $lt $lte $in $nin $and $or $nor $not $regex $exists. '
              'Example: {"view_index": {"$gte": 3}}',
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT
         ).pack(anchor=tk.W, padx=2)

images_listbox = tk.Listbox(tab_images_log, height=12, selectmode=tk.EXTENDED)
images_listbox.pack(fill=tk.BOTH, expand=1, pady=4)

# Same pattern as _recent_object_ids on the Objects tab — the object_id
# each row's image belongs to, kept in lockstep with the listbox rows
# so double-clicking a photo can jump straight to that object's full
# detail view (attributes + every one of its images, however many).
_recent_image_object_ids = []
_recent_image_ids = []  # the image doc's OWN _id — needed for delete_image(), separate
                         # from _recent_image_object_ids (which object each row belongs to)


def _populate_image_filter_dropdown():
    def worker():
        try:
            sources = mongo_client.distinct_image_sources()
        except Exception:
            sources = []

        def apply():
            img_filter_source["values"] = ["(any)"] + sources
            img_filter_source.set("(any)")

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def _build_image_filter() -> dict:
    """Same safety rule as _build_object_filter(): only known,
    hardcoded field names go into the filter from the dropdowns — the
    Advanced Query box's free-form JSON is parsed and passed through
    query_safety.validate_filter() before being merged in, same as the
    Objects tab (see _build_object_filter()'s docstring for details)."""
    mongo_filter = {}
    source = img_filter_source.get()
    if source and source != "(any)":
        mongo_filter["source"] = source
    if img_filter_today_only.get():
        # DEMO QUERY — "photos taken today":
        mongo_filter["session_id"] = session_manager.today_session_id()

    advanced_text = img_adv_query_entry.get().strip()
    if advanced_text:
        try:
            advanced_filter = json.loads(advanced_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Advanced Query isn't valid JSON: {e}")
        query_safety.validate_filter(advanced_filter)
        mongo_filter.update(advanced_filter)

    return mongo_filter


def refresh_images_list():
    """
    Populates the Images tab grouped by SNAPSHOT (object_id) — a header
    row per object ("=== object <id> — N image(s) ==="), followed by
    that object's own images indented beneath it, then the next
    object's header, and so on — rather than one flat list where every
    image (regardless of which capture it came from) is interleaved by
    timestamp and distinguished only by source/view_index. Grouping
    still respects whatever filter/sort the controls above are set to;
    it only changes how the matching images are laid out in the list.
    """
    def worker():
        err = None
        try:
            ascending = (img_sort_order.get() == "Oldest first")
            mongo_filter = _build_image_filter()
            if mongo_filter:
                images = mongo_client.find_images(mongo_filter, limit=200, sort_ascending=ascending)
            else:
                images = mongo_client.list_recent_images(limit=30, sort_ascending=ascending)
        except Exception as e:
            images, err = None, str(e)

        def apply():
            images_listbox.delete(0, tk.END)
            _recent_image_object_ids.clear()
            _recent_image_ids.clear()
            if images is None:
                images_listbox.insert(tk.END, f"Error: {err}")
                return
            if not images:
                images_listbox.insert(tk.END, "(no images match this filter)")
                return

            # Group while preserving each object's first-appearance order
            # (which already reflects whatever sort was picked above —
            # oldest-first or newest-first — since `images` came back
            # pre-sorted by captured_at).
            groups: dict = {}
            group_order: list = []
            for i in images:
                object_id = i.get("object_id")
                if object_id not in groups:
                    groups[object_id] = []
                    group_order.append(object_id)
                groups[object_id].append(i)

            for object_id in group_order:
                group_images = groups[object_id]
                images_listbox.insert(
                    tk.END,
                    f"=== object {object_id or '(none)'} — {len(group_images)} image(s) ===")
                _recent_image_object_ids.append(object_id)  # header row still opens this object
                _recent_image_ids.append(None)              # but isn't itself a deletable image

                for img in group_images:
                    when = str(img.get("captured_at", ""))
                    images_listbox.insert(
                        tk.END,
                        f"      {when}  |  {img.get('source', '?')}  |  view {img.get('view_index', '?')}"
                    )
                    _recent_image_object_ids.append(object_id)
                    _recent_image_ids.append(img.get("_id"))

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def open_image_object_detail(event=None):
    """Images tab double-click handler — jumps to the SAME detail
    viewer the Objects tab uses, for whichever object this row belongs
    to (the header row or any image row beneath it both work, since
    both are stamped with that object's id — see refresh_images_list())
    — showing that object's attributes and ALL of its images, not just
    the one row that was clicked."""
    selection = images_listbox.curselection()
    if not selection:
        return
    idx = selection[0]
    if idx >= len(_recent_image_object_ids):
        return  # clicked a placeholder row like "(no images...)" or "Error: ..."
    object_id = _recent_image_object_ids[idx]
    if not object_id:
        messagebox.showinfo("No object link", "This image record has no linked object_id.")
        return
    show_object_detail(object_id)


images_listbox.bind("<Double-Button-1>", open_image_object_detail)


def delete_selected_images():
    """Images tab's "Delete Selected" button — multi-select delete of
    INDIVIDUAL image docs (mongo_client.delete_image()), not whole
    objects. Deleting a photo here leaves its object and that object's
    other images untouched — use the Objects tab's "Delete Selected"
    instead to remove a whole capture."""
    selection = images_listbox.curselection()
    if not selection:
        return
    image_ids = [_recent_image_ids[i] for i in selection
                 if i < len(_recent_image_ids) and _recent_image_ids[i] is not None]
    if not image_ids:
        return  # selected only header/placeholder rows — nothing individually deletable there
    if not messagebox.askyesno(
            "Delete selected",
            f"Delete {len(image_ids)} image record(s) from MongoDB?\n\n"
            f"This only removes the image DOC — the object it belongs to and any other "
            f"images linked to it are untouched. Does NOT delete the file from disk."):
        return

    def worker():
        errors = []
        for image_id in image_ids:
            try:
                mongo_client.delete_image(image_id)
            except Exception as e:
                errors.append(f"{image_id}: {e}")

        def apply():
            if errors:
                messagebox.showerror("Some deletes failed", "\n".join(errors))
            refresh_images_list()
            try:
                refresh_objects_list()
            except NameError:
                pass

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()



def clear_image_filters():
    img_filter_source.set("(any)")
    img_filter_today_only.set(False)
    img_adv_query_entry.delete(0, tk.END)
    refresh_images_list()


img_buttons_frame = tk.Frame(tab_images_log)
img_buttons_frame.pack(anchor=tk.W, pady=4)
tk.Button(img_buttons_frame, text="Apply Filters", command=refresh_images_list,
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(img_buttons_frame, text="Clear Filters", command=clear_image_filters
          ).pack(side=tk.LEFT, padx=(0, 4))
tk.Button(img_buttons_frame, text="Refresh", command=refresh_images_list,
          bg="lightblue").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(img_buttons_frame, text="Delete Selected", command=delete_selected_images,
          bg="salmon").pack(side=tk.LEFT)
root.after(0, _populate_image_filter_dropdown)

# ---- Inventory sub-tab (object_catalog — distinct known objects) -----
tk.Label(tab_inventory,
         text="Distinct known objects (auto-matched by name — see "
              "vision/storage/object_catalog.py), most recently seen first:"
         ).pack(anchor=tk.W, pady=(4, 0))
inventory_listbox = tk.Listbox(tab_inventory, height=12)
inventory_listbox.pack(fill=tk.BOTH, expand=1, pady=4)


def refresh_inventory_list():
    def worker():
        err = None
        try:
            entries = object_catalog.list_inventory(limit=100)
        except Exception as e:
            entries, err = None, str(e)

        def apply():
            inventory_listbox.delete(0, tk.END)
            _recent_catalog_ids.clear()
            if entries is None:
                inventory_listbox.insert(tk.END, f"Error: {err}")
                return
            if not entries:
                inventory_listbox.insert(tk.END, "(no distinct objects catalogued yet)")
                return
            for e in entries:
                inventory_listbox.insert(
                    tk.END,
                    f"{e.get('name', '?')}  |  seen {e.get('times_seen', 0)}x  |  "
                    f"last {e.get('last_seen', '?')}  |  {e.get('category', '')}"
                )
                _recent_catalog_ids.append(e.get("_id"))

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def open_catalog_detail_viewer(event=None):
    """Inventory tab double-click handler. A catalog entry can be linked
    to several captures (times_seen > 1) — this shows the catalog
    summary AND every image from EVERY linked capture together,
    automatically, in one scrollable window (no extra button/click
    needed to actually see the photos). The "linked captures" list
    below the summary is still there if you want to drill into ONE
    specific capture's full attribute set via show_object_detail()."""
    selection = inventory_listbox.curselection()
    if not selection:
        return
    idx = selection[0]
    if idx >= len(_recent_catalog_ids):
        return  # clicked a placeholder row like "(no distinct objects...)" or "Error: ..."
    catalog_id = _recent_catalog_ids[idx]

    viewer = tk.Toplevel(root)
    viewer.title(f"Catalog entry {catalog_id}")
    viewer.geometry("860x760")
    loading_label = tk.Label(viewer, text="Loading...", padx=20, pady=20)
    loading_label.pack()

    def worker():
        try:
            entry = mongo_client.get_catalog_entry(catalog_id)
            err = None
        except Exception as e:
            entry, err = None, str(e)

        all_docs = []
        per_object_errors = []
        if err is None and entry is not None:
            for object_id in (entry.get("linked_object_ids", []) or []):
                # Each linked capture is fetched INDEPENDENTLY — one bad
                # object_id (a stale reference, a transient Mongo hiccup,
                # whatever) must not wipe out images already gathered
                # successfully from every OTHER linked capture. A single
                # try/except wrapped around the WHOLE loop used to do
                # exactly that: any one object's failure discarded
                # everything, including images already found — which is
                # why "show all photos at once" (the only place that
                # loops over more than one object) could fail while
                # single-object viewing (show_object_detail, never more
                # than one object) kept working fine.
                try:
                    for doc in mongo_client.get_images_for_object(object_id):
                        doc = dict(doc)
                        doc["_object_id"] = object_id
                        all_docs.append(doc)
                except Exception as e:
                    per_object_errors.append(f"{object_id}: {e}")
            all_docs.sort(key=lambda d: d.get("captured_at") or datetime.min)

        def build_ui():
            loading_label.destroy()
            if err is not None:
                tk.Label(viewer, text=f"Could not load catalog entry:\n{err}",
                         fg="red", padx=20, pady=20).pack()
                return
            if entry is None:
                tk.Label(viewer, text=f"No catalog entry found with id {catalog_id}.",
                         fg="red", padx=20, pady=20).pack()
                return

            # The whole window scrolls as one unit — summary, linked-capture
            # list, and every image, all visible without a separate button.
            outer_canvas = tk.Canvas(viewer)
            outer_scrollbar = tk.Scrollbar(viewer, orient=tk.VERTICAL, command=outer_canvas.yview)
            outer_frame = tk.Frame(outer_canvas)
            window_id = outer_canvas.create_window((0, 0), window=outer_frame, anchor="nw")

            def _sync_scrollregion(_event=None):
                outer_canvas.configure(scrollregion=outer_canvas.bbox("all"))

            def _sync_outer_width(event):
                outer_canvas.itemconfig(window_id, width=max(event.width, outer_frame.winfo_reqwidth()))

            outer_frame.bind("<Configure>", _sync_scrollregion)
            outer_canvas.bind("<Configure>", _sync_outer_width)
            outer_canvas.configure(yscrollcommand=outer_scrollbar.set)
            outer_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=1)
            outer_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            # Mouse wheel support (Windows/Mac deltas differ from Linux's Button-4/5) —
            # bound/unbound on enter/leave so it doesn't fight with other open windows.
            def _on_mousewheel(e):
                outer_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            def _on_enter(_e):
                outer_canvas.bind_all("<MouseWheel>", _on_mousewheel)
                outer_canvas.bind_all("<Button-4>", lambda e: outer_canvas.yview_scroll(-1, "units"))
                outer_canvas.bind_all("<Button-5>", lambda e: outer_canvas.yview_scroll(1, "units"))
            def _on_leave(_e):
                outer_canvas.unbind_all("<MouseWheel>")
                outer_canvas.unbind_all("<Button-4>")
                outer_canvas.unbind_all("<Button-5>")
            outer_canvas.bind("<Enter>", _on_enter)
            outer_canvas.bind("<Leave>", _on_leave)

            summary_frame = tk.LabelFrame(outer_frame, text=" Summary ", padx=8, pady=8)
            summary_frame.pack(fill=tk.X, padx=10, pady=(10, 4))
            for label, value in [
                ("Name", entry.get("name", "?")),
                ("Category", entry.get("category") or "(none)"),
                ("Times seen", entry.get("times_seen", 0)),
                ("First seen", entry.get("first_seen", "?")),
                ("Last seen", entry.get("last_seen", "?")),
            ]:
                row = tk.Frame(summary_frame)
                row.pack(fill=tk.X)
                tk.Label(row, text=f"{label}:", font=("Arial", 9, "bold"),
                         width=12, anchor="w").pack(side=tk.LEFT)
                tk.Label(row, text=str(value), anchor="w").pack(side=tk.LEFT)

            linked_ids = entry.get("linked_object_ids", []) or []
            tk.Label(outer_frame, text=f"Linked captures ({len(linked_ids)}) — "
                                        f"double-click one for its full attribute set:",
                     padx=10, pady=(8, 2), anchor="w").pack(fill=tk.X)
            linked_listbox = tk.Listbox(outer_frame, height=min(6, max(2, len(linked_ids))))
            linked_listbox.pack(fill=tk.X, padx=10, pady=(0, 10))
            for linked_id in linked_ids:
                linked_listbox.insert(tk.END, linked_id)

            def on_linked_double_click(_event=None):
                sel = linked_listbox.curselection()
                if not sel:
                    return
                show_object_detail(linked_listbox.get(sel[0]))

            linked_listbox.bind("<Double-Button-1>", on_linked_double_click)

            if per_object_errors:
                tk.Label(outer_frame,
                         text=f"{len(per_object_errors)} linked capture(s) failed to load — "
                              f"the rest are still shown below:\n" + "\n".join(per_object_errors),
                         fg="red", padx=10, pady=(4, 0), anchor="w", justify=tk.LEFT,
                         wraplength=680).pack(fill=tk.X)

            # ---- All images, every linked capture, shown right here ----
            tk.Label(outer_frame, text=f"All Images ({len(all_docs)} across "
                                        f"{len(linked_ids)} linked capture(s)):",
                     font=("Arial", 10, "bold"), padx=10, pady=(4, 2), anchor="w").pack(fill=tk.X)

            if not all_docs:
                if not linked_ids:
                    tk.Label(outer_frame, text="No linked captures at all for this entry.",
                             padx=10, pady=10, anchor="w").pack(fill=tk.X)
                else:
                    tk.Label(outer_frame,
                             text=f"{len(linked_ids)} linked capture(s), but none of them "
                                  f"have any image records left in MongoDB (they may have "
                                  f"been deleted individually via the Images tab's "
                                  f"\"Delete Selected\").",
                             padx=10, pady=10, anchor="w", wraplength=680, justify=tk.LEFT).pack(fill=tk.X)
            elif not _PIL_AVAILABLE:
                tk.Label(outer_frame, text="Install Pillow to view images: pip install Pillow",
                         fg="red", padx=10, pady=10, anchor="w").pack(fill=tk.X)
            else:
                viewer._photo_refs = []
                for doc in all_docs:
                    raw_path = doc.get("image_path", "")
                    path = resolve_image_path(raw_path)
                    source = doc.get("source", "?")
                    view_index = doc.get("view_index", "?")
                    when = doc.get("captured_at", "")
                    img_row = tk.Frame(outer_frame, pady=8)
                    img_row.pack(fill=tk.X, padx=10)
                    tk.Label(img_row, text=f"capture {doc.get('_object_id', '?')}  —  "
                                            f"{source} / view {view_index}  ({when})",
                             font=("Arial", 9, "bold")).pack(anchor="w")
                    try:
                        img = Image.open(path)
                        img.thumbnail((700, 525))
                        photo = ImageTk.PhotoImage(img)
                        viewer._photo_refs.append(photo)
                        tk.Label(img_row, image=photo).pack(anchor="w")
                    except Exception as e:
                        hint = "" if path == raw_path else f"\n(tried: {path})"
                        tk.Label(img_row, text=f"Could not load '{raw_path}': {e}{hint}",
                                 fg="red", wraplength=680, justify=tk.LEFT, anchor="w"
                                 ).pack(fill=tk.X)

        root.after(0, build_ui)

    threading.Thread(target=worker, daemon=True).start()


_recent_catalog_ids = []
inventory_listbox.bind("<Double-Button-1>", open_catalog_detail_viewer)

inv_btn_row = tk.Frame(tab_inventory)
inv_btn_row.pack(anchor=tk.W, pady=4)
tk.Button(inv_btn_row, text="Refresh", command=refresh_inventory_list,
          bg="lightblue").pack(side=tk.LEFT, padx=(0, 4))

inv_repair_status_label = tk.Label(tab_inventory, text="", fg="gray")


def repair_inventory_links():
    """"Repair Inventory Links" button — fixes catalog entries that
    still point at deleted captures from BEFORE delete_object()/
    delete_objects_bulk() started keeping linked_object_ids in sync
    (see mongo_client.repair_catalog_links()'s docstring). Run this
    once after upgrading if the Inventory tab shows entries with a
    nonzero "times seen" but zero images when you open them — that's
    exactly the symptom this fixes."""
    inv_repair_status_label.config(text="Repairing...", fg="gray")

    def worker():
        try:
            result = mongo_client.repair_catalog_links()
            msg = (f"Checked {result['entries_checked']} entries, fixed "
                   f"{result['entries_fixed']}, removed "
                   f"{result['dangling_links_removed']} dangling link(s).")
            color = "green"
        except Exception as e:
            msg, color = f"Repair failed: {e}", "red"

        def apply():
            inv_repair_status_label.config(text=msg, fg=color)
            refresh_inventory_list()

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


tk.Button(inv_btn_row, text="Repair Inventory Links", command=repair_inventory_links,
          bg="khaki").pack(side=tk.LEFT, padx=4)
inv_repair_status_label.pack(anchor=tk.W)

# Populate all three sub-tabs once at startup.
root.after(0, refresh_objects_list)
root.after(0, refresh_images_list)
root.after(0, refresh_inventory_list)

# =====================================================================
# CLEANUP — bulk-delete old captures (e.g. everything from a previous
# experiment/test run) straight out of MongoDB, without deleting one
# object at a time. Two ways to pick what gets deleted: by session
# (calendar day) via the dropdown, or by a custom MongoDB filter (same
# JSON-filter rules/safety as the Objects tab's Advanced Query box —
# see query_safety.validate_filter()). Deleting the image FILES from
# disk too is opt-in (checkbox) since that part is irreversible.
# =====================================================================
cleanup_frame = tk.LabelFrame(tab_database, text=" Cleanup — Bulk Delete ", padx=10, pady=10)
cleanup_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(cleanup_frame,
         text="Remove old captures in bulk — e.g. everything left over from a previous "
              "experiment — by session (day) or by a custom filter. This only touches the "
              "objects/images collections; it does NOT rewrite the CSV/JSON audit logs "
              "(a historical record that a capture happened) or the Inventory catalog's "
              "'times seen' counts.",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

cleanup_session_row = tk.Frame(cleanup_frame)
cleanup_session_row.pack(fill=tk.X, pady=(6, 2))
tk.Label(cleanup_session_row, text="Session (day):").pack(side=tk.LEFT)
cleanup_session_combo = ttk.Combobox(cleanup_session_row, width=14, state="readonly")
cleanup_session_combo.pack(side=tk.LEFT, padx=(4, 8))


def _populate_cleanup_sessions():
    def worker():
        try:
            session_ids = [s.get("_id") for s in mongo_client.list_sessions(limit=365)]
        except Exception:
            session_ids = []

        def apply():
            cleanup_session_combo["values"] = ["(none — use custom filter below)"] + session_ids
            cleanup_session_combo.set("(none — use custom filter below)")

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


tk.Button(cleanup_session_row, text="Refresh Sessions", command=_populate_cleanup_sessions
          ).pack(side=tk.LEFT)

cleanup_filter_frame = tk.Frame(cleanup_frame)
cleanup_filter_frame.pack(fill=tk.X, pady=(4, 2))
tk.Label(cleanup_filter_frame, text="Or a custom filter (MongoDB JSON — same syntax as "
                                     "the Objects tab's Advanced Query):").pack(anchor=tk.W)
cleanup_filter_entry = tk.Entry(cleanup_filter_frame, width=70)
cleanup_filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=1)

cleanup_delete_files_var = tk.BooleanVar(value=False)
tk.Checkbutton(cleanup_frame, text="Also delete the image files from disk (permanent, cannot be undone)",
               variable=cleanup_delete_files_var).pack(anchor=tk.W, pady=(4, 0))

cleanup_status_label = tk.Label(cleanup_frame, text="", fg="gray", wraplength=680, justify=tk.LEFT)
cleanup_status_label.pack(anchor=tk.W, pady=(4, 4))


def _cleanup_build_filter() -> dict:
    """Custom filter (if typed) wins outright over the session dropdown
    — same "one or the other, not merged" behavior kept simple on
    purpose for a destructive action. Raises ValueError if neither is
    set, so the caller can't accidentally build an empty {} filter
    (which would match — and delete — every single object)."""
    custom_text = cleanup_filter_entry.get().strip()
    if custom_text:
        try:
            custom_filter = json.loads(custom_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Custom filter isn't valid JSON: {e}")
        query_safety.validate_filter(custom_filter)
        if not custom_filter:
            raise ValueError("Custom filter can't be empty — that would match every object.")
        return custom_filter

    session = cleanup_session_combo.get().strip()
    if session and not session.startswith("("):
        return {"session_id": session}

    raise ValueError("Pick a session from the dropdown, or enter a custom filter, first.")


def cleanup_preview_count():
    try:
        mongo_filter = _cleanup_build_filter()
    except ValueError as e:
        messagebox.showerror("Invalid filter", str(e))
        return
    cleanup_status_label.config(text="Counting matches...", fg="gray")

    def worker():
        try:
            count = mongo_client.count_objects(mongo_filter)
            msg, color = f"{count} object(s) match this filter.", "blue"
        except Exception as e:
            msg, color = f"Count failed: {e}", "red"
        root.after(0, lambda: cleanup_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


def cleanup_delete_matching():
    try:
        mongo_filter = _cleanup_build_filter()
    except ValueError as e:
        messagebox.showerror("Invalid filter", str(e))
        return

    delete_files = cleanup_delete_files_var.get()
    if not messagebox.askyesno(
            "Bulk delete",
            f"Permanently delete every object (and its linked images) matching:\n\n"
            f"{json.dumps(mongo_filter)}\n\n"
            + ("Image FILES on disk will also be deleted.\n\n" if delete_files
               else "Image files on disk will be left alone (Mongo records only).\n\n")
            + "This cannot be undone. Continue?"):
        return

    cleanup_status_label.config(text="Deleting...", fg="gray")

    def worker():
        try:
            objs_deleted, imgs_deleted, image_paths = mongo_client.delete_objects_bulk(mongo_filter)
            files_deleted, file_errors = 0, 0
            if delete_files:
                for raw_path in image_paths:
                    try:
                        resolved = resolve_image_path(raw_path)
                        if os.path.exists(resolved):
                            os.remove(resolved)
                            files_deleted += 1
                    except Exception:
                        file_errors += 1
            msg = f"Deleted {objs_deleted} object(s), {imgs_deleted} image record(s)."
            if delete_files:
                msg += f" {files_deleted} image file(s) removed from disk"
                if file_errors:
                    msg += f" ({file_errors} could not be removed — see console)"
                msg += "."
            color = "green"
        except Exception as e:
            msg, color = f"Delete failed: {e}", "red"

        def apply():
            cleanup_status_label.config(text=msg, fg=color)
            try:
                refresh_objects_list()
                refresh_images_list()
                refresh_inventory_list()
            except NameError:
                pass

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


cleanup_btn_row = tk.Frame(cleanup_frame)
cleanup_btn_row.pack(anchor=tk.W, pady=(2, 0))
tk.Button(cleanup_btn_row, text="Preview Count", command=cleanup_preview_count,
          bg="lightblue").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(cleanup_btn_row, text="Delete Matching", command=cleanup_delete_matching,
          bg="salmon").pack(side=tk.LEFT)

root.after(0, _populate_cleanup_sessions)


# =====================================================================
# ASK (optional secondary NL layer — langchain-mongodb agent via local
# Ollama). See vision/services/mongo_nlp_agent.py's module docstring for
# why this is secondary and what it needs. Disabled automatically (not
# just erroring on click) if Ollama/the toolkit aren't available, so the
# rest of the Database tab keeps working with zero dependency on this
# being set up.
# =====================================================================
nl_query_frame = tk.LabelFrame(tab_database, text=" Ask (optional local AI) ", padx=10, pady=10)
nl_query_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

nl_query_status_label = tk.Label(nl_query_frame, text="Checking local AI availability...", fg="gray")
nl_query_status_label.pack(anchor=tk.W)

# ---- Model selection: whatever's actually installed in Ollama right
# now, plus a note about tool-calling-capable models worth installing
# that aren't. Selecting a different model re-checks availability
# against THAT model (installed doesn't necessarily mean "supports tool
# calling well" — see mongo_nlp_agent.RECOMMENDED_MODELS' notes) and
# uses it for every subsequent "Ask".
nl_model_row = tk.Frame(nl_query_frame)
nl_model_row.pack(fill=tk.X, pady=(4, 0))
tk.Label(nl_model_row, text="Model:").pack(side=tk.LEFT)
nl_model_selector = ttk.Combobox(nl_model_row, width=22, state="readonly")
nl_model_selector.pack(side=tk.LEFT, padx=(4, 8))
tk.Button(nl_model_row, text="Refresh models",
          command=lambda: _refresh_nl_model_list()).pack(side=tk.LEFT)

nl_recommended_label = tk.Label(nl_query_frame, text="", fg="gray", font=("Arial", 8),
                                 wraplength=680, justify=tk.LEFT)
nl_recommended_label.pack(anchor=tk.W, pady=(2, 0))

nl_query_input_row = tk.Frame(nl_query_frame)
nl_query_input_row.pack(fill=tk.X, pady=(4, 2))

tk.Label(nl_query_input_row, text="Question:").pack(side=tk.LEFT)
nl_query_entry = tk.Entry(nl_query_input_row)
nl_query_entry.pack(side=tk.LEFT, fill=tk.X, expand=1, padx=4)

nl_query_ask_btn = tk.Button(nl_query_input_row, text="Ask", state=tk.DISABLED)
nl_query_ask_btn.pack(side=tk.LEFT, padx=(4, 0))

nl_query_answer_label = tk.Label(nl_query_frame, text="", fg="gray", wraplength=680, justify=tk.LEFT)
nl_query_answer_label.pack(anchor=tk.W, pady=(4, 0))


def _refresh_nl_model_list():
    """Populates the model dropdown from Ollama's actual installed
    models, and lists any RECOMMENDED_MODELS entries not yet installed
    underneath with their `ollama pull` command — so recommendations
    are always shown alongside, never instead of, what's really there.
    Re-checks availability for whatever ends up selected."""
    def worker():
        installed = mongo_nlp_agent.list_installed_models()

        def apply():
            values = installed if installed else [NLP_AGENT_MODEL]
            nl_model_selector["values"] = values
            # Prefer NLP_AGENT_MODEL if it's actually installed; otherwise
            # just default to whatever's first rather than a model that
            # isn't there.
            default = NLP_AGENT_MODEL if NLP_AGENT_MODEL in values else values[0]
            nl_model_selector.set(default)

            not_installed = [
                m for m in mongo_nlp_agent.RECOMMENDED_MODELS
                if not any(name.startswith(m["name"]) for name in installed)
            ]
            if not_installed:
                lines = "; ".join(f'{m["name"]} ({m["note"]}) — ollama pull {m["name"]}'
                                   for m in not_installed)
                nl_recommended_label.config(text=f"Recommended, not installed: {lines}")
            else:
                nl_recommended_label.config(text="All recommended tool-calling models are installed.")

            _check_nl_query_availability()

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def _check_nl_query_availability():
    """Runs at startup and whenever the model selection changes, to
    enable/disable the Ask button based on whether Ollama + the
    langchain-mongodb toolkit are actually available FOR THE CURRENTLY
    SELECTED MODEL. Never crashes the GUI — worst case the feature just
    stays disabled with an explanatory error, while standard MongoDB
    browsing above keeps working."""
    model = nl_model_selector.get() or NLP_AGENT_MODEL

    def worker():
        try:
            mongo_nlp_agent.check_agent_available(model)
            msg, color, enabled = f"Local AI ready ({model})", "green", tk.NORMAL
        except Exception as e:
            msg, color, enabled = f"Local AI unavailable — disabled: {e}", "red", tk.DISABLED

        def apply():
            nl_query_status_label.config(text=msg, fg=color)
            nl_query_ask_btn.config(state=enabled)

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def run_nl_query_from_gui():
    """Runs the NL question through the langchain-mongodb agent
    (vision.services.mongo_nlp_agent), using whichever model is
    currently selected in the dropdown, and shows its answer text.
    Unlike the old deepseek_query layer, this doesn't repopulate the
    Objects listbox — the agent's generate -> validate -> execute steps
    and result are summarized in its own answer instead."""
    question = nl_query_entry.get().strip()
    if not question:
        return
    model = nl_model_selector.get() or NLP_AGENT_MODEL

    nl_query_ask_btn.config(state=tk.DISABLED)
    nl_query_answer_label.config(text="Thinking...", fg="gray")

    def worker():
        try:
            answer = mongo_nlp_agent.ask(question, model=model)
            err = None
        except mongo_nlp_agent.MongoNLPAgentError as e:
            answer, err = None, str(e)
        except Exception as e:
            answer, err = None, str(e)

        def apply():
            nl_query_ask_btn.config(state=tk.NORMAL)
            if err is not None:
                nl_query_answer_label.config(text=f"Couldn't run that query: {err}", fg="red")
            else:
                nl_query_answer_label.config(text=answer, fg="black")

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


nl_query_ask_btn.config(command=run_nl_query_from_gui)
nl_query_entry.bind("<Return>", lambda event: run_nl_query_from_gui())
nl_model_selector.bind("<<ComboboxSelected>>", lambda event: _check_nl_query_availability())

root.after(0, _refresh_nl_model_list)

# =====================================================================
# SYNC & STORAGE TAB — everything about getting data OUT of / INTO
# MongoDB via Excel/JSON/photos, plus managing the folders that data
# lives in on disk. Split out from the Database tab (which is just
# browsing/querying/deleting what's live in MongoDB) and from Data
# Collection (which is about producing new captures), so this tab is
# purely "move data between Mongo and files, and manage those files."
# =====================================================================
tk.Label(tab_sync_storage, text="Import / Export / Sync",
         font=("Arial", 12, "bold")).pack(pady=(10, 5))

# =====================================================================
# CSV / EXCEL — regenerated report + (mode-dependent) reconcile.
# See vision/storage/csv_logger.py and vision/storage/excel_export.py.
# Every capture always appends to the CSV log and (best-effort) refreshes
# this report already — these buttons are for an on-demand full/manual
# refresh, and for pulling hand-edited Excel values back into MongoDB.
# =====================================================================
excel_frame = tk.LabelFrame(tab_sync_storage, text=" CSV / Excel / JSON Report ", padx=10, pady=10)
excel_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(excel_frame, text=f"Mode: {DATA_AUTHORITY_MODE}  "
         f"({'MongoDB is authoritative; report is generated-only' if DATA_AUTHORITY_MODE == 'mongo' else 'hand-edit the Excel report, then Reconcile to push edits into MongoDB'})",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

excel_status_label = tk.Label(excel_frame, text="", fg="gray")
excel_status_label.pack(anchor=tk.W, pady=(2, 4))

excel_btn_row = tk.Frame(excel_frame)
excel_btn_row.pack(fill=tk.X)


def export_excel_report(session_only: bool = False):
    excel_status_label.config(text="Exporting...", fg="gray")

    def worker():
        try:
            from vision.storage import session_manager
            sid = session_manager.today_session_id() if session_only else None
            path = excel_export.build_report(session_id=sid)
            msg, color = f"Report written to {path}", "green"
        except Exception as e:
            msg, color = f"Export failed: {e}", "red"
        root.after(0, lambda: excel_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


def reconcile_from_excel_gui():
    excel_status_label.config(text="Reconciling...", fg="gray")

    def worker():
        try:
            updated, warnings = excel_export.reconcile_from_excel()
            msg = f"Reconciled {updated} row(s)."
            if warnings:
                msg += f" {len(warnings)} warning(s) — see console."
                for w in warnings:
                    print(f"[RECONCILE] {w}")
            color = "green" if not warnings else "orange"
        except Exception as e:
            msg, color = f"Reconcile failed: {e}", "red"
        root.after(0, lambda: excel_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


tk.Button(excel_btn_row, text="Export Today's Report", command=lambda: export_excel_report(True),
          bg="lightblue").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(excel_btn_row, text="Export Full History", command=lambda: export_excel_report(False),
          bg="lightblue").pack(side=tk.LEFT, padx=4)
if DATA_AUTHORITY_MODE == "excel":
    tk.Button(excel_btn_row, text="Reconcile from Excel", command=reconcile_from_excel_gui,
              bg="khaki").pack(side=tk.LEFT, padx=4)

# ---- JSON report — same metadata as the CSV/Excel report above, as a
# regenerated JSON file (vision.storage.json_logger.build_json_report),
# for anything that would rather read JSON than parse CSV/xlsx. This is
# IN ADDITION to the live captures_log.jsonl every capture already
# appends to automatically (see vision.storage.json_logger's module
# docstring) — these buttons are the on-demand "everything, right now,
# as one JSON file" regenerate, same as the Excel buttons above.
json_status_label = tk.Label(excel_frame, text="", fg="gray", wraplength=680, justify=tk.LEFT)
json_status_label.pack(anchor=tk.W, pady=(6, 4))

json_btn_row = tk.Frame(excel_frame)
json_btn_row.pack(fill=tk.X)


def export_json_report(session_only: bool = False):
    json_status_label.config(text="Exporting JSON...", fg="gray")

    def worker():
        try:
            sid = session_manager.today_session_id() if session_only else None
            path = json_logger.build_json_report(session_id=sid)
            msg, color = f"JSON report written to {path}", "green"
        except Exception as e:
            msg, color = f"JSON export failed: {e}", "red"
        root.after(0, lambda: json_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


tk.Button(json_btn_row, text="Export Today's JSON", command=lambda: export_json_report(True),
          bg="lightblue").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(json_btn_row, text="Export Full History JSON", command=lambda: export_json_report(False),
          bg="lightblue").pack(side=tk.LEFT, padx=4)
tk.Label(excel_frame,
         text=f"Live append log (every capture, automatically): "
              f"{os.path.join(JSON_LOG_DIR, JSON_LOG_FILENAME)}",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

# ---- Export by Date Range — same Excel + JSON reports as the buttons
# above, scoped to a specific "YYYY-MM-DD" .. "YYYY-MM-DD" window
# instead of just "today" or "everything" (see mongo_client.
# objects_in_date_range — filters on captured_at, so it's correct even
# when the range spans more than one session/day).
excel_range_frame = tk.Frame(excel_frame)
excel_range_frame.pack(fill=tk.X, pady=(8, 0))
tk.Label(excel_range_frame, text="Export by Date Range:", font=("Arial", 9, "bold")
          ).grid(row=0, column=0, columnspan=4, sticky=tk.W)
tk.Label(excel_range_frame, text="Start (YYYY-MM-DD):").grid(row=1, column=0, sticky=tk.W, pady=(2, 0))
excel_range_start_entry = tk.Entry(excel_range_frame, width=12)
excel_range_start_entry.grid(row=1, column=1, sticky=tk.W, padx=(4, 16), pady=(2, 0))
tk.Label(excel_range_frame, text="End (YYYY-MM-DD):").grid(row=1, column=2, sticky=tk.W, pady=(2, 0))
excel_range_end_entry = tk.Entry(excel_range_frame, width=12)
excel_range_end_entry.grid(row=1, column=3, sticky=tk.W, padx=(4, 0), pady=(2, 0))

excel_range_btn_row = tk.Frame(excel_frame)
excel_range_btn_row.pack(fill=tk.X, pady=(4, 0))


def _read_date_range_or_error(start_entry: tk.Entry, end_entry: tk.Entry):
    """Shared helper — pulls/validates the two date fields, shows a
    messagebox and returns None if either is blank or malformed (raw
    format errors are caught here; start-after-end is still caught by
    mongo_client.objects_in_date_range down in the worker thread)."""
    start_date = start_entry.get().strip()
    end_date = end_entry.get().strip()
    if not start_date or not end_date:
        messagebox.showerror("Date range required", "Enter both a start and end date (YYYY-MM-DD).")
        return None
    for label, value in (("Start", start_date), ("End", end_date)):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("Invalid date", f"{label} date '{value}' isn't in YYYY-MM-DD form.")
            return None
    return start_date, end_date


def export_excel_and_json_range():
    parsed = _read_date_range_or_error(excel_range_start_entry, excel_range_end_entry)
    if parsed is None:
        return
    start_date, end_date = parsed
    excel_status_label.config(text=f"Exporting {start_date}..{end_date} (Excel)...", fg="gray")
    json_status_label.config(text=f"Exporting {start_date}..{end_date} (JSON)...", fg="gray")

    def worker():
        try:
            xlsx_path = excel_export.build_report(start_date=start_date, end_date=end_date)
            xlsx_msg, xlsx_color = f"Report written to {xlsx_path}", "green"
        except Exception as e:
            xlsx_msg, xlsx_color = f"Excel export failed: {e}", "red"
        try:
            json_path = json_logger.build_json_report(start_date=start_date, end_date=end_date)
            json_msg, json_color = f"JSON report written to {json_path}", "green"
        except Exception as e:
            json_msg, json_color = f"JSON export failed: {e}", "red"

        def apply():
            excel_status_label.config(text=xlsx_msg, fg=xlsx_color)
            json_status_label.config(text=json_msg, fg=json_color)
        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


tk.Button(excel_range_btn_row, text="Export Range (Excel + JSON)",
          command=export_excel_and_json_range, bg="lightblue").pack(side=tk.LEFT)

# =====================================================================
# DATA PACKAGE — export a self-contained folder (images + a portable
# CSV, relative paths) for handing captured data off to another
# machine, archiving it, or backing it up; import reads one of these
# folders back in and replays each row through the same
# capture_pipeline.record_capture() every other capture path uses, so
# an imported object gets identical session/catalog/CSV/Excel
# treatment. See vision/storage/package_export.py's module docstring
# for why this is a separate thing from the live CSV/Excel report
# above (that one uses THIS machine's absolute image paths — not
# portable to a different machine on its own; a package's paths are
# relative to the package folder and its images are physically copied
# alongside it).
# =====================================================================
package_frame = tk.LabelFrame(tab_sync_storage, text=" Data Package (Export / Import) ", padx=10, pady=10)
package_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(package_frame,
         text="Export copies images + a portable CSV into one folder you can move to another "
              "machine, back up, or archive. Import reads that folder back in.",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

package_status_label = tk.Label(package_frame, text="", fg="gray", wraplength=680, justify=tk.LEFT)
package_status_label.pack(anchor=tk.W, pady=(2, 4))

package_btn_row = tk.Frame(package_frame)
package_btn_row.pack(fill=tk.X)


def export_package_gui(all_history: bool):
    parent_dir = filedialog.askdirectory(title="Choose where to create the export folder")
    if not parent_dir:
        return  # user cancelled
    folder_name = f"export_{'all_history' if all_history else session_manager.today_session_id()}_{datetime.now().strftime('%H%M%S')}"
    dest_dir = os.path.join(parent_dir, folder_name)

    package_status_label.config(text="Exporting package...", fg="gray")

    def worker():
        try:
            path, count, warnings = package_export.export_package(dest_dir, all_history=all_history)
            msg = f"Exported {count} object(s) to {path}."
            if warnings:
                msg += f" {len(warnings)} warning(s) — see console."
                for w in warnings:
                    print(f"[EXPORT PACKAGE] {w}")
            color = "green" if not warnings else "orange"
        except Exception as e:
            msg, color = f"Export failed: {e}", "red"
        root.after(0, lambda: package_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


def import_package_gui():
    package_dir = filedialog.askdirectory(title="Choose a package folder to import (must contain captures_log.csv)")
    if not package_dir:
        return  # user cancelled

    if not messagebox.askyesno(
            "Import package",
            f"Import every capture from:\n{package_dir}\n\n"
            f"Each row becomes a NEW object in this machine's MongoDB (fresh "
            f"object_id, images copied into local storage) — this does not "
            f"overwrite or deduplicate against anything already here. Continue?"):
        return

    package_status_label.config(text="Importing package...", fg="gray")

    def worker():
        try:
            imported, skipped, warnings = package_export.import_package(package_dir)
            msg = f"Imported {imported} object(s), skipped {skipped}."
            if warnings:
                msg += f" {len(warnings)} warning(s) — see console."
                for w in warnings:
                    print(f"[IMPORT PACKAGE] {w}")
            color = "green" if not warnings and skipped == 0 else "orange"
        except Exception as e:
            msg, color = f"Import failed: {e}", "red"

        def apply():
            package_status_label.config(text=msg, fg=color)
            try:
                refresh_objects_list()
                refresh_images_list()
                refresh_inventory_list()
            except NameError:
                pass

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


tk.Button(package_btn_row, text="Export Today's Package", command=lambda: export_package_gui(False),
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(package_btn_row, text="Export Full History Package", command=lambda: export_package_gui(True),
          bg="lightgreen").pack(side=tk.LEFT, padx=4)
tk.Button(package_btn_row, text="Import Package...", command=import_package_gui,
          bg="khaki").pack(side=tk.LEFT, padx=4)

# ---- Export Package by Date Range — same portable images+CSV+JSON
# folder as the buttons above, scoped to a "YYYY-MM-DD" .. "YYYY-MM-DD"
# window instead of just "today" or "everything".
package_range_frame = tk.Frame(package_frame)
package_range_frame.pack(fill=tk.X, pady=(8, 0))
tk.Label(package_range_frame, text="Export Package by Date Range:", font=("Arial", 9, "bold")
          ).grid(row=0, column=0, columnspan=5, sticky=tk.W)
tk.Label(package_range_frame, text="Start (YYYY-MM-DD):").grid(row=1, column=0, sticky=tk.W, pady=(2, 0))
package_range_start_entry = tk.Entry(package_range_frame, width=12)
package_range_start_entry.grid(row=1, column=1, sticky=tk.W, padx=(4, 16), pady=(2, 0))
tk.Label(package_range_frame, text="End (YYYY-MM-DD):").grid(row=1, column=2, sticky=tk.W, pady=(2, 0))
package_range_end_entry = tk.Entry(package_range_frame, width=12)
package_range_end_entry.grid(row=1, column=3, sticky=tk.W, padx=(4, 8), pady=(2, 0))


def export_package_range_gui():
    parsed = _read_date_range_or_error(package_range_start_entry, package_range_end_entry)
    if parsed is None:
        return
    start_date, end_date = parsed

    parent_dir = filedialog.askdirectory(title="Choose where to create the export folder")
    if not parent_dir:
        return  # user cancelled
    folder_name = f"export_{start_date}_to_{end_date}_{datetime.now().strftime('%H%M%S')}"
    dest_dir = os.path.join(parent_dir, folder_name)

    package_status_label.config(text=f"Exporting {start_date}..{end_date} package...", fg="gray")

    def worker():
        try:
            path, count, warnings = package_export.export_package(
                dest_dir, start_date=start_date, end_date=end_date)
            msg = f"Exported {count} object(s) to {path}."
            if warnings:
                msg += f" {len(warnings)} warning(s) — see console."
                for w in warnings:
                    print(f"[EXPORT PACKAGE] {w}")
            color = "green" if not warnings else "orange"
        except Exception as e:
            msg, color = f"Export failed: {e}", "red"
        root.after(0, lambda: package_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


tk.Button(package_range_frame, text="Export Range Package...", command=export_package_range_gui,
          bg="lightgreen").grid(row=1, column=4, sticky=tk.W, padx=(4, 0), pady=(2, 0))

# =====================================================================
# STORAGE LOCATION (PERMANENT) — where everything above actually lives
# on disk: images, CSV/JSON logs, the Excel report. See
# vision.storage.storage_location's module docstring for why this
# exists — short version: a path relative to the repo folder meant
# deleting/replacing the repo silently orphaned every photo/report ever
# captured. The chosen root is persisted OUTSIDE the repo (in the
# user's home directory), so it — and the data at it — survive that.
# =====================================================================
storage_loc_frame = tk.LabelFrame(tab_sync_storage, text=" Storage Location (Permanent) ", padx=10, pady=10)
storage_loc_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(storage_loc_frame,
         text="Where images, CSV/JSON logs, and the Excel report are actually written. "
              "Persisted outside this folder, so it (and the data at it) survive deleting "
              "or replacing this repo/app folder. Switching does NOT move existing files — "
              "use \"Manage Storage Folders\" below if you want to consolidate an old "
              "location into the new one.",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

storage_loc_path_label = tk.Label(storage_loc_frame, text="", font=("Arial", 9, "bold"),
                                   wraplength=680, justify=tk.LEFT, anchor="w")
storage_loc_path_label.pack(fill=tk.X, pady=(6, 4))


def refresh_storage_location_label():
    storage_loc_path_label.config(text=f"Current: {storage_location.get_storage_root()}")


def choose_storage_location():
    path = filedialog.askdirectory(title="Choose a folder for permanent storage")
    if not path:
        return
    try:
        storage_location.set_storage_root(path)
    except Exception as e:
        messagebox.showerror("Could not set storage location", str(e))
        return
    refresh_storage_location_label()
    messagebox.showinfo(
        "Storage location updated",
        f"New captures/exports will now be written to:\n{path}\n\n"
        f"Existing files at the previous location were NOT moved.")


def reset_storage_location_to_default():
    if not messagebox.askyesno(
            "Reset to default",
            f"Reset the storage location to the default?\n\n{storage_location.DEFAULT_STORAGE_ROOT}\n\n"
            f"Existing files at the current location will NOT be moved."):
        return
    try:
        storage_location.set_storage_root(storage_location.DEFAULT_STORAGE_ROOT)
    except Exception as e:
        messagebox.showerror("Could not reset storage location", str(e))
        return
    refresh_storage_location_label()


storage_loc_btn_row = tk.Frame(storage_loc_frame)
storage_loc_btn_row.pack(anchor=tk.W, pady=(2, 0))
tk.Button(storage_loc_btn_row, text="Choose Folder...", command=choose_storage_location,
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(storage_loc_btn_row, text="Reset to Default", command=reset_storage_location_to_default
          ).pack(side=tk.LEFT, padx=4)
tk.Button(storage_loc_btn_row, text="Refresh", command=refresh_storage_location_label,
          bg="lightblue").pack(side=tk.LEFT, padx=4)

refresh_storage_location_label()

# =====================================================================
# MANAGE STORAGE FOLDERS — pure file-level tools for the folders the
# Excel reports and exported package photos actually live in (see
# vision.storage.package_export's "FOLDER-LEVEL MERGE / SYNC" section).
# Separate from the Data Package Export/Import above: those talk to
# MongoDB (export reads Mongo, import writes to it); this section only
# ever touches folders on disk — merging two exported packages
# together, two-way syncing them, or deleting one that's just taking
# up space (e.g. left over from a previous experiment).
# =====================================================================
folders_frame = tk.LabelFrame(tab_sync_storage, text=" Manage Storage Folders ", padx=10, pady=10)
folders_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(folders_frame,
         text="Merge, sync, or delete exported package folders (each one holding "
              "captures_log.csv/.json + an images/ folder — see Data Package above). "
              "Purely file-level: nothing here touches MongoDB.",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

folders_pick_frame = tk.Frame(folders_frame)
folders_pick_frame.pack(fill=tk.X, pady=(6, 2))
tk.Label(folders_pick_frame, text="Folder A:").grid(row=0, column=0, sticky=tk.W)
folder_a_entry = tk.Entry(folders_pick_frame, width=48)
folder_a_entry.grid(row=0, column=1, sticky=tk.W, padx=(4, 4))
tk.Button(folders_pick_frame, text="Browse...",
          command=lambda: _browse_into(folder_a_entry)).grid(row=0, column=2, sticky=tk.W)

tk.Label(folders_pick_frame, text="Folder B:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
folder_b_entry = tk.Entry(folders_pick_frame, width=48)
folder_b_entry.grid(row=1, column=1, sticky=tk.W, padx=(4, 4), pady=(4, 0))
tk.Button(folders_pick_frame, text="Browse...",
          command=lambda: _browse_into(folder_b_entry)).grid(row=1, column=2, sticky=tk.W, pady=(4, 0))


def _browse_into(entry_widget: tk.Entry):
    path = filedialog.askdirectory(title="Choose a folder")
    if path:
        entry_widget.delete(0, tk.END)
        entry_widget.insert(0, path)


folders_status_label = tk.Label(folders_frame, text="", fg="gray", wraplength=680, justify=tk.LEFT)
folders_status_label.pack(anchor=tk.W, pady=(6, 4))


def _both_folders_or_error():
    a = folder_a_entry.get().strip()
    b = folder_b_entry.get().strip()
    if not a or not b:
        messagebox.showerror("Pick both folders", "Choose both Folder A and Folder B first.")
        return None
    if not os.path.isdir(a):
        messagebox.showerror("Not found", f"Folder A doesn't exist:\n{a}")
        return None
    if not os.path.isdir(b):
        messagebox.showerror("Not found", f"Folder B doesn't exist:\n{b}")
        return None
    if os.path.abspath(a) == os.path.abspath(b):
        messagebox.showerror("Same folder picked twice", "Folder A and Folder B must be different.")
        return None
    return a, b


def merge_folders_into_new():
    picked = _both_folders_or_error()
    if picked is None:
        return
    a, b = picked
    dest = filedialog.askdirectory(title="Choose (or create) a destination folder for the merged result")
    if not dest:
        return
    folders_status_label.config(text="Merging...", fg="gray")

    def worker():
        try:
            count, warnings = package_export.merge_packages([a, b], dest)
            msg = f"Merged {count} object(s) into '{dest}'."
            if warnings:
                msg += f" {len(warnings)} warning(s) — see console."
                for w in warnings:
                    print(f"[MERGE FOLDERS] {w}")
            color = "green" if not warnings else "orange"
        except Exception as e:
            msg, color = f"Merge failed: {e}", "red"
        root.after(0, lambda: folders_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


def sync_folders_two_way():
    picked = _both_folders_or_error()
    if picked is None:
        return
    a, b = picked
    if not messagebox.askyesno(
            "Two-way sync",
            f"Sync Folder A and Folder B so both end up holding the union of what either "
            f"has (images + combined captures_log.csv/.json)?\n\nA: {a}\nB: {b}\n\n"
            f"Existing files in each folder are kept; nothing is deleted."):
        return
    folders_status_label.config(text="Syncing...", fg="gray")

    def worker():
        try:
            count_a, warnings_a = package_export.merge_packages([a, b], a)
            count_b, warnings_b = package_export.merge_packages([a, b], b)
            warnings = warnings_a + warnings_b
            msg = f"Synced — both folders now hold {max(count_a, count_b)} object(s)."
            if warnings:
                msg += f" {len(warnings)} warning(s) — see console."
                for w in warnings:
                    print(f"[SYNC FOLDERS] {w}")
            color = "green" if not warnings else "orange"
        except Exception as e:
            msg, color = f"Sync failed: {e}", "red"
        root.after(0, lambda: folders_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


folders_btn_row = tk.Frame(folders_frame)
folders_btn_row.pack(anchor=tk.W, pady=(2, 0))
tk.Button(folders_btn_row, text="Merge A + B into New Folder...", command=merge_folders_into_new,
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(folders_btn_row, text="Sync A <-> B (two-way)", command=sync_folders_two_way,
          bg="lightblue").pack(side=tk.LEFT, padx=4)

# ---- Delete a folder — separate row, separate confirmation, since this
# one is destructive and irreversible (unlike merge/sync above, which
# only ever ADD files).
tk.Label(folders_frame, text="Delete a folder (e.g. an old export left over from a "
                              "previous experiment) — permanent, cannot be undone:",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 2))

delete_folder_row = tk.Frame(folders_frame)
delete_folder_row.pack(fill=tk.X, pady=(0, 2))
delete_folder_entry = tk.Entry(delete_folder_row, width=48)
delete_folder_entry.grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
tk.Button(delete_folder_row, text="Browse...",
          command=lambda: _browse_into(delete_folder_entry)).grid(row=0, column=1, sticky=tk.W)


def delete_storage_folder():
    folder = delete_folder_entry.get().strip()
    if not folder:
        messagebox.showerror("Pick a folder", "Choose a folder to delete first.")
        return
    if not os.path.isdir(folder):
        messagebox.showerror("Not found", f"Folder doesn't exist:\n{folder}")
        return
    if not package_export.looks_like_package_folder(folder):
        if not messagebox.askyesno(
                "Doesn't look like a package folder",
                f"'{folder}' doesn't contain a captures_log.csv or an images/ folder — "
                f"are you sure this is the right folder to delete?"):
            return
    if not messagebox.askyesno(
            "Delete folder",
            f"Permanently delete this folder and everything in it?\n\n{folder}\n\n"
            f"This cannot be undone."):
        return
    # Second, explicit confirmation for a destructive, irreversible disk
    # operation — matches the Cleanup tab's bulk-Mongo-delete pattern
    # (preview/confirm before anything irreversible happens), just via
    # a second dialog instead of a preview-count step since there's no
    # meaningful "count" to preview for a folder delete.
    if not messagebox.askyesno("Are you sure?", "Really delete this folder? Last chance to cancel."):
        return

    folders_status_label.config(text="Deleting...", fg="gray")

    def worker():
        try:
            shutil.rmtree(folder)
            msg, color = f"Deleted '{folder}'.", "green"
        except Exception as e:
            msg, color = f"Delete failed: {e}", "red"
        root.after(0, lambda: folders_status_label.config(text=msg, fg=color))

    threading.Thread(target=worker, daemon=True).start()


tk.Button(folders_frame, text="Delete Folder...", command=delete_storage_folder,
          bg="salmon").pack(anchor=tk.W, pady=(2, 0))


# =====================================================================
# DATA COLLECTION TAB — "pick up an object, rotate it via J4, take N
# photos, log ONE object with all N images." Works in three
# control_mode_var states:
#   - "physical_manual" / "demo": this machine drives the arm + camera
#     directly (mirrors the proven run_automatic_capture_sequence loop
#     a few hundred lines up, but logs through capture_pipeline.
#     record_capture() once at the end instead of the old 4DAI REST
#     submission flow that function still uses).
#   - "middleman_other" (Remote Control): this machine has no local arm/
#     camera. It drives J4 moves and requests captures over MQTT via
#     other_side_controller, coordinating the async photo bundles with
#     vision.services.rotation_coordinator so all N views land under
#     ONE object, then calls record_capture() itself once done — this
#     machine's Mongo/CSV/Excel is authoritative either way, matching
#     "the controller still has all of this work, and Excel is used
#     for data collection there."
#   - "middleman_physical" (Robot Side): this machine's arm/camera are
#     being driven remotely — the Start button here is refused with an
#     explanatory message, since starting a second, locally-initiated
#     sequence at the same time as a remote-driven one would fight over
#     the same hardware.
# =====================================================================
tk.Label(tab_data_collection,
         text="Pick up an object (Arm tab), then rotate + photograph it here to "
              "log it as one object with multiple views.",
         font=("Arial", 10, "bold"), wraplength=680, justify=tk.LEFT
         ).pack(anchor=tk.W, padx=10, pady=(10, 4))

dc_mode_note_label = tk.Label(tab_data_collection, text="", wraplength=680, justify=tk.LEFT)
dc_mode_note_label.pack(anchor=tk.W, padx=10)

# ---- Object metadata (what we know before a real classifier exists) ----
dc_meta_frame = tk.LabelFrame(tab_data_collection, text=" Object Info ", padx=10, pady=10)
dc_meta_frame.pack(fill=tk.X, padx=10, pady=(8, 4))

tk.Label(dc_meta_frame, text="Name (required):").grid(row=0, column=0, sticky=tk.W, pady=2)
dc_name_entry = tk.Entry(dc_meta_frame, width=24)
dc_name_entry.grid(row=0, column=1, sticky=tk.W, padx=(4, 20))

tk.Label(dc_meta_frame, text="Category:").grid(row=0, column=2, sticky=tk.W, pady=2)
dc_category_entry = tk.Entry(dc_meta_frame, width=16)
dc_category_entry.grid(row=0, column=3, sticky=tk.W, padx=4)

tk.Label(dc_meta_frame, text="Color:").grid(row=1, column=0, sticky=tk.W, pady=2)
dc_color_entry = tk.Entry(dc_meta_frame, width=24)
dc_color_entry.grid(row=1, column=1, sticky=tk.W, padx=(4, 20))

tk.Label(dc_meta_frame, text="Size:").grid(row=1, column=2, sticky=tk.W, pady=2)
dc_size_entry = tk.Entry(dc_meta_frame, width=16)
dc_size_entry.grid(row=1, column=3, sticky=tk.W, padx=4)

tk.Label(dc_meta_frame,
         text="Leave any of these blank if unknown for now — a classifier can fill "
              "them in later (see vision/model/classifier.py). Position and any "
              "other fixed columns can be set/edited afterward on the Database tab.",
         fg="gray", font=("Arial", 8), wraplength=640, justify=tk.LEFT
         ).grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=(4, 0))

# ---- Rotation sequence controls ----
dc_sweep_frame = tk.LabelFrame(tab_data_collection, text=" Rotation Sweep ", padx=10, pady=10)
dc_sweep_frame.pack(fill=tk.X, padx=10, pady=4)

tk.Label(dc_sweep_frame, text="Number of views:").grid(row=0, column=0, sticky=tk.W, pady=2)
dc_num_views_entry = tk.Entry(dc_sweep_frame, width=6)
dc_num_views_entry.insert(0, str(NUM_VIEWS))
dc_num_views_entry.grid(row=0, column=1, sticky=tk.W, padx=(4, 20))

tk.Label(dc_sweep_frame, text="Degrees per step:").grid(row=0, column=2, sticky=tk.W, pady=2)
dc_degrees_entry = tk.Entry(dc_sweep_frame, width=8)
dc_degrees_entry.insert(0, str(round(360.0 / NUM_VIEWS, 1)))
dc_degrees_entry.grid(row=0, column=3, sticky=tk.W, padx=4)

tk.Label(dc_sweep_frame, text="Settle time (s):").grid(row=1, column=0, sticky=tk.W, pady=2)
dc_interval_entry = tk.Entry(dc_sweep_frame, width=6)
dc_interval_entry.insert(0, str(VIEW_SETTLE_SECONDS))
dc_interval_entry.grid(row=1, column=1, sticky=tk.W, padx=(4, 20))

# ---- Manual Snapshot — the Camera tab's one-click, no-arm-movement,
# multi-camera "Capture Photo" flow, available here too so a labeled
# reference/one-off shot can be taken without leaving the Data
# Collection tab. Uses the SAME run_manual_snapshot() helper as the
# Camera tab (defined further down, alongside that tab's UI) — one
# implementation, two entry points — and lands in the "Today's
# Captures" list below just like a rotation sequence would.
dc_manual_frame = tk.LabelFrame(tab_data_collection, text=" Manual Snapshot ", padx=10, pady=10)
dc_manual_frame.pack(fill=tk.X, padx=10, pady=4)

tk.Label(dc_manual_frame,
         text="One-click snapshot from every configured camera (no arm movement, no "
              "rotation) — for a labeled reference photo or one-off shot, separate "
              "from a full rotation sweep above.",
         fg="gray", font=("Arial", 8), wraplength=640, justify=tk.LEFT
         ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 4))

tk.Label(dc_manual_frame, text="Label:").grid(row=1, column=0, sticky=tk.W, pady=2)
dc_manual_label_var = tk.StringVar(value="")
dc_manual_label_combo = ttk.Combobox(
    dc_manual_frame, textvariable=dc_manual_label_var, width=26,
    values=["uncategorized", "test_object", "calibration_shot",
            "reference_photo", "manual_snapshot"])
dc_manual_label_combo.grid(row=1, column=1, sticky=tk.W, padx=(4, 20))

dc_manual_snapshot_btn = tk.Button(dc_manual_frame, text="Capture Snapshot", bg="lightgreen")
dc_manual_snapshot_btn.grid(row=1, column=2, sticky=tk.W, padx=4)

dc_manual_status_label = tk.Label(dc_manual_frame, text="", fg="gray", wraplength=640, justify=tk.LEFT)
dc_manual_status_label.grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=(4, 0))


def start_manual_snapshot_dc():
    label = dc_manual_label_var.get().strip() or _default_manual_snapshot_label()
    run_manual_snapshot(label, dc_manual_status_label, on_done=refresh_dc_today_list)


dc_manual_snapshot_btn.config(command=start_manual_snapshot_dc)

dc_status_label = tk.Label(tab_data_collection, text="", fg="gray", wraplength=680, justify=tk.LEFT)
dc_status_label.pack(anchor=tk.W, padx=10, pady=(4, 0))

dc_progress = ttk.Progressbar(tab_data_collection, mode="determinate", length=300)
dc_progress.pack(anchor=tk.W, padx=10, pady=(2, 4))

dc_cancel_event = threading.Event()


def _dc_set_status(text: str, color: str = "gray") -> None:
    root.after(0, lambda: dc_status_label.config(text=text, fg=color))


def _dc_set_progress(done: int, total: int) -> None:
    def apply():
        dc_progress["maximum"] = max(total, 1)
        dc_progress["value"] = done
    root.after(0, apply)


def _dc_finish_ui(refresh: bool = True) -> None:
    def apply():
        dc_start_btn.config(state=tk.NORMAL)
        dc_cancel_btn.config(state=tk.DISABLED)
        if refresh:
            try:
                refresh_objects_list()
                refresh_images_list()
                refresh_inventory_list()
                refresh_dc_today_list()
            except NameError:
                pass  # Database tab / this tab's own list not built yet — harmless
    root.after(0, apply)


def run_data_collection_rotation_local(name, category, color, size,
                                        num_views, degrees_per_step, interval_seconds):
    """Local-camera version — this machine drives the arm (if connected;
    DEMO MODE otherwise) and its own configured camera(s) directly.
    Mirrors run_automatic_capture_sequence's proven move+capture loop
    (same base-joint/hard-deck handling), but logs through
    capture_pipeline.record_capture() once at the end — ONE object,
    num_views * num_cameras images — instead of the old 4DAI REST
    submission flow that function still uses."""
    all_pairs = []  # list of (source, path) — see record_capture()'s docstring
                     # for why this must be a list, not a dict, once the same
                     # camera contributes more than one image (every view here).
    object_id = None
    try:
        base_joints = list(robot_data["joints"]) if robot_data["joints"] else [0.0, 0.0, 200.0, 0.0]
        base_j1, base_j2, base_z, base_j4 = (base_joints + [0.0, 0.0, 200.0, 0.0])[:4]
        hard_deck_error = _hard_deck_violation(base_z, "Data Collection rotation sequence")
        if hard_deck_error:
            raise RuntimeError(hard_deck_error)

        cameras_to_use = list_configured_cameras()
        failed_cameras = set()
        sample_id = new_sample_id()  # just a disk-folder id (images/<id>/) — unrelated
                                      # to the Mongo object_id record_capture() mints below

        for i in range(num_views):
            if dc_cancel_event.is_set():
                raise RuntimeError("Cancelled by user.")

            j4_target = _normalize_j4_target(base_j4 + (i * degrees_per_step))
            if ROBOT_CONNECTED and robot:
                move_error = _dispatch_joint_move(
                    [base_j1, base_j2, base_z, j4_target],
                    reason=f"data collection rotation step {i + 1}/{num_views}")
                if move_error is not None:
                    raise RuntimeError(f"Move failed at view {i + 1}: {move_error}")
                robot.movement.sync()
                sync_manual_position_from_feedback("data collection rotation step")
            else:
                print(f"DEMO MODE: rotating to J4={j4_target:.1f} deg (view {i + 1}/{num_views})")

            sleep(interval_seconds)

            _dc_set_status(f"View {i + 1}/{num_views}: capturing...")
            captured_this_step = False
            for camera_name in list(cameras_to_use):
                if camera_name in failed_cameras:
                    continue
                try:
                    frame = capture_frame(camera_name)
                except (RuntimeError, ImportError) as e:
                    print(f"[DATA COLLECTION] '{camera_name}' unavailable: {e}")
                    failed_cameras.add(camera_name)
                    continue
                path = save_image(frame, sample_id, camera_name, i)
                all_pairs.append((camera_name, path))
                captured_this_step = True

            if not captured_this_step:
                print(f"[DATA COLLECTION WARNING] No camera produced a frame for view {i + 1}")
            _dc_set_progress(i + 1, num_views)

        if not all_pairs:
            raise RuntimeError("Rotation sequence produced zero images — check camera connections.")

        object_id, warnings = record_capture(
            name=name, image_paths_by_source=all_pairs,
            category=category or None, color=color or None, size=size or None,
            position=current_arm_position(),  # snapshot at sequence end — None (-> null) if
                                                # the robot isn't connected/no feedback yet
        )
        for w in warnings:
            print(f"[DATA COLLECTION] {w}")
        _dc_set_status(
            f"Done — object {object_id}, {len(all_pairs)} image(s) recorded."
            + (f" ({len(warnings)} warning(s), see console)" if warnings else ""),
            "green" if not warnings else "orange")

    except Exception as e:
        _dc_set_status(f"Rotation sequence failed: {e}", "red")
    finally:
        _dc_finish_ui()
    return object_id


def run_data_collection_rotation_remote(name, category, color, size,
                                         num_views, degrees_per_step, interval_seconds):
    """Middleman 'Other Side' (Remote Control) version — no local arm/
    camera. Drives J4 via other_side_controller.send_move() and
    requests each view's photo via request_capture(object_id=...),
    coordinating the async bundles with rotation_coordinator so all
    num_views land under ONE shared object. THIS machine still owns
    Mongo/CSV/Excel — record_capture() is called here, once, after
    every view arrives, regardless of which machine's camera actually
    took the photos."""
    if other_side_controller is None or not other_side_controller.is_active_controller():
        _dc_set_status(
            "Not the active controller on the connected Robot Side — connect and wait "
            "for 'You are the ACTIVE controller' on the Arm tab first.", "red")
        _dc_finish_ui(refresh=False)
        return None

    object_id = new_sample_id()
    rotation_coordinator.begin_sequence(object_id)
    all_pairs = []
    try:
        for i in range(num_views):
            if dc_cancel_event.is_set():
                raise RuntimeError("Cancelled by user.")

            # absolute targets built up from 0 — Remote Control mode has
            # no reliable "current J4" to add a delta to the way local
            # mode does. Normalized the same way as the local rotation
            # loops (see _normalize_j4_target) so a longer sweep here
            # can't send the Physical Side an out-of-range J4 either.
            j4_target = _normalize_j4_target(i * degrees_per_step)
            if not other_side_controller.send_move({"j4": j4_target}):
                raise RuntimeError(f"Move command for view {i + 1} was refused/blocked "
                                    f"(not active controller, or Robot Side rejected it).")
            sleep(interval_seconds)

            _dc_set_status(f"View {i + 1}/{num_views}: requesting capture...")
            if not other_side_controller.request_capture(object_id=object_id, view_index=i):
                raise RuntimeError(f"Capture request for view {i + 1} was refused/blocked.")

            paths = rotation_coordinator.wait_for_view(object_id, timeout=15.0)
            for source, path in paths.items():
                all_pairs.append((source, path))
            _dc_set_progress(i + 1, num_views)

        if not all_pairs:
            raise RuntimeError("Rotation sequence produced zero images — check the Robot Side's camera.")

        recorded_id, warnings = record_capture(
            name=name, image_paths_by_source=all_pairs,
            category=category or None, color=color or None, size=size or None,
        )
        for w in warnings:
            print(f"[DATA COLLECTION] {w}")
        _dc_set_status(
            f"Done — object {recorded_id}, {len(all_pairs)} image(s) recorded via Remote Control."
            + (f" ({len(warnings)} warning(s), see console)" if warnings else ""),
            "green" if not warnings else "orange")
        object_id = recorded_id

    except Exception as e:
        _dc_set_status(f"Rotation sequence failed: {e}", "red")
    finally:
        rotation_coordinator.end_sequence(object_id)
        _dc_finish_ui()
    return object_id


def start_data_collection_sequence():
    name = dc_name_entry.get().strip()
    if not name:
        messagebox.showerror("Name required", "Enter an object name before starting a rotation sequence.")
        return
    category = dc_category_entry.get().strip()
    color = dc_color_entry.get().strip()
    size = dc_size_entry.get().strip()

    try:
        num_views = int(dc_num_views_entry.get().strip())
        degrees_per_step = float(dc_degrees_entry.get().strip())
        interval_seconds = float(dc_interval_entry.get().strip())
        if num_views < 1:
            raise ValueError("Number of views must be at least 1.")
    except ValueError as e:
        messagebox.showerror("Invalid input", f"Check the rotation sweep fields: {e}")
        return

    mode = control_mode_var.get()
    if mode == "middleman_physical":
        messagebox.showerror(
            "Driven remotely",
            "This machine's arm/camera are in 'Middleman (Robot Side)' mode — a "
            "connected Remote Control machine drives rotation sequences here. Use "
            "the Data Collection tab on THAT machine instead.")
        return

    if not try_start_arm_operation("a Data Collection rotation sequence"):
        return
    runner = run_data_collection_rotation_remote if mode == "middleman_other" \
        else run_data_collection_rotation_local

    dc_cancel_event.clear()
    dc_start_btn.config(state=tk.DISABLED)
    dc_cancel_btn.config(state=tk.NORMAL)
    _dc_set_progress(0, num_views)
    _dc_set_status("Starting rotation sequence...")

    def worker():
        try:
            runner(name, category, color, size, num_views, degrees_per_step, interval_seconds)
        finally:
            finish_arm_operation()

    threading.Thread(target=worker, daemon=True).start()


def cancel_data_collection_sequence():
    dc_cancel_event.set()
    dc_status_label.config(text="Cancelling...", fg="orange")


def _dc_update_mode_note(*_args):
    mode = control_mode_var.get()
    if mode == "middleman_physical":
        dc_mode_note_label.config(
            text="This machine is the Robot Side — rotation sequences here are driven "
                 "by the connected Remote Control machine, not by this Start button.",
            fg="orange")
    elif mode == "middleman_other":
        dc_mode_note_label.config(
            text="Remote Control mode — Start will drive the connected Robot Side's "
                 "arm/camera over the network and record the result in THIS machine's "
                 "Mongo/CSV/Excel.", fg="blue")
    else:
        dc_mode_note_label.config(
            text="Local mode — this machine's own arm and camera(s) will be used directly.",
            fg="gray")


control_mode_var.trace_add("write", _dc_update_mode_note)
_dc_update_mode_note()

dc_btn_row = tk.Frame(tab_data_collection)
dc_btn_row.pack(anchor=tk.W, padx=10, pady=(0, 4))
dc_start_btn = tk.Button(dc_btn_row, text="Start Rotation Capture", bg="lightgreen",
                          font=("Arial", 10, "bold"), command=start_data_collection_sequence)
dc_start_btn.pack(side=tk.LEFT, padx=(0, 4))
dc_cancel_btn = tk.Button(dc_btn_row, text="Cancel", bg="salmon", state=tk.DISABLED,
                           command=cancel_data_collection_sequence)
dc_cancel_btn.pack(side=tk.LEFT)

# ---- "Photo collection can also be managed from there" — today's
# captures, with a way to discard a bad one (e.g. arm bumped the object
# mid-sweep) without leaving Mongo/CSV/Excel out of sync with each
# other, which deleting through Excel or Mongo directly would risk. ----
dc_manage_frame = tk.LabelFrame(tab_data_collection, text=" Today's Captures ", padx=10, pady=10)
dc_manage_frame.pack(fill=tk.BOTH, expand=1, padx=10, pady=(8, 10))

dc_today_listbox = tk.Listbox(dc_manage_frame, height=8)
dc_today_listbox.pack(fill=tk.BOTH, expand=1)
_dc_today_object_ids = []


def refresh_dc_today_list():
    def worker():
        try:
            objects = mongo_client.find_objects(
                {"session_id": session_manager.today_session_id()}, limit=200)
            err = None
        except Exception as e:
            objects, err = None, str(e)

        def apply():
            dc_today_listbox.delete(0, tk.END)
            _dc_today_object_ids.clear()
            if objects is None:
                dc_today_listbox.insert(tk.END, f"Error: {err}")
                return
            if not objects:
                dc_today_listbox.insert(tk.END, "(nothing captured yet today)")
                return
            for o in objects:
                data = o.get("data") or {}
                # There's no forward image_ids array on the object doc —
                # images link back via their own object_id field, so
                # counting them means asking the images side directly
                # (get_images_for_object does the reverse lookup).
                image_count = len(mongo_client.get_images_for_object(o.get("_id")))
                dc_today_listbox.insert(
                    tk.END,
                    f"{data.get('name', '?')}  |  {data.get('category') or '(no category)'}  |  "
                    f"{image_count} image(s)  |  {o.get('_id', '?')}")
                _dc_today_object_ids.append(o.get("_id"))

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def open_dc_today_detail(event=None):
    """Today's Captures list is view-only — deleting a capture happens
    on the Objects tab (Database -> Objects, multi-select "Delete
    Selected") instead, so there's exactly one place that does it.
    Double-click here just jumps to the same detail viewer everywhere
    else uses."""
    selection = dc_today_listbox.curselection()
    if not selection:
        return
    idx = selection[0]
    if idx >= len(_dc_today_object_ids):
        return  # clicked a placeholder row like "(nothing captured...)" or "Error: ..."
    show_object_detail(_dc_today_object_ids[idx])


dc_today_listbox.bind("<Double-Button-1>", open_dc_today_detail)

dc_manage_btn_row = tk.Frame(dc_manage_frame)
dc_manage_btn_row.pack(anchor=tk.W, pady=(4, 0))
tk.Button(dc_manage_btn_row, text="Refresh", command=refresh_dc_today_list,
          bg="lightblue").pack(side=tk.LEFT)

root.after(0, refresh_dc_today_list)

# =====================================================================
# ATTRIBUTE COLUMNS — add/remove fixed columns on the fly (e.g. "Is
# Metal", "Diffusion"), backed by vision.storage.attribute_schema
# (attribute_schema.json). A newly added column immediately shows up
# everywhere that reads the schema live: the object detail viewer
# (shows "null" until a value is set), the next Excel/JSON report
# export, and the Attribute Review viewer below. Existing MongoDB
# documents don't retroactively gain the field — see attribute_schema.
# add_fixed_column()'s docstring — they just read back as missing/null
# until edited (e.g. via Attribute Review).
# =====================================================================
dc_attrs_frame = tk.LabelFrame(tab_data_collection, text=" Attribute Columns ", padx=10, pady=10)
dc_attrs_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(dc_attrs_frame,
         text="Add or remove the fixed attribute columns every capture gets tracked "
              "against (beyond Name/Category/Color/Size) — e.g. \"Is Metal\", "
              "\"Diffusion\". Changes apply immediately to new Excel/JSON exports and "
              "the object detail viewer; existing captures show \"null\" for a new "
              "column until given a value (see Attribute Review below).",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

dc_attrs_list_frame = tk.Frame(dc_attrs_frame)
dc_attrs_list_frame.pack(fill=tk.X, pady=(6, 4))
dc_attrs_listbox = tk.Listbox(dc_attrs_list_frame, height=6)
dc_attrs_listbox.pack(side=tk.LEFT, fill=tk.X, expand=1)
dc_attrs_scrollbar = tk.Scrollbar(dc_attrs_list_frame, orient=tk.VERTICAL, command=dc_attrs_listbox.yview)
dc_attrs_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
dc_attrs_listbox.config(yscrollcommand=dc_attrs_scrollbar.set)

_dc_attr_keys = []  # parallel to dc_attrs_listbox rows — the "removable" ones (fixed_columns only)


def refresh_dc_attrs_list():
    dc_attrs_listbox.delete(0, tk.END)
    _dc_attr_keys.clear()
    schema = attribute_schema.load_schema()
    for c in schema.get("fixed_columns", []):
        dc_attrs_listbox.insert(
            tk.END, f"{c['label']}  (key: {c['key']}, type: {c.get('type', 'string')})")
        _dc_attr_keys.append(c["key"])
    for c in schema.get("position_columns", []):
        dc_attrs_listbox.insert(tk.END, f"{c['label']}  (position column — fixed, not removable here)")
        _dc_attr_keys.append(None)
    for c in schema.get("reserved_columns", []):
        dc_attrs_listbox.insert(
            tk.END, f"{c['label']}  (reserved slot: {c['key']} — rename instead of removing)")
        _dc_attr_keys.append(None)


dc_attrs_add_row = tk.Frame(dc_attrs_frame)
dc_attrs_add_row.pack(fill=tk.X, pady=(2, 0))
tk.Label(dc_attrs_add_row, text="New attribute label (e.g. \"Is Metal\"):").grid(
    row=0, column=0, sticky=tk.W)
dc_attrs_new_label_entry = tk.Entry(dc_attrs_add_row, width=22)
dc_attrs_new_label_entry.grid(row=0, column=1, sticky=tk.W, padx=(4, 16))
tk.Label(dc_attrs_add_row, text="Type:").grid(row=0, column=2, sticky=tk.W)
dc_attrs_new_type_combo = ttk.Combobox(dc_attrs_add_row, width=10, state="readonly",
                                        values=["string", "number", "boolean"])
dc_attrs_new_type_combo.set("string")
dc_attrs_new_type_combo.grid(row=0, column=3, sticky=tk.W, padx=(4, 0))

dc_attrs_status_label = tk.Label(dc_attrs_frame, text="", fg="gray", wraplength=680, justify=tk.LEFT)
dc_attrs_status_label.pack(anchor=tk.W, pady=(4, 2))


def _slugify_attr_key(label: str) -> str:
    """'Is Metal' -> 'is_metal' — a safe, stable Mongo/CSV/JSON field
    key derived from whatever label the user typed."""
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in label.strip())
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "attr"


def add_dc_attribute():
    label = dc_attrs_new_label_entry.get().strip()
    if not label:
        messagebox.showerror("Label required", "Enter a label for the new attribute first.")
        return
    key = _slugify_attr_key(label)
    col_type = dc_attrs_new_type_combo.get() or "string"
    try:
        attribute_schema.add_fixed_column(key, label, col_type=col_type)
        dc_attrs_status_label.config(text=f"Added '{label}' (key: {key}).", fg="green")
        dc_attrs_new_label_entry.delete(0, tk.END)
        refresh_dc_attrs_list()
    except ValueError as e:
        dc_attrs_status_label.config(text=str(e), fg="red")


def remove_dc_attribute():
    selection = dc_attrs_listbox.curselection()
    if not selection:
        return
    idx = selection[0]
    key = _dc_attr_keys[idx] if idx < len(_dc_attr_keys) else None
    if key is None:
        messagebox.showinfo("Not removable here",
                             "Position and reserved columns aren't removable from this list — "
                             "edit attribute_schema.json directly if you really need to.")
        return
    if not messagebox.askyesno(
            "Remove attribute",
            f"Remove the '{key}' column from the schema going forward?\n\n"
            f"This does NOT delete the field from existing MongoDB documents/CSV rows — "
            f"it just stops appearing in newly generated Excel/JSON reports and the "
            f"object detail viewer."):
        return
    attribute_schema.remove_fixed_column(key)
    dc_attrs_status_label.config(text=f"Removed '{key}'.", fg="green")
    refresh_dc_attrs_list()


dc_attrs_btn_row = tk.Frame(dc_attrs_frame)
dc_attrs_btn_row.pack(anchor=tk.W, pady=(4, 0))
tk.Button(dc_attrs_btn_row, text="Add Attribute", command=add_dc_attribute,
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(dc_attrs_btn_row, text="Remove Selected", command=remove_dc_attribute,
          bg="salmon").pack(side=tk.LEFT, padx=4)
tk.Button(dc_attrs_btn_row, text="Refresh List", command=refresh_dc_attrs_list,
          bg="lightblue").pack(side=tk.LEFT, padx=4)

refresh_dc_attrs_list()

# =====================================================================
# ATTRIBUTE REVIEW — upload an exported .xlsx (the "Attribute Data
# Collection" sheet) and step through its rows one object at a time:
# every image for that object, plus editable fields for every current
# fixed column (including anything added above), saved straight back to
# MongoDB per-object as you go (mongo_client.update_object_data — the
# same write reconcile_from_excel does, just interactively and one row
# at a time with the photo right there instead of hand-editing cells
# blind). See vision.storage.excel_export.read_log_rows().
# =====================================================================
dc_review_frame = tk.LabelFrame(tab_data_collection, text=" Attribute Review ", padx=10, pady=10)
dc_review_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(dc_review_frame,
         text="Review the current Excel report (or upload any other exported one) and step "
              "through its captures one at a time — see every image for that object and edit "
              "its attributes (including any you've added above), saved to MongoDB as you go.",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

dc_review_missing_only_var = tk.BooleanVar(value=False)
tk.Checkbutton(dc_review_frame,
               text="Only show captures missing attribute data (any fixed column left blank)",
               variable=dc_review_missing_only_var).pack(anchor=tk.W, pady=(4, 0))

dc_review_status_label = tk.Label(dc_review_frame, text="", fg="gray", wraplength=680, justify=tk.LEFT)
dc_review_status_label.pack(anchor=tk.W, pady=(4, 4))


def _row_is_missing_data(row: dict) -> bool:
    """True if this row's `data` dict is missing (null/blank/"unknown")
    a value for any fixed column other than 'name' — used by the
    "only show captures missing attribute data" filter, both here and
    for the Inventory Attribute Review below."""
    for key in attribute_schema.fixed_column_keys():
        if key == "name":
            continue
        value = row["data"].get(key)
        if value in (None, "", attribute_schema.UNKNOWN):
            return True
    return False


def _apply_missing_filter(rows: list) -> list:
    if not dc_review_missing_only_var.get():
        return rows
    return [r for r in rows if _row_is_missing_data(r)]


def review_current_report():
    """Regenerates the full-history Excel report fresh, then opens the
    Attribute Review viewer against it directly — no file picker
    needed, this is always "whatever's actually in Mongo right now"."""
    dc_review_status_label.config(text="Regenerating current report...", fg="gray")

    def worker():
        try:
            path = excel_export.build_report()
            rows = excel_export.read_log_rows(path)
            err = None
        except Exception as e:
            rows, path, err = None, None, str(e)

        def apply():
            if err is not None:
                dc_review_status_label.config(text=f"Could not build/read report: {err}", fg="red")
                return
            filtered = _apply_missing_filter(rows)
            if not filtered:
                msg = ("No captures found." if not rows
                       else "No captures are missing attribute data — nothing to review!")
                dc_review_status_label.config(text=msg, fg="green" if rows else "orange")
                return
            dc_review_status_label.config(
                text=f"Reviewing {len(filtered)} of {len(rows)} capture(s) from the current report.",
                fg="green")
            open_attribute_review_viewer(filtered)

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def start_attribute_review():
    path = filedialog.askopenfilename(
        title="Choose an exported Excel report to review",
        filetypes=[("Excel files", "*.xlsx")])
    if not path:
        return  # user cancelled

    dc_review_status_label.config(text="Reading sheet...", fg="gray")

    def worker():
        try:
            rows = excel_export.read_log_rows(path)
            err = None
        except Exception as e:
            rows, err = None, str(e)

        def apply():
            if err is not None:
                dc_review_status_label.config(text=f"Could not read '{path}': {err}", fg="red")
                return
            if not rows:
                dc_review_status_label.config(text=f"No rows found in '{path}'.", fg="orange")
                return
            filtered = _apply_missing_filter(rows)
            if not filtered:
                dc_review_status_label.config(
                    text="No captures in this sheet are missing attribute data — nothing to review!",
                    fg="orange")
                return
            dc_review_status_label.config(
                text=f"Reviewing {len(filtered)} of {len(rows)} row(s) from {path}.", fg="green")
            open_attribute_review_viewer(filtered)

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


dc_review_btn_row = tk.Frame(dc_review_frame)
dc_review_btn_row.pack(anchor=tk.W, pady=(2, 0))
tk.Button(dc_review_btn_row, text="Review Current Report", command=review_current_report,
          bg="lightgreen").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(dc_review_btn_row, text="Load & Review Excel Sheet...", command=start_attribute_review,
          bg="lightblue").pack(side=tk.LEFT, padx=4)


def open_attribute_review_viewer(rows: list):
    """
    One row/object at a time: every linked image, plus editable fields
    for every current fixed column and a raw-JSON box for the freeform
    "why" attributes. "Save & Next" writes straight to MongoDB
    (mongo_client.update_object_data) then advances — nothing is queued
    or held back to the end, matching how every other write in this
    app behaves (immediate, one object at a time).
    """
    viewer = tk.Toplevel(root)
    viewer.title(f"Attribute Review — {len(rows)} object(s)")
    viewer.geometry("820x720")

    state = {"index": 0}

    top_frame = tk.Frame(viewer, padx=10, pady=8)
    top_frame.pack(fill=tk.X)
    progress_label = tk.Label(top_frame, text="", font=("Arial", 10, "bold"))
    progress_label.pack(side=tk.LEFT)
    review_status_label = tk.Label(top_frame, text="", fg="gray")
    review_status_label.pack(side=tk.RIGHT)

    body_frame = tk.Frame(viewer)
    body_frame.pack(fill=tk.BOTH, expand=1)

    def render():
        for child in body_frame.winfo_children():
            child.destroy()
        idx = state["index"]
        row = rows[idx]
        object_id = row["object_id"]
        progress_label.config(text=f"Object {idx + 1} of {len(rows)}  —  {object_id}")
        review_status_label.config(text="")

        # --- Attributes panel (editable) ---
        attrs_frame = tk.LabelFrame(body_frame, text=" Attributes ", padx=8, pady=8)
        attrs_frame.pack(fill=tk.X, padx=10, pady=(6, 4))

        labels = attribute_schema.display_labels()
        field_widgets = {}  # key -> Entry/Combobox
        current_data = mongo_client.get_object(object_id) or {}
        live_data = current_data.get("data") or {}
        for key in attribute_schema.fixed_column_keys():
            schema_type = next(
                (c.get("type", "string") for c in attribute_schema.load_schema().get("fixed_columns", [])
                 if c["key"] == key), "string")
            # Prefer the value from the uploaded sheet's row; fall back to
            # whatever's currently live in Mongo if the sheet's cell was
            # blank (e.g. the column was added after that sheet was
            # exported).
            value = row["data"].get(key)
            if value in (None, ""):
                value = live_data.get(key)
            field_row = tk.Frame(attrs_frame)
            field_row.pack(fill=tk.X, pady=1)
            is_missing = value in (None, "")
            tk.Label(field_row, text=f"{labels.get(key, key)}:", font=("Arial", 9, "bold"),
                     width=16, anchor="w",
                     fg="#b35900" if is_missing else "black").pack(side=tk.LEFT)
            if schema_type == "boolean":
                widget = ttk.Combobox(field_row, width=20, state="readonly",
                                       values=["", "true", "false", attribute_schema.UNKNOWN])
                widget.set("" if value in (None, "") else str(value))
            else:
                widget = tk.Entry(field_row, width=40)
                if value not in (None, ""):
                    widget.insert(0, str(value))
            widget.pack(side=tk.LEFT, fill=tk.X, expand=1)
            field_widgets[key] = widget

        tk.Label(attrs_frame, text="Freeform attributes (JSON):", font=("Arial", 9, "bold"),
                 anchor="w").pack(fill=tk.X, pady=(6, 0))
        freeform_text = tk.Text(attrs_frame, height=4, font=("Courier", 8))
        freeform_text.insert("1.0", json.dumps(row.get("freeform") or {}, indent=2))
        freeform_text.pack(fill=tk.X)

        # --- Images panel (read-only, same viewer style as show_object_detail) ---
        images_outer = tk.LabelFrame(body_frame, text=" Images ", padx=8, pady=8)
        images_outer.pack(fill=tk.BOTH, expand=1, padx=10, pady=(4, 6))

        image_docs = mongo_client.get_images_for_object(object_id)
        image_docs.sort(key=lambda d: d.get("captured_at") or datetime.min)

        if not image_docs:
            tk.Label(images_outer, text="No images found for this object.").pack(pady=10)
        elif not _PIL_AVAILABLE:
            tk.Label(images_outer, text="Install Pillow to view images: pip install Pillow",
                     fg="red").pack(pady=10)
        else:
            canvas_frame = tk.Frame(images_outer)
            canvas_frame.pack(fill=tk.BOTH, expand=1)
            scroll_canvas = tk.Canvas(canvas_frame)
            scrollbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=scroll_canvas.yview)
            inner_frame = tk.Frame(scroll_canvas)
            inner_frame.bind("<Configure>", lambda e: scroll_canvas.configure(
                scrollregion=scroll_canvas.bbox("all")))
            scroll_canvas.create_window((0, 0), window=inner_frame, anchor="nw")
            scroll_canvas.configure(yscrollcommand=scrollbar.set)
            scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=1)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            viewer._photo_refs = []
            for doc in image_docs:
                raw_path = doc.get("image_path", "")
                path = resolve_image_path(raw_path)
                src = doc.get("source", "?")
                view_index = doc.get("view_index", "?")
                img_row = tk.Frame(inner_frame, pady=6)
                img_row.pack(fill=tk.X)
                tk.Label(img_row, text=f"{src} — view {view_index}", font=("Arial", 9, "bold")).pack()
                try:
                    img = Image.open(path)
                    img.thumbnail((520, 390))
                    photo = ImageTk.PhotoImage(img)
                    viewer._photo_refs.append(photo)
                    tk.Label(img_row, image=photo).pack()
                except Exception as e:
                    hint = "" if path == raw_path else f"\n(tried: {path})"
                    tk.Label(img_row, text=f"Could not load '{raw_path}': {e}{hint}",
                             fg="red", wraplength=520).pack()

        # --- Nav / save row ---
        nav_frame = tk.Frame(viewer, padx=10, pady=8)
        nav_frame.pack(fill=tk.X)

        def save_current(advance: int):
            new_data = {}
            for key, widget in field_widgets.items():
                raw_value = widget.get().strip()
                if raw_value == "":
                    new_data[key] = None
                    continue
                schema_type = next(
                    (c.get("type", "string") for c in attribute_schema.load_schema().get("fixed_columns", [])
                     if c["key"] == key), "string")
                if schema_type == "number":
                    try:
                        new_data[key] = float(raw_value) if "." in raw_value else int(raw_value)
                    except ValueError:
                        new_data[key] = raw_value  # keep as typed rather than losing the edit
                else:
                    new_data[key] = raw_value

            try:
                freeform_value = json.loads(freeform_text.get("1.0", tk.END).strip() or "{}")
            except json.JSONDecodeError as e:
                messagebox.showerror("Invalid JSON", f"Freeform attributes aren't valid JSON: {e}")
                return
            new_data[attribute_schema.freeform_key()] = freeform_value

            try:
                mongo_client.update_object_data(object_id, new_data)
            except Exception as e:
                messagebox.showerror("Save failed", str(e))
                return

            try:
                refresh_objects_list()
                refresh_images_list()
                refresh_inventory_list()
            except NameError:
                pass

            state["index"] = max(0, min(len(rows) - 1, state["index"] + advance))
            if advance != 0 and 0 <= state["index"] < len(rows):
                render()
            elif advance == 0:
                review_status_label.config(text="Saved.", fg="green")

        tk.Button(nav_frame, text="< Previous", command=lambda: (
            state.update(index=max(0, state["index"] - 1)), render())
        ).pack(side=tk.LEFT)
        tk.Button(nav_frame, text="Save", command=lambda: save_current(0),
                  bg="lightblue").pack(side=tk.LEFT, padx=8)
        tk.Button(nav_frame, text="Save & Next >", command=lambda: save_current(1),
                  bg="lightgreen").pack(side=tk.LEFT)
        tk.Button(nav_frame, text="Skip >", command=lambda: (
            state.update(index=min(len(rows) - 1, state["index"] + 1)), render())
        ).pack(side=tk.LEFT, padx=8)

    render()


# =====================================================================
# INVENTORY ATTRIBUTE REVIEW — same idea as the capture-level Attribute
# Review above, but for the object_catalog ("Inventory") entries
# themselves: step through catalog entries missing category/color/size,
# fill them in, saved via mongo_client.update_catalog_entry(). Separate
# from the capture-level review because a catalog entry's shape is
# different (name/category/color/size only — no freeform attributes,
# no fixed attribute columns, no single "captured_at").
# =====================================================================
dc_inv_review_frame = tk.LabelFrame(tab_data_collection, text=" Inventory Attribute Review ", padx=10, pady=10)
dc_inv_review_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(dc_inv_review_frame,
         text="Step through Inventory (catalog) entries missing Category/Color/Size and fill "
              "them in directly — separate from the per-capture review above, which edits "
              "individual log rows rather than the catalog entry itself.",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

dc_inv_review_status_label = tk.Label(dc_inv_review_frame, text="", fg="gray",
                                       wraplength=680, justify=tk.LEFT)
dc_inv_review_status_label.pack(anchor=tk.W, pady=(4, 4))


def review_inventory_missing_data():
    dc_inv_review_status_label.config(text="Loading inventory...", fg="gray")

    def worker():
        try:
            entries = object_catalog.list_inventory(limit=2000)
            missing = [e for e in entries
                       if not e.get("category") or not e.get("color") or not e.get("size")]
            err = None
        except Exception as e:
            entries, missing, err = None, None, str(e)

        def apply():
            if err is not None:
                dc_inv_review_status_label.config(text=f"Could not load inventory: {err}", fg="red")
                return
            if not missing:
                msg = ("No inventory entries found." if not entries
                       else "Every inventory entry already has Category/Color/Size — nothing to review!")
                dc_inv_review_status_label.config(text=msg, fg="green" if entries else "orange")
                return
            dc_inv_review_status_label.config(
                text=f"Reviewing {len(missing)} of {len(entries)} inventory entries missing data.",
                fg="green")
            open_inventory_review_viewer(missing)

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


tk.Button(dc_inv_review_frame, text="Review Inventory Entries Missing Data",
          command=review_inventory_missing_data, bg="lightgreen").pack(anchor=tk.W, pady=(2, 0))


def open_inventory_review_viewer(entries: list):
    """One catalog entry at a time: name (read-only) + editable
    Category/Color/Size, plus every image across every linked capture
    (same aggregation as the Inventory tab's detail viewer) so you can
    actually look at the object while filling in what it's missing.
    "Save & Next" writes via mongo_client.update_catalog_entry()."""
    viewer = tk.Toplevel(root)
    viewer.title(f"Inventory Attribute Review — {len(entries)} entr{'y' if len(entries)==1 else 'ies'}")
    viewer.geometry("820x720")

    state = {"index": 0}
    top_frame = tk.Frame(viewer, padx=10, pady=8)
    top_frame.pack(fill=tk.X)
    progress_label = tk.Label(top_frame, text="", font=("Arial", 10, "bold"))
    progress_label.pack(side=tk.LEFT)

    body_frame = tk.Frame(viewer)
    body_frame.pack(fill=tk.BOTH, expand=1)

    def render():
        for child in body_frame.winfo_children():
            child.destroy()
        idx = state["index"]
        entry = entries[idx]
        catalog_id = entry["_id"]
        progress_label.config(text=f"Entry {idx + 1} of {len(entries)}  —  {entry.get('name', '?')}")

        fields_frame = tk.LabelFrame(body_frame, text=" Fields ", padx=8, pady=8)
        fields_frame.pack(fill=tk.X, padx=10, pady=(6, 4))

        tk.Label(fields_frame, text=f"Name: {entry.get('name', '?')}   "
                                     f"(times seen: {entry.get('times_seen', 0)})",
                 font=("Arial", 9, "bold")).pack(anchor=tk.W, pady=(0, 4))

        field_widgets = {}
        for key, label in (("category", "Category"), ("color", "Color"), ("size", "Size")):
            value = entry.get(key)
            is_missing = value in (None, "")
            row = tk.Frame(fields_frame)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=f"{label}:", font=("Arial", 9, "bold"), width=12, anchor="w",
                     fg="#b35900" if is_missing else "black").pack(side=tk.LEFT)
            widget = tk.Entry(row, width=30)
            if not is_missing:
                widget.insert(0, str(value))
            widget.pack(side=tk.LEFT)
            field_widgets[key] = widget

        images_outer = tk.LabelFrame(body_frame, text=" Images (all linked captures) ", padx=8, pady=8)
        images_outer.pack(fill=tk.BOTH, expand=1, padx=10, pady=(4, 6))

        linked_ids = entry.get("linked_object_ids", []) or []
        all_docs = []
        for object_id in linked_ids:
            for doc in mongo_client.get_images_for_object(object_id):
                all_docs.append(doc)
        all_docs.sort(key=lambda d: d.get("captured_at") or datetime.min)

        if not all_docs:
            tk.Label(images_outer, text="No images found across any linked capture.").pack(pady=10)
        elif not _PIL_AVAILABLE:
            tk.Label(images_outer, text="Install Pillow to view images: pip install Pillow",
                     fg="red").pack(pady=10)
        else:
            canvas_frame = tk.Frame(images_outer)
            canvas_frame.pack(fill=tk.BOTH, expand=1)
            scroll_canvas = tk.Canvas(canvas_frame)
            scrollbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=scroll_canvas.yview)
            inner_frame = tk.Frame(scroll_canvas)
            inner_frame.bind("<Configure>", lambda e: scroll_canvas.configure(
                scrollregion=scroll_canvas.bbox("all")))
            scroll_canvas.create_window((0, 0), window=inner_frame, anchor="nw")
            scroll_canvas.configure(yscrollcommand=scrollbar.set)
            scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=1)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            viewer._photo_refs = []
            for doc in all_docs:
                raw_path = doc.get("image_path", "")
                path = resolve_image_path(raw_path)
                img_row = tk.Frame(inner_frame, pady=6)
                img_row.pack(fill=tk.X)
                tk.Label(img_row, text=f"{doc.get('source', '?')} — view {doc.get('view_index', '?')}",
                         font=("Arial", 9, "bold")).pack()
                try:
                    img = Image.open(path)
                    img.thumbnail((520, 390))
                    photo = ImageTk.PhotoImage(img)
                    viewer._photo_refs.append(photo)
                    tk.Label(img_row, image=photo).pack()
                except Exception as e:
                    tk.Label(img_row, text=f"Could not load '{raw_path}': {e}",
                             fg="red", wraplength=520).pack()

        nav_frame = tk.Frame(viewer, padx=10, pady=8)
        nav_frame.pack(fill=tk.X)

        def save_current(advance: int):
            fields = {}
            for key, widget in field_widgets.items():
                raw_value = widget.get().strip()
                fields[key] = raw_value if raw_value else None
            try:
                mongo_client.update_catalog_entry(catalog_id, fields)
            except Exception as e:
                messagebox.showerror("Save failed", str(e))
                return
            try:
                refresh_inventory_list()
            except NameError:
                pass
            state["index"] = max(0, min(len(entries) - 1, state["index"] + advance))
            if advance != 0:
                render()

        tk.Button(nav_frame, text="< Previous", command=lambda: (
            state.update(index=max(0, state["index"] - 1)), render())
        ).pack(side=tk.LEFT)
        tk.Button(nav_frame, text="Save", command=lambda: save_current(0),
                  bg="lightblue").pack(side=tk.LEFT, padx=8)
        tk.Button(nav_frame, text="Save & Next >", command=lambda: save_current(1),
                  bg="lightgreen").pack(side=tk.LEFT)
        tk.Button(nav_frame, text="Skip >", command=lambda: (
            state.update(index=min(len(entries) - 1, state["index"] + 1)), render())
        ).pack(side=tk.LEFT, padx=8)

    render()


# =====================================================================
# MERGE INVENTORY ENTRIES — manual fix for when object_catalog.
# match_or_create()'s exact-name auto-matching splits one real object
# into two (or more) catalog entries (a typo, a rephrasing, category
# on vs off — see object_catalog.py's docstring). Folds one entry's
# linked captures into another so "multiple objects/captures under one
# inventory entry" holds even when the automatic matching misses.
# =====================================================================
dc_merge_frame = tk.LabelFrame(tab_data_collection, text=" Merge Inventory Entries ", padx=10, pady=10)
dc_merge_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

tk.Label(dc_merge_frame,
         text="If two Inventory entries are really the same physical object (auto-matching "
              "only compares exact name/category — see the Inventory tab), merge them here: "
              "every capture linked to 'Merge away' gets folded into 'Keep', and the "
              "'Merge away' entry is deleted.",
         fg="gray", font=("Arial", 8), wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

dc_merge_row = tk.Frame(dc_merge_frame)
dc_merge_row.pack(fill=tk.X, pady=(6, 2))
tk.Label(dc_merge_row, text="Keep:").grid(row=0, column=0, sticky=tk.W)
dc_merge_keep_combo = ttk.Combobox(dc_merge_row, width=40, state="readonly")
dc_merge_keep_combo.grid(row=0, column=1, sticky=tk.W, padx=(4, 16))
tk.Label(dc_merge_row, text="Merge away:").grid(row=0, column=2, sticky=tk.W)
dc_merge_away_combo = ttk.Combobox(dc_merge_row, width=40, state="readonly")
dc_merge_away_combo.grid(row=0, column=3, sticky=tk.W, padx=(4, 0))

dc_merge_status_label = tk.Label(dc_merge_frame, text="", fg="gray", wraplength=680, justify=tk.LEFT)
dc_merge_status_label.pack(anchor=tk.W, pady=(4, 4))

_dc_merge_catalog_ids = []  # parallel to both combos' values


def refresh_dc_merge_options():
    def worker():
        try:
            entries = object_catalog.list_inventory(limit=500)
        except Exception:
            entries = []

        def apply():
            _dc_merge_catalog_ids.clear()
            display_values = []
            for e in entries:
                _dc_merge_catalog_ids.append(e["_id"])
                display_values.append(
                    f"{e.get('name', '?')}  (times seen: {e.get('times_seen', 0)}, id: {e['_id'][:8]}...)")
            dc_merge_keep_combo["values"] = display_values
            dc_merge_away_combo["values"] = display_values

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


def merge_dc_inventory_entries():
    keep_idx = dc_merge_keep_combo.current()
    away_idx = dc_merge_away_combo.current()
    if keep_idx < 0 or away_idx < 0:
        messagebox.showerror("Pick both entries", "Choose both a 'Keep' and a 'Merge away' entry first.")
        return
    keep_id = _dc_merge_catalog_ids[keep_idx]
    away_id = _dc_merge_catalog_ids[away_idx]
    if keep_id == away_id:
        messagebox.showerror("Same entry picked twice", "'Keep' and 'Merge away' must be different entries.")
        return
    if not messagebox.askyesno(
            "Merge inventory entries",
            f"Merge '{dc_merge_away_combo.get()}' into '{dc_merge_keep_combo.get()}'?\n\n"
            f"Every capture linked to the 'Merge away' entry will be re-pointed to 'Keep', "
            f"and the 'Merge away' entry will be deleted. This cannot be undone."):
        return

    dc_merge_status_label.config(text="Merging...", fg="gray")

    def worker():
        try:
            moved = mongo_client.merge_catalog_entries(keep_id, away_id)
            msg, color = f"Merged — {moved} capture(s) re-pointed to the kept entry.", "green"
        except Exception as e:
            msg, color = f"Merge failed: {e}", "red"

        def apply():
            dc_merge_status_label.config(text=msg, fg=color)
            try:
                refresh_inventory_list()
            except NameError:
                pass
            refresh_dc_merge_options()

        root.after(0, apply)

    threading.Thread(target=worker, daemon=True).start()


dc_merge_btn_row = tk.Frame(dc_merge_frame)
dc_merge_btn_row.pack(anchor=tk.W, pady=(2, 0))
tk.Button(dc_merge_btn_row, text="Refresh List", command=refresh_dc_merge_options,
          bg="lightblue").pack(side=tk.LEFT, padx=(0, 4))
tk.Button(dc_merge_btn_row, text="Merge", command=merge_dc_inventory_entries,
          bg="salmon").pack(side=tk.LEFT, padx=4)

root.after(0, refresh_dc_merge_options)



def get_point_settings(px, py):
    # Create the popup window
    dialog = tk.Toplevel(root)
    dialog.title("Point Settings")
    dialog.geometry("300x320")
    dialog.transient(root)
    dialog.grab_set()  # Forces user to interact with this window before the main one
    
    # Center the dialog on screen
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() // 2) - (300 // 2)
    y = (dialog.winfo_screenheight() // 2) - (320 // 2)
    dialog.geometry(f"300x320+{x}+{y}")
    
    # Initialize the result dictionary
    result = {'z': None, 'claw': 0, 'j4': None}
    
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

    # J4 input — optional, leave blank to hold whatever J4 is currently
    # tracked (m_j4) when this point is actually sent.
    tk.Label(dialog, text="\nJ4 (-358 to 358 deg, blank = hold current):").pack()
    j4_dialog_entry = tk.Entry(dialog, width=15)
    j4_dialog_entry.pack()
    
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
            j4_text = j4_dialog_entry.get().strip()
            j4_val = float(j4_text) if j4_text else None
            if j4_val is not None and not (-358.0 <= j4_val <= 358.0):
                messagebox.showerror("Invalid J4", "J4 must be between -358 and 358 degrees")
                return
            if 5.0 <= z_val <= 245.0:
                # SAVE the values into our result dictionary
                result['z'] = z_val
                result['claw'] = claw_var_inner.get()
                result['j4'] = j4_val
                
                # Signal that we are done and close window
                submitted.set(True)
                dialog.destroy()
            else:
                messagebox.showerror("Invalid Z", "Z must be between 5 and 245")
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter a numeric Z-value and J4 (or leave J4 blank)")
    
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
        # Get point settings (z-value, claw state, and J4)
        settings = get_point_settings(px, py)
        
        if settings['z'] is not None:  # User didn't cancel
            # Add valid point with its z-value, claw state, and J4 (None = hold current J4 when sent)
            valid_points.append((px, py, settings['z'], settings['claw'], settings['j4']))
            scatter = ax.scatter(px, py, color='green', s=50)
            valid_scatters.append(scatter)

            claw_text = "ON" if settings['claw'] == 1 else "OFF"
            j4_text_display = f"{settings['j4']:.1f}" if settings['j4'] is not None else "current"
            points_listbox.insert(tk.END, f"{len(valid_points)}: ({px:.2f}, {py:.2f}, z={settings['z']:.1f}, claw={claw_text}, J4={j4_text_display})")
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
    J4_MIN, J4_MAX = -358.0, 358.0   # matches dobot_util's Movement.SAFE_LIMITS["J4"]

    try:
        j1 = float(j1_entry.get())
        j2 = float(j2_entry.get())
        z  = float(zj_entry.get())
        # J4 previously hardcoded to -35.0 with no way to change it from
        # this row — now reads the new J4 entry field, defaulting to the
        # arm's current live J4 telemetry if the box is left blank so
        # leaving it empty doesn't yank the wrist to a fixed angle.
        j4_text = j4_entry.get().strip()
        if j4_text:
            j4 = float(j4_text)
        else:
            current_joints = robot_data.get("joints")
            j4 = float(current_joints[3]) if current_joints and len(current_joints) >= 4 else -35.0
        claw_state = claw_var_j.get()   # read the joint-row claw radio button

        if not (J1_MIN <= j1 <= J1_MAX and J2_MIN <= j2 <= J2_MAX and Z_MIN <= z <= Z_MAX):
            messagebox.showerror("Out of Range", "Joint values outside limits!")
            return
        if not (J4_MIN <= j4 <= J4_MAX):
            messagebox.showerror("Out of Range", f"J4 must be between {J4_MIN} and {J4_MAX} degrees!")
            return
        hard_deck_error = _hard_deck_violation(z, "Move Joints button")
        if hard_deck_error:
            messagebox.showerror("Move Rejected", hard_deck_error)
            return

        def execute():
            if ROBOT_CONNECTED and robot:
                print(f"Moving to J1:{j1}° J2:{j2}° Z:{z}mm J4:{j4}°")
                move_error = _dispatch_joint_move([j1, j2, z, j4], reason="manual Move Joints button")
                if move_error is not None:
                    print(f"[JOINT MOVE ERROR]: {move_error}")
                    return
                print(f"[JOINT MOVE SUCCESS]: J1:{j1} J2:{j2} Z:{z} J4:{j4}")
                # Apply claw state after move completes
                robot.movement.sync()
                sync_manual_position_from_feedback("manual Move Joints button")
                set_claw_dual_output(claw_state)
            else:
                print(f"DEMO MODE: J1:{j1} J2:{j2} Z:{z} J4:{j4} Claw:{'ON' if claw_state else 'OFF'}")

        # Run in background thread so ensure_robot_enabled and sync
        # don't block the Tkinter main thread
        threading.Thread(target=execute, daemon=True).start()

    except ValueError:
        messagebox.showerror("Input Error", "Please enter valid numbers for joints.")


def update_gui_from_feedback():
    """Refreshes the plot and labels with the robot's actual hardware
    position. Uses blitting (see _plot_background above) so only the
    small dot region gets redrawn each tick — the expensive workspace
    background is never touched here — which is what keeps this able to
    actually keep up with the 50Hz feedback feed instead of lagging."""
    global live_dot, is_jogging
    
    if ROBOT_CONNECTED and "cartesian" in robot_data and robot_data["cartesian"] is not None:
        try:
            # 1. Get Cartesian X, Y, Z from the real-time hardware telemetry
            raw_x = robot_data["cartesian"][0]
            raw_y = robot_data["cartesian"][1]
            live_z = robot_data["cartesian"][2]

            # Reactive hard-deck watchdog, jog only: jogging has no single
            # predictable target to check in advance (unlike move_to_point/
            # _handle_move_command's absolute moves, which are blocked
            # BEFORE they're ever sent instead — see the hard-deck checks
            # there), so this force-stops the instant live Z crosses below
            # the effective floor. Covers local AND remote-relayed jogging
            # alike, since both drive the same is_jogging/robot_data
            # globals regardless of who's driving.
            floor = _effective_hard_deck_z()
            if floor is not None and live_z < floor and is_jogging:
                try:
                    robot.movement.safe_move_jog("stop", [])
                finally:
                    is_jogging = False
                print(f"[HARD DECK] Jog stopped \u2014 Z {live_z:.1f} crossed below floor ({floor:.1f}).")
            
            # 2. Apply rotation matrix to align with your physical desk setup
            angle_deg = 90  # Change to -90, 180, etc. based on your setup
            theta = np.radians(angle_deg)
            rot_x = raw_x * np.cos(theta) - raw_y * np.sin(theta)
            rot_y = raw_x * np.sin(theta) + raw_y * np.cos(theta)
            
            # 3. Update the red tracking dot on the Matplotlib plot
            if live_dot is None:
                live_dot = ax.scatter(rot_x, rot_y, color='red', s=100, zorder=5, label="Live Robot Pos")
                ax.legend()
                canvas.draw()  # one full draw to establish the legend; also
                                # triggers _capture_plot_background via draw_event
            else:
                live_dot.set_offsets(np.c_[rot_x, rot_y])
                if _plot_background[0] is not None:
                    fig.canvas.restore_region(_plot_background[0])
                    ax.draw_artist(live_dot)
                    fig.canvas.blit(ax.bbox)
                else:
                    # Background not captured yet for some reason — fall
                    # back to a full draw rather than showing a stale dot.
                    fig.canvas.draw_idle()

            # 4. Update the text status label with X, Y, and Z
            if 'status_label' in globals() and status_label.winfo_exists():
                status_label.config(text=f"Robot Connected | X: {raw_x:.1f} | Y: {raw_y:.1f} | Z: {live_z:.1f}")
        except Exception as e:
            print(f"GUI telemetry loop warning: {e}")

    # ~30Hz — closely tracks the 50Hz feedback_loop() without over-driving
    # the GUI thread. Blitting (above) is what makes this rate affordable.
    root.after(33, update_gui_from_feedback)

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
# AUTO-CAPTURE / CAMERA BEHAVIOR TOGGLES
#   - Auto-capture on arm movement: every programmatic move (rotation
#     sequences, pickup+photograph pipeline, remote MQTT moves, the
#     manual "Move Joints" button) fires a photo when the move starts
#     and another 1s later, saved under a "robot_arm_moving_..." folder.
#     See trigger_arm_move_autocapture()/_dispatch_joint_move() above.
#   - Auto-capture while jogging: keyboard/button jogging fires a photo
#     on press, one every 1s while held, and one on release, saved under
#     a "manual_jog_moving_..." folder. See _start_jog_autocapture()/
#     _stop_jog_autocapture() above.
#   - Fix laggy/delayed photos: flushes a couple of buffered frames
#     before every capture so a photo can't come out showing the scene
#     from a moment before it was actually taken (see
#     vision/camera/capture.py's _capture_from_index). OFF by default -
#     it costs a little time per capture - turn on only if photos are
#     actually coming out visibly behind reality.
# Both auto-capture vars default ON per how this is meant to be used;
# the lag-fix defaults OFF since it's a fix for an intermittent issue,
# not something everyone needs paying the cost of on every capture.
# =============================================================================
auto_capture_settings_frame = tk.LabelFrame(gallery_frame, text=" Auto-Capture Settings ",
                                             padx=8, pady=6)
auto_capture_settings_frame.pack(fill=tk.X, padx=4, pady=(0, 10))

auto_capture_on_move_var = tk.BooleanVar(value=True)
auto_capture_on_jog_var = tk.BooleanVar(value=True)
fix_laggy_photos_var = tk.BooleanVar(value=False)


def _on_toggle_flush_stale_frames():
    set_flush_stale_frames(fix_laggy_photos_var.get())


tk.Checkbutton(auto_capture_settings_frame,
               text="Auto-capture photos when the robot arm moves (on move start + 1s later)",
               variable=auto_capture_on_move_var).pack(anchor=tk.W)
tk.Checkbutton(auto_capture_settings_frame,
               text="Auto-capture photos while manually jogging (on press, every 1s held, on release)",
               variable=auto_capture_on_jog_var).pack(anchor=tk.W)
tk.Checkbutton(auto_capture_settings_frame,
               text="Fix laggy/delayed photos (flushes buffered frames before each capture — slightly slower)",
               variable=fix_laggy_photos_var,
               command=_on_toggle_flush_stale_frames).pack(anchor=tk.W)

# =============================================================================
# CAPTURE PHOTO — pinned to the TOP of the Camera tab (this is the flow
# that actually stores something: save to disk, log to the local
# MongoDB, and upload to the server). The old top-of-tab "Take
# Photograph (current position)" button only saved locally and posted
# over MQTT — it never touched MongoDB or the server upload endpoints,
# so it's been dropped in favor of this one flow.
# =============================================================================
tk.Label(gallery_frame, text="Capture Photo",
         font=("Arial", 12, "bold"), bg="#f0f0f0").pack(pady=(0, 2))
tk.Label(gallery_frame, text="Grabs one frame from every configured camera (no arm\n"
         "movement), saves it locally, logs it to the local MongoDB, and\n"
         "uploads it to the server URL configured on the 'Server' tab.",
         font=("Arial", 8), bg="#f0f0f0", fg="gray", justify=tk.CENTER).pack(pady=(0, 6))

# --- Sample name/label — a preliminary tag you can attach before capturing,
# stored alongside the sample in MongoDB (both as the "label" field and as
# the upload category) so you can find this capture again later on the
# "Database" tab or via the natural-language query box there. "manual_snapshot"
# is offered here purely as ONE selectable option, same as the others — it is
# NOT the silent default anymore (see _default_manual_snapshot_label() below):
# leaving the box blank now gets a unique, timestamp-suffixed name instead, so
# a run of un-labeled captures don't all collide under one identical
# "manual_snapshot" name in the Inventory/catalog view (object_catalog.py
# matches captures into the same catalog entry by exact name). The dropdown
# offers a few common preliminary names, but the box is fully editable — type
# any name you want.
capture_label_row = tk.Frame(gallery_frame, bg="#f0f0f0")
capture_label_row.pack(pady=(0, 6))
tk.Label(capture_label_row, text="Sample name (optional, for querying later):",
         bg="#f0f0f0", font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 6))
capture_label_var = tk.StringVar(value="")
capture_label_combo = ttk.Combobox(
    capture_label_row, textvariable=capture_label_var, width=26,
    values=["uncategorized", "test_object", "calibration_shot",
            "reference_photo", "manual_snapshot"])
capture_label_combo.pack(side=tk.LEFT)

capture_status_label = tk.Label(gallery_frame, text="", bg="#f0f0f0", fg="gray",
                                 font=("Arial", 9), justify=tk.CENTER, wraplength=520)
capture_status_label.pack(pady=(0, 4))


def _default_manual_snapshot_label() -> str:
    """Fallback name used when a manual snapshot's label box is left
    blank. Timestamp-suffixed (not a single shared "manual_snapshot"
    string) so a run of un-labeled captures don't all get treated as
    repeat sightings of "the same object" by object_catalog.py's
    exact-name matching — "manual_snapshot" is still available any time
    you explicitly want it, as one of the dropdown's options."""
    return f"manual_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def run_manual_snapshot(sample_label: str, status_label_widget: tk.Label,
                         on_done=None) -> None:
    """
    One-click multi-camera snapshot (no arm movement, no rotation): grab
    one frame from EVERY USB camera configured in vision/config.py
    CAMERAS at once, save each to disk, log them all under a single
    sample in the local MongoDB (bringing back local-copy storage), and
    — if the server responds — upload each image too, through the same
    /collection/submission + /collection/images/upload endpoints the
    automatic capture sequence uses, against whatever SERVER_URL is
    currently configured on the "Server" tab.

    Shared by the Camera tab's "Capture Photo" button and the Data
    Collection tab's "Manual Snapshot" section (see dc_manual_frame
    below) so there is exactly one implementation of "what a manual
    snapshot does" instead of two near-duplicates.

    sample_label: required, non-blank — becomes both the object's
        "name"/catalog-match label and the upload category. Callers
        supply their own default (see _default_manual_snapshot_label())
        if their label field was left blank.
    status_label_widget: a tk.Label updated with progress/result text.
    on_done: optional no-arg callback, invoked on the GUI thread after
        the standard Database-tab list refreshes — e.g. the Data
        Collection tab's "Today's Captures" list.
    """
    camera_names = list(live_feed_panels.keys()) or list(list_configured_cameras().keys())
    status_label_widget.config(
        text=f"Capturing '{sample_label}' from {len(camera_names)} camera(s): "
             f"{', '.join(camera_names)}...",
        fg="gray")

    def worker():
        sample_id = new_sample_id()
        saved_pairs = []   # list of (camera_name, image_path) — a camera
                            # contributes TWO entries here if its "dual
                            # capture" toggle is on (see capture_frames_multi)
        cam_errors = {}    # camera name -> error message

        for cam in camera_names:
            try:
                for view_index, (suffix, frame) in enumerate(capture_frames_multi(cam)):
                    saved_pairs.append((cam, save_image(frame, sample_id, cam, view_index)))
            except Exception as e:
                cam_errors[cam] = str(e)

        # Routed through capture_pipeline.record_capture() — the SAME
        # function the automatic pipeline (vision_service ->
        # logger_service) uses — so a manual GUI capture and an
        # automatic one are recorded identically: Mongo `objects`/
        # `images` docs, catalog match/link, CSV + JSON append, Excel
        # refresh. See vision/storage/capture_pipeline.py. Passed as a
        # list of (source, path) pairs, not a dict, since dual-capture
        # can mean the SAME camera name appears twice — see
        # record_capture()'s docstring on why that needs the list form.
        arm_position = current_arm_position()  # snapshot NOW — right as the photos were taken,
                                                # not wherever the arm ends up later. None (-> null)
                                                # if the robot isn't connected/no feedback yet.
        mongo_ok = False
        object_id = sample_id  # kept as the local var name used below/upload code
        pipeline_warnings = []
        if saved_pairs:
            try:
                object_id, pipeline_warnings = record_capture(
                    name=sample_label,
                    image_paths_by_source=saved_pairs,
                    position=arm_position,
                )
                mongo_ok = True
                for w in pipeline_warnings:
                    print(f"[CAPTURE_PIPELINE] {w}")
            except Exception as e:
                print(f"[MONGO] Could not log capture locally: {e}")

        uploaded = 0
        if SERVER_URL and saved_pairs:
            try:
                submit_response = requests.post(
                    f"{SERVER_URL}/collection/submission",
                    json={"category": sample_label, "date": str(date.today()),
                          "data": {"label": sample_label,
                                   "predicted_label": sample_label,
                                   "num_images": len(saved_pairs)},
                          "sample_id": sample_id},
                    timeout=5,
                )
                submit_response.raise_for_status()
                remote_sample_id = submit_response.json()["sample_id"]
                for cam, path in saved_pairs:
                    try:
                        with open(path, "rb") as image_file:
                            upload_response = requests.post(
                                f"{SERVER_URL}/collection/images/upload",
                                files={"file": image_file},
                                data={"sample_id": remote_sample_id, "category": sample_label},
                                timeout=10,
                            )
                        upload_response.raise_for_status()
                        uploaded += 1
                    except Exception as e:
                        print(f"[UPLOAD] Could not upload '{cam}' image: {e}")
            except Exception as e:
                print(f"[UPLOAD] Could not create submission on server ({SERVER_URL}): {e}")

        def report():
            parts = [f"Saved {len(saved_pairs)} image(s) from "
                     f"{len(camera_names) - len(cam_errors)}/{len(camera_names)} camera(s) as '{sample_label}'"]
            if cam_errors:
                parts.append("Errors: " + ", ".join(
                    f"{c} ({m})" for c, m in cam_errors.items()))
            parts.append("MongoDB: OK" if mongo_ok else "MongoDB: failed")
            parts.append(f"Upload: {uploaded}/{len(saved_pairs)}" if saved_pairs
                         else "Upload: skipped")
            color = "green" if (mongo_ok or uploaded) else "orange"
            status_label_widget.config(text=" | ".join(parts), fg=color)
            try:
                refresh_objects_list()
                refresh_images_list()
                refresh_inventory_list()
            except NameError:
                pass  # Database tab not built yet — harmless
            if on_done is not None:
                try:
                    on_done()
                except Exception as e:
                    print(f"[MANUAL SNAPSHOT] on_done callback failed: {e}")
        root.after(0, report)

    threading.Thread(target=worker, daemon=True).start()


def capture_photo_and_store():
    """Camera tab's "Capture Photo" button — see run_manual_snapshot()
    for what this actually does."""
    sample_label = capture_label_var.get().strip() or _default_manual_snapshot_label()
    run_manual_snapshot(sample_label, capture_status_label)


tk.Button(gallery_frame, text="Capture Photo — All Cameras (Save + Upload)", bg="khaki",
          font=("Arial", 10, "bold"),
          command=capture_photo_and_store).pack(pady=6)

tk.Frame(gallery_frame, height=2, bd=1, relief=tk.SUNKEN, bg="#f0f0f0").pack(fill=tk.X, padx=20, pady=(6, 10))

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
#
# Only cameras that actually respond to is_camera_available() get a
# panel — a camera merely listed in vision.config.CAMERAS but not
# physically plugged in no longer gets an always-erroring placeholder
# panel. If ONE camera is connected, one panel shows; if three are
# connected, three show. "Refresh Cameras" (below) re-probes without
# restarting the app, for a camera plugged in after launch.
live_feed_panels = {}  # camera name -> {"image_label", "status_label", "busy"}
live_feeds_container = tk.Frame(gallery_frame, bg="#f0f0f0")
live_feeds_container.pack(fill=tk.BOTH, expand=1, padx=4, pady=4)
_camera_names = []


def _build_camera_panels():
    """(Re)builds one panel per camera that currently passes
    is_camera_available() — see the block comment above. Safe to call
    again later (e.g. from the "Refresh Cameras" button): clears
    whatever panels already exist first. Mutates live_feed_panels IN
    PLACE (.clear() + repopulate, not reassigned to a new dict) so
    other code that captured a reference to this exact dict object
    (e.g. run_manual_snapshot()'s live_feed_panels.keys() lookup)
    keeps working without needing to be told about a rebuild."""
    global _camera_names
    for child in live_feeds_container.winfo_children():
        child.destroy()
    live_feed_panels.clear()

    configured = list(list_configured_cameras().keys())
    _camera_names = [name for name in configured if is_camera_available(name)]
    if not _camera_names:
        # Nothing responded — DEMO MODE, no cv2/hardware, or genuinely
        # nothing plugged in yet. Fall back to showing every configured
        # camera anyway (as an error-panel, same as before this fix)
        # rather than an empty, seemingly-broken tab with zero panels
        # and no indication why.
        _camera_names = configured or ["station"]

    feed_cols = 2 if len(_camera_names) > 1 else 1
    for feed_i, cam_name in enumerate(_camera_names):
        row, col = divmod(feed_i, feed_cols)
        live_feeds_container.grid_columnconfigure(col, weight=1)
        live_feeds_container.grid_rowconfigure(row, weight=1)

        cam_panel = tk.Frame(live_feeds_container, bg="#f0f0f0", bd=1, relief=tk.GROOVE)
        cam_panel.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")

        tk.Label(cam_panel, text=cam_name.title(), font=("Arial", 10, "bold"),
                 bg="#f0f0f0").pack()
        status_lbl = tk.Label(cam_panel, text="Stopped", font=("Arial", 8),
                               bg="#f0f0f0", fg="gray")
        status_lbl.pack()
        img_lbl = tk.Label(cam_panel, bg="#222222", width=40, height=15,
                            text="Live feed will appear here", fg="white",
                            justify=tk.CENTER)
        img_lbl.pack(padx=4, pady=4, fill=tk.BOTH, expand=1)

        live_feed_panels[cam_name] = {
            "image_label": img_lbl, "status_label": status_lbl, "busy": False,
        }


_build_camera_panels()


def _warm_camera_handles():
    """
    Pre-opens (and caches — see vision/camera/capture.py's _capture_handles)
    every configured camera's cv2.VideoCapture handle right at startup,
    in the background, instead of waiting for the first real capture to
    open it "cold".

    WHY THIS MATTERS: opening a camera (probing DSHOW/MSMF/CAP_ANY,
    negotiating a format) commonly takes 1-3 seconds on Windows,
    especially the first time. capture_frame() already caches the
    handle across calls, so this cost is normally paid only once per
    camera per app run - but if that "once" happens to land on the very
    first auto-capture-on-move (see trigger_arm_move_autocapture), the
    whole feature *looks* like it has a multi-second lag right when it
    matters most (right after a move). Warming every handle here at
    launch means that cost is paid during startup instead, once, before
    anyone's watching a specific move for a fast response.
    """
    for cam_name in list_configured_cameras():
        try:
            capture_frame(cam_name)
        except Exception as e:
            print(f"[CAMERA WARMUP] '{cam_name}' not ready yet: {e}")


threading.Thread(target=_warm_camera_handles, daemon=True).start()

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
                # Clear whatever frame was last successfully shown —
                # otherwise a camera that drops mid-session (unplugged,
                # a USB hiccup, grabbed by another program) leaves its
                # LAST good frame frozen on screen forever, with only
                # the status label changing to "Error", making it look
                # like that camera is still live when it isn't.
                panel["image_label"].config(image="", text="No signal", fg="white")
                panel["image_label"].image = None
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


def rebuild_camera_panels():
    """"Refresh Cameras" button — re-probes and rebuilds the live-feed
    panels (see _build_camera_panels()) without restarting the app, for
    a camera plugged in (or unplugged) after launch. Stops any active
    feed first so a tick loop never ends up pointed at a panel that no
    longer exists, then restarts it afterward if it was running."""
    was_active = live_feed_active
    if was_active:
        stop_live_feed()
    _build_camera_panels()
    if was_active:
        start_live_feed()


tk.Button(live_feed_button_row, text="Start All Feeds", bg="lightgreen",
          command=start_live_feed).pack(side=tk.LEFT, padx=4)
tk.Button(live_feed_button_row, text="Stop All Feeds",
          command=stop_live_feed).pack(side=tk.LEFT, padx=4)
tk.Button(live_feed_button_row, text="Refresh Cameras", bg="lightblue",
          command=rebuild_camera_panels).pack(side=tk.LEFT, padx=4)

# =============================================================================
# CAMERA ASSIGNMENT — reassign which physical USB device index a named
# camera (e.g. "station", "wrist") points at, or add a brand-new named
# camera, without editing vision/config.py or restarting the app.
# Reassigning an EXISTING name takes effect immediately (both the live
# feed panel above and every capture path re-read the assignment on their
# next frame grab). Adding a brand-new name is picked up immediately by
# anything that calls list_configured_cameras() fresh each time it runs
# (the "Capture Photo — All Cameras" button, automatic capture sequences),
# but the live-feed panel grid above is built once at startup, so a new
# camera's preview panel needs an app restart to appear.
# =============================================================================
camera_assign_frame = tk.LabelFrame(gallery_frame, text=" Camera Assignment ", padx=8, pady=8)
camera_assign_frame.pack(fill=tk.X, padx=4, pady=(4, 10))

camera_assign_status = tk.Label(camera_assign_frame, text="", fg="gray",
                                 font=("Arial", 8), wraplength=520, justify=tk.LEFT)
camera_assign_status.pack(anchor=tk.W, pady=(0, 4))

camera_assign_rows_frame = tk.Frame(camera_assign_frame)
camera_assign_rows_frame.pack(fill=tk.X)

_camera_assign_index_vars = {}  # camera name -> tk.StringVar holding the index entry text
_camera_settings_vars = {}      # camera name -> {"a_label": tk.StringVar, "extract": tk.BooleanVar, ...}

_detected_modes_by_camera = {}  # camera name -> list of format dicts from the last Detect Formats run


def _do_apply_camera_settings(cam_name):
    extract = _camera_settings_vars[cam_name]["extract"].get()
    keep_orig = _camera_settings_vars[cam_name]["keep_orig"].get()
    alternate = _camera_settings_vars[cam_name]["alternate"].get()
    dual = _camera_settings_vars[cam_name]["dual"].get()
    extract_b = _camera_settings_vars[cam_name]["extract_b"].get()
    try:
        set_camera_settings(cam_name, extract_lenses=extract, keep_original=keep_orig,
                             alternate_lenses=alternate, dual_capture=dual, extract_lenses_b=extract_b)
        note = " — pick A/B formats from the Detected Formats list above (Use as A / Use as B applies immediately)."
        msg = (f"'{cam_name}': extract lenses = {extract}"
               + (f", keep original = {keep_orig}" if extract else "")
               + (f", alternate views (one per photo) = {alternate}" if extract else "")
               + (f", dual-capture B = on (extract lenses B = {extract_b})" if dual else "")
               + note)
        camera_assign_status.config(text=msg, fg="green")
    except Exception as e:
        camera_assign_status.config(text=f"Could not apply settings for '{cam_name}': {e}", fg="red")


def _do_detect_output_modes(cam_name, listbox_widget):
    camera_assign_status.config(
        text=f"Probing '{cam_name}' — this is an exhaustive test (13 resolutions x 4 "
             f"framerates x raw/normal pixel format) so it can take a minute or more, and "
             f"fully reconnects the camera repeatedly, so live feed for it will pause...",
        fg="gray")
    listbox_widget.delete(0, tk.END)
    listbox_widget.insert(tk.END, "Detecting... (this can take a minute or more)")

    def worker():
        modes = probe_camera_modes(cam_name)
        _detected_modes_by_camera[cam_name] = modes
        def report():
            listbox_widget.delete(0, tk.END)
            if modes:
                for m in modes:
                    listbox_widget.insert(tk.END, m["label"])
                camera_assign_status.config(
                    text=f"'{cam_name}': found {len(modes)} working format(s) — shown the "
                         f"same way e-con's own tool lists a camera's supported formats. "
                         f"Click one, then 'Use as A' or 'Use as B'.",
                    fg="green")
            else:
                listbox_widget.insert(tk.END, "(none found — see status message)")
                camera_assign_status.config(
                    text=f"'{cam_name}': none of the candidate resolutions/framerates produced "
                         f"a real (non-blank) frame. Check it's plugged into a USB 3.0 port "
                         f"(some cameras need the bandwidth) and not already open in another "
                         f"program.",
                    fg="orange")
        root.after(0, report)

    threading.Thread(target=worker, daemon=True).start()


def _do_pick_detected_mode(cam_name, listbox_widget, target):
    """target is 'a' or 'b' — applies the clicked detected format
    IMMEDIATELY (resolution + fps + raw/normal pixel format — the exact
    combo that was actually confirmed working during Detect Formats;
    Extract Lenses stays whatever the checkbox already says, toggled
    separately) and updates the on-screen A:/B: label."""
    selection = listbox_widget.curselection()
    modes = _detected_modes_by_camera.get(cam_name) or []
    if not selection or not modes or selection[0] >= len(modes):
        return
    mode = modes[selection[0]]
    vars_ = _camera_settings_vars[cam_name]
    try:
        if target == "a":
            set_camera_settings(cam_name, width=mode["width"], height=mode["height"],
                                 fps=mode["fps"], raw=mode["raw"])
            vars_["a_label"].set(f"A: {mode['width']}x{mode['height']} @ {mode['fps']}fps ({mode['fourcc']})")
        else:
            set_camera_settings(cam_name, width_b=mode["width"], height_b=mode["height"],
                                 fps_b=mode["fps"], raw_b=mode["raw"])
            vars_["b_label"].set(f"B: {mode['width']}x{mode['height']} @ {mode['fps']}fps ({mode['fourcc']})")
        camera_assign_status.config(
            text=f"'{cam_name}' profile {target.upper()} set to {mode['label']}.", fg="blue")
    except Exception as e:
        camera_assign_status.config(text=f"Could not set profile {target.upper()} for '{cam_name}': {e}", fg="red")


def _rebuild_camera_assign_rows():
    """(Re)draws one row per currently-configured camera name, each with
    an editable device-index box, a best-effort detected device-name
    hint (see list_camera_device_names()), an Assign button, a "Detect
    Formats" button + results list that actually tests what THIS
    camera's hardware really supports — shown the same way e-con's own
    OpenCVCam.exe reference tool lists a camera's formats ("FormatType:
    Y16 Width: 752 Height: 480 Fps: 60") — click one to use it as
    profile A or B, an "Extract Lenses" checkbox (splits a multi-channel
    frame into one photo per channel — see
    vision/camera/capture.py's _extract_lenses()), and an optional
    second "Capture B" toggle — turning that on makes every photo
    request from this camera rapid-fire both profiles back to back (see
    capture_frames_multi())."""
    for child in camera_assign_rows_frame.winfo_children():
        child.destroy()
    _camera_assign_index_vars.clear()
    _camera_settings_vars.clear()

    device_names = list_camera_device_names()
    current = list_configured_cameras()
    for cam_name, cam_index in sorted(current.items()):
        row = tk.Frame(camera_assign_rows_frame)
        row.pack(fill=tk.X, pady=2)
        tk.Label(row, text=cam_name, width=12, anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(row, text="device index:").pack(side=tk.LEFT, padx=(4, 2))
        idx_var = tk.StringVar(value=str(cam_index))
        _camera_assign_index_vars[cam_name] = idx_var
        tk.Entry(row, textvariable=idx_var, width=5).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(row, text="Assign", bg="lightblue",
                  command=lambda n=cam_name: _do_assign_camera(n)).pack(side=tk.LEFT)
        tk.Button(row, text="Remove Override", fg="darkred",
                  command=lambda n=cam_name: _do_remove_camera_override(n)).pack(side=tk.LEFT, padx=(6, 0))
        detected_name = device_names.get(cam_index)
        if detected_name:
            tk.Label(row, text=f"(detected: {detected_name})", fg="gray",
                     font=("Arial", 8)).pack(side=tk.LEFT, padx=(8, 0))

        current_settings = get_camera_settings(cam_name)

        selected_row = tk.Frame(camera_assign_rows_frame)
        selected_row.pack(fill=tk.X, pady=(0, 2), padx=(12, 0))
        a_label_var = tk.StringVar(
            value=f"A: {current_settings['width']}x{current_settings['height']} "
                  f"@ {current_settings['fps'] or 'default'}fps")
        tk.Label(selected_row, textvariable=a_label_var, font=("Arial", 8), fg="blue").pack(side=tk.LEFT, padx=(0, 12))
        extract_var = tk.BooleanVar(value=current_settings["extract_lenses"])
        tk.Checkbutton(selected_row, text="Extract Lenses (split channels into separate photos)",
                        variable=extract_var, font=("Arial", 8)).pack(side=tk.LEFT)

        extract_options_row = tk.Frame(camera_assign_rows_frame)
        extract_options_row.pack(fill=tk.X, pady=(0, 2), padx=(28, 0))
        keep_orig_var = tk.BooleanVar(value=current_settings["keep_original"])
        tk.Checkbutton(extract_options_row, text="Also keep the original (un-split) photo",
                        variable=keep_orig_var, font=("Arial", 8)).pack(side=tk.LEFT, padx=(0, 12))
        alternate_var = tk.BooleanVar(value=current_settings["alternate_lenses"])
        tk.Checkbutton(extract_options_row,
                        text="Alternate views (each photo saves just one view, cycling to "
                             "the next one next time — instead of all views every photo)",
                        variable=alternate_var, font=("Arial", 8)).pack(side=tk.LEFT)

        modes_frame = tk.Frame(camera_assign_rows_frame)
        modes_frame.pack(fill=tk.X, pady=(0, 2), padx=(12, 0))
        modes_listbox = tk.Listbox(modes_frame, height=5, width=70, font=("Consolas", 8))
        modes_listbox.pack(side=tk.LEFT)
        modes_btns = tk.Frame(modes_frame)
        modes_btns.pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(modes_btns, text="Use as A", font=("Arial", 8),
                  command=lambda n=cam_name, lb=modes_listbox: _do_pick_detected_mode(n, lb, "a")
                  ).pack(fill=tk.X)
        tk.Button(modes_btns, text="Use as B", font=("Arial", 8),
                  command=lambda n=cam_name, lb=modes_listbox: _do_pick_detected_mode(n, lb, "b")
                  ).pack(fill=tk.X, pady=(2, 0))

        detect_row = tk.Frame(camera_assign_rows_frame)
        detect_row.pack(fill=tk.X, pady=(0, 4), padx=(12, 0))
        tk.Button(detect_row, text="Detect Formats", bg="lightgray", font=("Arial", 8),
                  command=lambda n=cam_name, lb=modes_listbox: _do_detect_output_modes(n, lb)
                  ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(detect_row, text="(tests the camera's actual supported resolutions/framerates/"
                 "pixel formats — shown the same way e-con's own tool lists them)", fg="gray",
                 font=("Arial", 8), wraplength=520, justify=tk.LEFT).pack(side=tk.LEFT)

        dual_row = tk.Frame(camera_assign_rows_frame)
        dual_row.pack(fill=tk.X, pady=(0, 2), padx=(12, 0))
        dual_var = tk.BooleanVar(value=current_settings["dual_capture"])
        tk.Checkbutton(dual_row, text="Also capture a second (B) format on every photo request",
                        variable=dual_var, font=("Arial", 8)).pack(side=tk.LEFT, padx=(0, 12))
        b_label_var = tk.StringVar(
            value=f"B: {current_settings['width_b']}x{current_settings['height_b']} "
                  f"@ {current_settings['fps_b'] or 'default'}fps")
        tk.Label(dual_row, textvariable=b_label_var, font=("Arial", 8), fg="blue").pack(side=tk.LEFT, padx=(0, 12))
        extract_b_var = tk.BooleanVar(value=current_settings["extract_lenses_b"])
        tk.Checkbutton(dual_row, text="Extract Lenses (B)", variable=extract_b_var,
                        font=("Arial", 8)).pack(side=tk.LEFT)

        _camera_settings_vars[cam_name] = {
            "a_label": a_label_var, "extract": extract_var,
            "keep_orig": keep_orig_var, "alternate": alternate_var,
            "dual": dual_var, "b_label": b_label_var, "extract_b": extract_b_var,
        }

        apply_row = tk.Frame(camera_assign_rows_frame)
        apply_row.pack(fill=tk.X, pady=(0, 8), padx=(12, 0))
        tk.Button(apply_row, text="Apply Settings", bg="lightyellow",
                  command=lambda n=cam_name: _do_apply_camera_settings(n)).pack(side=tk.LEFT)


def _do_remove_camera_override(cam_name):
    """Clears a runtime assignment for cam_name, falling back to whatever
    vision/config.py's CAMERAS says (or dropping it if not in there)."""
    remove_camera_assignment(cam_name)
    _rebuild_camera_assign_rows()
    camera_assign_status.config(
        text=f"Cleared runtime override for '{cam_name}' — back to the "
             f"vision/config.py default (if any).",
        fg="green")


def _do_assign_camera(cam_name):
    idx_text = _camera_assign_index_vars[cam_name].get().strip()
    try:
        idx = int(idx_text)
    except ValueError:
        camera_assign_status.config(
            text=f"'{idx_text}' is not a valid device index (must be a whole number).",
            fg="red")
        return
    try:
        assign_camera(cam_name, idx)
        camera_assign_status.config(
            text=f"'{cam_name}' assigned to device index {idx}. Takes effect on the "
                 f"next capture/live-feed frame immediately.",
            fg="green")
    except Exception as e:
        camera_assign_status.config(text=f"Could not assign '{cam_name}': {e}", fg="red")


def _do_add_camera():
    name = new_cam_name_var.get().strip()
    idx_text = new_cam_index_var.get().strip()
    if not name:
        camera_assign_status.config(text="Enter a name for the new camera.", fg="red")
        return
    try:
        idx = int(idx_text)
    except ValueError:
        camera_assign_status.config(
            text=f"'{idx_text}' is not a valid device index (must be a whole number).",
            fg="red")
        return
    try:
        assign_camera(name, idx)
        new_cam_name_var.set("")
        new_cam_index_var.set("")
        _rebuild_camera_assign_rows()
        camera_assign_status.config(
            text=f"Added camera '{name}' at device index {idx}. It's usable now by "
                 f"'Capture Photo — All Cameras' and automatic capture right away; "
                 f"click 'Refresh Cameras' above to also give it its own Live Feed "
                 f"preview panel (no restart needed).",
            fg="green")
    except Exception as e:
        camera_assign_status.config(text=f"Could not add '{name}': {e}", fg="red")


_CAMERA_DETECT_MAX_INDEX = 20  # probe 0..19 - widened from 0-9 so cameras
                               # landing at a higher index (extra USB hub
                               # ports, virtual/IP-camera drivers registering
                               # their own index first, etc.) still get found,
                               # and setups with several cameras plugged in
                               # at once have enough headroom to find them all.


def _do_detect_cameras():
    camera_assign_status.config(
        text=f"Detecting cameras (probing indices 0-{_CAMERA_DETECT_MAX_INDEX - 1})...",
        fg="gray")

    def worker():
        try:
            found = list_camera_indices(max_index=_CAMERA_DETECT_MAX_INDEX)
            # Best-effort Device-Manager-style names (Windows only, see
            # list_camera_device_names()'s docstring for the positional-
            # match caveat) so cameras are identifiable by name, not just
            # a bare number, once there are more than one or two.
            device_names = list_camera_device_names()
        except Exception as e:
            found = None
            device_names = {}
            err = str(e)
        def report():
            if found is None:
                camera_assign_status.config(text=f"Detection failed: {err}", fg="red")
            elif found:
                labeled = [f"{i} ({device_names[i]})" if i in device_names else str(i)
                           for i in found]
                camera_assign_status.config(
                    text=f"Working device indices found: {', '.join(labeled)}. Enter "
                         f"one of these above and click Assign for the camera you want "
                         f"it on. If a camera you expect isn't listed, it may be at an "
                         f"even higher index — type it directly into the index box and "
                         f"Assign anyway, or use 'Add new camera' below.",
                    fg="green")
            else:
                camera_assign_status.config(
                    text=f"No working camera devices found on indices "
                         f"0-{_CAMERA_DETECT_MAX_INDEX - 1}. Check connections, that "
                         f"no other program (Zoom/Teams/OBS/another Python process) "
                         f"has them open, and try unplugging/replugging.",
                    fg="orange")
        root.after(0, report)

    threading.Thread(target=worker, daemon=True).start()


_rebuild_camera_assign_rows()

camera_assign_new_row = tk.Frame(camera_assign_frame)
camera_assign_new_row.pack(fill=tk.X, pady=(8, 0))
tk.Label(camera_assign_new_row, text="Add new camera —  name:").pack(side=tk.LEFT)
new_cam_name_var = tk.StringVar()
tk.Entry(camera_assign_new_row, textvariable=new_cam_name_var, width=12).pack(side=tk.LEFT, padx=(2, 8))
tk.Label(camera_assign_new_row, text="index:").pack(side=tk.LEFT)
new_cam_index_var = tk.StringVar()
tk.Entry(camera_assign_new_row, textvariable=new_cam_index_var, width=5).pack(side=tk.LEFT, padx=(2, 8))
tk.Button(camera_assign_new_row, text="Add Camera", bg="lightgreen",
          command=_do_add_camera).pack(side=tk.LEFT, padx=(0, 8))
tk.Button(camera_assign_new_row, text="Detect Cameras", bg="khaki",
          command=_do_detect_cameras).pack(side=tk.LEFT)

tk.Label(tab_laser, text="Laser Control", font=("Arial", 12, "bold")).pack(pady=(10, 0))

# =============================================================================
# LASER CONTROL (ESP32) — controls the structured-light / scan laser via the
# ESP32 GPIO+laser controller firmware (see ESP32_Laser_Control/src/main.cpp
# and laser_control/relay_controller.py for the full serial protocol). This
# is a separate physical board/serial connection from the arm itself, so it
# gets its own connect/disconnect lifecycle here.
#
# Mirrors the firmware's own safety model rather than adding a competing one:
#   - Configure (pin/freq/max-duty) before anything else is allowed.
#   - Explicit ARM step required before any nonzero duty is accepted —
#     configuring or connecting never arms it by itself.
#   - A background heartbeat thread pings the board every second while
#     connected so the firmware's 2-second host-silence watchdog doesn't
#     auto-disarm mid-use just because the user hasn't touched a control
#     lately. (The watchdog itself only ever fires while armed AND duty>0,
#     per firmware — this heartbeat just keeps the host from going quiet.)
#   - Disconnecting (or closing the app) always disarms first.
# =============================================================================
laser_ctl = None                 # RelayController instance, once connected
laser_heartbeat_stop = threading.Event()
laser_configured = False
laser_armed_state = False

laser_frame = tk.LabelFrame(tab_laser, text=" Laser Control (ESP32) ", padx=8, pady=8)
laser_frame.pack(fill=tk.X, padx=10, pady=(10, 10))

laser_status_label = tk.Label(laser_frame, text="Not connected.", fg="gray",
                               font=("Arial", 8), wraplength=520, justify=tk.LEFT)
laser_status_label.pack(anchor=tk.W, pady=(0, 6))

# --- Connection row ---
laser_conn_row = tk.Frame(laser_frame)
laser_conn_row.pack(fill=tk.X, pady=2)
tk.Label(laser_conn_row, text="Port:").pack(side=tk.LEFT)
laser_port_var = tk.StringVar(value="Auto-Detect")
laser_port_combo = ttk.Combobox(laser_conn_row, textvariable=laser_port_var, width=16,
                                 values=["Auto-Detect"], state="readonly")
laser_port_combo.pack(side=tk.LEFT, padx=(2, 6))
laser_refresh_btn = tk.Button(laser_conn_row, text="Refresh Ports")
laser_refresh_btn.pack(side=tk.LEFT, padx=(0, 6))
laser_connect_btn = tk.Button(laser_conn_row, text="Connect", bg="lightgreen")
laser_connect_btn.pack(side=tk.LEFT, padx=(0, 6))
laser_disconnect_btn = tk.Button(laser_conn_row, text="Disconnect", state=tk.DISABLED)
laser_disconnect_btn.pack(side=tk.LEFT)

# --- Configuration row ---
laser_config_row = tk.Frame(laser_frame)
laser_config_row.pack(fill=tk.X, pady=(8, 2))
tk.Label(laser_config_row, text="Pin:").pack(side=tk.LEFT)
laser_pin_var = tk.StringVar(value="25")
tk.Entry(laser_config_row, textvariable=laser_pin_var, width=5).pack(side=tk.LEFT, padx=(2, 10))
tk.Label(laser_config_row, text="Freq (Hz):").pack(side=tk.LEFT)
laser_freq_var = tk.StringVar(value="1000")
tk.Entry(laser_config_row, textvariable=laser_freq_var, width=7).pack(side=tk.LEFT, padx=(2, 10))
tk.Label(laser_config_row, text="Max Duty (%):").pack(side=tk.LEFT)
laser_maxduty_var = tk.StringVar(value="100")
tk.Entry(laser_config_row, textvariable=laser_maxduty_var, width=5).pack(side=tk.LEFT, padx=(2, 10))
laser_configure_btn = tk.Button(laser_config_row, text="Configure Laser",
                                 state=tk.DISABLED)
laser_configure_btn.pack(side=tk.LEFT)

# --- Arm / duty row ---
laser_fire_row = tk.Frame(laser_frame)
laser_fire_row.pack(fill=tk.X, pady=(8, 2))
laser_arm_btn = tk.Button(laser_fire_row, text="ARM", bg="darkorange", fg="white",
                          width=10, state=tk.DISABLED)
laser_arm_btn.pack(side=tk.LEFT, padx=(0, 10))
tk.Label(laser_fire_row, text="Duty %:").pack(side=tk.LEFT)
laser_duty_var = tk.IntVar(value=0)
laser_duty_scale = tk.Scale(laser_fire_row, from_=0, to=100, orient=tk.HORIZONTAL,
                             variable=laser_duty_var, length=180, state=tk.DISABLED)
laser_duty_scale.pack(side=tk.LEFT, padx=(4, 10))
laser_set_duty_btn = tk.Button(laser_fire_row, text="Set Duty", state=tk.DISABLED)
laser_set_duty_btn.pack(side=tk.LEFT, padx=(0, 10))
laser_off_btn = tk.Button(laser_fire_row, text="LASER OFF", bg="red", fg="white",
                          width=12, state=tk.DISABLED)
laser_off_btn.pack(side=tk.LEFT)

# --- Diagnostics row — for "ERR PIN_IS_RELAY" (a pin is already registered
# as a relay channel on the board's flash-persisted config, so it refuses
# PWM/relay double-duty on that pin) and similar conflicts. The board can
# hold up to 16 relay channels but this tab only shows slots 1-4, so a
# conflicting channel from earlier testing may not be visible above —
# "Query Board Status" lists every channel the board actually has
# configured (regardless of which GUI slot, if any, it's tracked in), and
# "Remove Channel #" frees a specific one back up.
laser_diag_row = tk.Frame(laser_frame)
laser_diag_row.pack(fill=tk.X, pady=(8, 2))
laser_status_query_btn = tk.Button(laser_diag_row, text="Query Board Status", state=tk.DISABLED)
laser_status_query_btn.pack(side=tk.LEFT, padx=(0, 14))
tk.Label(laser_diag_row, text="Remove channel #:").pack(side=tk.LEFT)
laser_remove_ch_var = tk.StringVar(value="")
tk.Entry(laser_diag_row, textvariable=laser_remove_ch_var, width=4).pack(side=tk.LEFT, padx=(2, 6))
laser_remove_ch_btn = tk.Button(laser_diag_row, text="Remove Channel", state=tk.DISABLED)
laser_remove_ch_btn.pack(side=tk.LEFT)

laser_diag_output_label = tk.Label(laser_frame, text="", fg="gray", font=("Arial", 8),
                                    wraplength=800, justify=tk.LEFT)
laser_diag_output_label.pack(anchor=tk.W, pady=(2, 0))

# =============================================================================
# [WIRED] LASER RELAY CHANNELS — per the board photos, this rig's lasers are
# each behind their own plain ON/OFF relay module (SRD-05VDC-SL-C, "1 Relay
# Module High/Low Level Trigger"), not one PWM-dimmable laser. So these use
# RelayController's generic multi-channel CONFIG/SET commands (channels
# 1-16) — the SAME connection (laser_ctl) as the PWM panel above, just a
# different command family. Connect above first; this section just adds
# per-channel configure + individual ON/OFF toggles for up to 4 lasers.
# =============================================================================
laser_channels_frame = tk.LabelFrame(laser_frame.master, text=" Laser Channels \u2014 Relay On/Off (up to 4) ",
                                      padx=8, pady=8)
laser_channels_frame.pack(fill=tk.X, padx=4, pady=(4, 10))

tk.Label(laser_channels_frame,
         text="For relay-switched laser diodes (individual on/off, no dimming). "
              "Uses the same ESP32 connection as the panel above \u2014 connect there first.",
         fg="gray", font=("Arial", 8), wraplength=520, justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))

laser_channel_assignments = channel_assignments.load_assignments()  # "1".."4" -> {"name","pin"}
laser_channel_rows_container = tk.Frame(laser_channels_frame)
laser_channel_rows_container.pack(fill=tk.X)


def _rebuild_laser_channel_rows():
    for widget in laser_channel_rows_container.winfo_children():
        widget.destroy()

    for ch in range(1, 5):
        saved = laser_channel_assignments.get(str(ch), {})
        row = tk.Frame(laser_channel_rows_container)
        row.pack(fill=tk.X, pady=2)

        tk.Label(row, text=f"Ch {ch} \u2014 name:", width=10, anchor=tk.W).pack(side=tk.LEFT)
        name_var = tk.StringVar(value=saved.get("name", f"Laser {ch}"))
        tk.Entry(row, textvariable=name_var, width=12).pack(side=tk.LEFT, padx=(2, 8))

        tk.Label(row, text="pin:").pack(side=tk.LEFT)
        pin_var = tk.StringVar(value=str(saved.get("pin", "")))
        tk.Entry(row, textvariable=pin_var, width=5).pack(side=tk.LEFT, padx=(2, 8))

        configure_btn = tk.Button(row, text="Configure")
        configure_btn.pack(side=tk.LEFT, padx=(0, 6))
        on_btn = tk.Button(row, text="ON", bg="lightgreen", state=tk.DISABLED, width=6)
        on_btn.pack(side=tk.LEFT, padx=(0, 4))
        off_btn = tk.Button(row, text="OFF", bg="salmon", state=tk.DISABLED, width=6)
        off_btn.pack(side=tk.LEFT, padx=(0, 4))
        status_lbl = tk.Label(row, text="Not configured.", fg="gray", font=("Arial", 8))
        status_lbl.pack(side=tk.LEFT, padx=(8, 0))

        def make_configure(ch=ch, name_var=name_var, pin_var=pin_var,
                            status_lbl=status_lbl, on_btn=on_btn, off_btn=off_btn,
                            configure_btn=configure_btn):
            def do_configure():
                if laser_ctl is None:
                    status_lbl.config(text="Connect the ESP32 above first.", fg="red")
                    return
                try:
                    pin = int(pin_var.get())
                except ValueError:
                    status_lbl.config(text="Pin must be a whole number.", fg="red")
                    return

                configure_btn.config(state=tk.DISABLED)

                def worker():
                    ok, raw_lines = laser_ctl.configure_channel_verbose(ch, pin, active_high=True, safe_on=False)

                    def finish():
                        configure_btn.config(state=tk.NORMAL)
                        if ok:
                            on_btn.config(state=tk.NORMAL)
                            off_btn.config(state=tk.NORMAL)
                            status_lbl.config(text=f"Configured on pin {pin}.", fg="green")
                            laser_channel_assignments[str(ch)] = {"name": name_var.get(), "pin": pin}
                            channel_assignments.save_assignments(laser_channel_assignments)
                        else:
                            reason = " | ".join(raw_lines) if raw_lines else "(board sent no response)"
                            status_lbl.config(text=f"Rejected: {reason}", fg="red")
                    root.after(0, finish)

                threading.Thread(target=worker, daemon=True).start()
            return do_configure

        def make_toggle(ch=ch, state=True, status_lbl=status_lbl):
            def do_toggle():
                if laser_ctl is None:
                    return

                def worker():
                    ok = laser_ctl.set_channel(ch, state)

                    def finish():
                        if ok:
                            status_lbl.config(text="ON" if state else "OFF",
                                               fg="orange" if state else "gray")
                        else:
                            status_lbl.config(text="Command rejected by board.", fg="red")
                    root.after(0, finish)

                threading.Thread(target=worker, daemon=True).start()
            return do_toggle

        configure_btn.config(command=make_configure())
        on_btn.config(command=make_toggle(state=True))
        off_btn.config(command=make_toggle(state=False))


root.after(0, _rebuild_laser_channel_rows)



def _laser_set_status(text, fg="gray"):
    laser_status_label.config(text=text, fg=fg)


def _laser_refresh_ports():
    try:
        ports = RelayController.list_ports()
    except Exception as e:
        _laser_set_status(f"Could not list serial ports: {e}", fg="red")
        return
    laser_port_combo["values"] = ["Auto-Detect"] + ports
    if laser_port_var.get() not in laser_port_combo["values"]:
        laser_port_var.set("Auto-Detect")


def _laser_connect():
    laser_connect_btn.config(state=tk.DISABLED)
    _laser_set_status("Connecting...", fg="gray")

    def worker():
        global laser_ctl
        chosen_port = laser_port_var.get()
        try:
            if chosen_port == "Auto-Detect":
                found = RelayController.find_esp32()
                if found is None:
                    root.after(0, lambda: (_laser_set_status(
                        "No ESP32 laser controller found on any serial port. "
                        "Check the USB connection, or pick a port manually.", fg="red"),
                        laser_connect_btn.config(state=tk.NORMAL)))
                    return
                port = found
            else:
                port = chosen_port

            rc = RelayController(port)
            ok = rc.connect()

            def finish():
                global laser_ctl
                if ok:
                    laser_ctl = rc
                    _laser_set_status(f"Connected on {port}.", fg="green")
                    laser_connect_btn.config(state=tk.DISABLED)
                    laser_disconnect_btn.config(state=tk.NORMAL)
                    laser_configure_btn.config(state=tk.NORMAL)
                    laser_status_query_btn.config(state=tk.NORMAL)
                    laser_remove_ch_btn.config(state=tk.NORMAL)
                    laser_heartbeat_stop.clear()
                    threading.Thread(target=_laser_heartbeat_loop, daemon=True).start()
                else:
                    reason = rc.last_error or "no PING response"
                    _laser_set_status(f"Failed to connect on {port}: {reason}", fg="red")
                    laser_connect_btn.config(state=tk.NORMAL)
            root.after(0, finish)
        except Exception as e:
            root.after(0, lambda: (_laser_set_status(f"Connection error: {e}", fg="red"),
                                    laser_connect_btn.config(state=tk.NORMAL)))

    threading.Thread(target=worker, daemon=True).start()


def _laser_disconnect():
    global laser_ctl, laser_configured, laser_armed_state
    laser_heartbeat_stop.set()

    def worker():
        global laser_ctl, laser_configured, laser_armed_state
        try:
            if laser_ctl is not None:
                laser_ctl.laser_disarm()   # always leave hardware safe on disconnect
                laser_ctl.disconnect()
        except Exception:
            pass

        def finish():
            global laser_ctl, laser_configured, laser_armed_state
            laser_ctl = None
            laser_configured = False
            laser_armed_state = False
            _laser_set_status("Disconnected (laser disarmed).", fg="gray")
            laser_connect_btn.config(state=tk.NORMAL)
            laser_disconnect_btn.config(state=tk.DISABLED)
            laser_configure_btn.config(state=tk.DISABLED)
            laser_status_query_btn.config(state=tk.DISABLED)
            laser_remove_ch_btn.config(state=tk.DISABLED)
            laser_arm_btn.config(state=tk.DISABLED, text="ARM", bg="darkorange")
            laser_duty_scale.config(state=tk.DISABLED)
            laser_set_duty_btn.config(state=tk.DISABLED)
            laser_off_btn.config(state=tk.DISABLED)
        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def _laser_heartbeat_loop():
    """Pings the board once a second while connected so the firmware's
    host-silence watchdog (2s) never trips just because the user hasn't
    clicked anything recently. Also keeps the status label reasonably
    fresh. Stops as soon as laser_heartbeat_stop is set (disconnect)."""
    while not laser_heartbeat_stop.wait(1.0):
        ctl = laser_ctl
        if ctl is None or not ctl.is_connected():
            return
        try:
            ctl.ping()
        except Exception:
            return


def _laser_query_status():
    """List every channel (1-16) the board actually has configured right
    now, regardless of whether this GUI's 4 slots know about it. This is
    the tool for tracking down an "ERR PIN_IS_RELAY"-type conflict: the
    pin you want for the PWM laser may have been registered as a relay
    channel in an earlier session (channel config persists in the board's
    flash across power cycles/reconnects), under a channel number this
    tab doesn't show a row for."""
    if laser_ctl is None:
        return
    laser_status_query_btn.config(state=tk.DISABLED)

    def worker():
        channels = laser_ctl.status()

        def finish():
            laser_status_query_btn.config(state=tk.NORMAL)
            configured = [c for c in channels if c.configured]
            if not configured:
                laser_diag_output_label.config(
                    text="Board reports no relay channels configured (checked 1\u201316).",
                    fg="gray")
            else:
                parts = [f"Ch {c.ch}: pin {c.pin} ({'HIGH' if c.active_high else 'LOW'}-active, "
                         f"{'ON' if c.state else 'OFF'})" for c in configured]
                laser_diag_output_label.config(
                    text="Board has these relay channels configured: " + "; ".join(parts) +
                         ". To free a pin for PWM use, remove its channel number above.",
                    fg="black")
        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def _laser_remove_channel():
    """Send REMOVE <ch> to clear a relay channel's configuration on the
    board (drives it to its safe state first). Use this when "Configure
    Laser" (PWM) is rejected with ERR PIN_IS_RELAY for a pin that's
    currently a relay channel — remove that channel number here, then
    retry Configure Laser."""
    if laser_ctl is None:
        return
    try:
        ch = int(laser_remove_ch_var.get())
    except ValueError:
        laser_diag_output_label.config(text="Enter a whole channel number (1\u201316) to remove.", fg="red")
        return

    laser_remove_ch_btn.config(state=tk.DISABLED)

    def worker():
        ok = laser_ctl.remove_channel(ch)

        def finish():
            laser_remove_ch_btn.config(state=tk.NORMAL)
            if ok:
                laser_diag_output_label.config(
                    text=f"Channel {ch} removed from the board \u2014 its pin is now free to reuse "
                         f"(e.g. for the PWM laser above).", fg="green")
                # If this was one of the 4 GUI-tracked slots, clear its saved
                # assignment too so the row doesn't show a stale pin.
                if str(ch) in laser_channel_assignments:
                    del laser_channel_assignments[str(ch)]
                    channel_assignments.save_assignments(laser_channel_assignments)
                    _rebuild_laser_channel_rows()
            else:
                laser_diag_output_label.config(
                    text=f"Board didn't confirm removing channel {ch} "
                         f"(it may not have been configured, or the board didn't respond).",
                    fg="red")
        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


laser_status_query_btn.config(command=_laser_query_status)
laser_remove_ch_btn.config(command=_laser_remove_channel)


def _laser_configure():
    if laser_ctl is None:
        return
    try:
        pin = int(laser_pin_var.get())
        freq = int(laser_freq_var.get())
        maxduty = int(laser_maxduty_var.get())
    except ValueError:
        _laser_set_status("Pin, frequency, and max duty must all be whole numbers.", fg="red")
        return
    if not (0 <= maxduty <= 100):
        _laser_set_status("Max duty must be between 0 and 100.", fg="red")
        return

    laser_configure_btn.config(state=tk.DISABLED)

    def worker():
        global laser_configured
        ok, raw_lines = laser_ctl.laser_config_verbose(pin=pin, freq_hz=freq, max_duty_pct=maxduty)

        def finish():
            global laser_configured
            laser_configure_btn.config(state=tk.NORMAL)
            if ok:
                laser_configured = True
                laser_arm_btn.config(state=tk.NORMAL)
                _laser_set_status(
                    f"Laser configured: pin {pin}, {freq} Hz, max duty {maxduty}%. "
                    f"Disarmed / 0% until you click ARM.", fg="green")
            else:
                laser_configured = False
                board_reason = " | ".join(raw_lines) if raw_lines else "(board sent no response)"
                _laser_set_status(
                    f"Laser configuration rejected by the board: {board_reason}", fg="red")
        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def _laser_toggle_arm():
    if laser_ctl is None or not laser_configured:
        return
    laser_arm_btn.config(state=tk.DISABLED)

    def worker():
        global laser_armed_state
        if not laser_armed_state:
            ok = laser_ctl.laser_arm()
            new_state = True if ok else laser_armed_state
        else:
            ok = laser_ctl.laser_disarm()
            new_state = False if ok else laser_armed_state

        def finish():
            global laser_armed_state
            laser_armed_state = new_state
            laser_arm_btn.config(state=tk.NORMAL)
            if laser_armed_state:
                laser_arm_btn.config(text="DISARM", bg="red")
                laser_duty_scale.config(state=tk.NORMAL)
                laser_set_duty_btn.config(state=tk.NORMAL)
                laser_off_btn.config(state=tk.NORMAL)
                _laser_set_status("Laser ARMED. Duty is still 0% until you Set Duty.",
                                   fg="darkorange")
            else:
                laser_arm_btn.config(text="ARM", bg="darkorange")
                laser_duty_var.set(0)
                laser_duty_scale.config(state=tk.DISABLED)
                laser_set_duty_btn.config(state=tk.DISABLED)
                laser_off_btn.config(state=tk.DISABLED)
                if ok:
                    _laser_set_status("Laser DISARMED (forced to 0%).", fg="gray")
                else:
                    _laser_set_status("Arm/disarm command failed — check the connection.",
                                       fg="red")
        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def _laser_set_duty():
    if laser_ctl is None or not laser_armed_state:
        return
    pct = laser_duty_var.get()
    laser_set_duty_btn.config(state=tk.DISABLED)

    def worker():
        ok = laser_ctl.laser_set(pct)

        def finish():
            laser_set_duty_btn.config(state=tk.NORMAL)
            if ok:
                _laser_set_status(f"Laser duty set to {pct}%.", fg="green")
            else:
                _laser_set_status(
                    f"Board rejected {pct}% (over the configured max duty, or not "
                    f"armed). Duty unchanged.", fg="red")
        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def _laser_force_off():
    if laser_ctl is None:
        return

    def worker():
        laser_ctl.laser_off()

        def finish():
            laser_duty_var.set(0)
            _laser_set_status("Laser forced to 0% (still armed).", fg="gray")
        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


laser_refresh_btn.config(command=_laser_refresh_ports)
laser_connect_btn.config(command=_laser_connect)
laser_disconnect_btn.config(command=_laser_disconnect)
laser_configure_btn.config(command=_laser_configure)
laser_arm_btn.config(command=_laser_toggle_arm)
laser_set_duty_btn.config(command=_laser_set_duty)
laser_off_btn.config(command=_laser_force_off)

_laser_refresh_ports()


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
root.after(0, refresh_objects_list)
root.after(0, refresh_images_list)

# Start listening for automatic-capture commands published by 4DAI (see
# vision/config.py TOPIC_CAPTURE_COMMAND). This is what lets 4DAI's GUI
# trigger a full "rotate + photograph" sequence with no manual
# button-clicking on the arm side, per the transcript's requirement.
root.after(0, start_capture_command_listener)

# Also start the server-dependent (continuous-sweep) capture listener, on
# its own topic (TOPIC_CAPTURE_COMMAND_SERVER_DEPENDENT) so it doesn't
# collide with the listener above.
root.after(0, start_capture_command_listener_server_dependent)

# Generic remote control - lets 4DAI's own Arm Control page, an external
# AI model, or any other MQTT publisher move the arm without touching
# this machine (see TOPIC_ARM_MOVE_COMMAND in vision/config.py).
root.after(0, start_move_command_listener)


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

    # The ESP32 laser controller wired up on the Laser tab (laser_ctl) is a
    # separate connection from the vision.camera.laser stub above — always
    # disarm before disconnecting so a live laser is never left armed/firing
    # if the app is closed mid-use.
    try:
        if laser_ctl is not None:
            laser_heartbeat_stop.set()
            laser_ctl.laser_disarm()
            laser_ctl.disconnect()
    except Exception as e:
        print(f"[CLEANUP] ESP32 laser controller cleanup skipped: {e}")

    # Middleman controllers (if either mode was active) own their own
    # separate MQTT client from the publisher/subscriber module below —
    # stop them explicitly so a lingering session/heartbeat/discovery
    # thread doesn't keep publishing after the window closes, and so a
    # Physical Side properly announces itself offline (discovery "Last
    # Will" also covers an unclean exit, but a clean stop() is faster
    # for anyone watching the Other Side's dropdown).
    try:
        if physical_side_controller is not None:
            physical_side_controller.stop()
    except Exception as e:
        print(f"[CLEANUP] Physical Side controller stop skipped: {e}")

    try:
        if other_side_controller is not None:
            other_side_controller.stop()
    except Exception as e:
        print(f"[CLEANUP] Other Side controller stop skipped: {e}")

    try:
        from vision.messaging.publisher import disconnect as disconnect_mqtt
        disconnect_mqtt()
    except Exception as e:
        print(f"[CLEANUP] MQTT disconnect skipped: {e}")

    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_app_close)
root.mainloop()



