
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pymongo import MongoClient 
from pydantic import BaseModel
import os 
import re
import uuid
import json
from datetime import datetime

app = FastAPI()

client = MongoClient("mongodb://localhost:27017")
db = client["Collections"]
settings_folder = "settings"
os.makedirs(settings_folder,exist_ok=True)
roboflow_folder = "roboflow_settings"
os.makedirs(roboflow_folder, exist_ok=True)


def safe_filename(name: str) -> str:
    """
    Guard against path traversal in any user-supplied name used to build a
    file path (category, roboflow setting name, etc.). Strips path
    separators and '..' so a value like '../../etc/passwd' can't escape
    settings_folder/roboflow_folder. Raises HTTPException(400) if nothing
    valid is left after sanitizing.
    """
    cleaned = os.path.basename(str(name)).strip()
    cleaned = re.sub(r'\.\.+', '', cleaned)
    if not cleaned:
        raise HTTPException(status_code=400, detail="Invalid or empty name.")
    return cleaned


def safe_collection_name(name) -> str:
    """
    Guard against invalid/dangerous MongoDB collection names built from a
    user-supplied category string. Collection names can't be empty, can't
    contain '$', and can't start with 'system.' (Mongo reserves that
    prefix) - none of that was being checked before, so a malformed or
    malicious category name would surface as an unhandled 500 instead of
    a clean error.
    """
    cleaned = str(name).strip() if name is not None else ""
    if not cleaned:
        raise HTTPException(status_code=400, detail="Category name cannot be empty.")
    if "$" in cleaned or cleaned.startswith("system."):
        raise HTTPException(status_code=400, detail="Invalid category name.")
    if len(cleaned) > 200:
        raise HTTPException(status_code=400, detail="Category name is too long.")
    return cleaned

@app.post("/collection/submission")
def submission(submission:dict):
    for required_key in ("category", "date", "data"):
        if required_key not in submission:
            raise HTTPException(status_code=400, detail=f"Submission is missing required field '{required_key}'.")

    category = safe_collection_name(submission["category"].lower())
    table = db[category]
    sample_id = str(uuid.uuid4())
    
    table.insert_one({
        "_id": sample_id,
        "date" : submission["date"],
        "data": submission["data"]
    })
    return {"sample_id": sample_id}

@app.post("/collection/images/upload")
def upload_image(sample_id: str = Form(...), category:str = Form(...), file:UploadFile = File(...)):
    category = safe_filename(category)
    sample_id = safe_filename(sample_id)
    image_folder = f"images/{category}/{sample_id}"
    os.makedirs(image_folder,exist_ok=True)

    image_id = str(uuid.uuid4())

    image_file = f"{image_folder}/{image_id}.jpg"
    
    with open(image_file,"wb") as infile:
        infile.write(file.file.read())
    
    image_table = db["images"]
    
    image_table.insert_one({
        "_id": image_id,
        "sample_id":sample_id,
        "image_path": image_file

    })
    return {"image_id":image_id, "image_path":image_file}


@app.get("/collection/samples/{selection}")
def get_samples(selection:str):
    category = safe_collection_name(selection.lower())
    table = db[category]
   
    cursor = table.find({})
    
    samples = []
    for doc in cursor:
        samples.append({
            "sample_id": doc.get("_id"),
            "date": doc.get("date"),
            "data": doc.get("data")
        })
    return samples

@app.get("/settings/{category}")
def get_collections_configuration(category:str):
    category = safe_filename(category)
    file_path = f"{settings_folder}/{category}.json"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Category '{category}' not found.")
    with open(file_path) as infile:
        settings = json.load(infile)
    return settings 

@app.get("/home")
def home_configuration():
    categories = []

    for file_name in os.listdir(settings_folder):
        categories.append(file_name.removesuffix(".json"))

    return categories

@app.get("/collection/image/{image_id}")
def get_image(image_id:str):
  image_collections = db["images"]
  image = image_collections.find_one({
      "_id": image_id
  })

  if image is None or not os.path.exists(image["image_path"]):
    raise HTTPException(status_code=404, detail="Image not found.")
  
  return FileResponse(path=image["image_path"])

@app.get("/collection/images/{sample_id}")
def get_list_sample_images(sample_id: str):
    image_table = db["images"]
    cursor = image_table.find({"sample_id": sample_id})
    
    images = []
    for doc in cursor:
        images.append({
            "image_id": doc["_id"],  # Maps to your "_id": image_id storage scheme
            "sample_id": doc["sample_id"]
        })
    return images

@app.post("/settings")
def create_page_configuration(page:dict):
    category = safe_filename(page["category"])
    folder_path = "settings/"

    os.makedirs(folder_path,exist_ok=True)
    file_path = f"{folder_path}/{category}.json"

    with open(file_path,"w") as infile:
        json.dump(page,infile,indent=4)

    return {"message": "saved", "file": file_path}

@app.post("/roboflow")
def create_roboflow_home_configuration(roboflow:dict):
    setting_name = safe_filename(roboflow["name"])

    with open(f"{roboflow_folder}/{setting_name}.json", "w") as infile:
        json.dump(roboflow,infile,indent=4)

    return{"message":"saved"}

@app.get("/roboflow")
def get_roboflow_configuration():
    roboflow_settings = []

    for file_name in os.listdir(roboflow_folder):
        roboflow_settings.append(file_name.removesuffix(".json"))

    return roboflow_settings

@app.get("/roboflow/{selection}")
def get_roboflow_configuration(selection:str):
    selection = safe_filename(selection)
    file_path = f"{roboflow_folder}/{selection}.json"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"RoboFlow setting '{selection}' not found.")
    with open(file_path) as infile:
        roboflow_settings = json.load(infile)
    return roboflow_settings


# =====================================================================
# ROBOTIC ARM INTEGRATION - CAPTURE TRIGGER + STANDALONE AUTO-SAVE
# =====================================================================
# The arm posts a trigger here when it wants a photo taken; the Streamlit
# UI polls check-trigger and (if automatic capture is enabled) snaps a
# photo and saves it via /collection/auto-capture-image below. This is
# just a photo-taking signal - there's no object identification/
# classification pipeline on this side (that lived on the robotic-arm/
# vision project and has been removed).
# =====================================================================

active_sweep_state = {
    "pending": False,
    "category": "",
    "sample_id": "",
    "image_index": 0
}

class TriggerCaptureRequest(BaseModel):
    category: str
    sample_id: str
    image_index: int
    source: str

@app.post("/collection/trigger-webcam-capture")
async def trigger_webcam_capture(payload: TriggerCaptureRequest):
    """Stores trigger request details in memory for the Streamlit UI to poll."""
    active_sweep_state["pending"] = True
    active_sweep_state["category"] = payload.category
    active_sweep_state["sample_id"] = payload.sample_id
    active_sweep_state["image_index"] = payload.image_index

    print(f"[TRIGGER ACK] Queued frame #{payload.image_index} for '{payload.category}'")
    
    return {
        "status": "success",
        "message": f"Capture trigger queued for {payload.category}",
        "sample_id": payload.sample_id,
        "image_index": payload.image_index
    }

@app.get("/collection/check-trigger")
async def check_trigger():
    """Polled by Streamlit UI. Returns true when a trigger event is pending."""
    if active_sweep_state["pending"]:
        active_sweep_state["pending"] = False  # Reset flag after consuming
        return {"trigger": True, "data": active_sweep_state}
    
    return {"trigger": False}


@app.post("/collection/auto-capture-image")
def save_auto_captured_image(category: str = Form(...), file: UploadFile = File(...),
                              filename: str = Form(None)):
    """
    Saves a photo taken automatically in response to an arm trigger.

    This is intentionally standalone - just "take a photo, save it to
    disk" - with no sample_id, no Mongo record, and no link to a
    submission form. There used to be a fuller "captured object"
    pipeline here (feeding automatic captures into object identification/
    classification), but that lived entirely on the robotic-arm/vision
    side and has been removed, so this endpoint no longer tries to
    participate in it.
    """
    category = safe_filename(category)
    folder = f"images/{category}/auto_capture"
    os.makedirs(folder, exist_ok=True)

    if filename:
        safe_name = safe_filename(filename)
    else:
        safe_name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    if not safe_name.lower().endswith((".jpg", ".jpeg", ".png")):
        safe_name = f"{safe_name}.jpg"

    # Guard against filename collisions: multiple auto-captures can land
    # in the same second during a fast arm sweep, and a same-name write
    # would otherwise silently overwrite (and lose) the earlier photo.
    base_name, ext = os.path.splitext(safe_name)
    candidate = safe_name
    suffix = 1
    while os.path.exists(f"{folder}/{candidate}"):
        candidate = f"{base_name}_{suffix}{ext}"
        suffix += 1
    safe_name = candidate

    file_path = f"{folder}/{safe_name}"
    with open(file_path, "wb") as outfile:
        outfile.write(file.file.read())

    return {"message": "saved", "file": file_path, "filename": safe_name}