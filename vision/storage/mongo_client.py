"""
[WIRED] Direct MongoDB access for the arm/vision pipeline.

Writes to the same "Collections" database 4DAI's Server/main.py uses
(see vision/config.py: MONGO_URI/MONGO_DB_NAME point at the same
defaults - "mongodb://localhost:27017" / "Collections"), so 4DAI's
Streamlit view_data.py can browse the "objects" category with no changes
on the 4DAI side, as long as both point at the same MongoDB instance.

This is deliberately the ONLY module that talks pymongo directly.
Everything else (session_manager, object_catalog, csv_logger,
excel_export, capture_pipeline, the NLP agent) calls into here rather
than opening its own connection, so there is exactly one place that
knows about collection names / connection details.

COLLECTIONS
-----------
  objects        - one document per capture (the "log"). _id = object_id.
  images         - one document per photo. _id = image_id, object_id = FK.
  sessions       - one document per calendar day. _id = "YYYY-MM-DD".
  object_catalog - one document per distinct *known* object (the
                   "inventory" view). _id = catalog_id. Populated/linked
                   automatically by vision.storage.object_catalog, never
                   written to directly by callers elsewhere.

SETUP
-----
    pip install pymongo

Make sure MongoDB is running (see 4DAI's README - `mongosh` should
connect successfully) before using this module.
"""

from __future__ import annotations

from datetime import datetime

try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure
    _PYMONGO_AVAILABLE = True
except ImportError:
    _PYMONGO_AVAILABLE = False

from vision.config import (
    MONGO_URI,
    MONGO_DB_NAME,
    MONGO_OBJECTS_COLLECTION,
    MONGO_IMAGES_COLLECTION,
    MONGO_SESSIONS_COLLECTION,
    MONGO_OBJECT_CATALOG_COLLECTION,
)

_client = None
_db = None


def _require_pymongo():
    if not _PYMONGO_AVAILABLE:
        raise ImportError("pymongo is not installed. Run: pip install pymongo")


def _get_db():
    global _client, _db
    _require_pymongo()
    if _db is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        try:
            _client.admin.command("ping")  # fail fast if Mongo isn't reachable
        except ConnectionFailure as e:
            _client = None
            raise RuntimeError(
                f"Could not reach MongoDB at {MONGO_URI}: {e}\n"
                f"Make sure `mongod` is running (see 4DAI's README), or "
                f"update MONGO_URI in vision/config.py."
            )
        _db = _client[MONGO_DB_NAME]
    return _db


# ===========================================================================
# OBJECTS (the capture log)
# ===========================================================================

def save_object(object_id: str, session_id: str, date_str: str, data: dict,
                 captured_at: "datetime | None" = None) -> None:
    """
    Write one capture document to the `objects` collection. `data` holds
    the fixed attribute columns (name/category/color/size/position/
    reserved_*) plus the freeform "attributes" dict — see
    vision.storage.attribute_schema for the column list, and
    vision.storage.capture_pipeline for the single place that should be
    assembling `data` and calling this.
    """
    db = _get_db()
    db[MONGO_OBJECTS_COLLECTION].insert_one({
        "_id": object_id,
        "session_id": session_id,
        "date": date_str,
        "captured_at": captured_at or datetime.now(),
        "data": data,
    })


def update_object_data(object_id: str, data: dict) -> None:
    """Overwrite the `data` sub-document for an existing object — used by
    excel_export.reconcile_from_excel() to pull hand-edited attribute
    values back into MongoDB."""
    db = _get_db()
    db[MONGO_OBJECTS_COLLECTION].update_one({"_id": object_id}, {"$set": {"data": data}})


def set_object_catalog_id(object_id: str, catalog_id: str) -> None:
    """Links a capture-log row to an object_catalog entry (see
    vision.storage.object_catalog). Nullable by design — a capture with
    no catalog_id just hasn't been matched/linked yet."""
    db = _get_db()
    db[MONGO_OBJECTS_COLLECTION].update_one(
        {"_id": object_id}, {"$set": {"catalog_id": catalog_id}}
    )


def list_recent_objects(limit: int = 30) -> list:
    """Read-side query for the GUI's "Objects" log view: most recent
    captures, newest first."""
    db = _get_db()
    cursor = (
        db[MONGO_OBJECTS_COLLECTION]
        .find({})
        .sort("captured_at", -1)
        .limit(limit)
    )
    return list(cursor)


def get_object(object_id: str) -> dict | None:
    """Single object document by id, for the click-to-view-details panel."""
    db = _get_db()
    return db[MONGO_OBJECTS_COLLECTION].find_one({"_id": object_id})


def find_objects(mongo_filter: dict, limit: int = 30) -> list:
    """
    Read-only query against the `objects` collection using a caller-
    supplied Mongo filter (e.g. one generated by
    vision.services.mongo_nlp_agent, or typed by hand). Newest first.

    Callers are responsible for validating/sanitizing `mongo_filter`
    before it reaches here — this function does not restrict operators.
    """
    db = _get_db()
    cursor = (
        db[MONGO_OBJECTS_COLLECTION]
        .find(mongo_filter)
        .sort("captured_at", -1)
        .limit(limit)
    )
    return list(cursor)


def object_recent_data_fields(limit: int = 50) -> list:
    """
    Read-side helper: union of the "data" dict keys seen across the most
    recent `limit` object documents. Used to tell a natural-language
    query model what fields actually exist to query against, without
    hardcoding a schema anywhere.
    """
    db = _get_db()
    cursor = (
        db[MONGO_OBJECTS_COLLECTION]
        .find({}, {"data": 1})
        .sort("captured_at", -1)
        .limit(limit)
    )
    fields = set()
    for doc in cursor:
        fields.update((doc.get("data") or {}).keys())
    return sorted(fields)


def all_objects_for_export(limit: int | None = None) -> list:
    """Read-side query for csv_logger/excel_export: every object
    document, oldest first (natural log order), optionally capped."""
    db = _get_db()
    cursor = db[MONGO_OBJECTS_COLLECTION].find({}).sort("captured_at", 1)
    if limit:
        cursor = cursor.limit(limit)
    return list(cursor)


def objects_for_session(session_id: str) -> list:
    """Every object captured under one session (day), oldest first — used
    for the default "today only" Excel export."""
    db = _get_db()
    cursor = db[MONGO_OBJECTS_COLLECTION].find({"session_id": session_id}).sort("captured_at", 1)
    return list(cursor)


def distinct_object_categories() -> list:
    """Every distinct, non-null `category` value seen in the `objects`
    collection — used to populate the Database tab's Category filter
    dropdown without hardcoding a category list anywhere."""
    db = _get_db()
    values = db[MONGO_OBJECTS_COLLECTION].distinct("data.category")
    return sorted(v for v in values if v not in (None, ""))


def distinct_object_colors() -> list:
    """Same as distinct_object_categories(), for `color`."""
    db = _get_db()
    values = db[MONGO_OBJECTS_COLLECTION].distinct("data.color")
    return sorted(v for v in values if v not in (None, ""))


def distinct_image_sources() -> list:
    """Every distinct, non-null `source` value seen in the `images`
    collection (e.g. "station", "wrist") — used to populate the
    Database tab's Images-view Source filter dropdown."""
    db = _get_db()
    values = db[MONGO_IMAGES_COLLECTION].distinct("source")
    return sorted(v for v in values if v not in (None, ""))


def delete_object(object_id: str) -> int:
    """Deletes an object doc and every image doc linked to it (does NOT
    delete the image files themselves from disk, and does NOT touch/
    decrement the object_catalog entry it was linked to — the catalog's
    times_seen count intentionally stays as a historical high-water
    mark rather than being rolled back, since "this was seen 3 times,
    one capture later got deleted" is a different fact than "this was
    only ever seen twice"; if you need exact-match inventory counts,
    treat the catalog as a summary rather than authoritative). Used by
    the Data Collection tab's "Delete" button for discarding a bad
    rotation-sequence capture without leaving Mongo/CSV/Excel out of
    sync with each other.

    Returns the number of image docs deleted (0 or more); raises
    nothing special if object_id doesn't exist — just deletes 0 docs.
    """
    db = _get_db()
    db[MONGO_OBJECTS_COLLECTION].delete_one({"_id": object_id})
    result = db[MONGO_IMAGES_COLLECTION].delete_many({"object_id": object_id})
    return result.deleted_count


# ===========================================================================
# IMAGES
# ===========================================================================

def save_image_record(image_id: str, object_id: str, image_path: str,
                       source: str, view_index: int, session_id: str = None,
                       captured_at: "datetime | None" = None) -> None:
    """Write an image document. `object_id` is the FK back to the
    `objects` collection (the capture this photo belongs to)."""
    db = _get_db()
    db[MONGO_IMAGES_COLLECTION].insert_one({
        "_id": image_id,
        "object_id": object_id,
        "session_id": session_id,
        "image_path": image_path,
        "source": source,
        "view_index": view_index,
        "captured_at": captured_at or datetime.now(),
    })


def get_images_for_object(object_id: str) -> list:
    """Read-side query: all image documents linked to one object_id, for
    displaying thumbnails in the GUI detail panel."""
    db = _get_db()
    cursor = db[MONGO_IMAGES_COLLECTION].find({"object_id": object_id})
    return list(cursor)


def list_recent_images(limit: int = 30) -> list:
    """Read-side query for the GUI's "Images" view: most recent photos,
    newest first, independent of which object they belong to."""
    db = _get_db()
    cursor = (
        db[MONGO_IMAGES_COLLECTION]
        .find({})
        .sort("captured_at", -1)
        .limit(limit)
    )
    return list(cursor)


def find_images(mongo_filter: dict, limit: int = 30) -> list:
    """
    Read-only query against the `images` collection using a caller-
    supplied Mongo filter — the images-collection counterpart to
    find_objects() above. Newest first.

    Same rule as find_objects(): this does NOT validate/sanitize
    `mongo_filter`. It's safe to call with a filter built from known,
    hardcoded field names (e.g. the GUI's filter bar, or the example
    below) or one that's already been through
    vision.services.mongo_nlp_agent's validator. Never pass raw,
    unvalidated user text straight in as (part of) the filter.

    EXAMPLE — "photos taken today":
        from datetime import datetime, time
        today_start = datetime.combine(datetime.now().date(), time.min)
        find_images({"captured_at": {"$gte": today_start}})

    (In practice you'd normally just filter on session_id instead, since
    session_id is already the calendar day as a string — e.g.
    find_images({"session_id": "2026-08-18"}) — the captured_at range
    version above is here to show the pattern for “between two
    timestamps” queries, e.g. "photos from the last hour".)
    """
    db = _get_db()
    cursor = (
        db[MONGO_IMAGES_COLLECTION]
        .find(mongo_filter)
        .sort("captured_at", -1)
        .limit(limit)
    )
    return list(cursor)


def distinct_object_values(field: str) -> list:
    """Distinct values for a single field.data.<field> across all objects
    — used to populate the GUI filter bar's dropdowns (e.g. every
    category/color that's actually been captured, instead of a
    hardcoded guess at what values exist)."""
    db = _get_db()
    values = db[MONGO_OBJECTS_COLLECTION].distinct(f"data.{field}")
    return sorted(v for v in values if v not in (None, "", UNKNOWN_PLACEHOLDER))


# ===========================================================================
# SESSIONS (one per calendar day)
# ===========================================================================

def upsert_session_started(session_id: str) -> None:
    """Creates the session doc if it doesn't exist yet; no-op otherwise.
    $setOnInsert means calling this on every capture is safe/cheap — it
    only actually writes on the first capture of a new day."""
    db = _get_db()
    db[MONGO_SESSIONS_COLLECTION].update_one(
        {"_id": session_id},
        {"$setOnInsert": {"_id": session_id, "started_at": datetime.now(), "ended_at": None}},
        upsert=True,
    )


def update_session_ended(session_id: str) -> None:
    db = _get_db()
    db[MONGO_SESSIONS_COLLECTION].update_one(
        {"_id": session_id}, {"$set": {"ended_at": datetime.now()}}
    )


def list_sessions(limit: int = 30) -> list:
    db = _get_db()
    cursor = db[MONGO_SESSIONS_COLLECTION].find({}).sort("_id", -1).limit(limit)
    return list(cursor)


# ===========================================================================
# OBJECT CATALOG (the "inventory" view — see vision.storage.object_catalog
# for the matching/linking logic; this section is just the raw CRUD).
# ===========================================================================

def find_catalog_entry_by_name(normalized_name: str, category: str | None = None) -> dict | None:
    db = _get_db()
    query = {"normalized_name": normalized_name}
    if category:
        query["category"] = category
    return db[MONGO_OBJECT_CATALOG_COLLECTION].find_one(query)


def create_catalog_entry(catalog_id: str, name: str, normalized_name: str,
                          category: str | None, color: str | None, size: str | None,
                          object_id: str, captured_at: datetime) -> None:
    db = _get_db()
    db[MONGO_OBJECT_CATALOG_COLLECTION].insert_one({
        "_id": catalog_id,
        "name": name,
        "normalized_name": normalized_name,
        "category": category,
        "color": color,
        "size": size,
        "first_seen": captured_at,
        "last_seen": captured_at,
        "times_seen": 1,
        "linked_object_ids": [object_id],
    })


def bump_catalog_entry(catalog_id: str, object_id: str, captured_at: datetime) -> None:
    db = _get_db()
    db[MONGO_OBJECT_CATALOG_COLLECTION].update_one(
        {"_id": catalog_id},
        {
            "$set": {"last_seen": captured_at},
            "$inc": {"times_seen": 1},
            "$push": {"linked_object_ids": object_id},
        },
    )


def list_catalog(limit: int = 100) -> list:
    """Read-side query for the GUI's "Inventory" view: distinct known
    objects, most recently seen first."""
    db = _get_db()
    cursor = db[MONGO_OBJECT_CATALOG_COLLECTION].find({}).sort("last_seen", -1).limit(limit)
    return list(cursor)


# ===========================================================================
# BACK-COMPAT ALIASES
#
# vision/services/logger_service.py and older code referred to these
# names before the objects/images collections gained session_id /
# captured_at / catalog linking. Kept as thin wrappers (not deleted) so
# nothing importing the old names breaks; new code should prefer
# vision.storage.capture_pipeline.record_capture() instead of calling
# these directly.
# ===========================================================================

def save_sample(sample_id: str, date_str: str, data: dict) -> None:
    save_object(sample_id, session_id=None, date_str=date_str, data=data)


def list_recent_samples(limit: int = 30) -> list:
    return list_recent_objects(limit=limit)


def get_images_for_sample(sample_id: str) -> list:
    return get_images_for_object(sample_id)


def find_samples(mongo_filter: dict, limit: int = 30) -> list:
    return find_objects(mongo_filter, limit=limit)


def sample_recent_data_fields(limit: int = 50) -> list:
    return object_recent_data_fields(limit=limit)


if __name__ == "__main__":
    print(f"Testing connection to MongoDB at {MONGO_URI} ...")
    db = _get_db()
    print(f"Connected. Using database '{MONGO_DB_NAME}', collections "
          f"'{MONGO_OBJECTS_COLLECTION}', '{MONGO_IMAGES_COLLECTION}', "
          f"'{MONGO_SESSIONS_COLLECTION}', '{MONGO_OBJECT_CATALOG_COLLECTION}'.")
