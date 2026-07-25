"""
[WIRED] Direct MongoDB access for the arm/vision pipeline.

Writes to the same "Collections" database 4DAI's Server/main.py uses
(see vision/config.py: MONGO_URI/MONGO_DB_NAME point at the same
defaults - "mongodb://localhost:27017" / "Collections"), so 4DAI's
Streamlit view_data.py can browse the "objects" category with no changes
on the 4DAI side, as long as both point at the same MongoDB instance.

SETUP
-----
    pip install pymongo

Make sure MongoDB is running (see 4DAI's README - `mongosh` should
connect successfully) before using this module.
"""

from __future__ import annotations

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


def save_sample(sample_id: str, date_str: str, data: dict) -> None:
    """
    Write a classification result document, matching 4DAI's existing
    sample document shape ({_id, date, data}).
    """
    db = _get_db()
    db[MONGO_OBJECTS_COLLECTION].insert_one({
        "_id": sample_id,
        "date": date_str,
        "data": data,
    })


def save_image_record(image_id: str, sample_id: str, image_path: str,
                       source: str, view_index: int) -> None:
    """
    Write an image document, matching 4DAI's existing "images" collection
    shape, extended with source/view_index metadata so multi-view/
    multi-camera captures can be told apart later.
    """
    db = _get_db()
    db[MONGO_IMAGES_COLLECTION].insert_one({
        "_id": image_id,
        "sample_id": sample_id,
        "image_path": image_path,
        "source": source,
        "view_index": view_index,
    })


def list_recent_samples(limit: int = 30) -> list:
    """
    Read-side query for the GUI gallery panel: most recent "objects"
    samples, newest first. Returns 4DAI's standard sample document shape
    ({_id, date, data}).
    """
    db = _get_db()
    cursor = (
        db[MONGO_OBJECTS_COLLECTION]
        .find({})
        .sort("date", -1)
        .limit(limit)
    )
    return list(cursor)


def get_images_for_sample(sample_id: str) -> list:
    """
    Read-side query: all image documents linked to one sample_id, for
    displaying thumbnails in the GUI gallery panel.
    """
    db = _get_db()
    cursor = db[MONGO_IMAGES_COLLECTION].find({"sample_id": sample_id})
    return list(cursor)


if __name__ == "__main__":
    print(f"Testing connection to MongoDB at {MONGO_URI} ...")
    db = _get_db()
    print(f"Connected. Using database '{MONGO_DB_NAME}', "
          f"collections '{MONGO_OBJECTS_COLLECTION}' and '{MONGO_IMAGES_COLLECTION}'.")
