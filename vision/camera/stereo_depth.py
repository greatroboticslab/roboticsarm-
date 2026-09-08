"""
Stereo depth-map generation for "extract lenses" cameras (e.g. the
See3CAM_Stereo) — turns a Left/Right pair from vision.camera.capture's
lens extraction into a depth map, with an optional calibration workflow
for accuracy.

IMPORTANT HONESTY NOTE: the See3CAM_Stereo (and UVC stereo cameras
generally) have NO onboard depth hardware — unlike e.g. an Intel
RealSense, which computes depth on its own ASIC and exposes it as its
own stream, this camera is a pure pair of image sensors. There is no
generic OpenCV/UVC property to ask an arbitrary camera "do you have a
native depth stream" — that's vendor-SDK territory, not something plain
OpenCV can discover. So depth here is ALWAYS computed in software using
OpenCV's StereoSGBM — the standard, well-established open-source stereo
matching algorithm — after a Left/Right pair has been produced by lens
extraction. has_native_depth_support() below is a clear hook for future
vendor-SDK-specific depth support should a camera that actually has it
ever get added, but it always returns False today because no such
integration exists — there's no camera plugged into this that this code
could have tested against for real onboard depth.

CALIBRATION: raw, uncalibrated stereo images have lens distortion and
aren't perfectly aligned along epipolar lines, which makes disparity/
depth noisy and geometrically inaccurate. A calibration workflow here
(show a checkerboard to both lenses from several angles, run OpenCV's
standard cv2.calibrateCamera/stereoCalibrate/stereoRectify pipeline)
computes rectification maps that correct for both, saved per camera
name so it only needs to be done once (until the camera's physical
mounting or lens changes). Depth computation works without calibration
too (falls back to the raw, unrectified images) — it's just less
accurate — calibration is optional, not required.
"""

import json
import os

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False


def _require_cv2():
    if not _CV2_AVAILABLE:
        raise ImportError(
            "opencv-python (cv2) is required for stereo depth features but is not "
            "installed. Run: pip install opencv-python"
        )


CALIBRATION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "camera_calibration",
)

DEFAULT_CHECKERBOARD_SIZE = (9, 6)  # internal corners, not squares — standard OpenCV convention
DEFAULT_SQUARE_SIZE_MM = 25.0


def has_native_depth_support(camera_name: str) -> bool:
    """Always False today — see module docstring. This exists as a
    deliberate, clearly-named hook: if a camera with genuine onboard
    depth hardware and a real SDK integration is ever added, THIS is
    where that check would go, and compute_depth_map() below would
    prefer it over the software fallback. Right now nothing sets it to
    True for any camera."""
    return False


def _safe_name(camera_name: str) -> str:
    return "".join(c for c in camera_name if c.isalnum() or c in "-_") or "camera"


def _calib_path(camera_name: str) -> str:
    return os.path.join(CALIBRATION_DIR, f"{_safe_name(camera_name)}_stereo_calibration.json")


def has_calibration(camera_name: str) -> bool:
    return os.path.exists(_calib_path(camera_name))


def load_calibration(camera_name: str):
    """Returns the saved calibration dict for `camera_name` (rectification
    maps + image size + reprojection error), or None if none is saved."""
    path = _calib_path(camera_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        _require_cv2()
        for key in ("map1x", "map1y", "map2x", "map2y"):
            data[key] = np.array(data[key], dtype=np.float32)
        return data
    except Exception as e:
        print(f"[STEREO DEPTH] Could not load calibration for '{camera_name}': {e}")
        return None


def clear_calibration(camera_name: str) -> None:
    path = _calib_path(camera_name)
    if os.path.exists(path):
        os.remove(path)


# In-memory accumulation state for the calibration wizard (Add Calibration
# Image / Finish Calibration buttons on the Camera tab) — deliberately not
# persisted to disk; a half-finished calibration session isn't meaningful
# to resume across an app restart, and starting over is cheap.
_calib_state: dict = {}


def start_calibration_session(camera_name: str) -> None:
    """Clears any in-progress calibration image collection for this
    camera so 'Add Calibration Image' starts a fresh batch."""
    _calib_state[camera_name] = {"objpoints": [], "imgpoints_l": [], "imgpoints_r": [], "image_size": None}


def calibration_progress(camera_name: str) -> int:
    state = _calib_state.get(camera_name)
    return len(state["imgpoints_l"]) if state else 0


def add_calibration_image(camera_name: str, left_frame, right_frame,
                           checkerboard_size=DEFAULT_CHECKERBOARD_SIZE,
                           square_size_mm: float = DEFAULT_SQUARE_SIZE_MM) -> dict:
    """
    Attempts to find checkerboard corners in both frames of one Left/
    Right pair (see vision.camera.capture's lens extraction — this is
    meant to be called with two of the extracted lens images) and, if
    found in BOTH, accumulates them toward the running calibration.

    The camera itself stays completely still/mounted for the whole
    process — this is standard practice for stereo calibration and is
    what this workflow assumes throughout. Print a checkerboard pattern
    (checkerboard_size default is 9x6 INTERNAL corners — a 10x7-square
    board) and move ONLY THE BOARD to several different positions,
    angles, and distances in front of the stationary camera, calling
    this once per position; run_calibration() once enough images (10+)
    are collected.

    Returns {"found": bool, "count": int, "message": str}.
    """
    _require_cv2()
    state = _calib_state.setdefault(
        camera_name, {"objpoints": [], "imgpoints_l": [], "imgpoints_r": [], "image_size": None})

    gray_l = _to_gray_u8(left_frame)
    gray_r = _to_gray_u8(right_frame)

    found_l, corners_l = cv2.findChessboardCorners(gray_l, checkerboard_size)
    found_r, corners_r = cv2.findChessboardCorners(gray_r, checkerboard_size)

    if not (found_l and found_r):
        missing = "left" if not found_l else "right"
        return {
            "found": False, "count": len(state["imgpoints_l"]),
            "message": f"Checkerboard not found in the {missing} view — reposition it "
                       f"(fully visible, flat, well-lit, not too close/far) and try again.",
        }

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    corners_l = cv2.cornerSubPix(gray_l, corners_l, (11, 11), (-1, -1), criteria)
    corners_r = cv2.cornerSubPix(gray_r, corners_r, (11, 11), (-1, -1), criteria)

    objp = np.zeros((checkerboard_size[0] * checkerboard_size[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:checkerboard_size[0], 0:checkerboard_size[1]].T.reshape(-1, 2)
    objp *= square_size_mm

    state["objpoints"].append(objp)
    state["imgpoints_l"].append(corners_l)
    state["imgpoints_r"].append(corners_r)
    state["image_size"] = (gray_l.shape[1], gray_l.shape[0])

    return {
        "found": True, "count": len(state["imgpoints_l"]),
        "message": f"Checkerboard found in both views — {len(state['imgpoints_l'])} "
                   f"calibration image(s) collected so far.",
    }


def run_calibration(camera_name: str, min_images: int = 10) -> dict:
    """
    Runs OpenCV's standard stereo calibration pipeline (calibrateCamera
    per lens, then stereoCalibrate + stereoRectify for the pair) over
    every image collected via add_calibration_image() since the last
    start_calibration_session(), and saves the resulting rectification
    maps for compute_depth_map() to use. Requires at least `min_images`
    (default 10) checkerboard detections — fewer than that produces an
    unreliable calibration, so this refuses rather than saving a bad
    one silently.　Returns {"ok": bool, "message": str}.
    """
    _require_cv2()
    state = _calib_state.get(camera_name)
    got = len(state["imgpoints_l"]) if state else 0
    if not state or got < min_images:
        return {
            "ok": False,
            "message": f"Need at least {min_images} calibration images with the "
                       f"checkerboard found in both views — only have {got}. Keep using "
                       f"'Add Calibration Image' from different angles/distances.",
        }

    image_size = state["image_size"]
    objpoints, imgpoints_l, imgpoints_r = state["objpoints"], state["imgpoints_l"], state["imgpoints_r"]

    try:
        _, K_l, D_l, _, _ = cv2.calibrateCamera(objpoints, imgpoints_l, image_size, None, None)
        _, K_r, D_r, _, _ = cv2.calibrateCamera(objpoints, imgpoints_r, image_size, None, None)

        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-5)
        reproj_error, K_l, D_l, K_r, D_r, R, T, _, _ = cv2.stereoCalibrate(
            objpoints, imgpoints_l, imgpoints_r, K_l, D_l, K_r, D_r, image_size,
            criteria=criteria, flags=cv2.CALIB_FIX_INTRINSIC)

        R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(K_l, D_l, K_r, D_r, image_size, R, T, alpha=0)

        map1x, map1y = cv2.initUndistortRectifyMap(K_l, D_l, R1, P1, image_size, cv2.CV_32FC1)
        map2x, map2y = cv2.initUndistortRectifyMap(K_r, D_r, R2, P2, image_size, cv2.CV_32FC1)
    except Exception as e:
        return {"ok": False, "message": f"Calibration failed: {e}"}

    data = {
        "image_size": list(image_size),
        "map1x": map1x.tolist(), "map1y": map1y.tolist(),
        "map2x": map2x.tolist(), "map2y": map2y.tolist(),
        "reprojection_error": float(reproj_error),
    }
    os.makedirs(CALIBRATION_DIR, exist_ok=True)
    with open(_calib_path(camera_name), "w") as f:
        json.dump(data, f)

    _calib_state.pop(camera_name, None)
    quality = "good" if reproj_error < 1.0 else ("okay" if reproj_error < 2.0 else "poor - consider redoing")
    return {
        "ok": True,
        "message": f"Calibration complete for '{camera_name}' — reprojection error "
                   f"{reproj_error:.3f}px ({quality}; lower is better, under ~1.0px is "
                   f"generally good). Saved and will be used automatically for depth maps.",
    }


def _to_gray_u8(frame):
    """Normalizes any frame (color or mono, any bit depth) to a plain
    8-bit single-channel grayscale image for stereo matching/checkerboard
    detection."""
    _require_cv2()
    if frame.ndim == 3:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.shape[2] >= 3 else frame[:, :, 0]
    if frame.dtype != np.uint8:
        frame = cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return frame


def compute_depth_map(camera_name: str, left_frame, right_frame):
    """
    Computes a depth/disparity visualization from a Left/Right pair.

    Prefers a native/hardware depth source if has_native_depth_support()
    is ever True for this camera (see that function's docstring — always
    False today, for any camera); otherwise (always, currently) uses
    OpenCV's StereoSGBM — a well-established open-source stereo matching
    algorithm — which is what actually runs here.

    If a saved calibration exists for this camera AND matches the
    current image size, the Left/Right images are rectified (lens-
    undistorted + epipolar-aligned) first, giving a substantially more
    accurate and consistent result. Without calibration, this still
    computes a usable disparity map directly from the raw images, but
    it's a RELATIVE grayscale visualization (brighter = closer), not a
    calibrated metric distance — true metric depth needs the physical
    baseline distance between the two lenses, which calibration provides
    and an uncalibrated map doesn't have.

    Returns an 8-bit single-channel grayscale image (brighter = closer),
    or None if the pair doesn't look usable for stereo matching.
    """
    _require_cv2()
    if left_frame is None or right_frame is None:
        return None

    gray_l = _to_gray_u8(left_frame)
    gray_r = _to_gray_u8(right_frame)

    if has_native_depth_support(camera_name):
        # Hook for a future vendor-SDK depth integration — nothing
        # implements this path today (see module docstring), so
        # has_native_depth_support() always returns False and this
        # branch is unreachable in practice right now.
        raise NotImplementedError(
            "has_native_depth_support() returned True but no native depth backend "
            "is actually implemented yet — this is a placeholder for future work."
        )

    calib = load_calibration(camera_name)
    if calib is not None and calib.get("image_size") == [gray_l.shape[1], gray_l.shape[0]]:
        gray_l = cv2.remap(gray_l, calib["map1x"], calib["map1y"], cv2.INTER_LINEAR)
        gray_r = cv2.remap(gray_r, calib["map2x"], calib["map2y"], cv2.INTER_LINEAR)

    # StereoSGBM with reasonable general-purpose defaults. numDisparities
    # must be a positive multiple of 16; blockSize odd, typically 3-11.
    block_size = 7
    num_disparities = 128
    stereo = cv2.StereoSGBM_create(
        minDisparity=0,
        numDisparities=num_disparities,
        blockSize=block_size,
        P1=8 * 3 * block_size ** 2,
        P2=32 * 3 * block_size ** 2,
        disp12MaxDiff=1,
        uniquenessRatio=10,
        speckleWindowSize=100,
        speckleRange=32,
        mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
    )
    disparity = stereo.compute(gray_l, gray_r).astype(np.float32) / 16.0
    disp_vis = cv2.normalize(disparity, None, 0, 255, cv2.NORM_MINMAX)
    return np.uint8(disp_vis)
