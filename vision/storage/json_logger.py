"""
[WIRED] JSON mirror of csv_logger.py — every capture's metadata, in
native JSON, in addition to the CSV/Excel forms.

WHY THIS FILE EXISTS
---------------------
The CSV log (vision.storage.csv_logger) and Excel report
(vision.storage.excel_export) were the only on-disk "metadata" outputs.
CSV can't represent nested structures natively (the freeform
"attributes" dict already has to be JSON-encoded into a single CSV
cell — see csv_logger.append_capture_row), and reading either format
back programmatically means pulling in a CSV/xlsx parser. This module
gives the same information as plain, nested JSON instead, in two forms:

  1. append_capture_row(...) — called from
     vision.storage.capture_pipeline.record_capture() right alongside
     csv_logger.append_capture_row(), on every single capture. Appends
     ONE JSON object per line (JSON Lines / .jsonl) to
     JSON_LOG_DIR/JSON_LOG_FILENAME — append-only and safe to write to
     from multiple captures in a row without re-reading/re-writing the
     whole file (same reasoning as csv_logger: this needs to be the
     "can't fail" side, so it stays dead simple).

  2. build_json_report(session_id=None) — an on-demand REGENERATED
     full report (mirrors excel_export.build_report): reads every
     matching object (+ its linked images) straight from MongoDB and
     writes one pretty-printed JSON array to
     JSON_EXPORT_DIR/JSON_EXPORT_FILENAME. This is the one to point a
     script at if you want "everything, right now, as one JSON file" —
     the .jsonl log above is better for "tail this file live" or
     appending to a pipeline.

Both are best-effort, same as the CSV/Excel side: a failure here is
reported as a warning by the caller, never allowed to fail the capture
itself (Mongo is the safety-critical write).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, date
from typing import List

from vision.config import (
    JSON_LOG_FILENAME,
    JSON_EXPORT_FILENAME,
)
from vision.storage import attribute_schema, mongo_client, storage_location


def _log_path() -> str:
    directory = storage_location.json_log_dir()
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, JSON_LOG_FILENAME)


def _export_path(session_id: str = None, start_date: str = None, end_date: str = None) -> str:
    directory = storage_location.json_export_dir()
    os.makedirs(directory, exist_ok=True)
    stem = os.path.splitext(JSON_EXPORT_FILENAME)[0]
    if start_date and end_date:
        filename = f"{stem}_{start_date}_to_{end_date}.json"
    elif session_id:
        filename = f"{stem}_{session_id}.json"
    else:
        filename = JSON_EXPORT_FILENAME
    return os.path.join(directory, filename)


class _JSONEncoder(json.JSONEncoder):
    """Handles the couple of non-JSON-native types that show up in Mongo
    documents here — datetimes (captured_at, first_seen/last_seen on
    catalog entries) — without callers having to remember to
    isoformat() everything themselves before handing data to this
    module."""

    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


def append_capture_row(object_id: str, session_id: str, catalog_id: str,
                        captured_at: datetime, data: dict,
                        image_paths: list) -> None:
    """
    Appends exactly one JSON object (one line) to the JSON log for one
    capture. Same call signature/shape as csv_logger.append_capture_row
    — see vision.storage.capture_pipeline for the single call site,
    which calls both.

    Unlike the CSV row, the freeform "attributes" dict is written as a
    real nested JSON object here rather than a JSON-encoded string
    inside one cell.
    """
    path = _log_path()
    row = {
        "object_id": object_id,
        "session_id": session_id,
        "catalog_id": catalog_id or None,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "data": data,
        "primary_image": image_paths[0] if image_paths else None,
        "all_images": list(image_paths),
        "num_images": len(image_paths),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, cls=_JSONEncoder))
        f.write("\n")


def _object_to_entry(obj: dict) -> dict:
    object_id = obj["_id"]
    data = obj.get("data") or {}
    image_docs = mongo_client.get_images_for_object(object_id)
    image_docs.sort(key=lambda d: d.get("captured_at") or datetime.min)
    images = [
        {
            "image_id": img.get("_id"),
            "source": img.get("source"),
            "view_index": img.get("view_index"),
            "image_path": img.get("image_path"),
            "captured_at": img.get("captured_at"),
        }
        for img in image_docs
    ]
    return {
        "object_id": object_id,
        "session_id": obj.get("session_id"),
        "catalog_id": obj.get("catalog_id"),
        "captured_at": obj.get("captured_at"),
        "data": data,
        "images": images,
    }


def build_json_report(session_id: str = None,
                       start_date: str = None, end_date: str = None) -> str:
    """
    Regenerates the full JSON report — every fixed + freeform attribute
    plus every linked image — and writes it as one pretty-printed JSON
    array. Returns the path written to. Scope is one of:
      - start_date AND end_date ("YYYY-MM-DD" each, inclusive) — every
        object captured in that range. Takes priority over session_id
        if both are passed.
      - session_id — limited to that one session (e.g. "today only").
      - neither — full history.
    Mirrors excel_export.build_report()'s scope rules exactly. Raises
    ValueError if start_date/end_date are malformed or start_date is
    after end_date (see mongo_client.objects_in_date_range).
    """
    if start_date and end_date:
        objects = mongo_client.objects_in_date_range(start_date, end_date)
    elif session_id:
        objects = mongo_client.objects_for_session(session_id)
    else:
        objects = mongo_client.all_objects_for_export()

    entries: List[dict] = [_object_to_entry(o) for o in objects]

    if start_date and end_date:
        scope = {"session_id": None, "start_date": start_date, "end_date": end_date}
    elif session_id:
        scope = {"session_id": session_id}
    else:
        scope = {"session_id": None, "all_history": True}

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope": scope,
        "count": len(entries),
        "attribute_schema": {
            "fixed_columns": attribute_schema.fixed_column_keys(),
            "freeform_key": attribute_schema.freeform_key(),
        },
        "objects": entries,
    }

    path = _export_path(session_id, start_date, end_date)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, cls=_JSONEncoder)
        f.write("\n")
    return path
