import streamlit as st 
import requests 
from key import URL 
from datetime import datetime 
import re 
import json

@st.fragment(run_every="1s")
def listen_for_arm_capture_trigger():
    """
    Polls 4DAI's /collection/check-trigger endpoint once a second. If the
    arm has requested a photo, switches to the Collection page for the
    requested category - this runs on every page so a capture request is
    honored even while on the RoboFlow page. Switching to Collection also
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

st.title("RoboFlow")

if "lock1" not in st.session_state:
    st.session_state.lock1 = False 
if "lock2" not in st.session_state:
    st.session_state.lock2 = True 
if "selected_images" not in st.session_state:
    st.session_state.selected_images = []

with st.expander("RoboFlow Configuration"):
    api_key = st.text_input("Please Input RoboFlow API Key:", type="password", key="api_key")
    workspace = st.text_input ("Please Input Workspace:", key="workspace")
    project_id = st.text_input("Please Input Project ID:", key="project_id")

    if api_key and workspace and project_id:
        try:
            roboflow_response = requests.get(f"https://api.roboflow.com/{workspace}/{project_id}", params={"api_key":api_key})
        
            if roboflow_response.status_code == 200:

                name = st.text_input("Please enter name for settings:",key= "roboflow_name")
                
                if not name.strip():
                    st.stop()
                else:
                    roboflow = {"name": name.strip(), "api_key":api_key, "workspace":workspace, "project_id": project_id}
                    response = requests.post(f"{URL}/roboflow", json=roboflow)
                    st.success("Credentials verified successfully!")
                    st.rerun()
                
            else:
                st.error("Invalid Credentials")
                roboflow = False
        except:
            roboflow = False
    else:
        roboflow = False 

    if roboflow:
        if st.button("Save"):
            response = requests.post(f"{URL}/roboflow", json=roboflow)
            if response.status_code in [200,201]:
                st.success("Saved!!!")

st.divider()
roboflow_settings = requests.get(f"{URL}/roboflow").json()

if not roboflow_settings:
    st.write("No roboflow settings exists")
    st.stop()

roboflow_selection = st.selectbox("Select roboflow account/project to upload images",roboflow_settings,disabled=st.session_state.lock1)

if st.button("Use",disabled=st.session_state.lock1):
    st.session_state.lock1= True 
    st.session_state.lock2= False 
    st.rerun()
   

categories = requests.get(f"{URL}/home").json()

if not categories:
    st.write("No categories available")
    st.stop()

category_selection = st.selectbox("Select category to select images from:", categories, disabled=st.session_state.lock2, help="Please select RoboFlow account first")

st.divider()

samples = requests.get(f"{URL}/collection/samples/{category_selection}").json()

if not samples:
    st.write(f"No submissions found for {category_selection}.")
    st.stop()


st.header("Filter")

today = datetime.today().date()
date_range = st.date_input(
    "Select Date Range:",
    value=(today, today),
    max_value=today
)

cols = st.columns([0.7,0.3])

with cols[0]:
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

                    is_already_selected = any(item["image_id"] == image_id for item in st.session_state.selected_images)
                    lock = is_already_selected or st.session_state.lock2

                    with columns[count % 3]:
                        st.image(actual_image.content,caption=f"Image ID: {image_id}",width="stretch")
                        if st.button("Add", key=f"add_button{image_id}", disabled=lock, help="Please select RoboFlow account first/already added into selected images"):
                            st.session_state.selected_images.append({
                                "image_id": image_id,
                                "sample": sample,
                            })
                            st.rerun()
                                

with cols[1]:
    st.subheader("Selected Images")

    if not st.session_state.selected_images:
        st.write("No images selected.")
        st.stop()
    else:
        for selected_image in list(st.session_state.selected_images):
            img = selected_image["image_id"]
            samp_id = selected_image["sample"].get("sample_id", "unknown")

            selected_cols = st.columns([0.8, 0.2])

            with selected_cols[0]:
                st.write(img)
            
            with selected_cols[1]:
                
                unique_delete_key = f"delete_{samp_id}_{img}"
                
                if st.button("Delete", key=unique_delete_key):
                    for index, item in enumerate(st.session_state.selected_images):
                        if item["image_id"] == img and item["sample"].get("sample_id") == samp_id:
                            st.session_state.selected_images.pop(index)
                            break 
                    st.rerun()
        
        st.divider()

        if st.button("Upload Selected images to RoboFlow", type="primary"):
            
            for image_info in st.session_state.selected_images:
                img_id = image_info["image_id"]
                actual_image = requests.get(f"{URL}/collection/image/{img_id}").content
                image_info["actual_image"] = actual_image
                sample_data = image_info["sample"]["data"]

                metadata= {}

                for prompt, answer in sample_data.items():
                    clean_key = re.sub(r'[^a-zA-Z0-9\s]', '', prompt)
                    clean_key = clean_key.strip().replace(" ", "_")

                    metadata[clean_key] = answer

                image_info["cleaned_metadata"] = metadata

            robo_settings = requests.get(f"{URL}/roboflow/{roboflow_selection}").json()

            selected_api_key = robo_settings["api_key"]
            selected_project_id = robo_settings["project_id"]

            roboflow_URL = f"https://api.roboflow.com/dataset/{selected_project_id}/upload"

            params = {"api_key": selected_api_key}

            success_count = 0

            for image in st.session_state.selected_images:
                img_id = image["image_id"]
                files = {
                    "file": image["actual_image"]
                }
                
                payload_data = {
                    "name": f"image_id: {img_id}",
                    "metadata": json.dumps(image['cleaned_metadata'])
                }

                try:
                    roboflow_response = requests.post(roboflow_URL, params=params, files=files, data=payload_data)

                    if roboflow_response.status_code == 200:
                        success_count += 1
                        st.success(f"Uploaded {img_id} successfully!")
                    else:
                        st.error(f"Failed to upload {img_id}: {roboflow_response.text}")
                except Exception as e:
                    st.error(f"Upload connection failed for {img_id}: {e}")

            if success_count > 0:
                st.success(f"Successfully uploaded {success_count} images to RoboFlow!")
                st.session_state.selected_images = []
                st.rerun()
