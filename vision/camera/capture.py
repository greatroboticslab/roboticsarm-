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
    import numpy as np
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


# ---------------------------------------------------------------------------
# PER-CAMERA SETTINGS — resolution + color/mono output mode
# ---------------------------------------------------------------------------
# Some cameras (depth/industrial UVC sensors in particular — the ones
# that sometimes trigger the harmless "obsensor" probing noise mentioned
# in _candidate_backends() above) can output either a converted color/
# "overlay" frame or a raw grayscale/mono one, switched via OpenCV's
# standard CAP_PROP_CONVERT_RGB (nonzero = color/overlay, 0 = raw/mono).
# Persisted the same way camera index assignments are (camera_settings.
# json next to camera_assignments.json), so a chosen resolution/mode
# survives a restart without editing vision/config.py.
# ---------------------------------------------------------------------------
_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "camera_settings.json")

_camera_settings: dict = {}  # name -> {"width": int, "height": int, "mono": bool}


def _load_camera_settings() -> None:
    global _camera_settings
    try:
        if os.path.exists(_SETTINGS_FILE):
            with open(_SETTINGS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                _camera_settings = data
    except Exception as e:
        print(f"[CAMERA CONFIG] Could not load {_SETTINGS_FILE}: {e}")
        _camera_settings = {}


def _save_camera_settings() -> None:
    try:
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(_camera_settings, f, indent=2)
    except Exception as e:
        print(f"[CAMERA CONFIG] Could not save {_SETTINGS_FILE}: {e}")


_load_camera_settings()


def get_camera_settings(name: str) -> dict:
    """Returns the resolved settings dict for `name` — defaults
    (vision.config's CAMERA_FRAME_WIDTH/HEIGHT, color mode, dual-capture
    off, "combined" view) filled in for anything not explicitly set yet:
      width/height/mono   - the camera's normal/primary output.
      view                 - for a stereo/multi-eye camera whose raw
                              frame is two (or more) views packed side
                              by side (e.g. the e-con See3CAM_Stereo's
                              native "<left><right>" combined output):
                              "combined" (the raw frame, unmodified),
                              "left"/"right" (crop to just that half),
                              or "blend" (left | right | combined,
                              stacked into one wide image — all
                              perspectives in one photo at a glance).
                              No-op for an ordinary single-eye camera.
      dual_capture         - if True, every capture_frames_multi() call
                              for this camera rapid-fires a SECOND shot
                              at width_b/height_b/mono_b/view_b right
                              after the primary one (see
                              capture_frames_multi()).
      width_b/height_b/mono_b/view_b - the second profile. mono_b
                              defaults to the OPPOSITE of mono (e.g.
                              primary=color, secondary=mono) since "the
                              other output the camera offers" is the
                              common case; width_b/height_b/view_b
                              default to the same as primary if not set
                              separately.
    """
    saved = _camera_settings.get(str(name).strip(), {})
    width = saved.get("width") or CAMERA_FRAME_WIDTH
    height = saved.get("height") or CAMERA_FRAME_HEIGHT
    mono = bool(saved.get("mono", False))
    view = saved.get("view") or "combined"
    return {
        "width": width,
        "height": height,
        "mono": mono,
        "view": view,
        "dual_capture": bool(saved.get("dual_capture", False)),
        "width_b": saved.get("width_b") or width,
        "height_b": saved.get("height_b") or height,
        "mono_b": bool(saved.get("mono_b", not mono)),
        "view_b": saved.get("view_b") or view,
    }


def set_camera_settings(name: str, width: int = None, height: int = None,
                         mono: bool = None, view: str = None,
                         dual_capture: bool = None,
                         width_b: int = None, height_b: int = None,
                         mono_b: bool = None, view_b: str = None) -> None:
    """Persists resolution/mono-vs-color/stereo-view (and optional second
    dual-capture profile) settings for camera `name` and applies the
    PRIMARY profile's resolution/mono immediately to its handle if one's
    already open/cached (cap.set() on a live handle - no reopen needed),
    so a change takes effect on the very next frame rather than needing
    a reconnect. `view`/`view_b` are purely a post-processing crop
    applied after the frame is read (see _apply_stereo_view) - nothing
    to push to the camera hardware for those."""
    name = str(name).strip()
    if not name:
        raise ValueError("Camera name cannot be empty.")
    entry = _camera_settings.setdefault(name, {})
    if width is not None:
        entry["width"] = int(width)
    if height is not None:
        entry["height"] = int(height)
    if mono is not None:
        entry["mono"] = bool(mono)
    if view is not None:
        entry["view"] = str(view)
    if dual_capture is not None:
        entry["dual_capture"] = bool(dual_capture)
    if width_b is not None:
        entry["width_b"] = int(width_b)
    if height_b is not None:
        entry["height_b"] = int(height_b)
    if mono_b is not None:
        entry["mono_b"] = bool(mono_b)
    if view_b is not None:
        entry["view_b"] = str(view_b)
    _save_camera_settings()

    index = list_configured_cameras().get(name)
    if index is not None and index in _capture_handles:
        _apply_camera_settings(_capture_handles[index], get_camera_settings(name))


def _apply_camera_settings(cap, settings: dict) -> None:
    """Applies a resolved settings dict (see get_camera_settings) to an
    already-open cv2.VideoCapture handle. Best-effort: not every backend
    honors CAP_PROP_CONVERT_RGB or an arbitrary resolution - cv2 silently
    ignores what it can't do, same as it already does elsewhere in this
    file (see e.g. CAP_PROP_BUFFERSIZE above)."""
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, settings["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, settings["height"])
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0 if settings["mono"] else 1)
    except Exception:
        pass


def _normalize_frame_for_save(frame):
    """
    Converts a raw frame from cap.read() into something safe to save/
    display as a normal 8-bit image, whatever raw pixel format the
    camera/backend handed back.

    BUGFIX (dual-capture returning solid-white photos): some cameras —
    notably a mono/stereo Y16 sensor like the e-con See3CAM_Stereo, when
    CAP_PROP_CONVERT_RGB is off — hand back 16-bit-per-pixel raw data.
    Treating that as if it were an ordinary 8-bit BGR frame (which is
    what happens if it's saved/displayed with no conversion) blows the
    high byte of every 16-bit value out toward white, or otherwise
    scrambles the image — exactly the "white photos" symptom reported.
    This min/max-normalizes any non-8-bit frame down to the full 0-255
    uint8 range before returning it, regardless of the sensor's actual
    bit depth, so it always saves as a real, viewable image.
    """
    if frame is None or frame.dtype == np.uint8:
        return frame
    return cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def _apply_stereo_view(frame, view: str):
    """
    For a stereo/multi-eye camera whose raw frame is two views packed
    side by side (left eye | right eye, in one wide frame — the
    e-con See3CAM_Stereo's native output, and common to UVC stereo
    cameras generally): crops to just the left or right half, or builds
    a "blend" composite of left | right | combined stacked into one wide
    image so all three perspectives are visible in a single photo.
    No-op (returns `frame` unchanged) for view == "combined"/None, or
    for a frame that isn't at least twice as wide as it is tall (not
    plausibly a side-by-side pair — an ordinary single-eye camera should
    just leave `view` on "combined" and never hit the crop logic at
    all).
    """
    if not view or view == "combined" or frame is None:
        return frame
    h, w = frame.shape[:2]
    if w < h * 2:
        return frame  # doesn't look like a side-by-side stereo pair — leave it alone
    half_w = w // 2
    left = frame[:, :half_w]
    right = frame[:, half_w:half_w * 2]
    if view == "left":
        return left
    if view == "right":
        return right
    if view == "blend":
        return np.hstack([left, right, frame[:, :half_w * 2]])
    return frame


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


def _get_handle(index: int, camera_name: str = None):
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
        # Resolution + color/mono mode: per-camera-name settings if this
        # index has a configured name (see get_camera_settings/
        # set_camera_settings above), else the vision.config global
        # defaults (index-only callers - is_camera_available(),
        # list_camera_indices() - have no name to look a setting up by).
        settings = (get_camera_settings(camera_name) if camera_name
                    else {"width": CAMERA_FRAME_WIDTH, "height": CAMERA_FRAME_HEIGHT, "mono": False})
        _apply_camera_settings(cap, settings)
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


def _capture_from_index(index: int, _retry: bool = True, camera_name: str = None):
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
        cap = _get_handle(index, camera_name=camera_name)
        if _flush_stale_frames_enabled:
            for _ in range(_STALE_FRAME_FLUSH_COUNT):
                cap.grab()
        ok, frame = cap.read()

        if not (ok and frame is not None) and _retry:
            cap.release()
            _capture_handles.pop(index, None)
            fresh_cap = _open_camera(index)
            if fresh_cap is not None:
                settings = (get_camera_settings(camera_name) if camera_name
                            else {"width": CAMERA_FRAME_WIDTH, "height": CAMERA_FRAME_HEIGHT, "mono": False})
                _apply_camera_settings(fresh_cap, settings)
                _capture_handles[index] = fresh_cap
                ok, frame = fresh_cap.read()

    if not ok or frame is None:
        raise RuntimeError(
            f"Camera at index {index} did not return a frame, even after "
            f"reconnecting. Check the USB connection and that no other "
            f"program has it open."
        )
    frame = _normalize_frame_for_save(frame)
    if camera_name:
        frame = _apply_stereo_view(frame, get_camera_settings(camera_name)["view"])
    return frame


def probe_camera_modes(camera_name: str) -> list:
    """
    Actually tests each candidate (width, height) against the live
    camera hardware — closes whatever handle is cached first (many
    industrial/stereo UVC cameras, the e-con See3CAM_Stereo included,
    need a full stream restart to change resolution/format; a property
    set on an already-streaming handle can silently no-op or, per the
    "white photos" report, hand back garbage — see capture_frames_multi()
    for the same fix applied to actual captures), opens a fresh handle
    per candidate, sets it, discards a few warm-up frames to let the
    sensor settle, reads one frame, and reports whether that frame looks
    like real image data (not blank/constant) plus its actual returned
    shape/dtype. Restores a normal handle at this camera's configured
    settings once done.

    Candidates include the See3CAM_Stereo's documented native combined
    (left+right side by side) resolutions — WVGA/VGA/QVGA, per e-con's
    own datasheet — alongside generic common ones, each tried in both
    color and mono, so this can tell you exactly which combinations your
    specific unit actually accepts instead of guessing.

    Returns a list of dicts, one per WORKING combo — only combos that
    produced a plausible non-blank frame are included, so this is the
    definitive "what does this camera actually support" answer:
      {"width", "height", "mono", "label", "actual_shape", "actual_dtype"}
    """
    configured = list_configured_cameras()
    if camera_name not in configured:
        return []
    index = configured[camera_name]

    candidates = [
        (1504, 480, "1504x480 (WVGA combined L+R — See3CAM_Stereo native)"),
        (1280, 480, "1280x480 (VGA combined L+R — See3CAM_Stereo native)"),
        (640, 240, "640x240 (QVGA combined L+R — See3CAM_Stereo native)"),
        (1920, 1080, "1920x1080"),
        (1280, 720, "1280x720"),
        (640, 480, "640x480"),
    ]

    lock = _get_lock(index)
    working = []
    with lock:
        cached = _capture_handles.pop(index, None)
        if cached is not None:
            cached.release()

        for width, height, label in candidates:
            for mono in (False, True):
                cap = _open_camera(index)
                if cap is None:
                    continue
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                cap.set(cv2.CAP_PROP_CONVERT_RGB, 0 if mono else 1)
                # Let the sensor settle after a resolution/format change
                # before trusting what comes back - the first frame or
                # two after a UVC stream (re)negotiation is commonly
                # garbage/blank while auto-exposure/the sensor catches up.
                for _ in range(5):
                    cap.grab()
                ok, frame = cap.read()
                cap.release()
                if not ok or frame is None:
                    continue
                # A "blank" frame (solid white/black/constant color) is
                # exactly the reported symptom — this combo opened but
                # isn't actually streaming real data.
                if float(np.std(frame)) < 1.0:
                    continue
                working.append({
                    "width": width, "height": height, "mono": mono,
                    "label": f"{label} — {'mono' if mono else 'color/overlay'} "
                             f"(actual {frame.shape}, {frame.dtype})",
                    "actual_shape": tuple(frame.shape), "actual_dtype": str(frame.dtype),
                })

        # Restore a live handle at this camera's configured settings so
        # nothing else (live feed, the next ordinary capture) is left
        # without a working handle after probing.
        try:
            _get_handle(index, camera_name=camera_name)
        except Exception:
            pass

    return working


def capture_frames_multi(camera_name: str) -> list:
    """
    Grabs one frame from `camera_name`, or TWO in rapid succession (one
    per configured resolution/output-mode profile) if that camera's
    "dual capture" toggle is on (see set_camera_settings' dual_capture/
    width_b/height_b/mono_b/view_b args, exposed on the Camera tab as a
    second "Capture B" resolution/mode row) — e.g. one full-resolution
    shot and one lower-res/mono shot from the SAME physical camera,
    every time a photo is requested from it.

    BUGFIX (dual capture returning solid-white photos and crashing): the
    first version of this switched profiles with a bare cap.set() on the
    SAME still-open handle, then read immediately. Several UVC cameras —
    stereo/industrial ones especially, the e-con See3CAM_Stereo
    included — don't actually renegotiate their stream that fast (or at
    all without a full restart); reading right after a live property
    change caught the sensor mid-reconfiguration, producing blank/white
    frames and in some cases wedging the driver badly enough to crash.
    Fixed by fully CLOSING the handle, reopening fresh at the B profile,
    discarding a few warm-up frames to let the new stream settle, THEN
    reading — the same "give the hardware a moment" approach
    probe_camera_modes() uses — before closing that and reopening once
    more back at the A/primary profile for whatever uses this camera
    next (live feed, the next capture).

    Returns a list of (suffix, frame) pairs: one pair (suffix "a") for a
    normal single capture, two pairs ("a", "b") for dual capture —
    callers save each under a different view_index so they never
    collide on disk (see main.py's capture_movement_snapshot()/
    run_manual_snapshot()).
    """
    configured = list_configured_cameras()
    if camera_name not in configured:
        raise ValueError(
            f"Unknown camera '{camera_name}'. Configured cameras: "
            f"{list(configured.keys())}. Add it to CAMERAS in "
            f"vision/config.py, or assign it from the Camera tab."
        )
    index = configured[camera_name]
    settings = get_camera_settings(camera_name)

    frame_a = _capture_from_index(index, camera_name=camera_name)
    if not settings["dual_capture"]:
        return [("a", frame_a)]

    settings_b = {"width": settings["width_b"], "height": settings["height_b"],
                  "mono": settings["mono_b"]}
    lock = _get_lock(index)
    frame_b = None
    with lock:
        cached = _capture_handles.pop(index, None)
        if cached is not None:
            cached.release()
        cap_b = _open_camera(index)
        if cap_b is not None:
            _apply_camera_settings(cap_b, settings_b)
            for _ in range(5):  # let the reconfigured stream settle — see BUGFIX above
                cap_b.grab()
            ok_b, raw_b = cap_b.read()
            cap_b.release()
            if ok_b and raw_b is not None:
                frame_b = _apply_stereo_view(_normalize_frame_for_save(raw_b), settings["view_b"])

        # Reopen fresh at the primary/A profile, same "settle first"
        # treatment, for whatever uses this camera next.
        cap_a = _open_camera(index)
        if cap_a is not None:
            _apply_camera_settings(cap_a, settings)
            for _ in range(5):
                cap_a.grab()
            _capture_handles[index] = cap_a

    if frame_b is None:
        print(f"[CAMERA] '{camera_name}' dual-capture secondary shot failed — "
              f"only the primary was saved.")
        return [("a", frame_a)]
    return [("a", frame_a), ("b", frame_b)]


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
    return _capture_from_index(configured[camera_name], camera_name=camera_name)


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
