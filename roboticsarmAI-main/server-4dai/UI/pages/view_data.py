import streamlit as st
import requests
from key import URL
from datetime import datetime
import json 

@st.fragment(run_every="1s")
def listen_for_arm_capture_trigger():
    """
    Polls 4DAI's /collection/check-trigger endpoint once a second. If the
    arm has requested a photo, switches to the Collection page for the
    requested category - this runs on every page so a capture request is
    honored even while viewing collections. Switching to Collection also
    mounts its camera widget, which is what makes the browser show its
    webcam permission prompt if it hasn't already been granted.
    """
    try:
        res = requests.get(f"{URL}/collection/check-trigger", timeout=2)
        if res.status_code != 200:
            return

        trigger_info = res.json()
        if not trigger_info.get("trigger"):
            return

        data = trigger_info["data"]
        target_category = data.get("category")

        if not target_category:
            return

        cat_check = requests.get(f"{URL}/settings/{target_category}", timeout=5)
        if cat_check.status_code == 200:
            st.session_state.category = target_category
            st.session_state.arm_trigger_data = data
            st.toast(f"🤖 Arm requested a photo for '{target_category}' — switching to Collection")
            st.switch_page("pages/collection.py")
        else:
            st.toast(f"⚠️ Arm triggered unknown category: '{target_category}'")
    except Exception:
        pass

listen_for_arm_capture_trigger()

st.title("View Collections")

categories = requests.get(f"{URL}/home").json()

if not categories:
   st.write("No Categories")
   st.stop()

selection = st.selectbox("Select Category:", categories)

st.divider()

samples_response = requests.get(f"{URL}/collection/samples/{selection}")

if samples_response.status_code != 200:
    st.error("Failed to load collection data.")
    st.stop()

samples = samples_response.json()

if not samples:
    st.info(f"No submissions found for {selection}.")
    st.stop()

st.header("Filter")

today = datetime.today().date()
date_range = st.date_input(
    "Select Date Range:",
    value=(today, today),
    max_value=today
)


for sample in samples:
    sample_id = sample["sample_id"]

    try:
        sample_date = datetime.strptime(sample["date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        sample_date = None 

    sample_information = sample["data"]

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
        if sample_date and not (start_date <= sample_date <= end_date):
            continue  # Skip this sample if outside range

        with st.expander(f"Sample ID: {sample_id}, Date: {sample['date']}"):
            for question, answer in sample_information.items():
                st.write(f"**{question}:** {answer}")

            images_list = requests.get(f"{URL}/collection/images/{sample_id}").json()

                
            st.write("## Captured Images")
            
            columns = st.columns(3)
            for count, image in enumerate(images_list):
                image_id = image["image_id"]
                actual_image = requests.get(f"{URL}/collection/image/{image_id}")


                with columns[count % 3]:
                    st.image(actual_image.content,caption=f"Image ID: {image_id}",width="stretch")

                    st.download_button(
                        label="Download Image",
                        data=actual_image.content,
                        file_name=f"{selection}_{image_id}.jpg",
                        mime="image/jpeg",
                        key=f"btn_{image_id}"
                        )
 