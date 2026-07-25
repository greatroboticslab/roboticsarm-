"""
vision package
==============
Everything related to object capture, identification, and logging lives
under this package. Nothing outside `vision/` should need to change to
add camera hardware, swap models, or change the message broker — those
are all isolated in their respective submodules.

Status legend used in docstrings throughout this package:
  [WIRED]  - integrated into main.py / the pipeline, safe to call today
  [STUB]   - structure/interface is final, but raises NotImplementedError
             until real hardware/model/broker code is filled in
"""
