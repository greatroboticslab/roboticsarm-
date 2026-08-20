"""
[WIRED] Loader/editor for the object-attribute table.

WHY THIS FILE EXISTS
---------------------
The user asked for the fixed attribute columns (size, color, category,
position x/y/z, a few reserved slots to assign meaning to later) to be
modifiable later without touching code. Rather than hardcoding those
column names as Python constants anywhere, they live in a plain JSON
file (attribute_schema.json, next to this module) that this file just
reads/writes. Every other module that needs "what are the fixed
columns" (vision.storage.mongo_client, csv_logger, excel_export) calls
into here instead of hardcoding its own copy — so editing the JSON (or
calling add_reserved_column_meaning() below) changes the schema
everywhere at once: new MongoDB documents, the CSV log, and the Excel
report.

Freeform attributes (whatever the classifier/model returns beyond the
fixed columns) live in a single "attributes" dict field alongside the
fixed columns, matching the user's request to "see why" — a fixed
`category="mug"` plus a freeform `attributes={"material": "ceramic",
"confidence_notes": "handle partially occluded in view 2"}` shows both
the decision and the reasoning behind it.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List

from vision.config import ATTRIBUTE_SCHEMA_PATH

# Sentinel for "the classifier/model looked at this and could not
# determine a value" — distinct from Python `None`, which means "this
# field was never attempted / doesn't apply yet" (e.g. every field on a
# freshly-stubbed capture before a real classifier exists). Use this
# constant (not the string "unknown" typed by hand, in case the
# convention ever needs to change) wherever a real classifier explicitly
# doesn't know an answer — e.g. classifier.identify()'s attributes dict,
# or a fixed column like color/category. Excel/CSV/GUI display it as-is
# (a visible "unknown", not a blank cell), so a blank vs. "unknown" cell
# always means something different and you're never guessing which case
# you're looking at.
UNKNOWN = "unknown"

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ATTRIBUTE_SCHEMA_PATH,
) if not os.path.isabs(ATTRIBUTE_SCHEMA_PATH) else ATTRIBUTE_SCHEMA_PATH

# Simpler, robust path resolution: attribute_schema.json always lives next
# to this file regardless of where the process is launched from.
_LOCAL_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attribute_schema.json")


def _schema_path() -> str:
    return _LOCAL_SCHEMA_PATH if os.path.exists(_LOCAL_SCHEMA_PATH) else _SCHEMA_PATH


def load_schema() -> dict:
    """Read the current attribute schema from disk. Safe to call often —
    this is a small JSON file, not a database round-trip."""
    with open(_schema_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def save_schema(schema: dict) -> None:
    """Write the schema back to disk (used by the add/remove/rename
    helpers below). Pretty-printed so it stays hand-editable too."""
    with open(_schema_path(), "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
        f.write("\n")


def fixed_column_keys() -> List[str]:
    """All fixed (non-freeform) column keys, in display order:
    fixed_columns + position_columns (flattened as position_x/y/z) +
    reserved_columns."""
    schema = load_schema()
    keys = [c["key"] for c in schema.get("fixed_columns", [])]
    keys += [f"position_{c['key']}" for c in schema.get("position_columns", [])]
    keys += [c["key"] for c in schema.get("reserved_columns", [])]
    return keys


def fixed_column_defaults() -> Dict[str, object]:
    """{column_key: default_value} for every fixed column — used to
    initialize a new object document/CSV row so every row always has
    every fixed column present (even if null), which is what lets a
    reserved column be "assigned later" without a migration."""
    schema = load_schema()
    defaults = {c["key"]: c.get("default") for c in schema.get("fixed_columns", [])}
    for c in schema.get("position_columns", []):
        defaults[f"position_{c['key']}"] = c.get("default")
    for c in schema.get("reserved_columns", []):
        defaults[c["key"]] = c.get("default")
    return defaults


def freeform_key() -> str:
    return load_schema().get("freeform_key", "attributes")


def display_labels() -> Dict[str, str]:
    """{column_key: human-readable label}, for Excel headers / GUI labels."""
    schema = load_schema()
    labels = {c["key"]: c["label"] for c in schema.get("fixed_columns", [])}
    for c in schema.get("position_columns", []):
        labels[f"position_{c['key']}"] = c["label"]
    for c in schema.get("reserved_columns", []):
        labels[c["key"]] = c["label"]
    return labels


def rename_reserved_column(reserved_key: str, new_label: str, new_type: str = "string") -> None:
    """
    Give meaning to one of the "reserved_N" slots later (e.g. once you
    know you want to track "material" or "weight") without renaming the
    underlying Mongo/CSV field — existing rows keep their data, only the
    display label (and expected type) changes. To add a brand-new fixed
    column instead of reusing a reserved slot, use add_fixed_column().
    """
    schema = load_schema()
    for c in schema.get("reserved_columns", []):
        if c["key"] == reserved_key:
            c["label"] = new_label
            c["type"] = new_type
            save_schema(schema)
            return
    raise KeyError(f"No reserved column named '{reserved_key}' in the schema.")


def add_fixed_column(key: str, label: str, col_type: str = "string", default=None) -> None:
    """Add a brand-new fixed column. Existing documents simply won't have
    this key until re-saved/edited — readers should use .get(key,
    default) rather than assuming every historical row has it."""
    schema = load_schema()
    if any(c["key"] == key for c in schema.get("fixed_columns", [])):
        raise ValueError(f"Fixed column '{key}' already exists.")
    schema.setdefault("fixed_columns", []).append(
        {"key": key, "label": label, "type": col_type, "default": default}
    )
    save_schema(schema)


def remove_fixed_column(key: str) -> None:
    """Remove a fixed column from the schema going forward. Does not
    delete the field from existing MongoDB documents/CSV rows — it just
    stops being included in newly generated Excel reports/GUI forms."""
    schema = load_schema()
    schema["fixed_columns"] = [c for c in schema.get("fixed_columns", []) if c["key"] != key]
    save_schema(schema)
