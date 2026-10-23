"""Canonical prompt semantics for historical joint-grid artifact comparison."""

from collections.abc import Mapping


def project_grid_image_prompt(image_prompt: object) -> str | dict[str, object]:
    """Preserve the prompt shape recorded in historical visual artifact bases."""
    if image_prompt is None:
        raise ValueError("grid image_prompt is pending; the cell has no prompt to render")
    if not isinstance(image_prompt, Mapping):
        return str(image_prompt)
    scene = image_prompt.get("scene")
    if scene is None:
        scene = ""
    if not isinstance(scene, str):
        raise ValueError("grid image_prompt.scene must be a string")
    raw_composition = image_prompt.get("composition")
    if raw_composition is None:
        raw_composition = {}
    if not isinstance(raw_composition, Mapping):
        raise ValueError("grid image_prompt.composition must be an object")
    if any(not isinstance(key, str) for key in raw_composition):
        raise ValueError("grid image_prompt.composition keys must be strings")
    composition: dict[str, str] = {}
    for key in sorted(raw_composition):
        raw_value = raw_composition[key]
        if raw_value is not None and str(raw_value):
            composition[key] = str(raw_value)
    return {"scene": scene, "composition": composition}
