# Robotic Arm — README

Covers the arm/vision side of the project: the Dobot control app
(`main.py`, renamed from `main-arm.py`), the Dobot library
(`api.py`/`util.py`/`types.py`), and the `vision/` package it depends on
(config, MQTT publish/subscribe, laser control).

## What this side does now

- Drives the Dobot arm (Cartesian/joint moves, pickup, jogging) via the
  `Dobot` class in `api.py`/`util.py`.
- Owns the laser (`vision/laser.py`) — unrelated to the camera changes
  below, still local hardware controlled from this machine.
- **Owns no camera hardware anymore.** All photo-taking was handed off to
  4DAI's server + browser. When this app wants a photo, it POSTs a
  trigger to 4DAI (`/collection/trigger-webcam-capture`) and moves on —
  it never touches `cv2`, a webcam, or a live preview.
- The right-side "Captured Objects" gallery panel that used to sit next
  to the live feed has been removed — it had no functionality left once
  the local camera/preview was removed, and was just taking up screen
  space.

## Dependencies

```bash
pip install -r requirements.txt
```
Covers: `numpy`, `strenum`, `matplotlib`, `customtkinter`, `pyserial`,
`ikpy`, `paho-mqtt`, `pymongo`, `Pillow`, `requests`. (`opencv-python` is
no longer required by this app now that local camera capture is removed —
keep it in `requirements.txt` only if something else in your environment
still needs it.)

## Configuration before running

1. **`vision/config.py`**
   - `TEST_MODE = True` for local testing — points `FOURDAI_API_URL`,
     `MQTT_BROKER_HOST`, and `MONGO_URI` all at `localhost`.
     Set `False` and fill in the real host/IP if 4DAI's server is on a
     separate machine.
   - `PHOTO_STATION` — the fixed pose the arm returns to before
     photographing. Currently near full extension; move it to a lower/
     off-axis corner position before relying on it for repeated capture
     runs (see the comment in `config.py`).
   - `NUM_VIEWS` / `VIEW_SETTLE_SECONDS` — how many rotation steps per
     capture sequence, and how long to pause before triggering each one.
   - `LASER_SERIAL_PORT` / `LASER_ON_COMMAND` / `LASER_OFF_COMMAND` —
     confirm these against your actual laser hardware before relying on
     `vision/laser.py`.

2. **Network** — the PC needs to reach 4DAI's FastAPI server over HTTP
   (`FOURDAI_API_URL`) and, if you're using MQTT-based jog/move commands
   or capture-status reporting, a running MQTT broker (Mosquitto) at
   `MQTT_BROKER_HOST`/`MQTT_BROKER_PORT`.

3. **Robot connection** — Windows: connect via the `Ethernet 2` adapter
   and set a static IP on the robot's subnet. See the project's main
   README for the full network setup steps.

## How to run

```bash
python main.py
```
(or whatever you've renamed `main-arm.py` to locally — nothing in this
app or `Interface.py` references it by filename, so renaming is safe.)

1. Confirm the GUI doesn't show "Demo Mode (No Robot)" — if it does,
   check the Ethernet connection and robot IP.
2. If you're relying on MQTT jog/move commands, start Mosquitto (or your
   broker) *before* launching this app.
3. Use the plot/jog controls to move the arm; use "Pickup & Photograph"
   to run the full pickup → photo-station → rotate → trigger-4DAI
   sequence.
4. Watch the terminal for `[TRIGGER ACK]`/`[REMOTE TRIGGER]`/
   `[CAPTURE COMPLETE]` log lines confirming each capture trigger
   actually reached 4DAI. See `KNOWN_ISSUES.md` — automatic capture not
   reliably completing is the current top issue, so this log output is
   the fastest way to tell whether the trigger left this machine at all.

## Interface.py

`Interface.py` is a separate, independent GUI script (also built on the
same `Dobot` class) — it doesn't import or depend on `main.py`/
`main-arm.py` in any way, so changes to one don't affect the other.
