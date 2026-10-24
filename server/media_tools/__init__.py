"""Host-neutral media generation tool definitions and handlers."""

from server.media_tools.assets import generate_assets, list_pending_assets
from server.media_tools.image_edits import edit_images
from server.media_tools.narration_audio import generate_narration_audio
from server.media_tools.shot_grids import (
    author_shot_grid_prompt,
    configure_shot_grid,
    generate_shot_grids,
)
from server.media_tools.storyboards import generate_storyboards
from server.media_tools.videos import generate_videos

__all__ = [
    "author_shot_grid_prompt",
    "configure_shot_grid",
    "edit_images",
    "generate_assets",
    "generate_narration_audio",
    "generate_shot_grids",
    "generate_storyboards",
    "generate_videos",
    "list_pending_assets",
]
