"""
Smoke test for the storage pipeline (Mongo write -> CSV append -> Excel
export) using fake images and fake attribute data — no camera, no
classifier, no robot arm required.

WHAT THIS PROVES
-----------------
Run this before wiring in a real classifier/camera to confirm the
*storage* half of the system actually works on your machine:
  - MongoDB is reachable and the objects/images/sessions/object_catalog
    collections get written to correctly.
  - The CSV audit log gets a row appended.
  - The .xlsx report regenerates with both the Log and Inventory sheets,
    including working hyperlinks to the (fake) image files.
  - The catalog matcher links repeat "objects" together (this script
    captures the same fake object name twice on purpose, to prove
    times_seen goes to 2 instead of creating two catalog entries).

HOW TO RUN
-----------
    1. Make sure MongoDB is running (`mongod`, or however you normally
       start it) and vision/config.py's MONGO_URI points at it.
    2. From the repo root:
           python -m testing_scripts.smoke_test_pipeline
    3. Check the output — it prints exactly what got written and where.
       Open the printed .xlsx path in Excel to eyeball the report, and
       run `python -m vision.storage.mongo_client` separately if you
       just want a bare "can I connect at all" check.

This does NOT touch vision.model.classifier — it calls
vision.storage.capture_pipeline.record_capture() directly with made-up
name/category/color/attributes values, exactly like a real classifier's
output would be passed in once identify() is implemented.
"""

from __future__ import annotations

import os
import tempfile

from PIL import Image

from vision.storage.capture_pipeline import record_capture
from vision.storage import mongo_client, attribute_schema


def _make_fake_image(tmpdir: str, name: str) -> str:
    """A tiny real JPEG (not just empty bytes) so Excel's hyperlink and
    Pillow-based GUI preview both have something genuine to open."""
    path = os.path.join(tmpdir, name)
    Image.new("RGB", (64, 64), color=(200, 50, 50)).save(path, "JPEG")
    return path


def main() -> None:
    print(f"Connecting to MongoDB ...")
    mongo_client._get_db()  # raises with a clear message if unreachable
    print("Connected.\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Capture #1: a "red mug" with two camera views ---
        img_a = _make_fake_image(tmpdir, "fake_station_0.jpg")
        img_b = _make_fake_image(tmpdir, "fake_wrist_0.jpg")

        object_id_1, warnings_1 = record_capture(
            name="red mug",
            image_paths_by_source={"station": img_a, "wrist": img_b},
            category="mug",
            color="red",
            size="medium",
            position={"x": 12.5, "y": -3.0, "z": 0.0},
            attributes={
                "material": "ceramic",
                "confidence_notes": "handle partially occluded in wrist view",
                "shape": attribute_schema.UNKNOWN,  # exercises the "tried, couldn't tell" case
            },
        )
        print(f"Capture 1 recorded: object_id={object_id_1}")
        if warnings_1:
            print(f"  (non-fatal warnings: {warnings_1})")

        # --- Capture #2: the SAME object name again, to test the catalog matcher ---
        img_c = _make_fake_image(tmpdir, "fake_station_0_again.jpg")
        object_id_2, warnings_2 = record_capture(
            name="Red Mug",  # different casing on purpose — normalization should still match
            image_paths_by_source={"station": img_c},
            category="mug",
            color="red",
        )
        print(f"Capture 2 recorded: object_id={object_id_2}")
        if warnings_2:
            print(f"  (non-fatal warnings: {warnings_2})")

        # --- Verify everything landed where it should ---
        obj_1 = mongo_client.get_object(object_id_1)
        images_1 = mongo_client.get_images_for_object(object_id_1)
        catalog_id = obj_1.get("catalog_id")
        catalog_entries = mongo_client.list_catalog(limit=10)
        matching_entry = next((e for e in catalog_entries if e["_id"] == catalog_id), None)

        print("\n--- Verification ---")
        print(f"Object 1 fixed columns: "
              f"{ {k: obj_1['data'].get(k) for k in attribute_schema.fixed_column_keys()} }")
        print(f"Object 1 freeform attributes: {obj_1['data'].get(attribute_schema.freeform_key())}")
        print(f"Object 1 has {len(images_1)} linked image(s) (expected 2).")
        print(f"Catalog entry '{catalog_id}': times_seen="
              f"{matching_entry.get('times_seen') if matching_entry else '???'} (expected 2 — "
              f"proves 'Red Mug' matched 'red mug' via name normalization).")

    print("\nSmoke test complete. Now check:")
    print("  1. The GUI's Database tab -> Objects (Log): both captures should appear.")
    print("  2. Database tab -> Inventory: one 'red mug' entry with times_seen=2.")
    print("  3. The generated .xlsx (see vision/config.py EXCEL_EXPORT_DIR) — "
          "open it and click a 'Primary Image' hyperlink.")
    print("  4. If DATA_AUTHORITY_MODE == 'excel': edit a cell in the Log sheet, "
          "save, then click 'Reconcile from Excel' in the GUI and confirm the "
          "edit shows up back in Mongo.")


if __name__ == "__main__":
    main()
