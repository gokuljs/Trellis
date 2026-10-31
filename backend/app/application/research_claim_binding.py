from collections.abc import Sequence
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token

_MARKDOWN = MarkdownIt("commonmark")


@dataclass(frozen=True, slots=True)
class _InlineProjection:
    visible: str
    character_offsets: tuple[int, ...]
    emphasis_pairs: tuple[tuple[int, int, int, int], ...]


def _project(raw: str, children: Sequence[Token]) -> _InlineProjection | None:
    fragments: list[str] = []
    visible: list[str] = []
    offsets: list[int] = []
    stack: list[tuple[str, str, int, int]] = []
    pairs: list[tuple[int, int, int, int]] = []
    raw_offset = 0
    for token in children:
        if token.type in {"text", "softbreak"}:
            content = "\n" if token.type == "softbreak" else token.content
            visible.extend(content)
            offsets.extend(range(raw_offset, raw_offset + len(content)))
            fragment = content
        elif token.type in {"strong_open", "em_open"}:
            if token.markup not in {"*", "**", "_", "__"}:
                return None
            stack.append((token.tag, token.markup, len(visible), raw_offset))
            fragment = token.markup
        elif token.type in {"strong_close", "em_close"}:
            if not stack:
                return None
            tag, markup, start, opening_offset = stack.pop()
            if (tag, markup) != (token.tag, token.markup):
                return None
            fragment = token.markup
            pairs.append((start, len(visible), opening_offset, raw_offset + len(fragment)))
        else:
            return None
        fragments.append(fragment)
        raw_offset += len(fragment)
    if stack or "".join(fragments) != raw:
        return None
    return _InlineProjection("".join(visible), tuple(offsets), tuple(pairs))


def bind_emphasized_claim(markdown: str, plain_text: str) -> str | None:
    if not plain_text:
        return None
    candidates = []
    for inline in _MARKDOWN.parse(markdown):
        if inline.type != "inline" or inline.children is None:
            continue
        projection = _project(inline.content, inline.children)
        if projection is None:
            continue
        start = projection.visible.find(plain_text)
        while start >= 0:
            end = start + len(plain_text)
            raw_start = projection.character_offsets[start]
            raw_end = projection.character_offsets[end - 1] + 1
            for opening, closing, opening_offset, closing_offset in projection.emphasis_pairs:
                if start <= opening and closing <= end:
                    raw_start = min(raw_start, opening_offset)
                    raw_end = max(raw_end, closing_offset)
            candidate = inline.content[raw_start:raw_end]
            parsed = _MARKDOWN.parseInline(candidate)
            verified = (
                _project(candidate, parsed[0].children)
                if len(parsed) == 1 and parsed[0].children is not None
                else None
            )
            if verified is not None and verified.visible == plain_text and candidate in markdown:
                candidates.append(candidate)
                if len(candidates) > 1:
                    return None
            start = projection.visible.find(plain_text, start + 1)
    return candidates[0] if candidates else None
