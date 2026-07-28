"""
[WIRED] USB camera capture via OpenCV.

Works for any standard UVC-class USB webcam (the vast majority of USB
cameras) using nothing but a device index - no vendor SDK needed. If your
camera turns out to need a specialized SDK (e.g. an industrial/machine-
vision camera rather than a plain webcam), swap the cv2.VideoCapture
calls below for that SDK's frame-grab call; everything calling
capture_station_frame()/capture_wrist_frame() elsewhere stays the same.

SETUP
-----
    pip install opencv-python

Then find your camera indices - plug in one camera at a time and run:

    python -m vision.camera.capture

This opens each index 0-4 briefly and reports which ones produce a frame,
so you can confirm STATION_CAMERA_INDEX / WRIST_CAMERA_INDEX in
vision/config.py before relying on them in the real pipeline.
"""

from __future__ import annotations
import os
import uuid
import threading

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

from vision.config import (
    CAMERAS,
    CAMERA_FRAME_WIDTH,
    CAMERA_FRAME_HEIGHT,
)

IMAGES_ROOT = "images/objects"

# Lazily-opened, cached VideoCapture handles - opened once, reused across
# calls rather than reopening the device every capture (slow + some UVC
# cameras don't like being reopened rapidly).
_capture_handles = {}

# CONCURRENCY NOTE: main.py has two independent triggers that both call
# into this module from their own background thread - the full
# pickup-and-photograph pipeline, and the standalone "Capture Photo"
# button. cv2.VideoCapture is not safe to .read() from two threads at
# once on the same handle (can corrupt frames or crash the backend). A
# per-camera-index lock below serializes access to a given camera while
# still letting station and wrist cameras (different indices) be used
# independently.
_capture_locks = {}
_locks_guard = threading.Lock()


def _get_lock(index: int) -> threading.Lock:
    with _locks_guard:
        if index not in _capture_locks:
            _capture_locks[index] = threading.Lock()
        return _capture_locks[index]


def _require_cv2():
    if not _CV2_AVAILABLE:
        raise ImportError(
            "opencv-python is not installed. Run: pip install opencv-python"
        )


def _candidate_backends():
    """
    Backends to try, in order, when opening a camera. Windows' default
    backend (MSMF) frequently fails to open a webcam that works fine
    under DSHOW on the exact same hardware - this isn't a code bug, it's
    a long-standing OpenCV/Windows quirk. cv2.CAP_ANY (0) lets OpenCV
    pick automatically as a last resort. Harmless "obsensor" warnings
    that sometimes print during this process (from OpenCV probing for
    an unrelated Orbbec/RealSense-style backend) are cosmetic noise, not
    the actual failure - ignore them.
    """
    _require_cv2()
    if os.name == "nt":
        return [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
    return [cv2.CAP_ANY]


def _open_camera(index: int):
    """Try each candidate backend in turn; return the first one that
    actually opens AND returns a real frame (isOpened() alone can lie)."""
    for backend in _candidate_backends():
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened():
            ok, _ = cap.read()
            if ok:
                return cap
        cap.release()
    return None


def _get_handle(index: int):
    _require_cv2()
    if index not in _capture_handles:
        cap = _open_camera(index)
        if cap is None:
            raise RuntimeError(
                f"Could not open camera at index {index} on any backend "
                f"(tried DSHOW/MSMF on Windows, or the default backend "
                f"elsewhere). Run `python -m vision.camera.capture` "
                f"(no .py) to list working indices. If nothing is found, "
                f"confirm the camera is actually plugged in and shows up "
                f"under Device Manager -> Cameras/Imaging devices, and "
                f"that no other program (Zoom, Teams, another Python "
                f"process, etc.) already has it open."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_FRAME_HEIGHT)
        _capture_handles[index] = cap
    return _capture_handles[index]


def _capture_from_index(index: int, _retry: bool = True):
    """
    BUGFIX: previously, once a camera's handle was cached in
    _capture_handles, it was never re-validated - if the camera got
    unplugged/replugged, hit a brief USB hiccup, or was grabbed and
    released by another program mid-session, the stale handle would keep
    returning ok=False forever, and every future capture would fail with
    "did not return a frame" until the whole app was restarted. Now, a
    failed read releases the stale handle and retries once with a fresh
    _open_camera() call before actually giving up.
    """
    lock = _get_lock(index)
    with lock:
        cap = _get_handle(index)
        ok, frame = cap.read()

        if not (ok and frame is not None) and _retry:
            cap.release()
            _capture_handles.pop(index, None)
            fresh_cap = _open_camera(index)
            if fresh_cap is not None:
                fresh_cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_FRAME_WIDTH)
                fresh_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_FRAME_HEIGHT)
                _capture_handles[index] = fresh_cap
                ok, frame = fresh_cap.read()

    if not ok or frame is None:
        raise RuntimeError(
            f"Camera at index {index} did not return a frame, even after "
            f"reconnecting. Check the USB connection and that no other "
            f"program has it open."
        )
    return frame


def capture_frame(camera_name: str):
    """
    [WIRED] Grab a single frame from any camera configured in
    vision.config.CAMERAS by name. This is the general entry point -
    works for any number of cameras, not just station/wrist.
    """
    if camera_name not in CAMERAS:
        raise ValueError(
            f"Unknown camera '{camera_name}'. Configured cameras: "
            f"{list(CAMERAS.keys())}. Add it to CAMERAS in vision/config.py."
        )
    return _capture_from_index(CAMERAS[camera_name])


def capture_station_frame():
    """[WIRED] Grab a single frame from the fixed station camera.
    Thin wrapper over capture_frame('station') for backward compatibility
    with existing pipeline code."""
    return capture_frame("station")


def capture_wrist_frame():
    """[WIRED] Grab a single frame from the wrist-mounted camera.
    Thin wrapper over capture_frame('wrist') for backward compatibility
    with existing pipeline code."""
    return capture_frame("wrist")


def list_configured_cameras() -> dict:
    """Returns the name -> index mapping from vision.config.CAMERAS, for
    populating UI camera-selector dropdowns etc."""
    return dict(CAMERAS)


def frame_to_rgb(frame):
    """
    Convert an OpenCV BGR frame to RGB, the format PIL/Tkinter expect for
    display. Kept as a pure-OpenCV helper (no PIL/Tkinter import here) so
    this module has no GUI dependency - main.py's live feed panel does
    the actual PIL.Image.fromarray()/ImageTk.PhotoImage() conversion.
    """
    _require_cv2()
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


def save_image(frame, sample_id: str, source: str, view_index: int = 0) -> str:
    """
    [WIRED] Persist a captured frame to disk and return its path.
    Mirrors 4DAI's Server/main.py image storage layout
    (images/<category>/<sample_id>/<id>.jpg) so the two systems'
    on-disk data stays consistent.
    """
    _require_cv2()
    sample_dir = ensure_sample_dir(sample_id)
    image_path = os.path.join(sample_dir, f"{source}_{view_index}.jpg")
    ok = cv2.imwrite(image_path, frame)
    if not ok:
        raise IOError(f"Failed to write image to {image_path}")
    return image_path


def release_all():
    """Release all opened camera handles. Call on app shutdown."""
    for cap in _capture_handles.values():
        cap.release()
    _capture_handles.clear()


def new_sample_id() -> str:
    """[WIRED] Helper - no hardware dependency."""
    return str(uuid.uuid4())


def ensure_sample_dir(sample_id: str) -> str:
    """[WIRED] Helper - creates and returns the folder for a sample's images."""
    path = os.path.join(IMAGES_ROOT, sample_id)
    os.makedirs(path, exist_ok=True)
    return path


def list_camera_indices(max_index: int = 5):
    """
    Utility: probe indices 0..max_index-1 and report which ones produce a
    frame, trying the same backend fallback _get_handle() uses. Run
    directly with:  python -m vision.camera.capture   (no .py — "-m"
    takes a module path, not a filename; including .py causes
    "Error while finding module specification").
    """
    _require_cv2()
    working = []
    for i in range(max_index):
        cap = _open_camera(i)
        if cap is not None:
            working.append(i)
            cap.release()
    return working


if __name__ == "__main__":
    print("Probing camera indices 0-4 ...")
    found = list_camera_indices()
    if found:
        print(f"Working camera indices: {found}")
        print(f"Currently configured cameras (vision/config.py CAMERAS): {CAMERAS}")
        for name, idx in CAMERAS.items():
            status = "OK" if idx in found else "NOT FOUND at that index"
            print(f"  '{name}' -> index {idx}: {status}")
        print("Update CAMERAS in vision/config.py if any indices don't match, "
              "or add more named entries for additional cameras.")
    else:
        print("No working cameras found on any backend. Checklist:")
        print("  1. Is the camera actually plugged in?")
        print("  2. Does it show up in Device Manager -> Cameras/Imaging devices (Windows)?")
        print("  3. Is it already open in another program (Zoom, Teams, OBS, etc.)?")
        print("  4. Try unplugging/replugging it, then re-run this command.")
