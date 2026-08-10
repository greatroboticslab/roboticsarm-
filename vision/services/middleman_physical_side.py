"""
[WIRED] Middleman — Physical Side networking/protocol. Owns nothing
hardware-specific itself — it's handed callables by main.py (which
still owns the actual robot/laser/camera calls, unchanged) and only
deals with: discovery broadcast, the control queue (see
middleman_session.py), and relaying move/laser/capture-request messages
to those callables.

main.py's job for this mode is just: construct a PhysicalSideController
with its executor callables, call .start()/.stop(), and reflect
on_control_status_change in the UI (jog/manual controls
enabled/disabled based on whether a controller is currently active).
"""

from __future__ import annotations
import json
import socket
import threading
import time

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
from vision.net_utils import get_local_ip
from vision.services.middleman_session import ControlQueue
from vision.services import photo_transfer


def _require_paho():
    if not _PAHO_AVAILABLE:
        raise ImportError("paho-mqtt is not installed. Run: pip install paho-mqtt")


class PhysicalSideController:
    def __init__(self, name=None, robot_connected_provider=None,
                 move_executor=None, laser_executor=None, capture_executor=None,
                 telemetry_provider=None, on_control_status_change=None,
                 on_log=print):
        """
        robot_connected_provider: () -> bool
        move_executor:            (payload: dict) -> str | None — same
                                   payload shape as the existing generic
                                   remote-control handler (jog / j1..j4).
                                   Returns None if accepted, or an error
                                   string (e.g. hard-deck rejection, real
                                   hardware move error) to relay back to
                                   whoever sent the command.
        laser_executor:           (channel: int | None, state: bool) -> None
                                   — channel-addressed since this rig's
                                   lasers are relay-switched (1-4), not
                                   one PWM-dimmable laser; channel=None
                                   means "all configured channels".
        capture_executor:         () -> (sample_id: str,
                                          frames: list[(source, view_idx, frame)],
                                          values: dict)
        telemetry_provider:       () -> dict (JSON-serializable)
        on_control_status_change: (snapshot: dict) -> None — called from a
                                   background thread; caller is responsible
                                   for marshalling to the GUI thread
                                   (e.g. tkinter's root.after).
        """
        self.name = name or socket.gethostname()
        self.ip = get_local_ip(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
        self.queue = ControlQueue(MIDDLEMAN_HEARTBEAT_TIMEOUT_SECONDS)

        self.robot_connected_provider = robot_connected_provider or (lambda: False)
        self.move_executor = move_executor
        self.laser_executor = laser_executor
        self.capture_executor = capture_executor
        self.telemetry_provider = telemetry_provider
        self.on_control_status_change = on_control_status_change
        self.on_log = on_log

        self._client = None
        self._stop_event = threading.Event()
        self._threads = []

        self._session_topic = MIDDLEMAN_SESSION_TOPIC_TEMPLATE.format(ip=self.ip)
        self._control_status_topic = MIDDLEMAN_CONTROL_STATUS_TOPIC_TEMPLATE.format(ip=self.ip)
        self._move_topic = MIDDLEMAN_MOVE_TOPIC_TEMPLATE.format(ip=self.ip)
        self._laser_topic = MIDDLEMAN_LASER_TOPIC_TEMPLATE.format(ip=self.ip)
        self._capture_request_topic = MIDDLEMAN_CAPTURE_REQUEST_TOPIC_TEMPLATE.format(ip=self.ip)
        self._photo_topic = MIDDLEMAN_PHOTO_TOPIC_TEMPLATE.format(ip=self.ip)
        self._telemetry_topic = MIDDLEMAN_TELEMETRY_TOPIC_TEMPLATE.format(ip=self.ip)
        self._error_topic = MIDDLEMAN_ERROR_TOPIC_TEMPLATE.format(ip=self.ip)

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        _require_paho()
        self._stop_event.clear()
        self._client = mqtt.Client()
        self._client.will_set(
            MIDDLEMAN_DISCOVERY_TOPIC,
            json.dumps(self._discovery_payload(online=False)),
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
        self._client.loop_start()

        self._threads = [
            threading.Thread(target=self._discovery_loop, daemon=True),
            threading.Thread(target=self._telemetry_loop, daemon=True),
            threading.Thread(target=self._timeout_watch_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._client is not None:
            try:
                self._client.publish(MIDDLEMAN_DISCOVERY_TOPIC,
                                      json.dumps(self._discovery_payload(online=False)))
            except Exception:
                pass
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self.queue.clear()

    def disconnect_all(self) -> None:
        """'Disconnect All / Clear Queue' — manual override button."""
        self.queue.clear()
        self._publish_control_status()

    # -- MQTT plumbing ---------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.on_log(f"[MIDDLEMAN] Physical Side connect failed, code {rc}")
            return
        client.subscribe(self._session_topic)
        client.subscribe(self._move_topic)
        client.subscribe(self._laser_topic)
        client.subscribe(self._capture_request_topic)
        self._publish_control_status()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.on_log(f"[MIDDLEMAN] Bad message on {msg.topic}: {e}")
            return

        try:
            if msg.topic == self._session_topic:
                self._handle_session(payload)
            elif msg.topic == self._move_topic:
                self._handle_move(payload)
            elif msg.topic == self._laser_topic:
                self._handle_laser(payload)
            elif msg.topic == self._capture_request_topic:
                self._handle_capture_request(payload)
        except Exception as e:
            self.on_log(f"[MIDDLEMAN] Handler error on {msg.topic}: {e}")

    def _handle_session(self, payload: dict) -> None:
        event = payload.get("event")
        controller_id = payload.get("controller_id")
        if not controller_id:
            return
        if event == "connect":
            self.queue.connect(controller_id)
        elif event == "heartbeat":
            self.queue.heartbeat(controller_id)
        elif event == "disconnect":
            self.queue.disconnect(controller_id)
        self._publish_control_status()

    def _handle_move(self, payload: dict) -> None:
        controller_id = payload.get("controller_id")
        if not self.queue.is_active(controller_id):
            return  # queued or stray controller — locked out
        if not self.robot_connected_provider():
            self.on_log("[MIDDLEMAN] Remote move ignored — no robot connected (demo)")
            return
        if self.move_executor:
            # move_executor's contract: returns None on success/accepted,
            # or an error string (e.g. a hard-deck rejection, or a real
            # hardware move error) on failure — relayed back to whoever
            # sent the command so it's not a silent failure on their end.
            error = self.move_executor(payload)
            if error:
                self._publish_error(controller_id, error)

    def _publish_error(self, controller_id, message: str) -> None:
        if self._client is None:
            return
        try:
            self._client.publish(self._error_topic,
                                  json.dumps({"controller_id": controller_id, "error": message}))
        except Exception as e:
            self.on_log(f"[MIDDLEMAN] Error-publish failed: {e}")

    def _handle_laser(self, payload: dict) -> None:
        controller_id = payload.get("controller_id")
        if not self.queue.is_active(controller_id):
            return
        if self.laser_executor:
            self.laser_executor(payload.get("channel"), bool(payload.get("laser")))

    def _handle_capture_request(self, payload: dict) -> None:
        controller_id = payload.get("controller_id")
        if not self.queue.is_active(controller_id):
            return
        if not self.capture_executor:
            return
        try:
            sample_id, frames, values = self.capture_executor()
            bundle = photo_transfer.build_photo_bundle(sample_id, frames, self.ip, values)
            self._client.publish(self._photo_topic, json.dumps(bundle))
        except Exception as e:
            self.on_log(f"[MIDDLEMAN] Capture-request error: {e}")

    # -- background loops --------------------------------------------

    def _discovery_payload(self, online: bool) -> dict:
        snapshot = self.queue.snapshot()
        return {
            "ip": self.ip,
            "name": self.name,
            "online": online,
            "active_controller": snapshot["active"],
            "queue_length": len(snapshot["queue"]),
        }

    def _discovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._client.publish(MIDDLEMAN_DISCOVERY_TOPIC,
                                      json.dumps(self._discovery_payload(online=True)))
            except Exception as e:
                self.on_log(f"[MIDDLEMAN] Discovery publish error: {e}")
            time.sleep(MIDDLEMAN_HEARTBEAT_INTERVAL_SECONDS)

    def _telemetry_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.telemetry_provider is not None:
                try:
                    data = self.telemetry_provider()
                    self._client.publish(self._telemetry_topic, json.dumps(data))
                except Exception as e:
                    self.on_log(f"[MIDDLEMAN] Telemetry publish error: {e}")
            time.sleep(MIDDLEMAN_HEARTBEAT_INTERVAL_SECONDS)

    def _timeout_watch_loop(self) -> None:
        while not self._stop_event.is_set():
            result = self.queue.check_timeout()
            if result is not None:
                # Active controller's heartbeat went stale. Agreed
                # behavior: let the current action finish (we can't
                # forcibly interrupt an in-flight robot move safely from
                # here), then stop/hold + laser off, then reflect the
                # promotion (or "nobody active") in the status broadcast.
                self.on_log("[MIDDLEMAN] Active controller timed out — "
                            "stopping/holding, laser off.")
                try:
                    if self.move_executor:
                        self.move_executor({"jog": "stop", "controller_id": None})
                except Exception as e:
                    self.on_log(f"[MIDDLEMAN] Stop-on-timeout error: {e}")
                try:
                    if self.laser_executor:
                        self.laser_executor(None, False)  # None = all configured channels off
                except Exception as e:
                    self.on_log(f"[MIDDLEMAN] Laser-off-on-timeout error: {e}")
                self._publish_control_status()
            time.sleep(0.5)

    def _publish_control_status(self) -> None:
        snapshot = self.queue.snapshot()
        status = {"ip": self.ip, "name": self.name, **snapshot}
        if self._client is not None:
            try:
                self._client.publish(self._control_status_topic, json.dumps(status))
            except Exception as e:
                self.on_log(f"[MIDDLEMAN] Control-status publish error: {e}")
        if self.on_control_status_change:
            self.on_control_status_change(status)
