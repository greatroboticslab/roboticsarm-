import streamlit as st
import requests 
from key import URL

@st.fragment(run_every="1s")
def listen_for_arm_capture_trigger():
    """
    Polls 4DAI's /collection/check-trigger endpoint once a second. If the
    arm has requested a photo, switches to the Collection page for the
    requested category - this runs on every page so a capture request is
    honored even while on the Google Colab script page. Switching to
    Collection also mounts its camera widget, which is what makes the
    browser show its webcam permission prompt if it hasn't already been
    granted.
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

st.title("Generate Scripts")

if "lock_1" not in st.session_state:
    st.session_state.lock_1 = False 

category_settings = requests.get(f"{URL}/home").json()
robo_settings = requests.get(f"{URL}/roboflow").json()

if not category_settings and not robo_settings:
    st.write("No RoboFlow settings exists")
    st.stop()

settings = category_settings + robo_settings

selection = st.selectbox("Select RoboFlow settings: ", settings, key="selection_key3", disabled=st.session_state.lock_1)

api_key = ""
workspace = ""
project_id = ""

if selection in category_settings:
    configurations = requests.get(f"{URL}/settings/{selection}").json()
    roboflow_configurations = configurations["roboflow"]

    if not roboflow_configurations:
        st.write("No Robflow configurations are available")
        st.stop()
    else:
        api_key = roboflow_configurations["api_key"]
        workspace = roboflow_configurations["workspace"]
        project_id = roboflow_configurations["project_id"]

elif selection in robo_settings:
    configurations = requests.get(f"{URL}/roboflow/{selection}").json()

    api_key = configurations["api_key"]
    workspace = configurations["workspace"]
    project_id = configurations["project_id"]

st.write(f"API key: {api_key}")
st.write (f"Workspace: {workspace}")
st.write (f"Project ID: {project_id}")

st.divider()
st.subheader("Model Configuration")

model_family = st.selectbox("Model Family", ["yolo26", "yolo11", "yolov8"], disabled=st.session_state.lock_1)
model_size = st.selectbox("Model Size", ["n (nano)", "s (small)", "m (medium)", "l (large)", "x (xlarge)"], disabled=st.session_state.lock_1)
epochs = st.number_input("Epochs", min_value=1, max_value=300, value=50, disabled=st.session_state.lock_1)

size_mapping = {"n (nano)": "n", "s (small)": "s", "m (medium)": "m", "l (large)": "l", "x (xlarge)": "x"}
chosen_size = size_mapping[model_size]
model_filename = f"{model_family}{chosen_size}.pt"

if not st.session_state.lock_1:
    if st.button("Lock Configuration & Generate Script"):
        st.session_state.lock_1 = True 
        st.rerun()

if st.session_state.lock_1:
    st.success("Configuration locked!")
    st.subheader("Google Colab Pro Training Code")
    st.write("Copy and paste this into the first cell of your Google Colab Pro notebook:")

    colab_script = f"""
    # 1. Install Roboflow and Ultralytics
    !pip install -q roboflow ultralytics

    # 2. Download the latest dataset version from your Roboflow project
    from roboflow import Roboflow
    from ultralytics import YOLO

    rf = Roboflow(api_key="{api_key}")
    project = rf.workspace("{workspace}").project("{project_id}")

    versions = project.versions()
    if not versions:
        raise ValueError("No dataset versions found! Please generate a version in your Roboflow dashboard first.")

    latest_v = versions[-1].version.split("/")[-1]
    print(f"Downloading latest dataset version: {{latest_v}}")
    dataset = project.version(int(latest_v)).download("{model_family}")

    # 3. Train using Colab Pro GPU
    model = YOLO("{model_filename}")
    model.train(
        data=f"{{dataset.location}}/data.yaml",
        epochs={epochs},
        imgsz=640,
        batch=16
)
"""
        
    st.code(colab_script, language="python")



