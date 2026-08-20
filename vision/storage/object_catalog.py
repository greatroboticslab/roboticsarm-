"""
[WIRED] Object catalog — separate, auto-maintained "inventory" view.

WHY THIS FILE EXISTS
---------------------
The `objects` collection is a LOG: every capture is its own row,
forever, even if it's the same physical object photographed five
times. That's the correct default (see main.py Database tab: "Log"
view). This module adds an "Inventory" view on top, without changing
the log at all — it maintains a SEPARATE collection
(object_catalog, see mongo_client.py) that tracks distinct known
objects, and loosely links each new capture to one.

MATCHING RULE (honest limitation, by design)
---------------------------------------------
There is no real object re-identification model wired in yet
(vision.model.classifier.identify() is still a stub). So today, the
only thing this can match on is the object's NAME, normalized
(casefold + whitespace-collapsed), optionally narrowed by category
(see vision.config.CATALOG_MATCH_ON_CATEGORY). This WILL produce:
  - false negatives: a typo or rephrasing reads as a "new" object.
  - false positives: two different objects that happen to share a
    name/category get merged into one catalog entry.

To keep this from ever blocking the pipeline, matching is treated as a
*suggestion*, applied automatically but cheap to correct later:
  - match found  -> bump times_seen/last_seen, link the new capture.
  - no match     -> create a brand-new catalog entry.
A capture is NEVER left waiting on manual confirmation. If a match
turns out wrong, fix it by editing the catalog entry directly (or the
object's `name`/`category` and re-running the matcher) — nothing else
depends on the link being correct.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from vision.config import CATALOG_MATCH_ON_CATEGORY
from vision.storage import mongo_client


def _normalize(name: str) -> str:
    return " ".join((name or "").strip().casefold().split())


def match_or_create(object_id: str, name: str, category: str | None,
                     color: str | None, size: str | None,
                     captured_at: "datetime | None" = None) -> str:
    """
    Called once per capture (from capture_pipeline.record_capture).
    Finds an existing catalog entry by normalized name (+ category, if
    CATALOG_MATCH_ON_CATEGORY is True) and links this capture to it, or
    creates a new catalog entry if nothing matches. Returns the
    catalog_id either way, and also stamps it onto the object document.
    """
    captured_at = captured_at or datetime.now()
    normalized = _normalize(name)

    existing = mongo_client.find_catalog_entry_by_name(
        normalized, category=category if CATALOG_MATCH_ON_CATEGORY else None
    )

    if existing:
        catalog_id = existing["_id"]
        mongo_client.bump_catalog_entry(catalog_id, object_id, captured_at)
    else:
        catalog_id = str(uuid.uuid4())
        mongo_client.create_catalog_entry(
            catalog_id=catalog_id,
            name=name,
            normalized_name=normalized,
            category=category,
            color=color,
            size=size,
            object_id=object_id,
            captured_at=captured_at,
        )

    mongo_client.set_object_catalog_id(object_id, catalog_id)
    return catalog_id


def list_inventory(limit: int = 100) -> list:
    """Read-side query for the GUI's "Inventory" tab."""
    return mongo_client.list_catalog(limit=limit)
