"""
roboticarm_main.py
===================
Run this (instead of main.py directly) to start the robotics-arm side of
the system. It launches the same GUI as before, but now that GUI also
starts an MQTT listener (see main.py: start_capture_command_listener())
that lets 4DAI's Streamlit app drive automatic capture sequences
remotely - no code here duplicates that logic, it just makes the
dependency between the two projects explicit and gives the arm side its
own clearly-named launcher (mirroring 4dai_main.py on the 4DAI side).

WHAT THIS SIDE TALKS TO
------------------------
- MQTT broker (vision/config.py: MQTT_BROKER_HOST/PORT) - subscribes to
  TOPIC_CAPTURE_COMMAND (commands from 4DAI) and publishes on
  TOPIC_CAPTURE_STATUS (progress/results back to 4DAI) and
  TOPIC_ARM_OBJECT_CAPTURED / TOPIC_VISION_RESULT (existing pipeline).
- MongoDB (vision/config.py: MONGO_URI/MONGO_DB_NAME) - same database
  4DAI's Server/main.py uses, so both projects see the same samples.

Run:
    python roboticarm_main.py
"""

if __name__ == "__main__":
    # main.py builds its Tkinter GUI and starts the MQTT command listener
    # at import time, then blocks on root.mainloop() - so simply
    # importing it here starts everything.
    import main  # noqa: F401
