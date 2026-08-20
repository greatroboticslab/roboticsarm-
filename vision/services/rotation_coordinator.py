"""
[WIRED] Turns the Middleman link's async, one-bundle-at-a-time photo
transport into something a rotation-capture loop can drive step by
step, on the Other Side (the machine that owns Mongo/CSV/Excel).

THE PROBLEM THIS SOLVES
-------------------------
OtherSideController.request_capture() just publishes an MQTT message
and returns immediately — the resulting photo bundle arrives later,
asynchronously, via _handle_photo(). A rotation sequence needs to do
N of these (move J4, request a capture, wait for that view's photo,
repeat) and end up with ONE object holding N images — not N separate
objects, which is what you'd get if every arriving bundle were
recorded to Mongo the instant it showed up (the ad hoc "Capture Now"
button's behavior, via photo_transfer.save_photo_bundle()).

HOW
----
begin_sequence(object_id) registers a queue for that id. Every bundle
that arrives (regardless of source) is offered to on_bundle_received()
first — if its "sample_id" matches a registered sequence, its images
are written to disk ONLY (photo_transfer.save_photo_bundle_files —
no Mongo write) and pushed onto that sequence's queue, and this
function returns True so the caller (middleman_other_side.py's
_handle_photo) knows NOT to also do the ad hoc immediate-record path.
Bundles for anything not currently registered fall through to that ad
hoc path unchanged (a plain "Capture Now" press still works exactly as
before).

The rotation loop itself (main.py, both local-camera and Middleman-
Other-Side versions) calls wait_for_view() after each request_capture()
to block (with a timeout) until that step's bundle shows up, then
moves on to the next J4 step. Once every view's arrived, the loop
calls vision.storage.capture_pipeline.record_capture() exactly once
with every accumulated path — same as the local-camera rotation loop —
so however many machines were involved, there's still exactly one
place a capture gets logged.
"""

from __future__ import annotations

import queue
import threading
from typing import Dict, Optional

from vision.services import photo_transfer

_pending: Dict[str, "queue.Queue"] = {}
_lock = threading.Lock()


def begin_sequence(object_id: str) -> None:
    """Call once, before the first request_capture() of a rotation
    sequence, so incoming bundles tagged with this object_id get routed
    here instead of being recorded as standalone objects."""
    with _lock:
        _pending[object_id] = queue.Queue()


def end_sequence(object_id: str) -> None:
    """Call when a sequence finishes (success, error, or cancel) so a
    stray late bundle for this id doesn't sit in memory forever, and so
    the id becomes available for the ad hoc single-capture path again."""
    with _lock:
        _pending.pop(object_id, None)


def on_bundle_received(bundle: dict) -> bool:
    """Called from middleman_other_side.py's _handle_photo for EVERY
    incoming bundle, before it decides how to handle it.

    Returns True if this bundle belonged to an active rotation sequence
    (already handled here — files written, nothing logged to Mongo yet)
    — the caller should stop and not also run the ad hoc record path.
    Returns False if no sequence is waiting on this id — the caller
    should fall through to its normal ad hoc single-capture handling.
    """
    object_id = bundle.get("sample_id")
    with _lock:
        q = _pending.get(object_id)
    if q is None:
        return False
    try:
        paths_by_source = photo_transfer.save_photo_bundle_files(bundle)
        q.put(("ok", paths_by_source))
    except Exception as e:
        q.put(("error", str(e)))
    return True


def wait_for_view(object_id: str, timeout: float) -> Dict[str, str]:
    """Blocks until the next bundle for this sequence arrives (or
    `timeout` seconds pass). Returns {source_name: local_path} for that
    one view. Raises TimeoutError if nothing arrived in time (e.g. the
    Physical Side is unreachable or its camera failed) or RuntimeError
    if the Physical Side reported/caused a decode error for this view.
    """
    with _lock:
        q = _pending.get(object_id)
    if q is None:
        raise RuntimeError(
            f"No active rotation sequence for object_id={object_id!r} — "
            f"call begin_sequence() before the first request_capture()."
        )
    try:
        status, payload = q.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(
            f"Timed out after {timeout}s waiting for a view — check the "
            f"Physical Side is still connected and its camera is working."
        )
    if status == "error":
        raise RuntimeError(f"Failed to decode/save the received view: {payload}")
    return payload
