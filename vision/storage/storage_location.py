"""
[WIRED] Permanent, repo-independent storage location for everything
this app writes to disk — images, CSV/JSON logs, Excel reports.

WHY THIS FILE EXISTS
---------------------
Before this, every write path (vision.camera.capture's old hardcoded
IMAGES_ROOT = "images/objects", vision.storage.csv_logger/excel_export/
json_logger's directories, vision.storage.package_export's old
hardcoded IMPORTED_IMAGES_ROOT, vision.services.photo_transfer's
save_root default) was a path relative to wherever the process's CWD
happened to be — normally the repo folder. That meant:

  (a) deleting/replacing the repo folder (a fresh `git pull` clone, or
      downloading a new zip to pick up an update) silently orphaned
      every photo/report ever captured — the data was never actually
      IN the repo, just sitting in a folder that happened to share a
      parent with it, and
  (b) the location wasn't configurable — you couldn't point it at an
      external drive or a synced folder without editing source.

This module fixes both: a single configurable ROOT, persisted in a
JSON file OUTSIDE the repo entirely (in the user's home directory), that
every storage-writing module now asks for its actual directory through,
resolved fresh on every call rather than cached at import — so changing
the location mid-run (via the Sync & Storage tab's "Storage Location"
panel) takes effect immediately, no restart needed.
"""

from __future__ import annotations

import json
import os

_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".roboticarmAI_storage_location.json")


def _default_storage_root() -> str:
    """~/Documents/roboticarmAI_data if ~/Documents exists (the common
    case on Windows/macOS, and plenty of Linux desktops), else
    ~/roboticarmAI_data. Computed once at import time into
    DEFAULT_STORAGE_ROOT below — whether ~/Documents exists doesn't
    change during a run, so there's no reason to re-check it on every
    get_storage_root() call."""
    home = os.path.expanduser("~")
    documents = os.path.join(home, "Documents")
    if os.path.isdir(documents):
        return os.path.join(documents, "roboticarmAI_data")
    return os.path.join(home, "roboticarmAI_data")


DEFAULT_STORAGE_ROOT = _default_storage_root()

_SUBDIRS = (
    os.path.join("images", "objects"),
    os.path.join("images", "middleman"),
    os.path.join("images", "imported"),
    os.path.join("data_logs", "csv"),
    os.path.join("data_logs", "excel"),
    os.path.join("data_logs", "json"),
)


def get_storage_root() -> str:
    """
    Reads the persisted root from _CONFIG_FILE, falling back to
    DEFAULT_STORAGE_ROOT if the config file is missing, unreadable, or
    corrupt — never raises, since a broken config file should degrade
    to "use the default," not crash every storage call in the app.
    Creates the directory if it doesn't exist yet, so every caller can
    assume the returned path is immediately usable.
    """
    root = DEFAULT_STORAGE_ROOT
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            candidate = data.get("storage_root")
            if candidate:
                root = candidate
    except (OSError, json.JSONDecodeError):
        pass  # corrupt/unreadable config file -> fall back to default, don't crash
    os.makedirs(root, exist_ok=True)
    return root


def set_storage_root(path: str) -> None:
    """
    Persists `path` as the new storage root (written to _CONFIG_FILE,
    outside the repo, so the setting survives deleting/replacing the
    repo folder) and pre-creates every subfolder the helpers below
    expect, so the very next capture/export after switching doesn't
    have to lazily create anything mid-operation.

    Does NOT move any existing files at the OLD location — deliberate:
    a partial copy/move that fails halfway is worse than doing nothing,
    and the old location stays fully reachable by pointing back at it.
    If you DO want to consolidate old + new locations, use the Sync &
    Storage tab's folder merge/sync tools (vision.storage.
    package_export.merge_packages()) explicitly, on your own terms.
    """
    path = os.path.abspath(path)
    os.makedirs(path, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"storage_root": path}, f, indent=2)
    for subdir in _SUBDIRS:
        os.makedirs(os.path.join(path, subdir), exist_ok=True)


def images_root() -> str:
    """Where local captures' per-object image folders live (images_root()/
    <object_id>/...). Replaces vision.camera.capture's old hardcoded
    IMAGES_ROOT = "images/objects"."""
    return os.path.join(get_storage_root(), "images", "objects")


def middleman_images_root() -> str:
    """Where a Middleman-received photo bundle gets written. Replaces
    vision.services.photo_transfer's old hardcoded save_root default of
    "images/middleman"."""
    return os.path.join(get_storage_root(), "images", "middleman")


def imported_images_root() -> str:
    """Where package_export.import_package() writes re-imported images.
    Replaces its old hardcoded IMPORTED_IMAGES_ROOT constant."""
    return os.path.join(get_storage_root(), "images", "imported")


def csv_log_dir() -> str:
    """Replaces vision.storage.csv_logger's old reliance on
    vision.config.CSV_LOG_DIR for the directory (CSV_LOG_FILENAME is
    still used for the filename — see vision/config.py)."""
    return os.path.join(get_storage_root(), "data_logs", "csv")


def excel_export_dir() -> str:
    """Replaces vision.storage.excel_export's old reliance on
    vision.config.EXCEL_EXPORT_DIR for the directory (EXCEL_EXPORT_FILENAME
    is still used for the filename)."""
    return os.path.join(get_storage_root(), "data_logs", "excel")


def json_log_dir() -> str:
    """Where vision.storage.json_logger's live append log (.jsonl) lives
    — same "own subfolder under the configured root" pattern as the
    CSV/Excel helpers above."""
    return os.path.join(get_storage_root(), "data_logs", "json")


def json_export_dir() -> str:
    """Where json_logger's on-demand regenerated full report(s) live."""
    return os.path.join(get_storage_root(), "data_logs", "json")
