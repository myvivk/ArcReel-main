---
status: accepted
---

# Generate reference grids directly from the storyboard prompt

## Decision

The reference-material dialog configures only the grid layout. Both individual and batch image generation use the current `image_prompt.scene` as the visual source. The image request contains the complete storyboard prompt, asset reference numbering, project style, layout, aspect ratios and instructions to select distinct frozen moments.

This supersedes ADR 0099 D-1/D-2/D-5 insofar as they require an editable per-cell prompt as the image input. It also revises ADR 0098 D-A/D-C: a storyboard camera shot is not necessarily one grid cell. When the counts differ, the image model selects moments from existing actions, preserving the opening, closing and important transitions without adding events or duplicating the closing image.

Legacy `ShotGrid.prompt` storage and authoring endpoints remain compatible, but generation ignores that field. Admission always validates the current storyboard prompt, including Agent submissions with a historical configured prompt.

## Consequences

- No separate text-model call is required to author or fill per-cell prompts before image generation.
- Changes to the storyboard prompt affect the next individual or batch generation.
- Camera motion, timing and sound remain narrative context; each image cell represents one frozen moment.
- Grid layout, reference images, style, confirmation, history and downstream K-frame splitting remain system responsibilities.
- The model chooses exact instants. Existing cell descriptions are derived source context, not a model-generated transcription of the selected instants. They must not be interpreted as proof of exact visual content.
- Legacy authoring endpoints are retained for compatibility; they no longer control generated pictures.

## Validation

Use code inspection, Python lint and TypeScript static analysis. Repository policy prohibits running or modifying tests. Visual adherence requires inspecting an actual generated grid; static analysis cannot establish image quality.
