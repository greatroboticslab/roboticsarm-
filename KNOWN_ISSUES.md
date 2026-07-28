# Known Issues / Open Items

> **Production is currently frozen.** Everything below is documented for
> tracking purposes only — none of it is being fixed right now.

This is a running list of things that have been identified but **not yet
fixed**, or that were fixed with a caveat worth knowing about. Reproduce
these against the setup in "How to run" below.

## Reported from live testing (highest priority)

1. **🔴 Automatic capture is not working — top priority issue.**
   Triggering a capture from the arm with "Enable Hands-Free Robotic Arm
   Capture" turned on is not reliably resulting in a photo being taken.
   Needs investigation into where the flow is breaking — possible
   candidates to check when this gets picked back up: whether the
   trigger is actually reaching the server (`[TRIGGER ACK]` log line on
   the arm side), whether `/collection/check-trigger` is being polled
   and returning `trigger: true`, whether the JS shutter-click is firing
   against the right widget, and whether `auto_mode`'s state is what's
   expected at the moment the trigger arrives.

2. **Photos are not being saved.** Related to #1, or possibly a separate
   failure further down the chain (e.g. the photo is captured in-browser
   but the `/collection/auto-capture-image` upload itself is failing).
   Worth checking server logs / response codes from that endpoint
   specifically, separately from whether the trigger/capture happened at
   all.

3. **General UI lagginess.** Reported as noticeably laggier than before,
   though not further diagnosed yet. Possibly related to the 1-second
   polling fragments now running on every page (arm-trigger listener) —
   worth checking network/CPU load if this gets picked back up.

## Previously identified, still open

4. **Camera/Kinect per-category gating is disabled.** `collection.py`
   currently shows Automatic + Manual capture for every category
   regardless of its "Use of Kinect camera" setting in Settings (this was
   an intentional, temporary change for testing). The old check is
   commented out and can be restored with a one-line swap
   (`if True:` → `if camera:`, uncomment the `camera =` line above it).

5. **Missing status-code checks in `roboflow.py` / `googleCollab.py`.**
   Some `requests.get(...).json()` calls (e.g. looking up a RoboFlow
   config by name) don't check the response status first. If the server
   returns an error (like a `404` for an unknown RoboFlow setting), these
   pages will throw an unhandled exception and show a raw traceback
   instead of a clean error message.

6. **Auto-capture filename dedup has a small race window.** The
   `/collection/auto-capture-image` endpoint checks for an existing
   filename and appends `_1`, `_2`, etc. to avoid overwriting — but under
   truly concurrent requests there's a brief check-then-write race (two
   requests could both see "no collision" before either writes). Low risk
   for a single-arm setup where captures are effectively sequential.

7. **JS auto-click targets the *first* camera button on the page.** The
   automatic-capture shutter-click (`document.querySelector('button[data-testid="stCameraButton"]')`)
   grabs the first matching button in DOM order. This works because
   Automatic Capture is rendered before Manual Photo Capture on the
   Collection page, but it's a fragile assumption — reordering that page
   would silently break which widget gets auto-clicked. **Possibly
   related to issue #1** — worth checking first if that gets picked back
   up, since a mis-targeted click would look exactly like "automatic
   capture isn't working."

8. **`active_sweep_state` is a single-slot global, not a real queue.**
   If the arm (or anything else) fires multiple capture triggers faster
   than the UI polls (once a second), only the most recent trigger
   survives — earlier ones are silently lost. **Also possibly related to
   issue #1** if the arm is firing triggers faster than once a second
   during a rotation sweep.

9. **Submission payload validation is shallow.** `/collection/submission`
   now checks that `category`/`date`/`data` keys exist, but doesn't
   validate the *contents* of `data` against the category's configured
   prompts.

## Known limitation (not fixable in code)

- **Browser camera permission can't be forced.** If a user clicks "Block"
  on the webcam permission prompt, no server- or client-side code can
  override that — it has to be reset manually in the browser's site
  settings (padlock icon → Site settings → Camera).

## How to run (to reproduce/verify any of the above)

1. **MongoDB**
   ```bash
   mongosh
   ```
2. **FastAPI server**
   ```bash
   cd Server
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
3. **Streamlit UI**
   ```bash
   cd UI
   streamlit run home.py
   ```
   `http://localhost:8501` — confirm `key.py`'s `URL` points at the running
   server.
4. **Robotic arm** (only needed for issues #1–#3 and #6–#8, which involve
   real triggers) — run the arm's main program per `ARM_README.md`; it
   just needs network access to the FastAPI server.

For issue #5 specifically: go to RoboFlow or Google Colab page and select
a RoboFlow config name that no longer exists on disk (e.g. delete its
`.json` file from `roboflow_settings/` while the page is open, then
refresh) to reproduce the unhandled exception.
