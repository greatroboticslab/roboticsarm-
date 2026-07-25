"""
[STUB - runnable once messaging + storage are implemented]

Standalone process: subscribes to TOPIC_VISION_RESULT, writes the
classification result (and its images) to MongoDB via
vision.storage.mongo_client.

Run as its own process:
    python -m vision.services.logger_service
"""

from datetime import date

from vision.config import TOPIC_VISION_RESULT
from vision.messaging.subscriber import subscribe
from vision.storage.mongo_client import save_sample, save_image_record


def on_result(payload: dict) -> None:
    """payload expected shape: {"sample_id": str, "result": {...}}"""
    sample_id = payload["sample_id"]
    result = payload["result"]

    save_sample(sample_id, str(date.today()), result)

    for view in result.get("per_view", []):
        save_image_record(
            image_id=f'{sample_id}_{view["source"]}_{view["view_index"]}',
            sample_id=sample_id,
            image_path=view["image_path"],
            source=view["source"],
            view_index=view["view_index"],
        )


def main() -> None:
    subscribe(TOPIC_VISION_RESULT, on_result)


if __name__ == "__main__":
    main()
