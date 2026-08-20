"""
[STUB - runnable once messaging + model are implemented]

Standalone process: subscribes to TOPIC_VISION_RESULT, and hands the
classification result off to vision.storage.capture_pipeline.
record_capture() — the SAME function the GUI's manual capture button
calls (see main.py capture_photo_and_store()) — so there is exactly one
definition of "what happens when a capture is saved" regardless of
which path triggered it. See capture_pipeline.py's module docstring for
the full save sequence (session -> Mongo -> catalog -> CSV -> Excel).

Run as its own process:
    python -m vision.services.logger_service
"""

from vision.config import TOPIC_VISION_RESULT
from vision.messaging.subscriber import subscribe
from vision.storage.capture_pipeline import record_capture


def on_result(payload: dict) -> None:
    """payload expected shape: {"sample_id": str, "result": {...}}
    where result is vision.model.fusion.classify_multi_source()'s
    return shape ({"predicted_label", "vote_scores", "attributes",
    "per_view": [...]})."""
    result = payload["result"]

    image_paths_by_source = {}
    for view in result.get("per_view", []):
        # per_view can have multiple entries per source (multiple
        # view_index steps) — keep the last one per source under a
        # disambiguated key so none get silently overwritten.
        key = f'{view["source"]}_{view["view_index"]}'
        image_paths_by_source[key] = view["image_path"]

    object_id, warnings = record_capture(
        name=result.get("predicted_label") or "unknown",
        image_paths_by_source=image_paths_by_source,
        attributes=result.get("attributes", {}),
    )
    for w in warnings:
        print(f"[LOGGER_SERVICE] {w}")


def main() -> None:
    subscribe(TOPIC_VISION_RESULT, on_result)


if __name__ == "__main__":
    main()
