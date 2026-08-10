"""
[WIRED] Persists the Z "hard deck" — a height floor the end-effector is
never allowed to move below, protecting the physical work surface from
a runaway or misjudged move.

Two independently settable values, both LOCAL-ONLY — never settable or
lowered remotely, per the safety design:

  base_hard_deck_z   - always enforced, in every mode (even Physical
                        Manual). This is the one that "applies normally
                        anyway."
  remote_hard_deck_z - optional. If set, it must be >= base_hard_deck_z,
                        and it's enforced ADDITIONALLY (as the stricter
                        of the two) only while this machine is actively
                        being driven by Middleman — Other Side. Gives
                        the local operator extra margin specifically
                        for remote sessions they can't physically
                        supervise as closely.

Neither value can be set below the robot's current live position by
more than a small step at a time in the UI (main.py enforces the
"nudge, don't teleport" calibration workflow) — this module only
persists whatever value the caller already validated.
"""

from __future__ import annotations
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "hard_deck.json")


def load() -> dict:
    """Returns {"base_hard_deck_z": float|None, "remote_hard_deck_z": float|None}"""
    if not os.path.exists(_PATH):
        return {"base_hard_deck_z": None, "remote_hard_deck_z": None}
    try:
        with open(_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"base_hard_deck_z": None, "remote_hard_deck_z": None}
    return {
        "base_hard_deck_z": data.get("base_hard_deck_z"),
        "remote_hard_deck_z": data.get("remote_hard_deck_z"),
    }


def save(base_hard_deck_z, remote_hard_deck_z) -> None:
    with open(_PATH, "w") as f:
        json.dump(
            {"base_hard_deck_z": base_hard_deck_z, "remote_hard_deck_z": remote_hard_deck_z},
            f, indent=2,
        )
