import streamlit as st 
from key import URL
import requests
from datetime import date, datetime
import re 
import json 

category = st.session_state.get("category", "")
st.header(f"{category} Collection")

if not category:
    st.warning("Please select a category from Home.")
    st.stop()

# Safely fetch settings to prevent crashes if the category is invalid
res = requests.get(f"{URL}/settings/{category}")
if res.status_code != 200:
    st.error(f"❌ Category '{category}' was not found in Settings. Please select a valid category from Home.")
    st.stop()

page = res.json()
prompts = page["prompts"]
# NOTE: gating capture on the per-category "camera" (Kinect) setting is
# temporarily disabled - Automatic/Manual capture now always render and
# request webcam access regardless of that setting, matching the old
# behavior before that gate was added. Re-add `if camera:` below (and
# uncomment the line above) to restore the gate later.
# camera = str(page.get("camera", "False")).strip().lower() == "true"

if "images" not in st.session_state:
    st.session_state.images = []

# =========================================================================
# 🤖 ROBOTIC ARM CONTROL
# =========================================================================
# Automatic capture: when the arm sends a trigger and this toggle is on,
# the browser webcam snaps a photo and it's saved right away with a
# timestamped filename via /collection/auto-capture-image - standalone,
# not tied to the sample form below. (There used to be a "captured
# object" pipeline here feeding these into identification/classification;
# that lived entirely on the robotic-arm/vision side and has been
# removed, so automatic capture is now just "take a photo, save it.")
#
# Manual capture (below it) is the classic flow: take a picture yourself
# and it gets attached to this sample's submission.
# =========================================================================
if True:  # was: if camera:
    st.subheader("Robotic Arm Control — Automatic Capture")

    auto_mode = st.toggle(
        "🤖 Enable Hands-Free Robotic Arm Capture",
        value=False,
        help="When enabled, a trigger from the arm auto-snaps a photo and saves it immediately, timestamped."
    )

    if "arm_trigger_data" not in st.session_state:
        st.session_state.arm_trigger_data = None
    if "last_auto_saved_capture" not in st.session_state:
        st.session_state.last_auto_saved_capture = None

    @st.fragment(run_every="1s")
    def listen_for_arm_trigger():
        """Background listener checking FastAPI server for arm trigger signals."""
        try:
            res = requests.get(f"{URL}/collection/check-trigger", timeout=2)
            if res.status_code == 200:
                trigger_info = res.json()
                if trigger_info.get("trigger"):
                    data = trigger_info["data"]
                    incoming_category = data.get("category")
                    current_category = st.session_state.get("category")

                    # If the arm triggered a different category than the open page
                    if incoming_category and incoming_category != current_category:
                        cat_check = requests.get(f"{URL}/settings/{incoming_category}", timeout=5)
                        if cat_check.status_code == 200:
                            st.session_state.category = incoming_category
                            st.session_state.arm_trigger_data = data
                            st.rerun()  # Reloads page with the new category
                        else:
                            st.toast(f"⚠️ Arm requested unknown category: '{incoming_category}'")
                            return

                    st.session_state.arm_trigger_data = data
                    st.toast(f"🤖 Arm Signal Received! Frame #{data['image_index']} for '{incoming_category}'")

                    # IF AUTO MODE IS ON: Programmatically click the browser camera shutter.
                    #
                    # BUGFIX: previously this grabbed the FIRST
                    # button[data-testid="stCameraButton"] on the whole page —
                    # correct only by coincidence of "Automatic Capture" being
                    # rendered before "Manual Photo Capture" below, and silently
                    # wrong (clicks the manual widget's shutter instead) the
                    # moment the page is reordered. Now it walks up from each
                    # candidate button to its camera-input container and picks
                    # the one whose label text actually matches the Automatic
                    # Capture widget, so it's correct regardless of page order.
                    # Also retries a few times (150ms apart) in case the button
                    # isn't mounted yet the instant this script runs.
                    if auto_mode:
                        st.components.v1.html("""
                            <script>
                                (function() {
                                    function clickAutoShutter(attemptsLeft) {
                                        const doc = window.parent.document;
                                        const buttons = doc.querySelectorAll('button[data-testid="stCameraButton"]');
                                        let target = null;
                                        for (const btn of buttons) {
                                            const container = btn.closest('[data-testid="stCameraInput"]') || btn.closest('div');
                                            if (container && container.innerText &&
                                                container.innerText.indexOf('Automatic Capture') !== -1) {
                                                target = btn;
                                                break;
                                            }
                                        }
                                        // Fallback: if the label-based match didn't find anything
                                        // (e.g. Streamlit changed its markup), fall back to the
                                        // first button rather than doing nothing.
                                        if (!target && buttons.length > 0) {
                                            target = buttons[0];
                                        }
                                        if (target) {
                                            target.click();
                                        } else if (attemptsLeft > 0) {
                                            setTimeout(function() { clickAutoShutter(attemptsLeft - 1); }, 150);
                                        } else {
                                            console.warn('[auto-capture] camera shutter button not found after retries');
                                        }
                                    }
                                    clickAutoShutter(5);
                                })();
                            </script>
                        """, height=0)
                    else:
                        st.rerun()
        except Exception:
            pass

    listen_for_arm_trigger()

    auto_picture = st.camera_input("Automatic Capture (driven by the arm trigger above)", key="cam_input_auto")

    if auto_picture is not None and auto_mode:
        # Save immediately as its own timestamped file - a new capture is
        # detected by comparing against the id of the last one we saved,
        # since Streamlit re-delivers the same camera_input value across
        # reruns until a new photo is actually taken.
        capture_signature = auto_picture.file_id if hasattr(auto_picture, "file_id") else id(auto_picture)
        if st.session_state.last_auto_saved_capture != capture_signature:
            timestamp_name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            files = {"file": (f"{timestamp_name}.jpg", auto_picture, "image/jpeg")}
            # BUGFIX: previously didn't forward the arm's sample_id at all, so
            # every image in a sweep/rotation was saved as an orphan file with
            # no linking sample record — the server had to invent a bare
            # "images/<category>/auto_capture/" folder with no Mongo entry
            # tying it to anything, which is why files existed on disk but the
            # app (View Collections) could never find/show them. Forwarding
            # the trigger's sample_id lets the server group every image from
            # the same sweep under one findable sample.
            trigger_sample_id = (st.session_state.arm_trigger_data or {}).get("sample_id")
            data = {"category": category, "filename": timestamp_name}
            if trigger_sample_id:
                data["sample_id"] = trigger_sample_id
            save_res = requests.post(f"{URL}/collection/auto-capture-image", files=files, data=data)
            if save_res.status_code == 200:
                st.session_state.last_auto_saved_capture = capture_signature
                st.toast(f"📸 Auto-saved photo as {save_res.json().get('filename', timestamp_name)}")
            else:
                st.error(f"Failed to save the automatically captured photo "
                         f"(status {save_res.status_code}): {save_res.text}")

    st.write("---")

    st.subheader("Manual Photo Capture")
    manual_picture = st.camera_input("Take a Picture", key="cam_input_manual")

    # BUGFIX: st.camera_input keeps returning the SAME photo across every
    # rerun until a new one is taken — and this page reruns for reasons
    # that have nothing to do with the camera (typing into a prompt field,
    # the automatic-capture listener firing a st.rerun() above, etc). The
    # old code unconditionally appended on every truthy `manual_picture`,
    # so a single manual photo could get appended dozens of times and all
    # of those duplicates got uploaded on Submit. Track the photo's own
    # file_id (same signature check the automatic-capture branch already
    # uses above) so a still-unchanged photo doesn't get re-appended, and
    # a freshly retaken one always saves as this sample's photo.
    if "last_manual_capture_id" not in st.session_state:
        st.session_state.last_manual_capture_id = None

    if manual_picture is not None:
        manual_signature = (manual_picture.file_id if hasattr(manual_picture, "file_id")
                             else id(manual_picture))
        if st.session_state.last_manual_capture_id != manual_signature:
            st.session_state.images.append(manual_picture)
            st.session_state.last_manual_capture_id = manual_signature

    st.write("---")
# =========================================================================

values = {}

for count, prompt in enumerate(prompts):
    selection = prompt["selection"]

    match selection:
        case "Text Box":
            values[prompt["prompt"]] = st.text_input(prompt["prompt"], key=f"input_{prompt['prompt']}{count}")

        case "Text Area (multi-line)":
            values[prompt["prompt"]] = st.text_area(prompt["prompt"], key=f"input_{prompt['prompt']}{count}")
        
        case "Number Input":
            values[prompt["prompt"]] = st.number_input(prompt["prompt"], min_value=prompt["min"], max_value=prompt["max"], key=f"input_{prompt['prompt']}{count}")
        
        case "Dropdown List":
            options = [opt.strip() for opt in prompt["options"].split(",")]
            values[prompt["prompt"]] = st.selectbox(prompt["prompt"], options=options, key=f"input_{prompt['prompt']}{count}")

        case "Radio Button":
            options = [opt.strip() for opt in prompt["options"].split(",")]
            values[prompt["prompt"]] = st.radio(prompt["prompt"], options=options, key=f"input_{prompt['prompt']}{count}")
        
        case "Slider": 
            values[prompt["prompt"]] = st.slider(prompt["prompt"], min_value=prompt["min"], max_value=prompt["max"], key=f"input_{prompt['prompt']}{count}")

        case "Date Input":
            values[prompt["prompt"]] = str(st.date_input(prompt["prompt"], value=date.today(), key=f"input_{prompt['prompt']}{count}"))

        case "Check box":
            values[prompt["prompt"]] = st.checkbox(prompt["prompt"], key=f"input_{prompt['prompt']}{count}")

if "submitted" not in st.session_state:
    st.session_state.submitted = False 

if "sample_id" not in st.session_state:
    st.session_state.sample_id = ""

if "robo_submission" not in st.session_state:
    st.session_state.robo_submission = False

st.write("---")

if st.button("Submit Data"):
    st.session_state.submitted = False 
    st.session_state.robo_submission = False 

    submission = {
        "category": category,
        "date": str(date.today()),
        "data": values
    }

    response = requests.post(f"{URL}/collection/submission", json=submission)

    if response.status_code == 200:
        st.session_state.submitted = True 
        sample_id = response.json()["sample_id"]
        st.session_state.sample_id = sample_id 

        for image in st.session_state.images:
            files = {"file": ("image.jpg", image, "image/jpeg")}
            data = {"sample_id": str(sample_id), "category": category}

            res = requests.post(f"{URL}/collection/images/upload", files=files, data=data)

            if res.status_code != 200:
                # Previously this just skipped setting image_id and fell
                # through anyway, reusing the *previous* image's ID (or
                # crashing with NameError on the very first image) below.
                # Now a failed upload is reported and that image is
                # skipped entirely rather than silently mislabeled.
                st.error(f"Failed to upload one of the images (status {res.status_code}); skipping it.")
                continue

            image_id = res.json()["image_id"]

            roboflow_settings = page.get("roboflow", False)

            if roboflow_settings:
                selected_project_id = roboflow_settings["project_id"]
                roboflow_URL = f"https://api.roboflow.com/dataset/{selected_project_id}/upload"

                image_information = {"sample_id": str(sample_id)}
                
                for prompt_text, user_answer in values.items():
                    clean_key = re.sub(r'[^a-zA-Z0-9\s]', '', prompt_text).strip().replace(" ", "_")
                    image_information[clean_key] = str(user_answer)

                payload_data = {
                    "name": f"{category}:Image ID:{image_id}",
                    "metadata": json.dumps(image_information)
                }
                
                files = {"file": image}
                params = {"api_key": roboflow_settings["api_key"]}

                requests.post(roboflow_URL, params=params, files=files, data=payload_data)

        st.session_state.images = []
        st.rerun()           

if st.session_state.submitted:
    st.success("Submission Successful!")
    st.success(f"Sample ID: {st.session_state.sample_id}")