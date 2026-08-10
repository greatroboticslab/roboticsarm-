"""
[PROOF OF CONCEPT — TEMPORARY]
==============================

WHY THIS FILE EXISTS
---------------------
vision/camera/capture.py and vision/camera/laser.py are real hardware
targets (final function signatures, waiting on actual camera/laser
integration). This file is NOT that — it's a quick way to exercise the
rest of the pipeline (save -> fusion -> Mongo/MQTT once those are wired
in too) using a browser camera instead of writing OpenCV/SDK code first.

It reuses the exact same trick 4DAI's UI/pages/collection.py already
uses: st.camera_input() opens the browser's camera permission dialog and
hands back a frame captured client-side in JS - zero camera driver code
needed on this machine. That's what makes it a fast PoC: you get a real
image in, in about 10 lines, regardless of what camera hardware you
actually end up mounting on the arm.

TODO / MERGE NOTE
------------------
This is explicitly a stopgap, not the end state. Once real camera
hardware is chosen and vision/camera/capture.py is implemented against
it, this file should be retired and the arm's own main.py (Tkinter GUI)
should call vision.camera.capture directly - one standalone desktop
program, one process owning the Dobot connection, no browser step in
the loop. Keep this file only as long as it's useful for testing; do not
build new permanent features on top of it.

RUNNING THIS
-------------
    pip install streamlit
    streamlit run vision/gui/web_poc.py

Runs at http://localhost:8501, independent of main.py - does NOT connect
to the Dobot socket, so it's safe to run alongside main.py without the
multiple-owner connection conflict mentioned earlier. The "laser" toggle
below is a placeholder checkbox only; it does not trigger real hardware
until wired to vision.camera.laser.set_laser(), which itself still needs
the arm's robot connection (see merge note above for why that step waits
for the standalone-program merge).
"""

import streamlit as st
from datetime import date

from vision.camera.capture import new_sample_id, ensure_sample_dir
from vision.model.fusion import classify_multi_source

st.title("Vision Pipeline — Proof of Concept (temporary, browser-based)")
st.caption(
    "Uses the browser camera the same way 4DAI's collection.py does. "
    "TODO: retire this once vision/camera/capture.py is implemented against "
    "real hardware and merged into main.py as one standalone program."
)

if "poc_views" not in st.session_state:
    st.session_state.poc_views = []
if "poc_sample_id" not in st.session_state:
    st.session_state.poc_sample_id = new_sample_id()

st.subheader(f"Sample ID: {st.session_state.poc_sample_id}")

st.divider()
st.subheader("Simulated laser toggle (placeholder only — no hardware yet)")
laser_on = st.checkbox("Laser ON", value=False)
if laser_on:
    st.info(
        "Laser would fire here once vision.camera.laser.set_laser() is "
        "implemented against real hardware and this PoC is merged into "
        "main.py, giving it access to the live robot connection."
    )

st.divider()
st.subheader("Capture a view")

source = st.radio("Simulated source (stand-in for station vs wrist camera):",
                   ["station", "wrist"])
picture = st.camera_input(f"Capture — {source}")

if picture is not None:
    if st.button("Add this view to the sample"):
        sample_dir = ensure_sample_dir(st.session_state.poc_sample_id)
        view_index = len(st.session_state.poc_views)
        image_path = f"{sample_dir}/{source}_{view_index}.jpg"

        with open(image_path, "wb") as f:
            f.write(picture.getbuffer())

        st.session_state.poc_views.append({
            "source": source,
            "view_index": view_index,
            "image_path": image_path,
            # No real arm pose available here since this PoC isn't
            # connected to the robot — placeholder pose only.
            "pose": {"note": "no live arm connection in this PoC"},
        })
        st.success(f"Saved view {view_index} ({source}) -> {image_path}")

st.divider()
st.subheader("Current views for this sample")
for v in st.session_state.poc_views:
    st.write(f"- {v['source']} view {v['view_index']}: `{v['image_path']}`")

st.divider()
if st.button("Run identification on all captured views"):
    if not st.session_state.poc_views:
        st.error("Capture at least one view first.")
    else:
        try:
            result = classify_multi_source(st.session_state.poc_views)
            st.success(f"Predicted label: {result['predicted_label']}")
            st.json(result)
        except NotImplementedError as e:
            st.warning(
                f"Fusion logic ran, but the classifier itself isn't wired "
                f"in yet:\n\n{e}"
            )

st.divider()
if st.button("Start a new sample"):
    st.session_state.poc_views = []
    st.session_state.poc_sample_id = new_sample_id()
    st.rerun()
