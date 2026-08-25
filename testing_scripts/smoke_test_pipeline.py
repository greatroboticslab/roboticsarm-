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
  - The CSV/JSON audit logs get a row appended.
  - The .xlsx report regenerates with both the Log and Inventory sheets,
    including working hyperlinks to the (fake) image files.
  - The catalog matcher links repeat "objects" together (this script
    captures the same fake object name twice on purpose, to prove
    times_seen goes to 2 instead of creating two catalog entries).
  - A rotation-style capture (several images from the SAME camera
    source) round-trips correctly — record_capture()'s
    image_paths_by_source list[(source, path)] form, view_index [0,1,2].
  - Fake images land under the actual configured storage location (see
    vision.storage.storage_location), not an arbitrary temp folder —
    proving a real capture would end up in the right place too.

HOW TO RUN
-----------
    1. Make sure MongoDB is running (`mongod`, or however you normally
       start it) and vision/config.py's MONGO_URI points at it.
    2. From the repo root:
           python -m testing_scripts.smoke_test_pipeline
       By default the 3 test captures are LEFT in MongoDB afterward —
       on purpose, so you can open the GUI (Database tab -> Objects/
       Images/Inventory) and actually see them: attributes, images,
       and the catalog-matched "red mug" entry with times_seen=2.
       Pass --cleanup to delete them again once you're done looking:
           python -m testing_scripts.smoke_test_pipeline --cleanup
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

import argparse
import os

from PIL import Image

from vision.camera.capture import ensure_sample_dir, new_sample_id
from vision.storage.capture_pipeline import record_capture
from vision.storage import mongo_client, attribute_schema, storage_location


def _make_fake_image(sample_dir: str, name: str) -> str:
    """A tiny real JPEG (not just empty bytes) so Excel's hyperlink and
    Pillow-based GUI preview both have something genuine to open.
    Written under a real ensure_sample_dir() folder (i.e. under the
    actual configured storage location — see vision.storage.
    storage_location) rather than an arbitrary temp folder, so this
    test proves images actually land where a real capture's would."""
    path = os.path.join(sample_dir, name)
    Image.new("RGB", (64, 64), color=(200, 50, 50)).save(path, "JPEG")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", action="store_true",
                         help="Delete the 3 test objects from MongoDB after running, instead "
                              "of the default (leave them in place so you can inspect them "
                              "in the GUI's Database tab).")
    args = parser.parse_args()

    print("Resolved storage location:")
    print(f"  Root:   {storage_location.get_storage_root()}")
    print(f"  Images: {storage_location.images_root()}")
    print(f"  CSV:    {storage_location.csv_log_dir()}")
    print(f"  Excel:  {storage_location.excel_export_dir()}")
    print(f"  JSON:   {storage_location.json_log_dir()}")
    print()

    print("Connecting to MongoDB ...")
    mongo_client._get_db()  # raises with a clear message if unreachable
    print("Connected.\n")

    test_object_ids = []

    # --- Capture #1: a "red mug" with two camera views ---
    sample_id_1 = new_sample_id()
    sample_dir_1 = ensure_sample_dir(sample_id_1)
    img_a = _make_fake_image(sample_dir_1, "fake_station_0.jpg")
    img_b = _make_fake_image(sample_dir_1, "fake_wrist_0.jpg")

    object_id_1, warnings_1 = record_capture(
        name="red mug",
        image_paths_by_source={"station": img_a, "wrist": img_b},
        category="mug",
        color="red",
        size="medium",
        position={"x": 12.5, "y": -3.0, "z": 0.0, "r": 90.0},
        attributes={
            "material": "ceramic",
            "confidence_notes": "handle partially occluded in wrist view",
            "shape": attribute_schema.UNKNOWN,  # exercises the "tried, couldn't tell" case
        },
    )
    test_object_ids.append(object_id_1)
    print(f"Capture 1 recorded: object_id={object_id_1}")
    if warnings_1:
        print(f"  (non-fatal warnings: {warnings_1})")

    # --- Capture #2: the SAME object name again, to test the catalog matcher ---
    sample_id_2 = new_sample_id()
    sample_dir_2 = ensure_sample_dir(sample_id_2)
    img_c = _make_fake_image(sample_dir_2, "fake_station_0_again.jpg")
    object_id_2, warnings_2 = record_capture(
        name="Red Mug",  # different casing on purpose — normalization should still match
        image_paths_by_source={"station": img_c},
        category="mug",
        color="red",
    )
    test_object_ids.append(object_id_2)
    print(f"Capture 2 recorded: object_id={object_id_2}")
    if warnings_2:
        print(f"  (non-fatal warnings: {warnings_2})")

    # --- Capture #3: rotation-style — 3 images, all from the SAME
    # "station" source, via the list[(source, path)] form (a plain dict
    # can't represent duplicate keys — see capture_pipeline.
    # record_capture()'s docstring). Proves the rotation-sequence path
    # actually works, and that view_index comes out [0, 1, 2] in order. ---
    sample_id_3 = new_sample_id()
    sample_dir_3 = ensure_sample_dir(sample_id_3)
    rotation_images = [
        ("station", _make_fake_image(sample_dir_3, f"fake_station_rot_{i}.jpg"))
        for i in range(3)
    ]
    object_id_3, warnings_3 = record_capture(
        name="rotation test widget",
        image_paths_by_source=rotation_images,
        category="test",
    )
    test_object_ids.append(object_id_3)
    print(f"Capture 3 recorded (rotation-style, 3 images): object_id={object_id_3}")
    if warnings_3:
        print(f"  (non-fatal warnings: {warnings_3})")

    # --- Verify everything landed where it should ---
    obj_1 = mongo_client.get_object(object_id_1)
    images_1 = mongo_client.get_images_for_object(object_id_1)
    catalog_id = obj_1.get("catalog_id")
    catalog_entries = mongo_client.list_catalog(limit=10)
    matching_entry = next((e for e in catalog_entries if e["_id"] == catalog_id), None)

    images_3 = mongo_client.get_images_for_object(object_id_3)
    view_indices_3 = sorted(img.get("view_index") for img in images_3)

    print("\n--- Verification ---")
    print(f"Object 1 fixed columns: "
          f"{ {k: obj_1['data'].get(k) for k in attribute_schema.fixed_column_keys()} }")
    print(f"Object 1 freeform attributes: {obj_1['data'].get(attribute_schema.freeform_key())}")
    print(f"Object 1 has {len(images_1)} linked image(s) (expected 2).")
    print(f"Catalog entry '{catalog_id}': times_seen="
          f"{matching_entry.get('times_seen') if matching_entry else '???'} (expected 2 — "
          f"proves 'Red Mug' matched 'red mug' via name normalization).")
    print(f"Object 3 (rotation-style) has {len(images_3)} linked image(s) (expected 3), "
          f"view_index={view_indices_3} (expected [0, 1, 2]).")

    if not args.cleanup:
        print("\nLeaving the 3 test objects in MongoDB (pass --cleanup to delete them). Now check:")
        print("  1. The GUI's Database tab -> Objects: all 3 captures should appear.")
        print("  2. Database tab -> Inventory: one 'red mug' entry with times_seen=2.")
        print("  3. The generated .xlsx (see vision.storage.storage_location.excel_export_dir()) "
              "— open it and click a 'Primary Image' hyperlink.")
        print("  4. If DATA_AUTHORITY_MODE == 'excel': edit a cell in the Attribute Data "
              "Collection sheet, save, then click 'Reconcile from Excel' in the GUI and "
              "confirm the edit shows up back in Mongo.")
        print(f"  Test object_ids: {test_object_ids}")
    else:
        print("\n--cleanup passed — deleting test objects...")
        for object_id in test_object_ids:
            deleted_images = mongo_client.delete_object(object_id)
            print(f"  Deleted object {object_id} ({deleted_images} image record(s)).")
        print("Done. (Image FILES on disk are left as-is — same as any other delete_object() "
              "call; see its docstring. The catalog entry's linked_object_ids IS kept in sync "
              "by delete_object() now, so re-running this script won't leave a stale "
              "'red mug' Inventory entry pointing at deleted captures.)")


if __name__ == "__main__":
    main()
