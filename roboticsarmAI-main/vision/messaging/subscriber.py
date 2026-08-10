"""
[WIRED] MQTT subscribing via paho-mqtt.

Backs vision/services/vision_service.py and
vision/services/logger_service.py - each subscribes to one topic and
calls a handler function per message. See publisher.py for broker setup
notes (same broker, same install).
"""

from __future__ import annotations
import json

try:
    import paho.mqtt.client as mqtt
    _PAHO_AVAILABLE = True
except ImportError:
    _PAHO_AVAILABLE = False

from vision.config import MQTT_BROKER_HOST, MQTT_BROKER_PORT


def _require_paho():
    if not _PAHO_AVAILABLE:
        raise ImportError("paho-mqtt is not installed. Run: pip install paho-mqtt")


def subscribe(topic: str, on_message) -> None:
    """
    Subscribe to `topic`, calling on_message(payload_dict) for each
    message received. Blocks forever - intended to be run as its own
    process (see vision/services/*), not called from the GUI's thread.
    """
    _require_paho()

    def _on_connect(client, userdata, flags, rc):
        if rc == 0:
            print(f"[MQTT] Connected, subscribing to '{topic}'")
            client.subscribe(topic)
        else:
            print(f"[MQTT] Connection failed with code {rc}")

    def _on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[MQTT] Could not decode message on '{msg.topic}': {e}")
            return
        try:
            on_message(payload)
        except Exception as e:
            # A bad message/handler shouldn't kill the whole subscriber loop.
            print(f"[MQTT] Handler raised an error for '{msg.topic}': {e}")

    client = mqtt.Client()
    client.on_connect = _on_connect
    client.on_message = _on_message

    try:
        client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
    except (ConnectionRefusedError, OSError) as e:
        raise RuntimeError(
            f"Could not connect to MQTT broker at "
            f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}: {e}\n"
            f"Make sure Mosquitto (or another broker) is running."
        )

    client.loop_forever()  # blocks - this IS the service's main loop
