"""
[WIRED] Central configuration for the vision/identification pipeline.

Everything here is a plain constant so it can be imported anywhere
(main.py, services/*, camera/*) without circular-import issues.
"""

# ---------------------------------------------------------------------------
# Photo station — fixed pose the arm returns to before photographing.
#
# TEMPORARY: currently near full extension (close to the IK singularity
# boundary discussed earlier). Fine for early wiring/testing, but should be
# moved to a corner position (lower radius, off-axis) before relying on it
# for repeated/production capture. Swap the values below when ready — every
# consumer of PHOTO_STATION (main.py's yellow dot, the capture pipeline)
# will pick up the change automatically.
# ---------------------------------------------------------------------------
PHOTO_STATION = {
    "x": 0.0,
    "y": 390.0,
    "z": 150.0,
    "r": 0.0,
}

# Corner candidate to switch to later (kept here for convenience):
# PHOTO_STATION = {"x": 150.0, "y": 200.0, "z": 150.0, "r": 0.0}

# Number of J4 rotation steps for a full 360 degree view during capture.
NUM_VIEWS = 6

# Small settle delay (seconds) after each J4 step before capturing a frame,
# to avoid motion blur from residual swing of the held object.
VIEW_SETTLE_SECONDS = 0.2

# ===========================================================================
# ENVIRONMENT TOGGLE
# ===========================================================================
TEST_MODE = True  # Default to Local. Set to False to target 3.134.125.175

if TEST_MODE:
    FOURDAI_API_URL = "http://localhost:8000"
    MQTT_BROKER_HOST = "localhost"
    MQTT_BROKER_PORT = 1883
    MONGO_URI = "mongodb://localhost:27017"
else:
    FOURDAI_API_URL = "http://3.134.125.175:443" 
    MQTT_BROKER_HOST = "3.134.125.175"
    MQTT_BROKER_PORT = 1883
    MONGO_URI = "mongodb://3.134.125.175:27017"

# Database collections
MONGO_DB_NAME = "Collections"
MONGO_OBJECTS_COLLECTION = "objects"
MONGO_IMAGES_COLLECTION = "images"

# ===========================================================================
# SWEEP SETTINGS
# ===========================================================================
# NOTE: local camera hardware settings (CAMERAS, CAMERA_FRAME_WIDTH/HEIGHT,
# LIVE_FEED_FPS) have been removed. All photo capture is now handed off to
# 4DAI over REST (see /collection/trigger-webcam-capture calls in
# main-arm.py) - this machine no longer owns any camera hardware.

# How many degrees of movement across J1, J2, or J3 triggers a photo
SWEEP_TRIGGER_DEGREES = 25.0

# ---------------------------------------------------------------------------
# Laser — USB serial (pyserial, already in requirements.txt). Most USB
# laser modules/relay boards enumerate as a plain serial port and accept a
# short text or byte command to switch on/off.
#
# CONFIRM BEFORE USE:
#   1. Plug in the laser, check Device Manager (Windows) for its COM port,
#      or `ls /dev/tty*` (Linux/Mac) before/after plugging it in.
#   2. Check any datasheet/manual for the exact ON/OFF command + baud rate.
#      b"1"/b"0" and b"ON\n"/b"OFF\n" are both common defaults for cheap
#      relay-style modules - try the simplest first.
# ---------------------------------------------------------------------------
LASER_SERIAL_PORT = "COM3"        # Windows placeholder; e.g. "/dev/ttyUSB0" on Linux/Mac
LASER_BAUD_RATE = 9600
LASER_ON_COMMAND = b"1"
LASER_OFF_COMMAND = b"0"



# ---------------------------------------------------------------------------
# 4DAI <-> arm automation contract.
#
# 4DAI's Streamlit "Collection" page publishes a capture-command here
# instead of asking the user to manually click a photo per position (see
# transcript: "click start image crash ... automatic, no click"). The arm
# subscribes, drives the sequence itself (rotate by N degrees, wait, snap
# a photo, repeat), and reports progress/completion on the status topic.
# 4DAI's mqtt_bridge.py subscribes to the status topic and files the
# result into the same REST endpoints/Mongo collections a manual
# submission would use - so from 4DAI's point of view an automatic
# capture looks identical to someone clicking "Submit" themselves.
#
# Both repos are independent projects; keep these topic strings identical
# on both sides (arm: vision/config.py, 4DAI: Server/bridge_config.py) if
# you ever rename them.
# ---------------------------------------------------------------------------
TOPIC_CAPTURE_STATUS = "arm/capture/status"          # arm -> 4DAI: progress/result

# ---------------------------------------------------------------------------
# Generic remote arm control. Anything - 4DAI's own UI, an external
# AI/automation script, etc - can publish here to move the arm without
# touching this machine directly. Two message shapes:
#   {"jog": "J4+"}  / {"jog": "J4-"} / {"jog": "stop"}   - jog control
#   {"j1": .., "j2": .., "j3": .., "j4": ..}             - absolute move
# See main.py: _handle_move_command().
# ---------------------------------------------------------------------------

# ===========================================================================
# MQTT TOPICS
# ===========================================================================
TOPIC_CAPTURE_COMMAND = "arm/command/capture"
TOPIC_ARM_MOVE_COMMAND = "arm/command/move"
TOPIC_ARM_OBJECT_CAPTURED = "arm/event/captured"
TOPIC_ARM_CAPTURE_STATUS = "arm/event/status"
TOPIC_VISION_RESULT = "vision/event/result"
TOPIC_TELEMETRY = "arm/telemetry"