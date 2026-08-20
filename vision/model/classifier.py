"""
[STUB] Object classifier.

Whatever model gets chosen (Roboflow API call, local YOLO/CLIP, etc.)
should be wired in behind `identify()` below - nothing else in the
pipeline needs to know which one is used.

RETURN CONTRACT (updated for the objects/images/attributes rework)
--------------------------------------------------------------------
identify() now returns (label, confidence, attributes) instead of just
(label, confidence). `attributes` is a plain dict of whatever else the
model can determine about the object beyond its label — e.g.
{"color": "red", "category": "mug", "material": "ceramic"}. It does NOT
need to match vision.storage.attribute_schema's fixed columns exactly;
vision.model.fusion / vision.storage.capture_pipeline.build_object_data
sort out which keys land in fixed columns vs. the freeform "attributes"
dict. Returning {} is fine (and expected) until the model can actually
say more than just a label.

UNKNOWN VALUES
--------------
If the model looked at the image and genuinely could not determine a
value for some field (e.g. it detected an object but couldn't tell the
color), set that field to vision.storage.attribute_schema.UNKNOWN
rather than leaving it out of `attributes` or setting it to None/"".
Leaving a key out or using None means "not attempted" everywhere else
in this pipeline (a fresh capture from before this model existed, for
instance); UNKNOWN means "attempted, couldn't tell" — Excel/CSV/the GUI
show these differently on purpose so you can tell the two cases apart
at a glance instead of just seeing a blank cell either way.
"""

from vision.storage.attribute_schema import UNKNOWN  # noqa: F401 (re-exported for convenience)


def identify(image_path: str):
    """
    [STUB] Classify a single image.

    Args:
        image_path: path to a saved frame (see vision.camera.capture.save_image).

    Returns:
        (label: str, confidence: float, attributes: dict) once implemented.
        `attributes` may be an empty dict if the model only produces a
        label, but the third element must always be present/returned as
        a dict (not omitted) so callers don't need a special case.

    Raises:
        NotImplementedError: until a real model/API call is wired in here.
    """
    raise NotImplementedError(
        "identify() is not implemented yet - wire in Roboflow, a local "
        "YOLO/CLIP model, or another classifier here. Expected return "
        "shape is (label: str, confidence: float, attributes: dict)."
    )
