"""Verbatim source-anchor comparison shared by visual script editing."""

import re
import unicodedata


def is_verbatim_source_anchor(source_text: str, novel_text: str) -> bool:
    """Compare after NFC and whitespace normalization without changing words."""

    def normalize(text: str) -> str:
        return re.sub(r"\\s+", " ", unicodedata.normalize("NFC", text)).strip()

    anchor = normalize(source_text)
    return bool(anchor) and anchor in normalize(novel_text)
