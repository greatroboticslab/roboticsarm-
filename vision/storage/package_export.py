"""
[WIRED] Export/import a self-contained "package": one folder holding
both the raw image files AND a CSV describing every captured object,
so the two travel together as a single portable unit — e.g. to hand
off data collected on one machine to another, back it up, or merge two
machines' captures into one Mongo instance.

WHY THIS EXISTS, SEPARATE FROM THE LIVE CSV/EXCEL OUTPUT
------------------------------------------------------------
vision.storage.csv_logger's captures_log.csv references images by
whatever path they were captured at on THIS machine (absolute, per the
vision.camera.capture.save_image fix) — not portable on its own, since
those paths won't exist on a different machine. A package's CSV instead
uses paths RELATIVE to the package folder itself
(images/<object_id>/<filename>), and the images are physically copied
alongside it, so the whole folder can be zipped, moved, handed to
someone else, or archived, and still make sense entirely on its own.

EXPORT
-------
export_package(dest_dir, session_id=None, all_history=False) copies
every matching object's images into dest_dir/images/<object_id>/ and
writes dest_dir/captures_log.csv describing them, using the same fixed
+ freeform columns as the live CSV log (see csv_logger._header()) so
the format is familiar.

IMPORT
-------
import_package(package_dir) reads that CSV back in and calls
vision.storage.capture_pipeline.record_capture() once per row — the
exact same path a manual GUI capture or an automatic classifier-driven
capture takes, so an imported row gets session/catalog/CSV/Excel
treatment identical to anything captured locally. Images get copied
into this machine's own local image storage first (under
images/imported/<new random object_id>/) rather than being read
in-place from the package folder, so the import still works correctly
even if the package folder is later moved or deleted.

Every imported object gets a BRAND NEW random object_id (not the one
from the source machine) — avoids id collisions if the same package
ever gets imported into more than one Mongo instance, or imported
twice. The original object_id is kept for traceability in the
freeform attributes dict (key "imported_from_object_id"), so you can
still trace an imported row back to its source row if needed.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import uuid
from datetime import datetime
from typing import List, Tuple

from vision.storage import attribute_schema, mongo_client, session_manager
from vision.storage.capture_pipeline import record_capture

IMPORTED_IMAGES_ROOT = os.path.join("images", "imported")

_CSV_HEADER = (
    ["object_id", "session_id", "catalog_id", "captured_at"]
    + attribute_schema.fixed_column_keys()
    + [attribute_schema.freeform_key()]
    + ["primary_image", "all_images", "num_images"]
)


class PackageExportError(Exception):
    """Raised for anything that should be shown to the user as a plain
    error message rather than crashing the GUI."""


def export_package(dest_dir: str, session_id: str = None, all_history: bool = False) -> Tuple[str, int, List[str]]:
    """
    Writes dest_dir/images/<object_id>/<file>.jpg for every matching
    object's photos, plus dest_dir/captures_log.csv describing them all
    (relative image paths, so the folder is portable as a unit).

    Scope: session_id (defaults to TODAY if all_history is False and no
    session_id given) or all_history=True for everything ever captured.

    Returns (dest_dir, objects_exported, warnings) — warnings covers
    individual missing image files (skipped, not fatal to the whole
    export) so one bad record doesn't block everything else.
    """
    if all_history:
        objects = mongo_client.list_recent_objects(limit=100000, sort_ascending=True)
    else:
        session_id = session_id or session_manager.today_session_id()
        objects = mongo_client.find_objects({"session_id": session_id}, limit=100000, sort_ascending=True)

    os.makedirs(dest_dir, exist_ok=True)
    images_root = os.path.join(dest_dir, "images")
    os.makedirs(images_root, exist_ok=True)
    csv_path = os.path.join(dest_dir, "captures_log.csv")
    warnings: List[str] = []
    freeform_col = attribute_schema.freeform_key()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_HEADER)
        writer.writeheader()

        for obj in objects:
            object_id = obj["_id"]
            data = obj.get("data") or {}
            image_docs = mongo_client.get_images_for_object(object_id)
            obj_image_dir = os.path.join(images_root, object_id)

            copied_rel_paths = []
            for img in image_docs:
                src = img.get("image_path", "")
                if not os.path.exists(src):
                    warnings.append(f"Object {object_id}: image file missing on disk, skipped: {src}")
                    continue
                os.makedirs(obj_image_dir, exist_ok=True)
                filename = os.path.basename(src)
                shutil.copy2(src, os.path.join(obj_image_dir, filename))
                # Relative to dest_dir, using forward slashes regardless of
                # OS, so the package opens the same way if moved between
                # Windows/Linux/Mac.
                copied_rel_paths.append("/".join(["images", object_id, filename]))

            captured_at = obj.get("captured_at")
            row = {
                "object_id": object_id,
                "session_id": obj.get("session_id", ""),
                "catalog_id": obj.get("catalog_id", ""),
                "captured_at": captured_at.isoformat(timespec="seconds") if captured_at else "",
                "primary_image": copied_rel_paths[0] if copied_rel_paths else "",
                "all_images": " | ".join(copied_rel_paths),
                "num_images": len(copied_rel_paths),
            }
            for key in attribute_schema.fixed_column_keys():
                row[key] = data.get(key, "")
            row[freeform_col] = json.dumps(data.get(freeform_col, {}), ensure_ascii=False)
            writer.writerow(row)

    return dest_dir, len(objects), warnings


def import_package(package_dir: str) -> Tuple[int, int, List[str]]:
    """
    Reads package_dir/captures_log.csv (as written by export_package)
    and calls capture_pipeline.record_capture() once per row, after
    copying that row's images into this machine's own local storage
    (images/imported/<new object_id>/) — every object gets a brand new
    random object_id; the source machine's original id is preserved in
    the freeform attributes dict for traceability.

    Returns (imported_count, skipped_count, warnings). A row whose
    image files can't be found (e.g. the package was only partially
    copied) is skipped, not fatal to the rest of the import.
    """
    csv_path = os.path.join(package_dir, "captures_log.csv")
    if not os.path.exists(csv_path):
        raise PackageExportError(f"No captures_log.csv found in '{package_dir}'.")

    imported = 0
    skipped = 0
    warnings: List[str] = []
    freeform_col = attribute_schema.freeform_key()

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            original_object_id = row.get("object_id", "?")
            all_images_field = (row.get("all_images") or "").strip()
            source_rel_paths = [p.strip() for p in all_images_field.split("|") if p.strip()]

            if not source_rel_paths:
                warnings.append(f"Row {original_object_id}: no images listed, skipped.")
                skipped += 1
                continue

            new_object_id = str(uuid.uuid4())
            dest_dir = os.path.join(IMPORTED_IMAGES_ROOT, new_object_id)
            os.makedirs(dest_dir, exist_ok=True)

            copied_pairs = []  # (source_field_name, local_path) for record_capture
            for rel_path in source_rel_paths:
                # rel_path may be relative to the package (the normal
                # case) OR already absolute (e.g. someone hand-edited
                # the CSV with a full path) — support both rather than
                # assuming.
                src = rel_path if os.path.isabs(rel_path) else os.path.join(package_dir, rel_path)
                if not os.path.exists(src):
                    warnings.append(f"Row {original_object_id}: missing image file, skipped entry: {src}")
                    continue
                filename = os.path.basename(src)
                dest_path = os.path.abspath(os.path.join(dest_dir, filename))
                shutil.copy2(src, dest_path)
                # Source camera name isn't in the CSV as its own column
                # (only bundled into primary_image/all_images), so it's
                # recovered from the filename's own
                # <timestamp>_<source>_<view>.jpg convention where
                # possible; falls back to "imported" rather than
                # guessing wrong.
                stem = os.path.splitext(filename)[0]
                parts = stem.split("_")
                source_name = parts[-2] if len(parts) >= 3 else "imported"
                copied_pairs.append((source_name, dest_path))

            if not copied_pairs:
                warnings.append(f"Row {original_object_id}: every listed image was missing, row skipped entirely.")
                skipped += 1
                continue

            try:
                freeform = json.loads(row.get(freeform_col) or "{}")
            except json.JSONDecodeError:
                freeform = {}
                warnings.append(f"Row {original_object_id}: bad JSON in '{freeform_col}', imported with empty attributes.")
            freeform["imported_from_object_id"] = original_object_id
            freeform["imported_from_package"] = os.path.abspath(package_dir)

            def _clean(value):
                return value if value not in (None, "") else None

            record_capture(
                name=row.get("name") or "imported object",
                image_paths_by_source=copied_pairs,
                category=_clean(row.get("category")),
                color=_clean(row.get("color")),
                size=_clean(row.get("size")),
                position={
                    "x": float(row["position_x"]) if row.get("position_x") else None,
                    "y": float(row["position_y"]) if row.get("position_y") else None,
                    "z": float(row["position_z"]) if row.get("position_z") else None,
                } if any(row.get(k) for k in ("position_x", "position_y", "position_z")) else None,
                attributes=freeform,
            )
            imported += 1

    return imported, skipped, warnings
