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

from vision.camera import stereo_depth

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

_camera_settings: dict = {}  # name -> {"width": int, "height": int, "fps": int|None, "extract_lenses": bool}


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
    (vision.config's CAMERA_FRAME_WIDTH/HEIGHT, no FPS override, lenses
    not extracted, dual-capture off) filled in for anything not
    explicitly set yet:
      width/height/fps/format_request - the camera's primary output
                              format. These are meant to be picked as a
                              set from a real detected format (see
                              probe_camera_modes()) — the exact
                              width/height/fps/pixel-format combos the
                              camera itself reports supporting — not
                              typed in freely; picking one detected entry
                              sets all four together (see main.py's
                              _do_pick_detected_mode()).

                              format_request is what actually
                              distinguishes two formats that share the
                              same resolution/fps (this camera's Y16 vs
                              RGB24 both exist at e.g. 752x480@60).

                              BUGFIX (Y16 was coming out identical to
                              RGB24 — "B just overridden by A"): the
                              previous version only ever toggled
                              CAP_PROP_CONVERT_RGB (raw=True/False) to
                              try to select between formats. That
                              property controls whether a backend does
                              color conversion AFTER capturing — it does
                              NOT select between genuinely different USB
                              Video Class format descriptors the way
                              this camera's Y16-vs-RGB24 actually work,
                              which are negotiated at the USB/driver
                              level. So toggling it did nothing on this
                              camera: A and B ended up requesting the
                              exact same real format regardless of the
                              setting, producing pixel-identical frames
                              under different filenames — which looked
                              like "B is just A again", because it
                              genuinely was. format_request now instead
                              stores either None (driver default), the
                              string "mono" (still tries
                              CAP_PROP_CONVERT_RGB=0 — this DOES work on
                              some ordinary webcams), or an explicit
                              4-character FOURCC string like "Y16 " —
                              set via CAP_PROP_FOURCC, which is the
                              property that actually selects between
                              distinct UVC format descriptors. fps=None
                              means "don't request a specific framerate,
                              take whatever's default".
      extract_lenses        - if True, every capture_frames_multi() call
                              for this camera splits the frame into ONE
                              PHOTO PER COLOR CHANNEL instead of saving
                              it as one image — see _extract_lenses().
                              For a camera whose "RGB24" output isn't
                              real color but three different views (e.g.
                              left/right/computed) packed one per
                              channel, this is how you get each of those
                              views out as its own separate, correctly-
                              viewable grayscale photo instead of one
                              frame that looks like RGB noise. No-op
                              (nothing to extract) for an already single-
                              channel format like Y16/GREY.
      keep_original          - only meaningful when extract_lenses is
                              on. If True, ALSO saves the original,
                              un-split combined frame as an extra photo
                              alongside the extracted per-channel ones —
                              off by default, since the whole point of
                              extraction is usually to get past a
                              combined frame that just looks like noise,
                              but it's there as an option if you want
                              both.
      alternate_lenses        - only meaningful when extract_lenses is
                              on. If True, each successive photo request
                              saves only ONE extracted lens instead of
                              all of them — cycling to the NEXT one each
                              time (view 0 this capture, view 1 the
                              next, back to view 0 after that, and so
                              on) rather than saving every view on every
                              single photo. Useful when you want each
                              capture event to be one lightweight image
                              instead of N, while still eventually
                              covering every view across a sequence of
                              captures. See _next_lens_index below for
                              the per-camera cycling state this uses.
      dual_capture          - if True, every capture_frames_multi() call
                              for this camera rapid-fires a SECOND shot
                              at width_b/height_b/fps_b/format_request_b/
                              extract_lenses_b right after the primary
                              one.
      width_b/height_b/fps_b/format_request_b/extract_lenses_b - the
                              second profile, same meaning as above.
                              Default to the same as primary if not set
                              separately.
    """
    saved = _camera_settings.get(str(name).strip(), {})
    width = saved.get("width") or CAMERA_FRAME_WIDTH
    height = saved.get("height") or CAMERA_FRAME_HEIGHT
    fps = saved.get("fps")
    format_request = saved.get("format_request")
    extract_lenses = bool(saved.get("extract_lenses", False))
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "format_request": format_request,
        "extract_lenses": extract_lenses,
        "keep_original": bool(saved.get("keep_original", False)),
        "alternate_lenses": bool(saved.get("alternate_lenses", False)),
        "depth_map": bool(saved.get("depth_map", False)),
        "dual_capture": bool(saved.get("dual_capture", False)),
        "width_b": saved.get("width_b") or width,
        "height_b": saved.get("height_b") or height,
        "fps_b": saved.get("fps_b", fps),
        "format_request_b": saved.get("format_request_b", format_request),
        "extract_lenses_b": bool(saved.get("extract_lenses_b", extract_lenses)),
    }


def set_camera_settings(name: str, width: int = None, height: int = None,
                         fps: int = None, format_request: str = None, extract_lenses: bool = None,
                         keep_original: bool = None, alternate_lenses: bool = None,
                         depth_map: bool = None,
                         dual_capture: bool = None,
                         width_b: int = None, height_b: int = None,
                         fps_b: int = None, format_request_b: str = None,
                         extract_lenses_b: bool = None) -> None:
    """Persists resolution/fps/pixel-format/lens-extraction (and
    optional second dual-capture profile) settings for camera `name` and
    applies the PRIMARY profile immediately to its handle if one's
    already open/cached (cap.set() on a live handle - no reopen needed),
    so a change takes effect on the very next frame rather than needing
    a reconnect. Pass format_request="" (empty string, not None — None
    means "leave whatever's already saved alone") to explicitly clear
    back to driver-default."""
    name = str(name).strip()
    if not name:
        raise ValueError("Camera name cannot be empty.")
    entry = _camera_settings.setdefault(name, {})
    if width is not None:
        entry["width"] = int(width)
    if height is not None:
        entry["height"] = int(height)
    if fps is not None:
        entry["fps"] = int(fps)
    if format_request is not None:
        entry["format_request"] = format_request or None
    if extract_lenses is not None:
        entry["extract_lenses"] = bool(extract_lenses)
    if keep_original is not None:
        entry["keep_original"] = bool(keep_original)
    if alternate_lenses is not None:
        entry["alternate_lenses"] = bool(alternate_lenses)
    if depth_map is not None:
        entry["depth_map"] = bool(depth_map)
    if dual_capture is not None:
        entry["dual_capture"] = bool(dual_capture)
    if width_b is not None:
        entry["width_b"] = int(width_b)
    if height_b is not None:
        entry["height_b"] = int(height_b)
    if fps_b is not None:
        entry["fps_b"] = int(fps_b)
    if format_request_b is not None:
        entry["format_request_b"] = format_request_b or None
    if extract_lenses_b is not None:
        entry["extract_lenses_b"] = bool(extract_lenses_b)
    _save_camera_settings()

    index = list_configured_cameras().get(name)
    if index is not None and index in _capture_handles:
        _apply_camera_settings(_capture_handles[index], get_camera_settings(name))


def _apply_camera_settings(cap, settings: dict) -> None:
    """Applies a resolved settings dict (see get_camera_settings) to an
    already-open cv2.VideoCapture handle — resolution, FPS if one's been
    set, and the pixel format if `format_request` is set: either the
    string "mono" (CAP_PROP_CONVERT_RGB=0 — works on some ordinary
    webcams, but NOT what actually distinguishes this camera's Y16 vs
    RGB24 — see get_camera_settings' BUGFIX note), or an explicit
    FOURCC string like "Y16 " (CAP_PROP_FOURCC — the property that
    actually selects between distinct UVC format descriptors, which IS
    what this camera needs). Only ever touched when format_request is
    set to something; an earlier version unconditionally forced
    CAP_PROP_CONVERT_RGB on every camera, even re-setting it to its own
    default for the normal case, which changed what some ordinary UVC
    webcams streamed back on some backends. Best-effort — cv2 silently
    ignores what a given backend can't do, same as elsewhere in this
    file (see e.g. CAP_PROP_BUFFERSIZE above)."""
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, settings["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, settings["height"])
        if settings.get("fps"):
            cap.set(cv2.CAP_PROP_FPS, settings["fps"])
        fmt = settings.get("format_request")
        if fmt == "mono":
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        elif fmt:
            cap.set(cv2.CAP_PROP_FOURCC, _fourcc_from_str(fmt))
    except Exception:
        pass


def _normalize_frame_for_save(frame):
    """
    Converts a raw frame from cap.read() into something safe to save/
    display as a normal 8-bit image, whatever raw pixel format the
    camera/backend handed back — e.g. 16-bit-per-pixel raw data (a Y16
    stereo/industrial sensor) treated as if it were ordinary 8-bit BGR
    with no conversion blows the high byte of every 16-bit value out
    toward white, or otherwise scrambles the image. This min/max-
    normalizes any non-8-bit frame down to the full 0-255 uint8 range
    before returning it, regardless of the sensor's actual bit depth, so
    it always saves as a real, viewable image. A no-op for the normal
    8-bit case (the vast majority of cameras/formats, always).
    """
    if frame is None or frame.dtype == np.uint8:
        return frame
    return cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def _extract_lenses(frame) -> list:
    """
    Splits a multi-channel frame into one grayscale image PER CHANNEL.

    BUGFIX (layer "split" was slicing the image into spatial strips
    instead of separating the actual layers): the previous version cut
    a frame into N vertical spatial slices left-to-right, which is the
    wrong operation for a camera like this — its "RGB24" output isn't
    real per-pixel color at all, it's three DIFFERENT views (e.g. left
    lens, right lens, and a third computed one) each packed whole into
    one of the R/G/B channels of an otherwise-normal-looking color
    frame. Viewed as real color, that looks like a psychedelic red/
    green/yellow mess — because it's not color data being displayed,
    it's three unrelated grayscale images fighting for the same three
    channels. The fix is to split by CHANNEL, not by space: this pulls
    the R plane, G plane, and B plane apart and returns each as its own
    separate grayscale image — the three actual "lenses"/views, each
    correctly viewable on its own.

    Returns a list of single-channel frames, one per channel (typically
    3 for a color-shaped frame). A frame that's already single-channel
    (Y16/GREY/mono) has nothing to extract — returned as a one-item list
    unchanged.
    """
    if frame is None:
        return [frame]
    if frame.ndim < 3 or frame.shape[2] < 2:
        return [frame]  # already single-channel — nothing to extract
    return [frame[:, :, i] for i in range(frame.shape[2])]


# Per-camera "which lens comes next" cycling position for "alternate_lenses"
# (see get_camera_settings/capture_frames_multi) — in-memory only, not
# persisted to disk, since it's just a rotating pointer rather than a
# real saved preference. Resets to 0 (start from the first lens again)
# on app restart.
_next_lens_index: dict = {}


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
                    else {"width": CAMERA_FRAME_WIDTH, "height": CAMERA_FRAME_HEIGHT, "fps": None})
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
    "Cameras" list Device Manager shows, so the Camera tab can display e.g.
    "index 2 - Logitech BRIO" instead of just "2".

    BUGFIX (printers showing up instead of cameras): the previous version
    filtered WMI's PNPClass on 'Camera' OR 'Image'. 'Image' is Windows'
    legacy WIA (Windows Image Acquisition) device class, which covers
    SCANNERS and multi-function PRINTERS with a scan feature too — not
    just cameras — so on a system with a networked/USB MFP printer, its
    name showed up in this list right alongside (or instead of) actual
    cameras. Fixed: uses Get-PnpDevice -Class Camera first — the modern,
    camera-SPECIFIC device class Device Manager's "Cameras" node uses,
    which doesn't include scanners/printers at all — and only falls back
    to the older 'Image' class (still filtering out anything whose name
    looks like a printer/scanner) for older cameras that only ever
    registered under that legacy class.

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
            "$cams = Get-PnpDevice -Class Camera -PresentOnly | "
            "Select-Object -ExpandProperty FriendlyName; "
            "if (-not $cams) { "
            "$cams = Get-PnpDevice -Class Image -PresentOnly | "
            "Where-Object { $_.FriendlyName -notmatch "
            "'printer|scan|mfp|fax|copier|multifunction' } | "
            "Select-Object -ExpandProperty FriendlyName "
            "}; $cams"
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
                            else {"width": CAMERA_FRAME_WIDTH, "height": CAMERA_FRAME_HEIGHT, "fps": None})
                _apply_camera_settings(fresh_cap, settings)
                _capture_handles[index] = fresh_cap
                ok, frame = fresh_cap.read()

    if not ok or frame is None:
        raise RuntimeError(
            f"Camera at index {index} did not return a frame, even after "
            f"reconnecting. Check the USB connection and that no other "
            f"program has it open."
        )
    return _normalize_frame_for_save(frame)


def _fourcc_to_str(fourcc_int: int) -> str:
    return "".join([chr((fourcc_int >> (8 * i)) & 0xFF) for i in range(4)]).strip() or "?"


def _fourcc_from_str(code: str):
    return cv2.VideoWriter_fourcc(*code.ljust(4)[:4])


def _describe_pixel_format(frame, fourcc_str: str) -> str:
    """
    Best-effort human-readable pixel format name for a captured frame.

    BUGFIX ("FormatType" showing gibberish like "}ë6ä" instead of a real
    name like "Y16"/"RGB24"): CAP_PROP_FOURCC isn't reliably populated by
    every backend — DirectShow on Windows especially will sometimes
    return some other internal numeric identifier instead of a real
    packed-ASCII FOURCC, which then decodes to non-printable garbage
    bytes when unpacked as 4 characters. That garbage was being shown
    directly as the format name.

    Fix: only trust the decoded FOURCC if it's actually 4 printable
    ASCII characters — a real FOURCC always is. Otherwise, fall back to
    inferring the format directly from the frame's actual shape/dtype,
    which is always correct regardless of what the backend reports for
    FOURCC: single-channel 16-bit -> "Y16", single-channel 8-bit ->
    "GREY", 3-channel 8-bit -> "RGB24", 4-channel 8-bit -> "RGBA32",
    anything else described generically as "<channels>ch-<dtype>".
    """
    if fourcc_str and fourcc_str != "?" and fourcc_str.isprintable() and all(ord(c) < 128 for c in fourcc_str):
        return fourcc_str
    if frame is None:
        return "?"
    channels = frame.shape[2] if frame.ndim == 3 else 1
    if channels == 1 and frame.dtype == np.uint16:
        return "Y16"
    if channels == 1 and frame.dtype == np.uint8:
        return "GREY"
    if channels == 3 and frame.dtype == np.uint8:
        return "RGB24"
    if channels == 4 and frame.dtype == np.uint8:
        return "RGBA32"
    return f"{channels}ch-{frame.dtype}"


# Common raw/compressed UVC pixel formats, tried by explicitly requesting
# each one via CAP_PROP_FOURCC. This list is GENERIC — the same
# candidates are tried on every camera, no model-specific assumptions —
# since which of these a given camera's driver actually honors is
# exactly what probe_camera_formats() below discovers from the hardware
# itself. Covers common color formats (MJPG/YUYV/YUY2/UYVY/NV12/BGR3/
# RGB3) and common raw-mono/depth-style formats (GREY/Y800/Y16) — this
# is what makes format detection generalize across "the See3CAM_Stereo,
# or any other camera" rather than being specific to one.
_CANDIDATE_FOURCCS = ["MJPG", "YUYV", "YUY2", "UYVY", "NV12", "GREY", "Y800", "Y16 ", "BGR3", "RGB3"]


def probe_camera_formats(camera_name: str) -> list:
    """
    Discovers which raw pixel formats (FOURCC codes) this camera's
    driver actually honors, independent of resolution — tries explicitly
    requesting each of _CANDIDATE_FOURCCS via CAP_PROP_FOURCC (at the
    camera's currently configured resolution — see get_camera_settings),
    and reports which ones came back with real (non-blank) data and what
    OpenCV actually negotiated. A requested format isn't always honored
    exactly by the driver, so the ACTUAL negotiated FOURCC read back
    afterward — not just the one requested — is what's reported;
    duplicate actual results (several requested codes all landing on the
    same real negotiated format) are only listed once.

    This is what answers "what pixel/RGB types does THIS camera support"
    straight from the hardware, the same way for any camera — nothing
    here assumes a particular model.

    Returns a list of dicts, one per distinct working format:
      {"requested_fourcc", "actual_fourcc", "actual_shape", "actual_dtype", "label"}
    """
    configured = list_configured_cameras()
    if camera_name not in configured:
        return []
    index = configured[camera_name]
    settings = get_camera_settings(camera_name)

    lock = _get_lock(index)
    working = []
    with lock:
        cached = _capture_handles.pop(index, None)
        if cached is not None:
            cached.release()

        seen_actual = set()
        for code in _CANDIDATE_FOURCCS:
            cap = _open_camera(index)
            if cap is None:
                continue
            cap.set(cv2.CAP_PROP_FOURCC, _fourcc_from_str(code))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, settings["width"])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, settings["height"])
            for _ in range(5):  # let the reconfigured stream settle, same as elsewhere in this file
                cap.grab()
            ok, frame = cap.read()
            actual_fourcc = _fourcc_to_str(int(cap.get(cv2.CAP_PROP_FOURCC)))
            cap.release()
            if not ok or frame is None:
                continue
            if float(np.std(frame)) < 1.0:  # blank/constant frame — this code didn't really work
                continue
            key = (actual_fourcc, frame.shape, str(frame.dtype))
            if key in seen_actual:
                continue
            seen_actual.add(key)
            working.append({
                "requested_fourcc": code, "actual_fourcc": actual_fourcc,
                "actual_shape": tuple(frame.shape), "actual_dtype": str(frame.dtype),
                "label": f"requested {code} → actual {actual_fourcc}, frame {frame.shape} {frame.dtype}",
            })

        try:
            _get_handle(index, camera_name=camera_name)
        except Exception:
            pass

    return working


def probe_camera_modes(camera_name: str, resolution_filter: tuple = None,
                        fps_filter: int = None) -> list:
    """
    Actually tests candidate (width, height, fps, pixel format) combos
    against the live camera hardware — closes whatever handle is cached
    first (some industrial/stereo UVC cameras need a full stream restart
    to change resolution/format; a property set on an already-streaming
    handle can silently no-op or hand back garbage — see
    capture_frames_multi() for the same fix applied to actual captures),
    opens a fresh handle per candidate, sets it, discards a few warm-up
    frames to let the sensor settle, reads one frame, and reports
    whether that frame looks like real image data (not blank/constant)
    plus its ACTUAL returned resolution, framerate, and pixel format
    name (see _describe_pixel_format() — a real FOURCC like "MJPG"/
    "YUY2" when the backend reports one properly, otherwise inferred
    from the frame's actual shape/dtype, e.g. "Y16"/"RGB24").

    resolution_filter=(width, height) and/or fps_filter=<int> narrow the
    search to just that resolution and/or framerate instead of the full
    candidate sweep — exposed on the Camera tab as "Detect Formats"
    filter fields. Narrowing the search also unlocks a MUCH deeper pixel
    format search (see below) since the total combo count stays bounded
    either way.

    BUGFIX (Y16 was coming out identical to RGB24): the previous version
    only ever toggled CAP_PROP_CONVERT_RGB to try to distinguish formats
    at the same resolution/fps. That property controls post-capture
    color conversion — it does NOT select between genuinely different
    USB Video Class format descriptors, which is what this camera's
    Y16-vs-RGB24 actually are. Toggling it did nothing on this camera:
    every attempt silently landed on the same real format, so entries
    that were supposed to be different formats came back pixel-
    identical. Fixed: format selection is now tried via explicit
    CAP_PROP_FOURCC requests (the property that actually does select
    between UVC format descriptors) in addition to the CONVERT_RGB=0
    "mono" attempt (kept — it DOES work on some ordinary webcams).
    When the search is unfiltered (testing every resolution/fps), only
    [driver default, "mono"] are tried per resolution/fps pair to keep
    the total sweep bounded (13 resolutions x 4 framerates x 2 = up to
    104 attempts). When resolution and/or fps has been narrowed down via
    the filters, the FULL set of candidate FOURCC codes is also tried at
    each remaining resolution/fps pair, since the search space shrinks
    enough to afford it — this is how you actually confirm/find e.g.
    "Y16 " vs "RGB24" as genuinely distinct results rather than
    duplicates.

    Labeled the same way e-con's own OpenCVCam.exe reference tool lists
    a camera's supported formats ("FormatType: Y16 Width: 752 Height:
    480 Fps: 60") so the list here reads the same way if you've compared
    against that tool.

    Generic candidate list only (common resolutions/framerates/pixel
    formats) — this makes no assumption about what kind of camera is
    plugged in; it reports whatever the hardware actually says it
    supports rather than assuming any particular camera model. Restores
    a normal handle at this camera's configured settings once done.

    Returns a list of dicts, one per WORKING combo — only combos that
    produced a plausible non-blank frame are included, so this is the
    definitive "what does this camera actually support" answer:
      {"width", "height", "fps", "format_request", "fourcc", "label", "actual_shape", "actual_dtype"}
    """
    configured = list_configured_cameras()
    if camera_name not in configured:
        return []
    index = configured[camera_name]

    if resolution_filter:
        resolutions = [tuple(resolution_filter)]
    else:
        resolutions = [
            (160, 120), (176, 144), (320, 240), (352, 288), (640, 480),
            (752, 480), (800, 600), (1024, 768), (1280, 720), (1280, 960),
            (1280, 1024), (1600, 1200), (1920, 1080),
        ]
    framerates = [fps_filter] if fps_filter else [15, 24, 30, 60]

    narrowed = bool(resolution_filter or fps_filter)
    format_requests = [None, "mono"] + (list(_CANDIDATE_FOURCCS) if narrowed else [])

    lock = _get_lock(index)
    working = []
    seen = set()
    with lock:
        cached = _capture_handles.pop(index, None)
        if cached is not None:
            cached.release()

        for width, height in resolutions:
            for fps in framerates:
                for format_request in format_requests:
                    cap = _open_camera(index)
                    if cap is None:
                        continue
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                    cap.set(cv2.CAP_PROP_FPS, fps)
                    if format_request == "mono":
                        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
                    elif format_request:
                        cap.set(cv2.CAP_PROP_FOURCC, _fourcc_from_str(format_request))
                    # Let the sensor settle after a resolution/format
                    # change before trusting what comes back - the first
                    # frame or two after a UVC stream (re)negotiation is
                    # commonly garbage/blank while auto-exposure/the
                    # sensor catches up. 8 (up from 5) for more reliable/
                    # accurate detection across this wider candidate set.
                    for _ in range(8):
                        cap.grab()
                    ok, frame = cap.read()
                    fourcc_raw = _fourcc_to_str(int(cap.get(cv2.CAP_PROP_FOURCC)))
                    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    actual_fps = cap.get(cv2.CAP_PROP_FPS)
                    cap.release()
                    if not ok or frame is None:
                        continue
                    # A "blank" frame (solid white/black/constant color)
                    # is exactly the reported symptom — this combo
                    # opened but isn't actually streaming real data.
                    if float(np.std(frame)) < 1.0:
                        continue
                    fourcc = _describe_pixel_format(frame, fourcc_raw)
                    key = (actual_w, actual_h, round(actual_fps), fourcc)
                    if key in seen:
                        continue  # several requested combos landing on the same actual negotiated one
                    seen.add(key)
                    working.append({
                        "width": actual_w, "height": actual_h, "fps": round(actual_fps) or fps,
                        "format_request": format_request, "fourcc": fourcc,
                        "label": f"FormatType: {fourcc} Width: {actual_w} Height: {actual_h} "
                                 f"Fps: {round(actual_fps) or fps}",
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


def _apply_extraction(camera_name: str, profile_label: str, frame, extract_on: bool,
                       keep_original: bool, alternate: bool, depth_map: bool) -> list:
    """
    Shared "what do we actually save for this frame" logic for both the
    primary and alternate profiles in capture_frames_multi() below.
    `profile_label` is "primary" for the A profile or "alternate" for
    the B/dual-capture profile — used directly in the saved suffix so
    filenames say what they actually are instead of a bare letter. If
    extraction is off, just the one frame under `profile_label`. If it's
    on:
      - alternate=False (default): every extracted lens, every capture,
        suffixed "<profile_label>_splitA", "..._splitB", "..._splitC"
        etc. (a letter per channel — descriptive, not a numeric index).
      - alternate=True: only ONE lens this capture, suffixed with
        whichever letter is next in rotation (see _next_lens_index),
        cycling to the next one next time — "the next photo is just the
        other secondary view" instead of every view every time.
      - keep_original=True additionally includes the un-split original
        combined frame as one more entry, suffixed "<profile_label>_original".
      - depth_map=True additionally computes and saves a depth/disparity
        image (see vision.camera.stereo_depth) from the FIRST TWO
        extracted lenses (splitA/splitB — assumed left/right, since a
        stereo camera's combined output conventionally packs left then
        right), suffixed "<profile_label>_depth". Requires at least 2
        lenses to have actually been extracted; no-ops (with a console
        note) if there weren't at least 2, or if depth computation
        itself fails for any reason (bad/misaligned pair, etc) — a
        failed depth map should never block saving the actual photos.
    """
    if not extract_on:
        return [(profile_label, frame)]
    lenses = _extract_lenses(frame)
    if len(lenses) <= 1:
        results = [(profile_label, lenses[0])]
    elif alternate:
        idx = _next_lens_index.get(camera_name, 0) % len(lenses)
        _next_lens_index[camera_name] = idx + 1
        results = [(f"{profile_label}_split{chr(65 + idx)}", lenses[idx])]
    else:
        results = [(f"{profile_label}_split{chr(65 + i)}", f) for i, f in enumerate(lenses)]
    if keep_original and len(lenses) > 1:
        results.append((f"{profile_label}_original", frame))
    if depth_map:
        if len(lenses) >= 2:
            try:
                depth = stereo_depth.compute_depth_map(camera_name, lenses[0], lenses[1])
                if depth is not None:
                    results.append((f"{profile_label}_depth", depth))
            except Exception as e:
                print(f"[STEREO DEPTH] Could not compute depth map for '{camera_name}': {e}")
        else:
            print(f"[STEREO DEPTH] '{camera_name}': depth map needs at least 2 extracted "
                  f"lenses (left+right) — this frame only produced {len(lenses)}.")
    return results


def capture_frames_multi(camera_name: str) -> list:
    """
    Grabs frame(s) from `camera_name` for one photo request, extracting
    lenses (see _extract_lenses/_apply_extraction) if that toggle is on,
    and — if the camera's "dual capture" toggle is on — rapid-firing a
    SECOND shot at a different resolution/fps right after the primary
    one (see set_camera_settings' dual_capture/width_b/height_b/fps_b/
    extract_lenses_b args, exposed on the Camera tab as a "Capture B"
    row) — the ACTUAL SECOND/OTHER camera output, hence "alternate"
    below.

    BUGFIX (dual capture returning solid-white photos and crashing): the
    first version of this switched profiles with a bare cap.set() on the
    SAME still-open handle, then read immediately. Several UVC cameras —
    stereo/industrial ones especially — don't actually renegotiate their
    stream that fast (or at all without a full restart); reading right
    after a live property change caught the sensor mid-reconfiguration,
    producing blank/white frames and in some cases wedging the driver
    badly enough to crash. Fixed by fully CLOSING the handle, reopening
    fresh at the B profile, discarding a few warm-up frames to let the
    new stream settle, THEN reading — the same "give the hardware a
    moment" approach probe_camera_modes() uses — before closing that and
    reopening once more back at the A/primary profile for whatever uses
    this camera next (live feed, the next capture).

    Returns a list of (suffix, frame) pairs. Normal case: one pair,
    suffix "primary". If the primary profile's extract_lenses is on and
    the frame has multiple channels: one pair per channel ("primary_
    splitA", "primary_splitB", ...) normally, or just ONE pair for
    whichever lens is next in rotation if alternate_lenses is on, plus a
    "primary_original" pair too if keep_original is on. Dual capture
    adds the equivalent "alternate"/"alternate_splitA".."alternate_
    original" pairs for the second (actually-different-camera-output)
    profile. Callers save each pair under a different view_index so they
    never collide on disk (see main.py's capture_movement_snapshot()/
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
    results = _apply_extraction(camera_name, "primary", frame_a, settings["extract_lenses"],
                                 settings["keep_original"], settings["alternate_lenses"],
                                 settings["depth_map"])

    if not settings["dual_capture"]:
        return results

    settings_b = {"width": settings["width_b"], "height": settings["height_b"],
                  "fps": settings["fps_b"], "format_request": settings["format_request_b"]}
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
            ok_b, frame_b_raw = cap_b.read()
            cap_b.release()
            if ok_b and frame_b_raw is not None:
                frame_b = _normalize_frame_for_save(frame_b_raw)

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
        return results

    results += _apply_extraction(camera_name, "alternate", frame_b, settings["extract_lenses_b"],
                                  settings["keep_original"], settings["alternate_lenses"],
                                  settings["depth_map"])
    return results


def capture_lens_pair(camera_name: str):
    """
    Grabs one frame from `camera_name` and extracts it into individual
    lenses (see _extract_lenses), returning the first two as
    (left, right) — regardless of that camera's saved extract_lenses/
    depth_map settings. Used by the stereo calibration wizard (see
    vision.camera.stereo_depth) to pull a fresh Left/Right pair on
    demand for "Add Calibration Image", without needing extraction
    turned on as a persistent capture setting. Raises ValueError if the
    frame doesn't actually split into at least 2 lenses (e.g. this isn't
    a multi-channel/stereo-style camera).
    """
    configured = list_configured_cameras()
    if camera_name not in configured:
        raise ValueError(f"Unknown camera '{camera_name}'.")
    frame = _capture_from_index(configured[camera_name], camera_name=camera_name)
    lenses = _extract_lenses(frame)
    if len(lenses) < 2:
        raise ValueError(
            f"'{camera_name}' only produced {len(lenses)} channel(s) — needs at least 2 "
            f"(left+right) for stereo calibration/depth. This only works for a camera whose "
            f"frame is a multi-channel combined stereo pair, like the See3CAM_Stereo's RGB24 "
            f"output."
        )
    return lenses[0], lenses[1]


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
    Structured as <YYYYMMDD>_<HHMMSS>_<4-char-suffix> (e.g.
    20260826_143210_a91f) rather than a bare UUID or a generic word
    prefix like "sample"/"object" — a folder can hold more than one
    object/sample, so a semantic prefix like that is misleading; plain
    date+time is what actually sorts and means something at a glance in
    a file browser. The short suffix (still drawn from uuid4, just
    truncated) exists only to keep two samples created within the same
    second from colliding - it isn't meant to be meaningful on its own.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:4]
    return f"{timestamp}_{suffix}"


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
