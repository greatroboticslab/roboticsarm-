"""
[WIRED] Middleman — Other Side (controller) networking/protocol.
Mirror of middleman_physical_side.py: discovers available Physical
Sides, joins one's control queue, and — once active — publishes
jog/move/laser/capture-request messages instead of main.py calling
local hardware directly. main.py still owns turning GUI actions
(button presses, jog keys) into calls on this class; this class only
owns the networking/session side of that.

Commands are refused client-side (not just relying on the Physical
Side to ignore them) whenever this instance isn't the currently-active
controller, per the agreed "locked out client-side too" design —
see is_active_controller().
"""

from __future__ import annotations
import json
import threading
import time
import uuid

try:
    import paho.mqtt.client as mqtt
    _PAHO_AVAILABLE = True
except ImportError:
    _PAHO_AVAILABLE = False

from vision.config import (
    MQTT_BROKER_HOST,
    MQTT_BROKER_PORT,
    MIDDLEMAN_DISCOVERY_TOPIC,
    MIDDLEMAN_SESSION_TOPIC_TEMPLATE,
    MIDDLEMAN_CONTROL_STATUS_TOPIC_TEMPLATE,
    MIDDLEMAN_MOVE_TOPIC_TEMPLATE,
    MIDDLEMAN_LASER_TOPIC_TEMPLATE,
    MIDDLEMAN_CAPTURE_REQUEST_TOPIC_TEMPLATE,
    MIDDLEMAN_PHOTO_TOPIC_TEMPLATE,
    MIDDLEMAN_TELEMETRY_TOPIC_TEMPLATE,
    MIDDLEMAN_ERROR_TOPIC_TEMPLATE,
    MIDDLEMAN_HEARTBEAT_INTERVAL_SECONDS,
    MIDDLEMAN_HEARTBEAT_TIMEOUT_SECONDS,
)
from vision.services import photo_transfer, rotation_coordinator


def _require_paho():
    if not _PAHO_AVAILABLE:
        raise ImportError("paho-mqtt is not installed. Run: pip install paho-mqtt")


class OtherSideController:
    def __init__(self, on_discovery_update=None, on_telemetry_update=None,
                 on_control_status_update=None, on_photo_received=None,
                 on_error_received=None, on_log=print):
        """
        on_discovery_update:      (dict[ip -> info]) -> None — called
                                   whenever a Physical Side's presence
                                   broadcast is seen/goes stale.
        on_telemetry_update:      (dict) -> None — live position/status
                                   from the connected Physical Side.
        on_control_status_update: (dict) -> None — active/queue snapshot
                                   from the connected Physical Side; used
                                   to know whether we're active or queued.
        on_photo_received:        (list[str] local paths) -> None — after
                                   a received photo bundle has been saved
                                   locally + written to local MongoDB.
        on_error_received:        (str message) -> None — a command this
                                   instance sent (move, most commonly a
                                   hard-deck rejection) was refused by the
                                   Physical Side; only fires for errors
                                   addressed to this controller_id.
        All callbacks fire from background threads — callers (main.py)
        are responsible for marshalling to the GUI thread.
        """
        self.controller_id = str(uuid.uuid4())
        self.on_discovery_update = on_discovery_update
        self.on_telemetry_update = on_telemetry_update
        self.on_control_status_update = on_control_status_update
        self.on_photo_received = on_photo_received
        self.on_error_received = on_error_received
        self.on_log = on_log

        self._client = None
        self._stop_event = threading.Event()
        self._discovery_thread = None
        self._heartbeat_thread = None

        self._discovered = {}  # ip -> {"name", "online", "last_seen", ...}
        self._discovered_lock = threading.Lock()

        self._connected_ip = None
        self._last_control_status = {"active": None, "queue": []}

    # -- discovery (always running once started) ---------------------

    def start_discovery(self) -> None:
        _require_paho()
        if self._client is not None:
            return
        self._stop_event.clear()
        self._client = mqtt.Client()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
        self._client.loop_start()
        self._discovery_thread = threading.Thread(target=self._prune_stale_loop, daemon=True)
        self._discovery_thread.start()

    def stop(self) -> None:
        self.disconnect()
        self._stop_event.set()
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    def get_discovered(self) -> dict:
        with self._discovered_lock:
            return dict(self._discovered)

    # -- connecting to one Physical Side -------------------------------

    def connect(self, physical_side_ip: str) -> None:
        if self._client is None:
            self.start_discovery()
        self.disconnect()  # only ever connected to one at a time
        self._connected_ip = physical_side_ip
        self._client.subscribe(MIDDLEMAN_CONTROL_STATUS_TOPIC_TEMPLATE.format(ip=physical_side_ip))
        self._client.subscribe(MIDDLEMAN_TELEMETRY_TOPIC_TEMPLATE.format(ip=physical_side_ip))
        self._client.subscribe(MIDDLEMAN_PHOTO_TOPIC_TEMPLATE.format(ip=physical_side_ip))
        self._client.subscribe(MIDDLEMAN_ERROR_TOPIC_TEMPLATE.format(ip=physical_side_ip))
        self._publish_session_event("connect")
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def disconnect(self) -> None:
        if self._connected_ip is None:
            return
        self._publish_session_event("disconnect")
        try:
            self._client.unsubscribe(MIDDLEMAN_CONTROL_STATUS_TOPIC_TEMPLATE.format(ip=self._connected_ip))
            self._client.unsubscribe(MIDDLEMAN_TELEMETRY_TOPIC_TEMPLATE.format(ip=self._connected_ip))
            self._client.unsubscribe(MIDDLEMAN_PHOTO_TOPIC_TEMPLATE.format(ip=self._connected_ip))
            self._client.unsubscribe(MIDDLEMAN_ERROR_TOPIC_TEMPLATE.format(ip=self._connected_ip))
        except Exception:
            pass
        self._connected_ip = None
        self._last_control_status = {"active": None, "queue": []}

    def is_active_controller(self) -> bool:
        return self._last_control_status.get("active") == self.controller_id

    # -- sending commands (only if active — locked out client-side) ---

    def send_move(self, move_payload: dict) -> bool:
        return self._send(MIDDLEMAN_MOVE_TOPIC_TEMPLATE, move_payload)

    def send_laser(self, channel, state: bool) -> bool:
        return self._send(MIDDLEMAN_LASER_TOPIC_TEMPLATE, {"channel": channel, "laser": state})

    def request_capture(self, object_id: str | None = None, view_index: int | None = None) -> bool:
        """
        object_id: if given, this capture is one step of a rotation
        sequence — pass the SAME object_id on every step (see
        rotation_coordinator.begin_sequence(), called by main.py's
        rotation loop before the first request_capture()) so the
        Physical Side echoes it back and this side's Other-Side
        _handle_photo() routes the resulting bundle into that
        sequence's queue instead of logging it as its own object.
        Omit for an ad hoc single "Capture Now" press — that still
        gets its own fresh object as before.
        view_index: which step of the sequence this is, for logging/
        ordering only — not required for correctness (bundles queue in
        arrival order regardless).
        """
        payload = {}
        if object_id is not None:
            payload["object_id"] = object_id
        if view_index is not None:
            payload["view_index"] = view_index
        return self._send(MIDDLEMAN_CAPTURE_REQUEST_TOPIC_TEMPLATE, payload)

    def _send(self, topic_template: str, payload: dict) -> bool:
        if self._connected_ip is None or not self.is_active_controller():
            self.on_log("[MIDDLEMAN] Command blocked — not the active controller.")
            return False
        payload = {**payload, "controller_id": self.controller_id}
        self._client.publish(topic_template.format(ip=self._connected_ip), json.dumps(payload))
        return True

    # -- MQTT plumbing -----------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.on_log(f"[MIDDLEMAN] Other Side connect failed, code {rc}")
            return
        client.subscribe(MIDDLEMAN_DISCOVERY_TOPIC)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.on_log(f"[MIDDLEMAN] Bad message on {msg.topic}: {e}")
            return

        try:
            if msg.topic == MIDDLEMAN_DISCOVERY_TOPIC:
                self._handle_discovery(payload)
            elif self._connected_ip and msg.topic == MIDDLEMAN_CONTROL_STATUS_TOPIC_TEMPLATE.format(ip=self._connected_ip):
                self._handle_control_status(payload)
            elif self._connected_ip and msg.topic == MIDDLEMAN_TELEMETRY_TOPIC_TEMPLATE.format(ip=self._connected_ip):
                if self.on_telemetry_update:
                    self.on_telemetry_update(payload)
            elif self._connected_ip and msg.topic == MIDDLEMAN_PHOTO_TOPIC_TEMPLATE.format(ip=self._connected_ip):
                self._handle_photo(payload)
            elif self._connected_ip and msg.topic == MIDDLEMAN_ERROR_TOPIC_TEMPLATE.format(ip=self._connected_ip):
                self._handle_error(payload)
        except Exception as e:
            self.on_log(f"[MIDDLEMAN] Handler error on {msg.topic}: {e}")

    def _handle_discovery(self, payload: dict) -> None:
        ip = payload.get("ip")
        if not ip:
            return
        with self._discovered_lock:
            if payload.get("online", True):
                self._discovered[ip] = {**payload, "last_seen": time.monotonic()}
            else:
                self._discovered.pop(ip, None)
            snapshot = dict(self._discovered)
        if self.on_discovery_update:
            self.on_discovery_update(snapshot)

    def _handle_control_status(self, payload: dict) -> None:
        self._last_control_status = {"active": payload.get("active"), "queue": payload.get("queue", [])}
        if self.on_control_status_update:
            self.on_control_status_update(payload)

    def _handle_photo(self, bundle: dict) -> None:
        try:
            if rotation_coordinator.on_bundle_received(bundle):
                # Part of an active rotation sequence — the coordinator
                # already wrote the files and queued them for whichever
                # rotation loop is waiting in wait_for_view(); nothing
                # gets logged to Mongo here (that happens once, at the
                # end of the sequence, via record_capture()). Do NOT
                # also fall through to the ad hoc single-capture path
                # below, or this view would additionally become its own
                # standalone object.
                return
            object_id, saved_paths, warnings = photo_transfer.save_photo_bundle(bundle)
            for w in warnings:
                self.on_log(f"[MIDDLEMAN] {w}")
        except Exception as e:
            self.on_log(f"[MIDDLEMAN] Failed to save received photo bundle: {e}")
            return
        if self.on_photo_received:
            self.on_photo_received(saved_paths)

    def _handle_error(self, payload: dict) -> None:
        if payload.get("controller_id") != self.controller_id:
            return  # addressed to a different (e.g. queued) controller
        message = payload.get("error", "(no details)")
        if self.on_error_received:
            self.on_error_received(message)
        else:
            self.on_log(f"[MIDDLEMAN] Command rejected: {message}")

    def _publish_session_event(self, event: str) -> None:
        if self._connected_ip is None:
            return
        payload = {"event": event, "controller_id": self.controller_id}
        self._client.publish(MIDDLEMAN_SESSION_TOPIC_TEMPLATE.format(ip=self._connected_ip),
                              json.dumps(payload))

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set() and self._connected_ip is not None:
            self._publish_session_event("heartbeat")
            time.sleep(MIDDLEMAN_HEARTBEAT_INTERVAL_SECONDS)

    def _prune_stale_loop(self) -> None:
        stale_after = MIDDLEMAN_HEARTBEAT_TIMEOUT_SECONDS
        while not self._stop_event.is_set():
            now = time.monotonic()
            with self._discovered_lock:
                stale_ips = [ip for ip, info in self._discovered.items()
                             if now - info.get("last_seen", 0) > stale_after]
                for ip in stale_ips:
                    self._discovered.pop(ip, None)
                snapshot = dict(self._discovered)
            if stale_ips and self.on_discovery_update:
                self.on_discovery_update(snapshot)
            time.sleep(MIDDLEMAN_HEARTBEAT_INTERVAL_SECONDS)
