"""
[STUB] Object classifier.

Whatever model gets chosen (Roboflow API call, local YOLO/CLIP, etc.)
should be wired in behind `identify()` below - nothing else in the
pipeline needs to know which one is used.
"""


def identify(image_path: str):
    """
    [STUB] Classify a single image.

    Args:
        image_path: path to a saved frame (see vision.camera.capture.save_image).

    Returns:
        (label: str, confidence: float) once implemented.

    Raises:
        NotImplementedError: until a real model/API call is wired in here.
    """
    raise NotImplementedError(
        "identify() is not implemented yet - wire in Roboflow, a local "
        "YOLO/CLIP model, or another classifier here. Expected return "
        "shape is (label: str, confidence: float)."
    )
