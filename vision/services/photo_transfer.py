"""
[WIRED] Encode/decode a capture's image set as one MQTT-portable bundle,
shared by both Middleman sides:
  - Physical Side (vision/services/middleman_physical_side.py) builds a
    bundle after a local capture and publishes it.
  - Other Side (vision/services/middleman_other_side.py) receives it,
    decodes it, and hands the saved local paths to vision.storage.
    mongo_client so it shows up in that machine's own local MongoDB
    (source="middleman", per the agreed design — 4DAI upload is future
    work, not part of this path).

Images are downscaled/re-compressed specifically for the relay
(MIDDLEMAN_PHOTO_TRANSFER_MAX_DIMENSION / _JPEG_QUALITY in
vision/config.py), independent of whatever full-resolution copy (if
any) stays on the Physical Side's own disk — keeps MQTT payloads
reasonable regardless of camera resolution.
"""

from __future__ import annotations
import base64
import io
import os
import uuid

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

from vision.config import (
    MIDDLEMAN_PHOTO_TRANSFER_MAX_DIMENSION,
    MIDDLEMAN_PHOTO_TRANSFER_JPEG_QUALITY,
)
from vision.storage import mongo_client


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
                        values: dict | None = None) -> dict:
    """
    `frames` is a list of (source_name, view_index, frame) tuples — raw
    BGR ndarrays straight from vision.camera.capture.capture_frame(),
    not file paths. Returns a JSON-serializable dict ready to publish.
    """
    images = []
    for source_name, view_index, frame in frames:
        jpeg_bytes = _downscale_and_encode_jpeg(frame)
        images.append({
            "source": source_name,
            "view_index": view_index,
            "jpeg_base64": base64.b64encode(jpeg_bytes).decode("ascii"),
        })
    return {
        "sample_id": sample_id,
        "physical_side_ip": physical_side_ip,
        "values": values or {},
        "images": images,
    }


def save_photo_bundle(bundle: dict, save_root: str = "images/middleman") -> list:
    """
    Decodes a received bundle, writes each image to disk under
    `save_root`/<sample_id>/, and records it in this machine's own
    local MongoDB via vision.storage.mongo_client (tagged
    source="middleman" so it's distinguishable from locally-captured
    samples once 4DAI upload is added later).

    Returns the list of local file paths written.
    """
    _require_cv2()
    sample_id = bundle["sample_id"]
    physical_side_ip = bundle.get("physical_side_ip", "unknown")
    sample_dir = os.path.join(save_root, sample_id)
    os.makedirs(sample_dir, exist_ok=True)

    saved_paths = []
    for image in bundle.get("images", []):
        jpeg_bytes = base64.b64decode(image["jpeg_base64"])
        source = image.get("source", "unknown")
        view_index = image.get("view_index", 0)
        path = os.path.join(sample_dir, f"{source}_{view_index}.jpg")
        with open(path, "wb") as f:
            f.write(jpeg_bytes)
        saved_paths.append(path)
        mongo_client.save_image_record(
            image_id=str(uuid.uuid4()),
            sample_id=sample_id,
            image_path=path,
            source=source,
            view_index=view_index,
        )

    values = dict(bundle.get("values", {}))
    values["source"] = "middleman"
    values["physical_side_ip"] = physical_side_ip
    import datetime
    mongo_client.save_sample(sample_id, str(datetime.date.today()), values)

    return saved_paths
