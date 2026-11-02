"""视频提示词的模型规范（spec）。"""

from .registry import GENERIC, parse_backend_ref, resolve_video_prompt_spec, spec_by_key
from .spec import (
    BodySpec,
    CameraSpec,
    DialogueSpec,
    MusicSpec,
    NegativeSpec,
    ReferenceSpec,
    SoundSpec,
    VideoPromptSpec,
    camera_bracket,
    camera_phrase,
)
from .workflow_specs import prompt_spec_for_definition, prompt_spec_for_workflow

__all__ = [
    "GENERIC",
    "BodySpec",
    "CameraSpec",
    "DialogueSpec",
    "MusicSpec",
    "NegativeSpec",
    "ReferenceSpec",
    "SoundSpec",
    "VideoPromptSpec",
    "camera_bracket",
    "camera_phrase",
    "parse_backend_ref",
    "prompt_spec_for_definition",
    "prompt_spec_for_workflow",
    "resolve_video_prompt_spec",
    "spec_by_key",
]
