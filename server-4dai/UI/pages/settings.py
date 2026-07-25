import streamlit as st 
from key import URL 
import requests 
import time 

@st.fragment(run_every="1s")
def listen_for_arm_capture_trigger():
    """
    Polls 4DAI's /collection/check-trigger endpoint once a second. If the
    arm has requested a photo, switches to the Collection page for the
    requested category - this runs on every page so a capture request is
    honored even while on Settings. Switching to Collection also mounts
    its camera widget, which is what makes the browser show its webcam
    permission prompt if it hasn't already been granted.
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

st.title ("Settings")
st.divider()
st.subheader("Add Category")

if "prompts" not in st.session_state:
    st.session_state.prompts = []

if "active_category" not in st.session_state:
    st.session_state.active_category = ""

if "disable" not in st.session_state:
    st.session_state.disable = False 

if "roboflow_settings" not in st.session_state:
    st.session_state.roboflow_settings = {}

if "camera_settings" not in st.session_state:
    st.session_state.camera_settings = False

if "new_prompts" not in st.session_state:
    st.session_state.new_prompts = []   

if "pushed_buttons" not in st.session_state:
    st.session_state.pushed_buttons = {}

if len(st.session_state.prompts) > 0:
    category_name = st.text_input("Category Name:",disabled=True, help= "Clear current prompts or click confrim to change category", value= st.session_state.active_category)
else:
    category_name= st.text_input("Category Name:", value=st.session_state.active_category)

if len(st.session_state.prompts)== 0:
    st.session_state.active_category = category_name


prompt = st.text_input("Add Prompt:",key="make_prompt")
selection = st.selectbox(
                    "How should this field be displayed:",
                    ["Text Box", 
                    "Text Area (multi-line)", 
                    "Number Input", 
                    "Dropdown List", 
                    "Radio Button", 
                    "Slider"],key="make_selection")
    
settings = {
            "selection": selection, 
            "prompt": prompt 
            }
    
range_error = False 
validation_error = False 
        
if selection == "Number Input" or selection == "Slider":

    max_value = st.number_input("Enter max value:")
    min_value = st.number_input("Enter min value:")

    if max_value <= min_value:
        range_error = True
        st.error("Max value must be strictly greater than the Min value.")

    settings["max"] = max_value
    settings["min"] = min_value

elif selection == "Dropdown List" or selection == "Radio Button" or selection == "Check Box":
    raw_options = st.text_input("Enter options (comma separated):")
    cleaned_options = raw_options.replace(".",",").strip().rstrip(",").strip()

    if "," in cleaned_options:
        settings["options"] = cleaned_options
    else:
        st.error(" Validation Error: You must enter at least two options separated by a comma.")
        validation_error = True



if st.button("Add"):
    if not prompt.strip():
        st.error("Please enter a name for the prompt before adding.")
    elif (selection in ["Dropdown List", "Radio Button"]) and not settings.get("options", "").strip():
        st.error(f"Please enter options for the {selection}.")
    elif not category_name.strip():
        st.error("Please enter a name for the Category before adding.")
    elif range_error:
        st.error("Cannot save: Please fix the Min/Max range issue first.")
    elif validation_error:
        st.error("Cannot save: Please fix options before adding.")
    else:
        st.session_state.prompts.append(settings)
        st.rerun()

            
st.divider()

kinect = st.selectbox("Use of Kinect camera:",["False", "True"],key="kinect_camera")

st.divider()

roboflow = st.selectbox("Enable Automatic Roboflow Upload",["False","True"], key="roboflow")

if roboflow == "True":
    api_key = st.text_input("Please Input RoboFlow API Key:", type="password", key="api_key")
    workspace = st.text_input ("Please Input Workspace:", key="workspace")
    project_id = st.text_input("Please Input Project ID:", key="project_id")

    if api_key and workspace and project_id:
        try:
            roboflow_response = requests.get(f"https://api.roboflow.com/{workspace}/{project_id}", params={"api_key":api_key})
        
            if roboflow_response.status_code == 200:
                st.success("Credentials verified successfully!")
                roboflow_settings = {"api_key":api_key, "workspace":workspace, "project_id": project_id}
            else:
                st.error("Invalid Credentials")
                roboflow_settings = False
        except:
            st.error("Network Connection Failed.")
            roboflow_settings = False
else:
    roboflow_settings = False 


st.divider()

st.subheader("Current Prompts")

for count, prompt in enumerate(st.session_state.prompts):
    col1, col2 = st.columns([4,1])

    with col1:
        st.write(f"{count+1}. {prompt}")

    with col2:
         if st.button("Delete",key=f"delete_key{count}"):
            st.session_state.prompts.pop(count)

if st.button("Confirm"):
    clean_category = " ".join(category_name.split())

    existing_categories = requests.get(f"{URL}/home").json()
    
    if not st.session_state.active_category.strip() or not st.session_state.prompts:
        st.error("Submission Denied: Category Name cannot be blank")
        st.error("Submission Denied: You must add at least one prompt field configuring.")

    elif clean_category.lower() in [category.lower() for category in existing_categories]:

        st.error("Submission Denied: Category already exist")
        st.info("Please use the Edit Category section below if you wish to modify it.")

    else:

        page = {
        "category" :clean_category,
        "prompts":[prompt for prompt in st.session_state.prompts],
        "camera": kinect,
        "roboflow": roboflow_settings

    }

        response = requests.post(
            f"{URL}/settings",
            json=page
        )
        if response.status_code in [200,201]:

            st.session_state.prompts = []
            st.session_state.active_category = ""

            # Display your clean confirmation message
            st.success("The settings have been added and everything is good!")
            
            # Wait 2 seconds so they can read the message before st.rerun clears it
            time.sleep(3)

            st.rerun()
        else:
            st.error(f"Server rejected update. Error code: {response.status_code}")

####################### Edit category ########################


st.divider()
st.subheader("Edit Category")
edit = st.fragment

if "new_prompts" not in st.session_state:
    st.session_state.new_prompts = []   

existing_categories = requests.get(f"{URL}/home").json()
if not existing_categories:
        st.stop()

if existing_categories:

    selected_category = st.selectbox("Select the category you wish to edit", existing_categories, key="edit_category")


    settings = requests.get(f"{URL}/settings/{selected_category}").json()

   


    with st.expander(f"Modify or Delete Existing Fields in: {selected_category}"):
        st.divider()
        old_prompts = settings["prompts"]

        st.subheader(f"Editing {settings['category']}")

        available_displays = [
                "Text Box", 
                "Text Area (multi-line)", 
                "Number Input", 
                "Dropdown List", 
                "Radio Button", 
                "Slider"
            ]

            
        category_key = settings['category']
        if category_key not in st.session_state.pushed_buttons:
            st.session_state.pushed_buttons[category_key] = [False] * len(settings["prompts"])

        @edit()
        def Editing():

            for count, prompt_item in enumerate(old_prompts):
                current_selection = prompt_item["selection"]
                default_index = available_displays.index(current_selection) if current_selection in available_displays else 0

                st.markdown(f"**Field #{count+1}**")

                prompt_key = f"{settings['category']}_new_prompt{count}"
                selection_key = f"{settings['category']}_new_selection{count}"

                # Pull lock state straight from session_state
                is_disabled = st.session_state.pushed_buttons[category_key][count]

                st.text_input(f"Edit Prompt:", value=prompt_item["prompt"], key=prompt_key, disabled=is_disabled)
                st.selectbox("How should this field be displayed:", available_displays, index=default_index, key=selection_key, disabled=is_disabled)

                update_prompt = {
                    "prompt": st.session_state[prompt_key],
                    "selection": st.session_state[selection_key]
                }
                
                range_error = False 
                if st.session_state[selection_key] == "Number Input" or st.session_state[selection_key] == "Slider":
                    default_max = prompt_item.get("max", 10)
                    default_min = prompt_item.get("min", 0)

                    new_max_val = st.number_input("Enter max value:", key=f"{settings['category']}_new_max_key{count}", value=default_max, disabled=is_disabled)
                    new_min_val = st.number_input("Enter min value:", key=f"{settings['category']}_new_min_key{count}", value=default_min, disabled=is_disabled)

                    if new_max_val <= new_min_val:
                        range_error = True 
                        st.error("Max value must be strictly greater than the Min value.")
                    
                    update_prompt["max"] = new_max_val
                    update_prompt["min"] = new_min_val

                validation_error = False 
                if st.session_state[selection_key] in ["Dropdown List", "Radio Button", "Check Box"]:
                    new_raw_options = st.text_input("Enter options (comma separated):", key=f"{settings['category']}_new_options_key{count}", value=prompt_item.get("options", ""), disabled=is_disabled)
                    new_cleaned_options = new_raw_options.replace(".", ",").strip().rstrip(",").strip()

                    if "," in new_cleaned_options:
                        update_prompt["options"] = new_cleaned_options
                    else:
                        st.error("Validation Error: You must enter at least two options separated by a comma.")
                        validation_error = True

                cols = st.columns(2)

                with cols[0]:
                    if st.button("Keep/Update", key=f"keep/update{count}", disabled=is_disabled):
                        if not update_prompt["prompt"].strip():
                            st.error("Please enter a name for the prompt before updating.")
                        elif (st.session_state[selection_key] in ["Dropdown List", "Radio Button"]) and not update_prompt.get("options", "").strip():
                            st.error(f"Please enter options for the {st.session_state[selection_key]}.")
                        elif range_error:
                            st.error("Cannot save: Please fix the Min/Max range issue first.")
                        elif validation_error:
                            st.error("Cannot save: Please fix options before updating.")
                        else:
                            st.session_state.new_prompts.append(update_prompt)
                            st.session_state.pushed_buttons[category_key][count] = True
                            st.rerun(scope="fragment")
                        
                with cols[1]:
                    if st.button("Delete", key=f"delete_prompt{count}", disabled=is_disabled):
                        st.session_state.pushed_buttons[category_key][count] = True 
                        st.rerun(scope="fragment")
                st.divider()

   
        Editing() 



        @edit()
        def Editing_roboflow():

            old_roboflow_settings = settings["roboflow"]

        
            if old_roboflow_settings == False:
            
                if st.button("Turn Off", key="turn_off_roboflow_false", disabled=st.session_state.disable):
                    st.session_state.roboflow_settings = False 
                    st.session_state.disable=True
                    old_roboflow_settings = False
                    st.rerun(scope="fragment")



                new_api_key = st.text_input("Please Input RoboFlow API Key:", type="password", key="new_api_key", disabled=st.session_state.disable)
                new_workspace = st.text_input("Please Input Workspace:", key="new_workspace", disabled=st.session_state.disable)
                new_project_id = st.text_input("Please Input Project ID:", key="new_project_id", disabled=st.session_state.disable)

                if new_api_key and new_workspace and new_project_id:
                    try:
                        roboflow_response = requests.get(f"https://api.roboflow.com/{new_workspace}/{new_project_id}", params={"api_key": new_api_key})
                    
                        if roboflow_response.status_code == 200:
                            st.success("Credentials verified successfully!")
                            st.session_state.roboflow_settings = {"api_key": new_api_key, "workspace": new_workspace, "project_id": new_project_id}
                        else:
                            st.error("Invalid Credentials")
                            st.session_state.roboflow_settings = False
                    except:
                        st.error("Network Connection Failed.")
                        st.session_state.roboflow_settings = False
                else:
                    st.session_state.roboflow_settings = False
            else:

            
                if st.button("Turn Off", key="turn_off_roboflow_true", disabled=st.session_state.disable):
                    roboflow_settings = False 
                    st.session_state.disable=True
                    st.session_state.roboflow_settings= False 
                    st.rerun(scope="fragment")

                new_api_key = st.text_input("Please Input RoboFlow API Key:", type="password", key="new_api_key", value=old_roboflow_settings["api_key"],disabled=st.session_state.disable )
                new_workspace = st.text_input ("Please Input Workspace:", key="new_workspace", value= old_roboflow_settings["workspace"],disabled=st.session_state.disable)
                new_project_id = st.text_input("Please Input Project ID:", key="new_project_id", value= old_roboflow_settings["project_id"],disabled=st.session_state.disable)

                if new_api_key and new_workspace and new_project_id:
                    try:
                        roboflow_response = requests.get(f"https://api.roboflow.com/{new_workspace}/{new_project_id}", params={"api_key":new_api_key})
                    
                        if roboflow_response.status_code == 200:
                            st.success("Credentials verified successfully!")
                            st.session_state.roboflow_settings = {"api_key":new_api_key, "workspace":new_workspace, "project_id": new_project_id}
                        else:
                            st.error("Invalid Credentials")
                            st.session_state.roboflow_settings = False
                    except:
                        st.error("Network Connection Failed.")
                        st.session_state.roboflow_settings = False
                else:
                    st.session_state.roboflow_settings = False 

            st.divider()
            current_camera_setting = str(settings.get("camera", "False")).strip()
            camera_options = ["False", "True"]
            camera_default_index = camera_options.index(current_camera_setting) if current_camera_setting in camera_options else 0
            st.session_state.camera_settings = st.selectbox(
                "Use of Kinect camera:", camera_options,
                index=camera_default_index, key="new_camera_settings")

        Editing_roboflow()
            
        st.divider()

        clicked = st.button("Confirm Changes")

        if clicked:
            
            category_key = settings["category"]
            all_buttons_pushed = all(st.session_state.pushed_buttons[category_key])
            
            if not all_buttons_pushed:
                st.error("Submission Denied: You must click 'Keep/Update' or 'Delete' on every field before confirming.")
            elif not st.session_state.new_prompts:
                st.error("Submission Denied: A category must contain at least one valid prompt field.")
            else:
                update_settings = {
                    "category":settings["category"],
                    "prompts":[new_prompt for new_prompt in st.session_state.new_prompts],
                    "camera": st.session_state.camera_settings,
                    "roboflow" : st.session_state.roboflow_settings
                }
                response = requests.post(f"{URL}/settings",json=update_settings)

                if response.status_code in [200,201]:
                    st.success("Settings have been updated")
                    st.session_state.camera_settings = False 
                    st.session_state.roboflow_settings = {}
                    st.session_state.new_prompts = [] 
                    st.session_state.pushed_buttons[category_key] = [False] * len(settings["prompts"])
                    time.sleep(3)
                    st.rerun()
                else:
                    st.error("Error. Changes are not made") 
                    time.sleep(3) 
                    st.rerun()
                

#########################################################################################

    with st.expander(f"Add a New Prompt Field to: {selected_category}"):
        st.write(f"This will immediately append a new field to the bottom of the **{selected_category}** category.")

        new_field_prompt = st.text_input("New Prompt Name:", key=f"{selected_category}_standalone_add_prompt")
        new_field_selection = st.selectbox(
                "Display Style:",
                ["Text Box", "Text Area (multi-line)", "Number Input", "Dropdown List", "Radio Button", "Slider"],
                key=f"{selected_category}_standalone_add_selection"
            )

        extra_settings = {"prompt": new_field_prompt, "selection": new_field_selection}
        new_range_error = False 
        new_validation_error = False

        if new_field_selection in ["Number Input", "Slider"]:
            new_max_value = st.number_input("Enter max value:", key=f"{selected_category}_standalone_max")
            new_min_value = st.number_input("Enter min value:", key=f"{selected_category}_standalone_min")

            if new_max_value <= new_min_value:
                new_range_error = True
                st.error("Max value must be strictly greater than the Min value.")

            extra_settings["max"] = new_max_value
            extra_settings["min"] = new_min_value

        elif new_field_selection in ["Dropdown List", "Radio Button", "Check Box"]:
            new_raw_options = st.text_input("Enter options (comma separated):", key=f"{selected_category}_standalone_opts")
            new_cleaned_options = new_raw_options.replace(".", ",").strip().rstrip(",").strip()

            if "," in new_cleaned_options:
                extra_settings["options"] = new_cleaned_options
            else:
                st.error("Validation Error: You must enter at least two options separated by a comma.")
                new_validation_error = True

        if st.button("Save & Append to Category", key=f"{selected_category}_standalone_append_btn"):
            if not new_field_prompt.strip():
                st.error("Please enter a name for the prompt before adding.")
            elif (new_field_selection in ["Dropdown List", "Radio Button"]) and not extra_settings.get("options", "").strip():
                st.error(f"Please enter options for the {new_field_selection}.")
            elif new_range_error:
                st.error("Cannot save: Please fix the Min/Max range issue first.")
            elif new_validation_error:
                st.error("Cannot save: Please fix options before adding.")
            else:
                try:
                    fresh_category_data = requests.get(f"{URL}/settings/{selected_category}").json()
                    current_prompts = fresh_category_data.get("prompts", [])
                    current_prompts.append(extra_settings)

                    append_payload = {
                            "category": fresh_category_data.get("category", selected_category),
                            "prompts": current_prompts,
                            "camera": fresh_category_data.get("camera", "False"),
                            "roboflow": fresh_category_data.get("roboflow", False)
                        }
                    
                    response = requests.post(f"{URL}/settings", json=append_payload)

                    if response.status_code in [200, 201]:
                        st.success(f"Successfully appended '{new_field_prompt}' to {selected_category}!")
                        time.sleep(2)
                        st.rerun()
                    else:
                        st.error(f"Failed to append. Server rejected with code: {response.status_code}")
                except Exception as e:
                    st.error(f"Network error trying to append: {str(e)}")