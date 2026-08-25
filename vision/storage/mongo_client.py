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

from datetime import datetime, timedelta

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


def list_recent_objects(limit: int = 30, sort_ascending: bool = False) -> list:
    """Read-side query for the GUI's "Objects" log view: most recent
    captures, newest first by default — pass sort_ascending=True for
    oldest first."""
    db = _get_db()
    cursor = (
        db[MONGO_OBJECTS_COLLECTION]
        .find({})
        .sort("captured_at", 1 if sort_ascending else -1)
        .limit(limit)
    )
    return list(cursor)


def get_object(object_id: str) -> dict | None:
    """Single object document by id, for the click-to-view-details panel."""
    db = _get_db()
    return db[MONGO_OBJECTS_COLLECTION].find_one({"_id": object_id})


def find_objects(mongo_filter: dict, limit: int = 30, sort_ascending: bool = False) -> list:
    """
    Read-only query against the `objects` collection using a caller-
    supplied Mongo filter (e.g. one generated by
    vision.services.mongo_nlp_agent, or typed by hand). Newest first by
    default — pass sort_ascending=True for oldest first (e.g. the
    Objects tab's sort-order toggle).

    Callers are responsible for validating/sanitizing `mongo_filter`
    before it reaches here — this function does not restrict operators.
    """
    db = _get_db()
    cursor = (
        db[MONGO_OBJECTS_COLLECTION]
        .find(mongo_filter)
        .sort("captured_at", 1 if sort_ascending else -1)
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


def objects_in_date_range(start_date: str, end_date: str) -> list:
    """
    Every object captured between start_date and end_date (inclusive),
    both given as "YYYY-MM-DD" strings — oldest first. Filters on
    `captured_at` directly (start of start_date's day through the very
    end of end_date's day) rather than session_id, so a range spanning
    more than one session/day works correctly regardless of session
    boundaries. Used by the Database tab's / Data Package's "Export by
    Date Range" pickers, and by json_logger/excel_export's date-range
    export mode.

    Raises ValueError if either date string isn't in "YYYY-MM-DD" form,
    or if start_date is after end_date — so the caller can show a clear
    message instead of silently returning zero rows.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError(f"Start date ({start_date}) is after end date ({end_date}).")
    end_dt = end_dt + timedelta(days=1) - timedelta(microseconds=1)  # end of that day, inclusive

    db = _get_db()
    cursor = (
        db[MONGO_OBJECTS_COLLECTION]
        .find({"captured_at": {"$gte": start_dt, "$lte": end_dt}})
        .sort("captured_at", 1)
    )
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
    """
    Deletes an object doc and every image doc linked to it (does NOT
    delete the image files themselves from disk). ALSO pulls object_id
    out of its catalog entry's linked_object_ids (if it had one) — this
    part is not optional: linked_object_ids is what the Inventory
    viewer actually walks to go fetch images, so leaving a deleted
    object_id in there means that catalog entry silently shows ZERO
    images from then on (it dutifully tries to load images for an
    object_id that no longer exists, finds none, and gives up) even
    though the catalog entry itself still shows up in the list with a
    nonzero times_seen. This was the #1 cause of "Inventory isn't
    showing images" after any capture had been deleted.

    times_seen is deliberately left untouched — "this was seen 3 times,
    one capture later got deleted" is a different fact than "this was
    only ever seen twice", and times_seen is meant to stay a historical
    high-water mark rather than being rolled back. It's specifically
    linked_object_ids (which images to actually try to load) that has
    to stay accurate, not the seen-count.

    Used by the Objects/Images tabs' "Delete Selected" buttons for
    discarding a bad capture without leaving Mongo/CSV/Excel — or the
    Inventory view — out of sync with each other.

    Returns the number of image docs deleted (0 or more); raises
    nothing special if object_id doesn't exist — just deletes 0 docs.
    """
    db = _get_db()
    obj = db[MONGO_OBJECTS_COLLECTION].find_one({"_id": object_id}, {"catalog_id": 1})
    db[MONGO_OBJECTS_COLLECTION].delete_one({"_id": object_id})
    result = db[MONGO_IMAGES_COLLECTION].delete_many({"object_id": object_id})

    catalog_id = obj.get("catalog_id") if obj else None
    if catalog_id:
        db[MONGO_OBJECT_CATALOG_COLLECTION].update_one(
            {"_id": catalog_id}, {"$pull": {"linked_object_ids": object_id}}
        )

    return result.deleted_count


def delete_image(image_id: str) -> bool:
    """Deletes ONE image doc only, leaving the object it belongs to
    (and that object's other images) untouched — for discarding a
    single bad/duplicate/blurry photo without losing the rest of that
    object's capture. Used by the Images tab's "Delete Selected"
    button. Does NOT delete the file from disk (same reasoning as
    delete_object() above). Returns True if a document was actually
    deleted, False if image_id didn't exist."""
    db = _get_db()
    result = db[MONGO_IMAGES_COLLECTION].delete_one({"_id": image_id})
    return result.deleted_count > 0


def count_objects(mongo_filter: dict) -> int:
    """Count of `objects` documents matching a filter, without actually
    fetching them — used by the Database tab's bulk-delete "Preview
    Count" button so you can see how many captures a filter matches
    BEFORE deleting anything. Same caller-responsible-for-validating-
    the-filter rule as find_objects()."""
    db = _get_db()
    return db[MONGO_OBJECTS_COLLECTION].count_documents(mongo_filter)


def delete_objects_bulk(mongo_filter: dict) -> tuple:
    """
    Bulk version of delete_object() — deletes every `objects` document
    matching `mongo_filter`, plus every `images` document linked to any
    of them. ALSO pulls every deleted object_id out of whichever
    catalog entries reference them (same reasoning as delete_object():
    linked_object_ids has to stay accurate, since that's what the
    Inventory viewer actually walks to fetch images — leaving dead
    object_ids in there is what makes a catalog entry silently show
    zero images despite a nonzero times_seen). times_seen is left
    untouched, same as delete_object(). Meant for clearing out old
    captures from a previous experiment/test run in one go, e.g.
    filtered by session_id, a date range on captured_at, or any other
    known field.

    This function never touches the filesystem — it only deletes
    MongoDB documents. It returns the on-disk image_path of every
    deleted image document so the CALLER can optionally also delete
    those files (the Sync & Storage tab's "Cleanup" section offers this
    as an opt-in checkbox, since removing files from disk is
    irreversible and shouldn't happen silently as a side effect of a
    Mongo cleanup).

    Same rule as find_objects()/delete_object(): callers are
    responsible for validating/sanitizing `mongo_filter` before it
    reaches here (e.g. via vision.storage.query_safety.validate_filter)
    — this function does not restrict operators itself, and an empty
    filter ({}) would match (and delete) EVERY object, so callers
    should treat an empty/near-empty filter as something to confirm
    loudly with the user first.

    Returns (objects_deleted, images_deleted, image_paths) where
    image_paths is the list of image_path strings belonging to every
    deleted image document (may contain duplicates only if the same
    path was logged more than once, which shouldn't normally happen).
    """
    db = _get_db()
    matching_docs = list(
        db[MONGO_OBJECTS_COLLECTION].find(mongo_filter, {"_id": 1, "catalog_id": 1})
    )
    matching_ids = [d["_id"] for d in matching_docs]
    if not matching_ids:
        return 0, 0, []

    image_docs = list(
        db[MONGO_IMAGES_COLLECTION].find(
            {"object_id": {"$in": matching_ids}}, {"image_path": 1}
        )
    )
    image_paths = [d.get("image_path") for d in image_docs if d.get("image_path")]

    images_result = db[MONGO_IMAGES_COLLECTION].delete_many({"object_id": {"$in": matching_ids}})
    objects_result = db[MONGO_OBJECTS_COLLECTION].delete_many({"_id": {"$in": matching_ids}})

    # Clean up catalog linkage for every deleted object, grouped by
    # catalog entry so each entry gets one $pull with every one of its
    # now-deleted object_ids at once, rather than one update per object.
    by_catalog: dict = {}
    for d in matching_docs:
        catalog_id = d.get("catalog_id")
        if catalog_id:
            by_catalog.setdefault(catalog_id, []).append(d["_id"])
    for catalog_id, object_ids in by_catalog.items():
        db[MONGO_OBJECT_CATALOG_COLLECTION].update_one(
            {"_id": catalog_id}, {"$pull": {"linked_object_ids": {"$in": object_ids}}}
        )

    return objects_result.deleted_count, images_result.deleted_count, image_paths


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


def list_recent_images(limit: int = 30, sort_ascending: bool = False) -> list:
    """Read-side query for the GUI's "Images" view: most recent photos,
    newest first by default, independent of which object they belong to
    — pass sort_ascending=True for oldest first."""
    db = _get_db()
    cursor = (
        db[MONGO_IMAGES_COLLECTION]
        .find({})
        .sort("captured_at", 1 if sort_ascending else -1)
        .limit(limit)
    )
    return list(cursor)


def find_images(mongo_filter: dict, limit: int = 30, sort_ascending: bool = False) -> list:
    """
    Read-only query against the `images` collection using a caller-
    supplied Mongo filter — the images-collection counterpart to
    find_objects() above. Newest first by default — pass
    sort_ascending=True for oldest first.

    Same rule as find_objects(): this does NOT validate/sanitize
    `mongo_filter`. It's safe to call with a filter built from known,
    hardcoded field names (e.g. the GUI's filter bar, or the example
    below) or one that's already been through
    vision.services.mongo_nlp_agent's validator. Never pass raw,
    unvalidated user text straight in as (part of) the filter.

    EXAMPLE — "photos taken today":
        from vision.storage import session_manager
        find_images({"session_id": session_manager.today_session_id()})

    (session_id is already the calendar day as a string, e.g.
    "2026-08-20", so this is the simplest "today" query. For "between
    two arbitrary timestamps" instead — e.g. "photos from the last
    hour" — filter on captured_at directly:
        from datetime import datetime, timedelta
        find_images({"captured_at": {"$gte": datetime.now() - timedelta(hours=1)}})
    )
    """
    db = _get_db()
    cursor = (
        db[MONGO_IMAGES_COLLECTION]
        .find(mongo_filter)
        .sort("captured_at", 1 if sort_ascending else -1)
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


def get_catalog_entry(catalog_id: str) -> dict | None:
    """Direct single-entry lookup by _id — used by the Inventory tab's
    detail viewer instead of fetching list_catalog(limit=N) and
    linear-searching for a match, which could miss the entry entirely
    if it happened to fall outside whatever N was used (e.g. an older
    entry, sorted by last_seen, once there are more than N catalog
    entries total). Returns None if catalog_id doesn't exist."""
    db = _get_db()
    return db[MONGO_OBJECT_CATALOG_COLLECTION].find_one({"_id": catalog_id})


def repair_catalog_links() -> dict:
    """
    One-time REPAIR for catalog entries that already went stale before
    delete_object()/delete_objects_bulk() started keeping
    linked_object_ids in sync (see those functions' docstrings) — those
    two only prevent NEW corruption; anything deleted before that fix
    existed is still sitting there as a dangling reference. This walks
    every catalog entry, drops any linked_object_ids that no longer
    point at a real object, and — if that leaves the entry with fewer
    linked objects than its recorded times_seen — leaves times_seen
    alone regardless (same "historical high-water mark, not a live
    count" reasoning as everywhere else).

    Returns {"entries_checked": int, "entries_fixed": int,
    "dangling_links_removed": int}. Safe to run any time, repeatedly —
    a fully-healthy catalog is a no-op.
    """
    db = _get_db()
    entries = list(db[MONGO_OBJECT_CATALOG_COLLECTION].find({}))
    entries_fixed = 0
    dangling_removed = 0

    for entry in entries:
        linked_ids = entry.get("linked_object_ids", []) or []
        if not linked_ids:
            continue
        existing_ids = {
            d["_id"] for d in db[MONGO_OBJECTS_COLLECTION].find(
                {"_id": {"$in": linked_ids}}, {"_id": 1}
            )
        }
        still_valid = [oid for oid in linked_ids if oid in existing_ids]
        removed_here = len(linked_ids) - len(still_valid)
        if removed_here:
            db[MONGO_OBJECT_CATALOG_COLLECTION].update_one(
                {"_id": entry["_id"]}, {"$set": {"linked_object_ids": still_valid}}
            )
            entries_fixed += 1
            dangling_removed += removed_here

    return {
        "entries_checked": len(entries),
        "entries_fixed": entries_fixed,
        "dangling_links_removed": dangling_removed,
    }


def update_catalog_entry(catalog_id: str, fields: dict) -> None:
    """Overwrite one or more top-level fields (name/category/color/size)
    on an existing catalog entry — used by the Data Collection tab's
    Inventory Attribute Review to fill in category/color/size that
    weren't known when the entry was auto-created. Does NOT touch
    linked_object_ids/times_seen/first_seen/last_seen — those are only
    ever changed by match_or_create()/bump_catalog_entry() or
    merge_catalog_entries() below."""
    db = _get_db()
    db[MONGO_OBJECT_CATALOG_COLLECTION].update_one({"_id": catalog_id}, {"$set": fields})


def merge_catalog_entries(keep_catalog_id: str, merge_catalog_id: str) -> int:
    """
    Folds `merge_catalog_id` into `keep_catalog_id`: every object
    linked to the entry being merged away gets re-pointed
    (`catalog_id` field on its object doc) to the surviving entry, the
    surviving entry's linked_object_ids/times_seen absorb them, and the
    now-empty catalog entry is deleted.

    Exists because object_catalog.match_or_create()'s auto-matching is
    exact-name-based (see that module's docstring) and WILL sometimes
    split what's really one physical object into two catalog entries
    (a typo, a rephrasing, category on vs off). This is the manual
    fix — "ensure multiple objects/captures can be grouped under one
    inventory entry" even when the automatic matching missed.

    Returns the number of objects re-pointed. Raises ValueError if
    either catalog_id doesn't exist, or if they're the same id.
    """
    if keep_catalog_id == merge_catalog_id:
        raise ValueError("Can't merge a catalog entry into itself.")

    db = _get_db()
    keep_entry = db[MONGO_OBJECT_CATALOG_COLLECTION].find_one({"_id": keep_catalog_id})
    merge_entry = db[MONGO_OBJECT_CATALOG_COLLECTION].find_one({"_id": merge_catalog_id})
    if keep_entry is None:
        raise ValueError(f"No catalog entry found with id {keep_catalog_id}.")
    if merge_entry is None:
        raise ValueError(f"No catalog entry found with id {merge_catalog_id}.")

    merged_object_ids = merge_entry.get("linked_object_ids", []) or []

    db[MONGO_OBJECTS_COLLECTION].update_many(
        {"_id": {"$in": merged_object_ids}}, {"$set": {"catalog_id": keep_catalog_id}}
    )

    combined_times_seen = (keep_entry.get("times_seen", 0) or 0) + (merge_entry.get("times_seen", 0) or 0)
    first_seen = min(
        [d for d in (keep_entry.get("first_seen"), merge_entry.get("first_seen")) if d is not None],
        default=None,
    )
    last_seen = max(
        [d for d in (keep_entry.get("last_seen"), merge_entry.get("last_seen")) if d is not None],
        default=None,
    )

    db[MONGO_OBJECT_CATALOG_COLLECTION].update_one(
        {"_id": keep_catalog_id},
        {
            "$set": {"times_seen": combined_times_seen, "first_seen": first_seen, "last_seen": last_seen},
            "$push": {"linked_object_ids": {"$each": merged_object_ids}},
        },
    )
    db[MONGO_OBJECT_CATALOG_COLLECTION].delete_one({"_id": merge_catalog_id})

    return len(merged_object_ids)


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
