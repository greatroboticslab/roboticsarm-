# 4DAI Server Update — README

This covers what changed on the **server side** (`main.py` + the Streamlit
pages) since the robotic arm's camera was fully handed off to 4DAI, and how
to run everything.

## What changed

### Camera handoff
The robotic arm no longer owns any camera hardware. When it wants a photo
taken, it POSTs a trigger to the server (`/collection/trigger-webcam-capture`).
4DAI's Streamlit UI polls for that trigger and takes the photo itself, using
the browser's webcam — the arm and server never touch a camera directly.

### New endpoint: standalone auto-capture save
`POST /collection/auto-capture-image` — saves a photo taken automatically in
response to an arm trigger. This is intentionally standalone: no sample ID,
no Mongo record, no link to the submission form — just "take a photo, save
it to disk" with a timestamped filename. Filenames are checked for
collisions and disambiguated (`_1`, `_2`, ...) instead of silently
overwriting if two captures land in the same second.

### Arm-trigger listener on every page
Previously only `home.py` polled for arm triggers, so a capture request sent
while you were on Settings/RoboFlow/View Collections/Google Colab would sit
queued and never get acted on. Every page now polls
`/collection/check-trigger` once a second and switches to the Collection
page if a trigger comes in — this also has the side effect of requesting
webcam permission (mounting the camera widget is what makes the browser
show its native permission prompt).

### Collection page — Automatic + Manual capture
- **Automatic Capture**: driven by the arm-trigger toggle. When a trigger
  arrives and the toggle is on, a bit of JS clicks the camera shutter for
  you; the resulting photo is saved immediately via the new endpoint above.
- **Manual Photo Capture**: a separate camera widget below it — click it
  yourself anytime to attach a photo to the current sample submission
  (the classic flow).
- **Camera/Kinect per-category gating is currently disabled.** Both
  sections always render and request webcam access, regardless of the
  category's "Use of Kinect camera" setting in Settings. The old gate is
  commented out in `collection.py` (`if True:  # was: if camera:`) so it
  can be restored with a one-line change later.

### Bug fixes
- `collection.py`: the "camera" setting used to be checked as a Python
  bool, but was stored as the *string* `"True"`/`"False"` — since any
  non-empty string is truthy, the camera widget used to show up
  regardless of the setting. Fixed (before the gate was disabled above).
- `collection.py`: the RoboFlow image-upload loop could reuse a stale
  `image_id` from the previous photo, or crash with `NameError`, if an
  image upload failed. Now skips that photo cleanly with an error message.
- `settings.py`: editing an existing category used to silently reset its
  camera setting back to `False` unless you manually reselected it, and
  a `.index()` lookup on an unrecognized prompt type could crash the
  whole Edit Category page.
- `main.py`: added path-traversal protection (`safe_filename`) and Mongo
  collection-name validation (`safe_collection_name`) across every
  endpoint that builds a file path or database collection name from
  user input. `/collection/submission` now validates required fields
  instead of raising an unhandled `KeyError`.
- `main.py`: `GET /collection/image/{image_id}` now returns a proper
  `404` for a missing/deleted image instead of a `200` with an error
  body, which the frontend would otherwise try to render as an image.
- `roboflow.py`: fixed an uninitialized `success_count`, a duplicated
  upload POST per image, and an undefined `img_id` reference in the
  "Upload Selected Images to RoboFlow" flow.
- `vision/config.py` (arm side): removed duplicate MQTT topic
  definitions that were silently overriding each other.

## Planned restructuring (not yet done)

Production is currently frozen while known issues (see
`KNOWN_ISSUES.md`) get worked through. When that's lifted, the server
code is planned to move into its own folder — likely `server-4dai/` or
an `mqtt/` folder depending on how the MQTT-based pieces get organized
alongside it. This README will need its paths updated at that point (see
the "Updating this guide for the GitHub branch" note below); nothing in
this document assumes a specific folder name today, so treat any bare
`main.py`/`Server/` references above as relative to wherever that folder
ends up living.

## How to run

1. **Start MongoDB**
   ```bash
   mongosh   # confirm it connects; Ctrl+D to exit
   ```

2. **Start the FastAPI server**
   ```bash
   cd Server
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
   Leave this running — both the Streamlit UI and the robotic arm talk to it.

3. **Start the Streamlit UI**
   ```bash
   cd UI
   streamlit run home.py
   ```
   Opens at `http://localhost:8501`. Check `key.py` points at the right
   `URL` (`http://localhost:8000` locally, or your ngrok URL if the arm is
   on a separate machine).

4. **Grant camera permission once, up front.** Open any category's
   Collection page and let the browser prompt for webcam access before
   relying on automatic capture — the widget has to be visible for the
   browser to ask.

5. **(Optional) Start the robotic arm program** — see the separate arm
   README for that side. It only needs to reach the FastAPI server over
   HTTP; it has no camera dependency anymore.

### Quick sanity check, in order
Mongo up → `http://localhost:8000/home` returns `[]` or your categories →
Streamlit loads and shows categories → open a Collection page and confirm
the camera permission prompt appears → send a manual test trigger from the
arm (or `curl` the trigger endpoint) and confirm a photo saves under
`images/<category>/auto_capture/`.
