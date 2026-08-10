"""
[WIRED] Persists the name/pin mapping for the ESP32 relay channels used
to switch the rig's laser diode modules on/off.

Per the board photos: this rig's 4 lasers are each behind their own
plain ON/OFF relay module ("1 Relay Module High/Low Level Trigger",
SRD-05VDC-SL-C) — not one PWM-dimmable laser. That means they're
plain relay outputs and belong on RelayController's generic multi-
channel CONFIG/SET commands (configure_channel/set_channel in
relay_controller.py, channels 1-16), not the separate single-instance
LASER CONFIG/ARM/SET PWM command family also exposed by the same
firmware (which is for a different kind of PWM-driven output entirely,
and is a likely source of the "rejected by the board" error if its
pin happened to collide with one of these already-configured relay
channels — the board only ever has one PIN per role).

Only the name/pin mapping is persisted — never "armed" or on/off state
(mirrors the existing safety model: every session starts with lasers
unconfigured and off, requiring an explicit Configure + ON click).
Mirrors vision/camera/capture.py's camera-assignment persistence
pattern for consistency.
"""

from __future__ import annotations
import json
import os

_ASSIGNMENTS_PATH = os.path.join(os.path.dirname(__file__), "laser_channel_assignments.json")


def load_assignments() -> dict:
    """Returns {"<channel_number_str>": {"name": str, "pin": int}, ...}"""
    if not os.path.exists(_ASSIGNMENTS_PATH):
        return {}
    try:
        with open(_ASSIGNMENTS_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_assignments(assignments: dict) -> None:
    with open(_ASSIGNMENTS_PATH, "w") as f:
        json.dump(assignments, f, indent=2)
