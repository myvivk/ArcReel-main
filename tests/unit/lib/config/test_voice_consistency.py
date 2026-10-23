"""Factory voice binding depends on model audio support."""

import pytest

from lib.config.resolver import derive_voice_consistency


@pytest.mark.parametrize(
    ("audio_mode", "has_audio", "binding", "expected"),
    [
        ("direct", True, "reference_audio", "native"),
        ("direct", False, "reference_audio", "none"),
        ("direct", True, "prompt", "soft"),
        ("none", True, "reference_audio", "soft"),
        ("direct", True, None, "soft"),
    ],
)
def test_voice_binding_respects_model_and_user_choice(audio_mode: str, has_audio: bool, binding, expected: str) -> None:
    assert (
        derive_voice_consistency(
            reference_audio_mode=audio_mode,
            has_audio=has_audio,
            character_voice_binding=binding,
        )
        == expected
    )
