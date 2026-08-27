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
from datetime import datetime

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
from vision.storage import storage_location

# ---------------------------------------------------------------------------
# RUNTIME CAMERA ASSIGNMENT
# ---------------------------------------------------------------------------
# vision/config.py's CAMERAS dict is the *default* name -> device-index
# mapping, but it's a plain file on disk — changing it means editing code
# and restarting the app. This lets a camera be (re)assigned from the GUI
# (main.py's Camera tab) while the app is running, and persists the choice
# to camera_assignments.json so it survives a restart without touching
# vision/config.py at all. CAMERAS itself is never mutated — the override
# dict below always takes priority over it in list_configured_cameras()/
# capture_frame(), so a runtime assignment always wins.
# ---------------------------------------------------------------------------
import json
import subprocess

_ASSIGNMENTS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "camera_assignments.json")

_camera_overrides: dict = {}


def _load_camera_overrides() -> None:
    global _camera_overrides
    try:
        if os.path.exists(_ASSIGNMENTS_FILE):
            with open(_ASSIGNMENTS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                _camera_overrides = {str(k): int(v) for k, v in data.items()}
    except Exception as e:
        print(f"[CAMERA CONFIG] Could not load {_ASSIGNMENTS_FILE}: {e}")
        _camera_overrides = {}


def _save_camera_overrides() -> None:
    try:
        with open(_ASSIGNMENTS_FILE, "w") as f:
            json.dump(_camera_overrides, f, indent=2)
    except Exception as e:
        print(f"[CAMERA CONFIG] Could not save {_ASSIGNMENTS_FILE}: {e}")


_load_camera_overrides()


def assign_camera(name: str, index: int) -> None:
    """Assign (or reassign) a camera name to a device index at runtime and
    persist it to camera_assignments.json. Releases any cached handle
    already open under that name's *previous* index so the next capture/
    live-feed read opens the newly assigned device instead of a stale one.
    """
    name = str(name).strip()
    if not name:
        raise ValueError("Camera name cannot be empty.")
    index = int(index)

    old_index = list_configured_cameras().get(name)
    _camera_overrides[name] = index
    _save_camera_overrides()

    if old_index is not None and old_index != index and old_index in _capture_handles:
        try:
            _capture_handles[old_index].release()
        except Exception:
            pass
        _capture_handles.pop(old_index, None)


def remove_camera_assignment(name: str) -> None:
    """Remove a runtime override, falling back to vision/config.py's CAMERAS
    (or dropping the camera entirely if it's not in CAMERAS either)."""
    _camera_overrides.pop(str(name).strip(), None)
    _save_camera_overrides()

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
            # Ask the backend to keep as small a frame queue as possible.
            # Most UVC backends (DSHOW, V4L2) honor this; some (MSMF)
            # silently ignore it - that's fine, it's just a hint, the
            # explicit flush in _capture_from_index() below is what
            # actually guarantees a fresh frame regardless of backend.
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
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


def list_camera_device_names() -> dict:
    """
    Best-effort index -> human-readable device name (e.g. "Logitech BRIO"),
    Windows only. OpenCV/cv2.VideoCapture only ever deals in bare numeric
    indices - it has no idea what Device Manager calls a camera - which
    makes it hard to tell cameras apart by index alone once more than one
    or two are plugged in. This shells out to PowerShell to pull the same
    "Cameras / Imaging devices" list Device Manager shows, so the Camera
    tab can display e.g. "index 2 - Logitech BRIO" instead of just "2".

    IMPORTANT CAVEAT: Windows' PnP device enumeration order and OpenCV's
    backend enumeration order are NOT contractually guaranteed to match -
    in practice they usually line up (both generally follow USB
    enumeration order), but this is a best-effort positional pairing for
    display purposes, not a verified index<->name mapping. Good enough to
    visually distinguish "is index 2 the BRIO or the cheap webcam" at a
    glance; don't treat it as authoritative for anything safety-critical.

    Returns {} (never raises) on non-Windows, if PowerShell isn't
    reachable, or on any enumeration failure - callers must handle a
    missing/empty mapping gracefully and just fall back to showing the
    bare index, same as before this existed.
    """
    if os.name != "nt":
        return {}
    try:
        ps_cmd = (
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
            "Select-Object -ExpandProperty Name"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=5,
        )
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return {i: name for i, name in enumerate(names)}
    except Exception as e:
        print(f"[CAMERA CONFIG] Could not enumerate device names: {e}")
        return {}


# ---------------------------------------------------------------------------
# STALE-FRAME FLUSH ("photos come out one behind what they should be")
# ---------------------------------------------------------------------------
# Discarding a couple of already-queued frames with cap.grab() right before
# the "real" read guarantees a fresh frame (see _capture_from_index below)
# but costs a little time on every single capture - noticeable when firing
# captures rapidly (e.g. jog auto-capture once a second, or a full rotation
# sequence). Since the underlying driver-level buffering this works around
# doesn't affect every camera/backend equally, this is OFF by default and
# toggled from a checkbox on the Camera tab ("Fix laggy/delayed photos") -
# turn it on only if photos are actually showing up a beat behind reality.
# ---------------------------------------------------------------------------
_flush_stale_frames_enabled = False


def set_flush_stale_frames(enabled: bool) -> None:
    """Camera tab checkbox calls this. See module note above."""
    global _flush_stale_frames_enabled
    _flush_stale_frames_enabled = bool(enabled)


# Number of buffered/stale frames to discard immediately before the
# "real" read in _capture_from_index() when the flush toggle is on - see
# the BUGFIX note there ("camera captures one photo behind"). 2 is enough
# headroom for every backend we've seen queue frames (DSHOW/V4L2
# typically hold 1-3), while staying fast (a few ms per grab() on a live
# camera).
_STALE_FRAME_FLUSH_COUNT = 2


def _capture_from_index(index: int, _retry: bool = True):
    """
    BUGFIX (stale/cached handle): previously, once a camera's handle was
    cached in _capture_handles, it was never re-validated - if the camera
    got unplugged/replugged, hit a brief USB hiccup, or was grabbed and
    released by another program mid-session, the stale handle would keep
    returning ok=False forever, and every future capture would fail with
    "did not return a frame" until the whole app was restarted. Now, a
    failed read releases the stale handle and retries once with a fresh
    _open_camera() call before actually giving up.

    BUGFIX (photo "one behind" what it should be): a cv2.VideoCapture
    handle that's left open between captures (as ours are - see the
    module docstring on caching handles) keeps an internal driver-level
    frame queue filling in the background even when nothing calls
    .read(). The very next .read() after any gap returns whatever frame
    was ALREADY sitting at the front of that queue - i.e. a frame grabbed
    before the thing you actually wanted to photograph happened (the
    object/arm as it looked a moment ago), not a fresh one grabbed right
    now. This is why a capture consistently shows the scene from just
    before the triggering event ("one photo behind"). Setting
    CAP_PROP_BUFFERSIZE=1 in _open_camera() reduces this on backends that
    honor it, but not all do - so on every real capture we now also
    explicitly discard _STALE_FRAME_FLUSH_COUNT already-queued frames
    with the cheap cap.grab() (decode-free) before the one .read() whose
    frame actually gets kept, guaranteeing what's returned was grabbed
    right now regardless of backend/buffering behavior. OFF by default
    (see _flush_stale_frames_enabled above) since it costs a little time
    on every capture - flip on the "Fix laggy/delayed photos" checkbox
    (Camera tab) if photos are actually coming out a beat behind reality.
    """
    lock = _get_lock(index)
    with lock:
        cap = _get_handle(index)
        if _flush_stale_frames_enabled:
            for _ in range(_STALE_FRAME_FLUSH_COUNT):
                cap.grab()
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
    vision.config.CAMERAS (or reassigned at runtime via assign_camera(),
    which takes priority) by name. This is the general entry point -
    works for any number of cameras, not just station/wrist.
    """
    configured = list_configured_cameras()
    if camera_name not in configured:
        raise ValueError(
            f"Unknown camera '{camera_name}'. Configured cameras: "
            f"{list(configured.keys())}. Add it to CAMERAS in "
            f"vision/config.py, or assign it from the Camera tab."
        )
    return _capture_from_index(configured[camera_name])


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
    """Returns the name -> index mapping for populating UI camera-selector
    dropdowns, capture_frame(), etc. Starts from vision.config.CAMERAS and
    layers any runtime assignments (assign_camera()) on top, so a camera
    reassigned from the GUI always overrides the on-disk default without
    editing vision/config.py."""
    merged = dict(CAMERAS)
    merged.update(_camera_overrides)
    return merged


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

    Filename is `YYYYMMDD_HHMMSS_<source>_<view_index>.jpg` (per the
    plan: date + time + the existing source/view_index naming), inside
    the existing images/<sample_id>/ per-object folder — so filenames
    stay sortable/searchable on their own, folder-per-object grouping
    is unchanged, and two captures of the same object/source/view_index
    can never collide/overwrite each other (each gets its own
    timestamp), which the old `{source}_{view_index}.jpg`-only naming
    did not guarantee.

    Returns an ABSOLUTE path. This matters: the path returned here gets
    stored verbatim in MongoDB (via capture_pipeline.record_capture) and
    is later opened by main.py's image viewer, possibly in a different
    process launched from a different working directory than the one
    that originally wrote the file. A relative path resolves against
    whatever the CURRENT process's cwd happens to be, which silently
    breaks ("No such file or directory") the moment the app is launched
    from anywhere other than the exact directory used at capture time —
    an absolute path always resolves to the same file regardless.
    """
    _require_cv2()
    sample_dir = ensure_sample_dir(sample_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_path = os.path.abspath(os.path.join(sample_dir, f"{timestamp}_{source}_{view_index}.jpg"))
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
    """[WIRED] Helper - no hardware dependency.
    Structured as sample_<YYYYMMDD>_<HHMMSS>_<4-char-suffix> (e.g.
    sample_20260826_143210_a91f) rather than a bare UUID, so folder
    names under images/ are sortable and readable at a glance in a file
    browser (you can tell WHEN a sample was captured just by its
    folder name) instead of opaque random hex. The short suffix (still
    drawn from uuid4, just truncated) exists only to keep two samples
    created within the same second from colliding - it isn't meant to
    be meaningful on its own.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:4]
    return f"sample_{timestamp}_{suffix}"


def ensure_sample_dir(sample_id: str) -> str:
    """[WIRED] Helper - creates and returns the folder for a sample's
    images, under the configured permanent storage location (see
    vision.storage.storage_location) rather than a path relative to
    wherever the process happened to be launched from."""
    path = os.path.join(storage_location.images_root(), sample_id)
    os.makedirs(path, exist_ok=True)
    return path


def is_camera_available(camera_name: str) -> bool:
    """
    Best-effort probe: True if `camera_name` (one of
    list_configured_cameras()'s keys) actually opens and returns a real
    frame right now, False otherwise (unplugged, wrong index, already
    held open by another program, etc.) — same underlying check
    _open_camera() uses for a real capture, just released immediately
    afterward rather than cached into _capture_handles, so probing
    doesn't hold a handle open that would then need releasing before
    the live feed or a real capture could use it.

    Used by main.py's Camera tab to only build/show a live-feed panel
    for cameras ACTUALLY connected right now, instead of one panel per
    entry in vision.config.CAMERAS regardless of whether it's
    physically plugged in — "if one camera's connected, show one
    panel; if three are connected, show three."
    """
    _require_cv2()
    configured = list_configured_cameras()
    if camera_name not in configured:
        return False
    cap = _open_camera(configured[camera_name])
    if cap is None:
        return False
    cap.release()
    return True


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
