"""
[WIRED] Encode/decode a capture's image set as one MQTT-portable bundle,
shared by both Middleman sides:
  - Physical Side (vision/services/middleman_physical_side.py) builds a
    bundle after a local capture and publishes it.
  - Other Side (vision/services/middleman_other_side.py) receives it and
    decodes it.

Two receive-side entry points, for two different situations:
  - save_photo_bundle_files(): decode + write to disk ONLY, no Mongo/
    CSV/Excel. Used mid-rotation-sequence (see
    vision/services/rotation_coordinator.py), where several bundles
    (one per view) need to accumulate into ONE object before anything
    gets logged.
  - save_photo_bundle(): the original all-in-one behavior — decode,
    write to disk, AND log as a brand-new one-object capture via
    vision.storage.capture_pipeline.record_capture(), so it gets the
    exact same session/catalog/CSV/Excel treatment as a locally-typed
    manual capture. Used for the ad hoc single "Capture Now" button,
    where each press is its own standalone object by design.

Images are downscaled/re-compressed specifically for the relay
(MIDDLEMAN_PHOTO_TRANSFER_MAX_DIMENSION / _JPEG_QUALITY in
vision/config.py), independent of whatever full-resolution copy (if
any) stays on the Physical Side's own disk — keeps MQTT payloads
reasonable regardless of camera resolution.
"""

from __future__ import annotations
import base64
import datetime
import io
import os

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

from vision.config import (
    MIDDLEMAN_PHOTO_TRANSFER_MAX_DIMENSION,
    MIDDLEMAN_PHOTO_TRANSFER_JPEG_QUALITY,
)
from vision.storage.capture_pipeline import record_capture


def _require_cv2():
    if not _CV2_AVAILABLE:
        raise ImportError("opencv-python is not installed. Run: pip install opencv-python")


def _downscale_and_encode_jpeg(frame) -> bytes:
    """Resize `frame` (BGR ndarray) so its longest side is at most
    MIDDLEMAN_PHOTO_TRANSFER_MAX_DIMENSION, JPEG-encode it, and return
    the raw bytes (not yet base64)."""
    _require_cv2()
    h, w = frame.shape[:2]
    longest = max(h, w)
    if longest > MIDDLEMAN_PHOTO_TRANSFER_MAX_DIMENSION:
        scale = MIDDLEMAN_PHOTO_TRANSFER_MAX_DIMENSION / longest
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", frame,
                            [int(cv2.IMWRITE_JPEG_QUALITY), MIDDLEMAN_PHOTO_TRANSFER_JPEG_QUALITY])
    if not ok:
        raise IOError("Failed to JPEG-encode frame for middleman photo transfer")
    return buf.tobytes()


def build_photo_bundle(sample_id: str, frames: list, physical_side_ip: str,
                        values: dict | None = None, view_index: int | None = None) -> dict:
    """
    `frames` is a list of (source_name, view_index, frame) tuples — raw
    BGR ndarrays straight from vision.camera.capture.capture_frame(),
    not file paths. `sample_id` doubles as the rotation-sequence's
    shared object_id when called from a rotation sequence (see
    rotation_coordinator.py) — the Physical Side never has to know
    which case it's in, it just echoes back whatever id it was given.
    `view_index`, if given, is bundle-level metadata (which step of a
    rotation sequence this is) separate from each individual image's
    own view_index (which camera/source it came from at that step).
    Returns a JSON-serializable dict ready to publish.
    """
    images = []
    for source_name, image_view_index, frame in frames:
        jpeg_bytes = _downscale_and_encode_jpeg(frame)
        images.append({
            "source": source_name,
            "view_index": image_view_index,
            "jpeg_base64": base64.b64encode(jpeg_bytes).decode("ascii"),
        })
    return {
        "sample_id": sample_id,
        "physical_side_ip": physical_side_ip,
        "values": values or {},
        "images": images,
        "sequence_view_index": view_index,
    }


def save_photo_bundle_files(bundle: dict, save_root: str = "images/middleman") -> dict:
    """
    Decode a received bundle and write each image to disk under
    `save_root`/<sample_id>/ — nothing else. No Mongo/CSV/Excel write.

    Returns {source_name: local_path}. If a bundle somehow has more
    than one image for the same source (shouldn't happen — one physical
    camera per source per capture), the last one wins; that mirrors
    record_capture()'s own image_paths_by_source contract, which this
    return value is meant to be passed straight into.

    Filenames incorporate bundle["sequence_view_index"] when present
    (i.e. this bundle is one step of a rotation sequence — see
    rotation_coordinator.py) rather than each image's own view_index
    (which only distinguishes cameras WITHIN one bundle, always 0 for a
    single-camera request). Without this, three rotation steps using
    the same camera, arriving within the same wall-clock second, would
    all resolve to the identical `<timestamp>_<source>_0.jpg` filename
    and silently overwrite each other.
    """
    _require_cv2()
    sample_id = bundle["sample_id"]
    sample_dir = os.path.join(save_root, sample_id)
    os.makedirs(sample_dir, exist_ok=True)
    sequence_view_index = bundle.get("sequence_view_index")

    paths_by_source = {}
    for image in bundle.get("images", []):
        jpeg_bytes = base64.b64decode(image["jpeg_base64"])
        source = image.get("source", "unknown")
        view_index = sequence_view_index if sequence_view_index is not None \
            else image.get("view_index", 0)
        # Same date+time+source+view_index naming as vision.camera.capture.
        # save_image, so filenames are sortable/searchable regardless of
        # which machine/path wrote them.
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(sample_dir, f"{timestamp}_{source}_{view_index}.jpg")
        with open(path, "wb") as f:
            f.write(jpeg_bytes)
        paths_by_source[source] = path

    return paths_by_source


def save_photo_bundle(bundle: dict, save_root: str = "images/middleman") -> tuple:
    """
    Full ad hoc single-shot handling: decode + write to disk (via
    save_photo_bundle_files) AND log it as a brand-new one-object
    capture through vision.storage.capture_pipeline.record_capture(),
    so it gets session/catalog/CSV/Excel treatment identical to a local
    manual capture — this machine's Mongo/CSV/Excel is the single
    source of truth regardless of which machine's camera the photo
    came from.

    Use this for a standalone "Capture Now" press. For a multi-view
    rotation sequence, use save_photo_bundle_files() per view instead
    and call record_capture() once yourself after all views arrive
    (see rotation_coordinator.py) — otherwise each view becomes its own
    separate object instead of one object with several images.

    `bundle["values"]` may contain any of record_capture()'s known
    fixed-column kwargs (name/category/color/size) — those are pulled
    out and passed through properly rather than being dumped into the
    freeform attributes dict, so e.g. a category set on the Physical
    Side actually lands in the `category` column, not buried in
    freeform JSON where filtering/Excel wouldn't see it.

    Returns (object_id, local_paths, warnings) — warnings is whatever
    record_capture() returned (e.g. a non-fatal Excel refresh failure).
    """
    paths_by_source = save_photo_bundle_files(bundle, save_root=save_root)
    values = dict(bundle.get("values", {}))
    values["physical_side_ip"] = bundle.get("physical_side_ip", "unknown")
    name = values.pop("name", None) or "middleman capture"
    category = values.pop("category", None)
    color = values.pop("color", None)
    size = values.pop("size", None)
    object_id, warnings = record_capture(
        name=name,
        image_paths_by_source=paths_by_source,
        category=category, color=color, size=size,
        attributes=values,  # whatever's left over (e.g. physical_side_ip)
    )
    return object_id, list(paths_by_source.values()), warnings
