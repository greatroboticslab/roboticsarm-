"""
[STUB - runnable once messaging + model are implemented]

Standalone process: subscribes to TOPIC_ARM_OBJECT_CAPTURED, runs fusion
classification across all views of a captured object, publishes the
result to TOPIC_VISION_RESULT.

Run as its own process, separate from main.py's GUI:
    python -m vision.services.vision_service

Does nothing until vision.messaging.subscriber.subscribe() and
vision.model.classifier.identify() are implemented - both currently
raise NotImplementedError, which will surface immediately on run rather
than fail silently.
"""

from vision.config import TOPIC_ARM_OBJECT_CAPTURED
from vision.messaging.subscriber import subscribe
from vision.messaging.publisher import publish_result
from vision.model.fusion import classify_multi_source


def on_captured(payload: dict) -> None:
    """payload expected shape: {"sample_id": str, "views": [...], "station_pose": {...}}"""
    result = classify_multi_source(payload["views"])
    publish_result(payload["sample_id"], result)


def main() -> None:
    subscribe(TOPIC_ARM_OBJECT_CAPTURED, on_captured)


if __name__ == "__main__":
    main()
