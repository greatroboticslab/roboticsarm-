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

# ---------------------------------------------------------------------------
# Camera — USB (OpenCV / UVC). Supports any number of cameras, each given
# a name. Device indices are OS-assigned by plug order; confirm with
# `python -m vision.camera.capture` (no .py) once cameras are plugged in.
#
# Add/remove entries here for however many cameras you actually have -
# nothing else in the code needs to change. "station" and "wrist" are
# just the two names the existing pipeline already uses; add more (e.g.
# "overhead", "side") and they immediately become selectable in the live
# feed panel and available to capture_frame().
#
# NOTE: this local-camera setup is still used by the base (non-server-
# dependent) capture pipeline in main.py (pickup_photograph_and_identify,
# run_automatic_capture_sequence, the Live Camera Feed panel). The
# server-dependent versions of those functions don't touch a local camera
# at all — see the ENVIRONMENT TOGGLE / 4DAI section below instead.
# ---------------------------------------------------------------------------
CAMERAS = {
    "station": 0,
    "wrist": 1,
}
CAMERA_FRAME_WIDTH = 1280
CAMERA_FRAME_HEIGHT = 720

# How often the live preview panel grabs a new frame. Lower = smoother
# but more CPU/USB bandwidth; 10 fps is a reasonable default for a
# Tkinter preview (not meant to be broadcast-quality video).
LIVE_FEED_FPS = 10

# ===========================================================================
# ENVIRONMENT TOGGLE
# ===========================================================================
# Controls where the server-dependent code (server-triggered capture,
# continuous sweep, generic remote control) looks for 4DAI's FastAPI
# server, the MQTT broker, and MongoDB. TEST_MODE=True reproduces the same
# localhost values the base config used before this toggle existed, so
# flipping it doesn't change behavior for local testing.
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

# Database collections — same database 4DAI's server uses, so 4DAI's
# view_data.py can browse the "objects" category with zero changes on
# its side.
MONGO_DB_NAME = "Collections"
MONGO_OBJECTS_COLLECTION = "objects"          # one doc per capture (the "log")
MONGO_IMAGES_COLLECTION = "images"            # one doc per photo
MONGO_SESSIONS_COLLECTION = "sessions"        # one doc per calendar day
MONGO_OBJECT_CATALOG_COLLECTION = "object_catalog"  # one doc per distinct known object ("inventory")

# ===========================================================================
# ATTRIBUTE / CSV / EXCEL LOGGING
# ===========================================================================
# Editable attribute-table definition (fixed columns + reserved slots for
# later + freeform key/value). Kept as a plain JSON file (not hardcoded
# constants) specifically so the columns can be changed later — add/rename/
# remove a fixed attribute — without touching any Python code. See
# vision/storage/attribute_schema.py for the loader/editor functions.
ATTRIBUTE_SCHEMA_PATH = "vision/storage/attribute_schema.json"

# Everything CSV/Excel writes lives under its own subfolder, separate from
# the raw per-object image folders (IMAGES_ROOT below), so "give me the
# spreadsheet(s)" and "give me the photos" are two clearly separate places.
DATA_LOGS_DIR = "data_logs"
CSV_LOG_DIR = "data_logs/csv"
CSV_LOG_FILENAME = "captures_log.csv"          # single, ever-growing append log
EXCEL_EXPORT_DIR = "data_logs/excel"
EXCEL_EXPORT_FILENAME = "captures_report.xlsx"  # regenerated report (log + inventory sheets)

# DATA_AUTHORITY_MODE controls whether the Database tab offers "Reconcile
# from Excel" (hand-edit the .xlsx, pull those edits back into MongoDB):
#   "excel" -> testing/now: hand-editing the report is expected; reconcile
#              is offered.
#   "mongo" -> the goal state: MongoDB is authoritative, Excel is a
#              read-only generated report; no reconcile needed.
# Regardless of this setting, MongoDB is always written first/live on every
# capture (the only side safe for concurrent/live writes) and the CSV log
# is always appended to — this flag only changes whether manual Excel edits
# are expected/pulled back in.
DATA_AUTHORITY_MODE = "excel"

# Catalog auto-matching: name-string match only for now (no real
# re-identification model yet) — normalized (casefold + strip whitespace),
# optionally narrowed by category. Treated as a loose *suggestion* link,
# not a hard guarantee — see vision/storage/object_catalog.py.
CATALOG_MATCH_ON_CATEGORY = True

# ===========================================================================
# SWEEP SETTINGS
# ===========================================================================
# How many degrees of movement across J1, J2, or J3 triggers a photo during
# run_continuous_sweep() (the server-dependent sweep feature in main.py).
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

# ===========================================================================
# MQTT TOPICS
# ===========================================================================
TOPIC_ARM_OBJECT_CAPTURED = "arm/object/captured"
TOPIC_VISION_RESULT = "vision/result"
TOPIC_ARM_COMMAND = "arm/command"

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
TOPIC_CAPTURE_COMMAND = "4dai/capture/command"       # 4DAI -> arm: start a sequence (base, local-camera)
TOPIC_CAPTURE_STATUS = "arm/capture/status"          # arm -> 4DAI: progress/result

# Server-dependent variant of TOPIC_CAPTURE_COMMAND: used by the
# continuous-sweep handler (_handle_capture_command_server_dependent /
# start_capture_command_listener_server_dependent in main.py) so it can run
# on its own topic without colliding with the base listener above.
TOPIC_CAPTURE_COMMAND_SERVER_DEPENDENT = "arm/command/capture"

# Additional status/telemetry topics introduced alongside the server-
# dependent capture flow. Not consumed anywhere yet — reserved for future
# use (e.g. richer status reporting, live telemetry streaming).
TOPIC_ARM_CAPTURE_STATUS = "arm/event/status"
TOPIC_TELEMETRY = "arm/telemetry"

# ---------------------------------------------------------------------------
# Generic remote arm control. Anything - 4DAI's own UI, an external
# AI/automation script, etc - can publish here to move the arm without
# touching this machine directly. Two message shapes:
#   {"jog": "J4+"}  / {"jog": "J4-"} / {"jog": "stop"}   - jog control
#   {"j1": .., "j2": .., "j3": .., "j4": ..}             - absolute move
# See main.py: _handle_move_command().
# ---------------------------------------------------------------------------
TOPIC_ARM_MOVE_COMMAND = "arm/command/move"

# ---------------------------------------------------------------------------
# MongoDB placeholders — same database 4DAI's server uses, so 4DAI's
# view_data.py can browse the "objects" category with zero changes on
# its side. (MONGO_URI/MONGO_DB_NAME/etc. now come from the ENVIRONMENT
# TOGGLE section above; kept here as a pointer for anyone looking for them
# near the rest of the MQTT/4DAI contract.)
# ---------------------------------------------------------------------------

# ===========================================================================
# LOCAL LLM (NATURAL-LANGUAGE MONGO QUERY) — see
# vision/services/mongo_nlp_agent.py for the full explanation.
#
# This is deliberately kept SECONDARY to standard/direct MongoDB access
# (vision.storage.mongo_client) — the "Objects"/"Images"/"Inventory" browser
# on the Database tab never touches an LLM at all. This section only
# configures the optional "Ask" box layered on top of it.
# ===========================================================================
# Points at a local Ollama install (https://ollama.com), not a hosted API —
# no key needed. Requires `ollama pull <model>` for whichever model is
# selected before it will work.
OLLAMA_HOST = "http://localhost:11434"

# The langchain-mongodb agent toolkit needs a model that reliably supports
# tool/function calling — reasoning models (deepseek-r1) and small (<7B)
# general models do NOT reliably support this (see
# mongodb-nlp-query-summary.txt). qwen2.5:7b+ and llama3.1:8b+ do.
NLP_AGENT_MODEL = "qwen2.5:7b"

# Old DeepSeek-via-Ollama defaults, kept only because
# vision/services/deepseek_query.py (the previous, now-superseded NL layer)
# still references them. Not used by mongo_nlp_agent.py.
OLLAMA_MODEL = "deepseek-r1:7b"
OLLAMA_MODEL_LIGHTWEIGHT = "deepseek-r1:1.5b"

# How many recent object documents to scan when building the "known
# fields" list handed to the model as context. User-adjustable from the
# GUI; this is just the default.
NL_QUERY_FIELD_SAMPLE_SIZE = 50

# ===========================================================================
# MIDDLEMAN MODE (Physical Control tab) — lets one machine (Physical Side,
# real robot/camera/laser attached) be driven remotely by another (Other
# Side, the controller) over the same MQTT broker already used elsewhere.
# See vision/services/middleman_*.py and vision/services/photo_transfer.py.
# ===========================================================================

# Shared, unnamespaced — every Physical Side announces itself here so Other
# Side instances can discover/select one instead of typing an IP blind.
MIDDLEMAN_DISCOVERY_TOPIC = "arm/middleman/discovery"

# Per-Physical-Side topics are namespaced by that machine's IP (chosen as
# the identifier — see net_utils.get_local_ip()) so multiple Physical
# Side/Other Side pairs on the same broker never cross-talk, with no
# separate ID scheme needed on top.
MIDDLEMAN_SESSION_TOPIC_TEMPLATE = "arm/middleman/{ip}/session"
MIDDLEMAN_CONTROL_STATUS_TOPIC_TEMPLATE = "arm/middleman/{ip}/control_status"
MIDDLEMAN_MOVE_TOPIC_TEMPLATE = "arm/middleman/{ip}/move"
MIDDLEMAN_LASER_TOPIC_TEMPLATE = "arm/middleman/{ip}/laser"
MIDDLEMAN_TELEMETRY_TOPIC_TEMPLATE = "arm/middleman/{ip}/telemetry"
MIDDLEMAN_CAPTURE_REQUEST_TOPIC_TEMPLATE = "arm/middleman/{ip}/capture_request"
MIDDLEMAN_PHOTO_TOPIC_TEMPLATE = "arm/middleman/{ip}/photo"
MIDDLEMAN_ERROR_TOPIC_TEMPLATE = "arm/middleman/{ip}/error"

# Heartbeat cadence for both the discovery broadcast and the active-
# controller session. 3 missed beats before something is considered gone.
MIDDLEMAN_HEARTBEAT_INTERVAL_SECONDS = 2
MIDDLEMAN_HEARTBEAT_TIMEOUT_SECONDS = 6

# How long a photo-bundle publish is allowed to take to assemble before
# giving up (base64-encoding + publishing several views can take a moment
# on a slow link).
MIDDLEMAN_PHOTO_TRANSFER_TIMEOUT_SECONDS = 30

# Max dimension (longest side, px) for images relayed over the middleman
# link. Downscaled independently of whatever full-res copy (if any) stays
# local on the Physical Side, to keep MQTT payloads reasonable regardless
# of camera resolution.
MIDDLEMAN_PHOTO_TRANSFER_MAX_DIMENSION = 800
MIDDLEMAN_PHOTO_TRANSFER_JPEG_QUALITY = 70
