"""
[WIRED] Append-only CSV log of every capture — always on, both modes.

WHY THIS FILE EXISTS
---------------------
Per the plan: MongoDB is written first/live on every capture (the only
side safe for concurrent/live writes), and every capture is ALSO
appended as one row to a plain CSV log, regardless of
vision.config.DATA_AUTHORITY_MODE. CSV append is safe even if something
else has the file open for reading (unlike .xlsx, which excel_export.py
handles separately with its own file-locking precautions) — this file
is deliberately the "can't fail" audit trail, kept dead simple on
purpose (stdlib csv module only, no pandas/openpyxl dependency).

Lives in its own subfolder (vision.config.CSV_LOG_DIR) separate from
the Excel report folder and the raw per-object image folders, so the
three kinds of output ("audit log", "human report", "photos") are each
easy to find on their own.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime

from vision.config import CSV_LOG_DIR, CSV_LOG_FILENAME
from vision.storage import attribute_schema

_HEADER_EXTRA = ["object_id", "session_id", "catalog_id", "captured_at",
                  "primary_image", "all_images", "num_images"]


def _log_path() -> str:
    os.makedirs(CSV_LOG_DIR, exist_ok=True)
    return os.path.join(CSV_LOG_DIR, CSV_LOG_FILENAME)


def _header() -> list:
    freeform_col = attribute_schema.freeform_key()
    return ["object_id", "session_id", "catalog_id", "captured_at"] + \
        attribute_schema.fixed_column_keys() + [freeform_col] + \
        ["primary_image", "all_images", "num_images"]


def append_capture_row(object_id: str, session_id: str, catalog_id: str,
                        captured_at: datetime, data: dict,
                        image_paths: list) -> None:
    """
    Appends exactly one row to the CSV log for one capture. `data` is
    the same dict stored in the object's MongoDB document (fixed
    columns + the freeform "attributes" dict) — see
    vision.storage.capture_pipeline for the single call site.
    """
    path = _log_path()
    is_new_file = not os.path.exists(path)

    freeform_col = attribute_schema.freeform_key()
    row = {
        "object_id": object_id,
        "session_id": session_id,
        "catalog_id": catalog_id or "",
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "primary_image": image_paths[0] if image_paths else "",
        "all_images": " | ".join(image_paths),
        "num_images": len(image_paths),
    }
    for key in attribute_schema.fixed_column_keys():
        row[key] = data.get(key, "")
    row[freeform_col] = data.get(freeform_col, {})

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_header())
        if is_new_file:
            writer.writeheader()
        writer.writerow(row)
