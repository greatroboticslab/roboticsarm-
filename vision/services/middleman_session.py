"""
[WIRED] First-come-first-served control queue for Middleman — Physical
Side. Pure logic, no MQTT/I-O here on purpose, so it's easy to reason
about/test independently of the networking in
vision/services/middleman_physical_side.py, which owns an instance of
ControlQueue and drives it from incoming session messages.

Protocol (messages on a Physical Side's session topic,
vision.config.MIDDLEMAN_SESSION_TOPIC_TEMPLATE):
    {"event": "connect",    "controller_id": "..."}
    {"event": "heartbeat",  "controller_id": "..."}
    {"event": "disconnect", "controller_id": "..."}

Rules (as agreed):
  - No active controller -> a "connect" becomes active immediately.
  - Active controller already set -> a "connect" joins the back of the
    queue instead.
  - Only the active controller's move/laser/capture commands are
    executed; anything from a queued (or unknown) controller_id is
    ignored, even if it arrives.
  - Active controller's heartbeat goes stale (no message within
    MIDDLEMAN_HEARTBEAT_TIMEOUT_SECONDS) -> per the agreed disconnect
    behavior, the in-progress action is allowed to finish, then the
    caller (middleman_physical_side.py) stops/holds + turns the laser
    off, and the next queued controller (if any) is promoted to active.
  - clear() (physical-side "Disconnect All / Clear Queue" button) wipes
    everything — active and queued — unconditionally.
"""

from __future__ import annotations
import threading
import time


class ControlQueue:
    def __init__(self, timeout_seconds: float):
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._active_id: str | None = None
        self._active_last_seen: float = 0.0
        self._queue: list[str] = []  # controller_ids, FIFO

    def connect(self, controller_id: str) -> str:
        """Returns 'active' or 'queued'."""
        with self._lock:
            now = time.monotonic()
            if self._active_id is None:
                self._active_id = controller_id
                self._active_last_seen = now
                return "active"
            if controller_id == self._active_id:
                self._active_last_seen = now
                return "active"
            if controller_id not in self._queue:
                self._queue.append(controller_id)
            return "queued"

    def heartbeat(self, controller_id: str) -> None:
        with self._lock:
            if controller_id == self._active_id:
                self._active_last_seen = time.monotonic()
            elif controller_id not in self._queue and controller_id != self._active_id:
                # Heartbeat from something we don't know about (e.g. this
                # side restarted mid-session) — treat like a fresh connect
                # rather than silently dropping it.
                pass

    def disconnect(self, controller_id: str) -> str | None:
        """Explicit disconnect. Returns the newly-promoted controller_id
        (if the active one left and someone was queued), else None."""
        with self._lock:
            return self._release_and_promote(controller_id)

    def check_timeout(self) -> str | None:
        """Call periodically. Returns the newly-promoted controller_id if
        the active controller just timed out and someone was queued,
        '' (empty string) if it timed out with nobody queued, or None if
        nothing changed."""
        with self._lock:
            if self._active_id is None:
                return None
            if time.monotonic() - self._active_last_seen <= self._timeout_seconds:
                return None
            timed_out_id = self._active_id
            promoted = self._release_and_promote(timed_out_id)
            return promoted if promoted is not None else ""

    def clear(self) -> None:
        """'Disconnect All' — wipes active + queue unconditionally."""
        with self._lock:
            self._active_id = None
            self._queue = []

    def is_active(self, controller_id: str) -> bool:
        with self._lock:
            return controller_id is not None and controller_id == self._active_id

    def snapshot(self) -> dict:
        """For status broadcasts / UI display."""
        with self._lock:
            return {"active": self._active_id, "queue": list(self._queue)}

    # -- internal, caller must hold self._lock --
    def _release_and_promote(self, controller_id: str) -> str | None:
        if controller_id != self._active_id:
            if controller_id in self._queue:
                self._queue.remove(controller_id)
            return None
        if self._queue:
            self._active_id = self._queue.pop(0)
            self._active_last_seen = time.monotonic()
            return self._active_id
        self._active_id = None
        return None
