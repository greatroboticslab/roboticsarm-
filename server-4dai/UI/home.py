import streamlit as st
import requests
from key import URL 

# =========================================================================
# 1. MAIN DASHBOARD CONTENT VIEW
# =========================================================================

def show_home_dashboard():
    st.session_state.category = None
    st.title("Collections")
    st.write("Pick a Collection")

    FIXED_COL = 6 

    try:
        categories = requests.get(f"{URL}/home").json()
    except Exception:
        categories = []

    if not categories:
       st.write("No Categories")
       st.stop()

    categories.sort()

    columns = st.columns(FIXED_COL)
    count = len(categories)

    for i in range(count):
       with columns[i % FIXED_COL]:
          if st.button(f"{categories[i]}", key=categories[i]):
             st.session_state.category = categories[i]
             st.switch_page(collection_page)

# =========================================================================
# 2. APP NAVIGATION ROUTER & AUTOMATED TRIGGER LISTENER
# =========================================================================

# Passes the function directly to break the recursion loop!
home_page = st.Page(show_home_dashboard, title="Home", icon="🏠", default=True)

collection_page = st.Page("pages/collection.py", title="Collection Form", visibility="hidden")
view_data_page = st.Page("pages/view_data.py", title="View Collections", icon="📊")
settings_page = st.Page("pages/settings.py", title="Settings Manager", icon="⚙️")
roboflow = st.Page("pages/roboflow.py", title= "RoboFlow", icon="🎯")
google_collab = st.Page("pages/googleCollab.py", title= "Google Collab",icon="🚀")

@st.fragment(run_every="1s")
def listen_for_arm_capture_trigger():
    """
    Polls 4DAI's /collection/check-trigger endpoint once a second. If the
    arm has requested a photo, switches to the Collection page for the
    requested category - this runs on every page (each page has its own
    copy of this function), not just Home, so a capture request is
    honored no matter which page happens to be open. Switching to
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

        # Check if the category actually exists on the backend first
        cat_check = requests.get(f"{URL}/settings/{target_category}", timeout=5)
        if cat_check.status_code == 200:
            st.session_state.category = target_category
            st.session_state.arm_trigger_data = data
            st.toast(f"🤖 Arm requested a photo for '{target_category}' — switching to Collection")
            st.switch_page(collection_page)
        else:
            st.toast(f"⚠️ Arm triggered unknown category: '{target_category}'")
    except Exception:
        pass

listen_for_arm_capture_trigger()

# Render sidebar navigation tree
pg = st.navigation([home_page, view_data_page, settings_page, collection_page, roboflow, google_collab])
pg.run()