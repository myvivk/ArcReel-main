"""Verbatim source-anchor comparison shared by visual script editing."""

import re
import unicodedata


def is_verbatim_source_anchor(source_text: str, novel_text: str) -> bool:
    """Compare after NFC and whitespace normalization without changing words."""

    def normalize(text: str) -> str:
        return re.sub(r"\\s+", " ", unicodedata.normalize("NFC", text)).strip()

    anchor = normalize(source_text)
    return bool(anchor) and anchor in normalize(novel_text)


def is_content_aligned_source_anchor(source_text: str, novel_text: str) -> bool:
    """Compare words/CJK content, tolerating Markdown line and quote formatting.

    源稿把「角色：台词」改成「角色 / 引号台词」分行，或加 Markdown 标记时，逐字锚点
    仍然是同一段内容。这里只去掉标点、引号、Markdown 符号与空白，不改变任何文字；
    文字本身变了仍判不一致。
    """

    def content(text: str) -> str:
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFC", text))

    anchor = content(source_text)
    return bool(anchor) and anchor in content(novel_text)
