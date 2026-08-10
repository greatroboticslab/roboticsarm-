"""
[WIRED] Multi-source/multi-view result fusion.

This logic itself has no hardware dependency - it only calls
vision.model.classifier.identify(), which is currently a stub. Once
identify() is implemented, this file works with no changes.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import List, Dict

from vision.model.classifier import identify


@dataclass
class ViewResult:
    source: str          # "wrist" or "station"
    view_index: int
    image_path: str
    label: str
    confidence: float
    pose: dict            # robot joint/cartesian state at capture time


# Station camera trusted more (controlled lighting/distance/no motion blur).
SOURCE_WEIGHTS = {"wrist": 1.0, "station": 1.5}


def classify_multi_source(views: List[dict]) -> Dict:
    """
    Run identify() on every captured view and combine the results by a
    weighted vote.

    Args:
        views: list of dicts, each shaped like:
            {"source": "station"|"wrist", "view_index": int,
             "image_path": str, "pose": dict}

    Returns:
        {
            "predicted_label": str,
            "vote_scores": {label: score, ...},
            "per_view": [ViewResult-as-dict, ...],
        }
    """
    results: List[ViewResult] = []

    for view in views:
        label, confidence = identify(view["image_path"])
        results.append(ViewResult(
            source=view["source"],
            view_index=view["view_index"],
            image_path=view["image_path"],
            label=label,
            confidence=confidence,
            pose=view["pose"],
        ))

    tally: Dict[str, float] = {}
    for r in results:
        weight = SOURCE_WEIGHTS.get(r.source, 1.0)
        tally[r.label] = tally.get(r.label, 0.0) + r.confidence * weight

    best_label = max(tally, key=tally.get) if tally else None

    return {
        "predicted_label": best_label,
        "vote_scores": tally,
        "per_view": [asdict(r) for r in results],
    }
