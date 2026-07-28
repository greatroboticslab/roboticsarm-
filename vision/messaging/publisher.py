"""
[WIRED] MQTT publishing via paho-mqtt.

No hardware dependency - just needs a broker running. Easiest local setup:

    # Install Mosquitto (a free, lightweight MQTT broker)
    # Windows: https://mosquitto.org/download/
    # Mac:     brew install mosquitto && brew services start mosquitto
    # Linux:   sudo apt install mosquitto && sudo systemctl start mosquitto

    pip install paho-mqtt

Then MQTT_BROKER_HOST/PORT in vision/config.py ("localhost"/1883) will
work out of the box for a broker running on the same machine. If the
broker runs elsewhere (e.g. a separate server the arm machine talks to
over the network), just update those two values.
"""

from __future__ import annotations
import json
import threading

try:
    import paho.mqtt.client as mqtt
    _PAHO_AVAILABLE = True
except ImportError:
    _PAHO_AVAILABLE = False

from vision.config import (
    MQTT_BROKER_HOST,
    MQTT_BROKER_PORT,
    TOPIC_ARM_OBJECT_CAPTURED,
    TOPIC_VISION_RESULT,
    TOPIC_CAPTURE_STATUS,
)

_client = None
_lock = threading.Lock()


def _require_paho():
    if not _PAHO_AVAILABLE:
        raise ImportError("paho-mqtt is not installed. Run: pip install paho-mqtt")


def _get_client():
    global _client
    _require_paho()
    with _lock:
        if _client is None:
            _client = mqtt.Client()
            try:
                _client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
                _client.loop_start()  # background thread handles network I/O
            except (ConnectionRefusedError, OSError) as e:
                _client = None
                raise RuntimeError(
                    f"Could not connect to MQTT broker at "
                    f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}: {e}\n"
                    f"Make sure Mosquitto (or another broker) is running, "
                    f"or update MQTT_BROKER_HOST/PORT in vision/config.py."
                )
    return _client


def publish_captured(sample_id: str, views: list, station_pose: dict) -> None:
    """Announce that a new object has been captured and is ready for
    classification."""
    client = _get_client()
    payload = json.dumps({
        "sample_id": sample_id,
        "views": views,
        "station_pose": station_pose,
    })
    client.publish(TOPIC_ARM_OBJECT_CAPTURED, payload)


def publish_result(sample_id: str, result: dict) -> None:
    """Announce a classification result."""
    client = _get_client()
    payload = json.dumps({
        "sample_id": sample_id,
        "result": result,
    })
    client.publish(TOPIC_VISION_RESULT, payload)


def publish_capture_status(event: str, **fields) -> None:
    """Report progress/completion of an automated capture sequence
    started by a TOPIC_CAPTURE_COMMAND message from 4DAI.

    event: "started" | "image" | "completed" | "error"
    fields: whatever is relevant for that event, e.g.
        started:   category, num_images, degrees_per_step
        image:     category, sample_id, image_index, image_path
        completed: category, sample_id, image_paths, values
        error:     category, message
    """
    client = _get_client()
    payload = json.dumps({"event": event, **fields})
    client.publish(TOPIC_CAPTURE_STATUS, payload)


def disconnect() -> None:
    """Cleanly stop the client. Call on app shutdown."""
    global _client
    with _lock:
        if _client is not None:
            _client.loop_stop()
            _client.disconnect()
            _client = None


if __name__ == "__main__":
    print(f"Testing publish to broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} ...")
    publish_captured("test-sample", [{"source": "station", "view_index": 0,
                                       "image_path": "test.jpg", "pose": {}}],
                      {"x": 0, "y": 390, "z": 150})
    print(f"Published a test message to '{TOPIC_ARM_OBJECT_CAPTURED}'. "
          f"Subscribe to it (e.g. `mosquitto_sub -t {TOPIC_ARM_OBJECT_CAPTURED}`) "
          f"to confirm it arrived.")
    disconnect()
