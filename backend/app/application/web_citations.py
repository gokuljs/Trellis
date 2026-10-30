"""Read explicit citation markers from visible answer prose."""

import re
import unicodedata
from bisect import bisect_right
from uuid import UUID

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline

from app.application.errors import ProviderError
from app.domain.web import CitationReference

CITATION_FAILURE_MESSAGE = "The answer's supporting source lines could not be verified. Try again."
MAX_CITATION_ANSWER_BYTES = 64 * 1024
_MARKER = re.compile(
    r"\[\[source:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):L([1-9][0-9]{0,5})(?:-L([1-9][0-9]{0,5}))?\]\]"
)
_BARE_URL = re.compile(
    r"(?P<prefix>(?<![A-Za-z])https?://(?=[A-Za-z0-9])|"
    r"(?<![^\s(*_\[\]~])www\.)(?P<remainder>[^\s<>]*)",
    re.IGNORECASE,
)


class _CitationMarkdown(MarkdownIt):
    def validateLink(self, url: str) -> bool:
        # Recognize Markdown syntax only; this parser never navigates link destinations.
        return True


def _raw_url_spans(source: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for match in _BARE_URL.finditer(source):
        remainder = match["remainder"]
        domain_end = 0
        for character in remainder:
            if character not in "._-" and unicodedata.category(character)[0] in "PSZC":
                break
            domain_end += 1
        domain = remainder[:domain_end]
        if not domain.isascii():
            continue
        if match["prefix"].lower().startswith("www"):
            domain = "www." + domain
        if not any("_" in segment for segment in domain.split(".")[-2:]):
            spans.append((match.start(), match.end()))
    return tuple(spans)


def _citation_parser() -> MarkdownIt:
    raw_urls: dict[str, tuple[tuple[int, int], ...]] = {}

    def candidate(state: StateInline, silent: bool) -> bool:
        if silent or state.linkLevel or not state.src.startswith("[[source:", state.pos):
            return False
        if state.src not in raw_urls:
            raw_urls[state.src] = _raw_url_spans(state.src)
        urls = raw_urls[state.src]
        index = bisect_right(urls, (state.pos, state.posMax)) - 1
        if index >= 0 and state.pos < urls[index][1]:
            return False
        line_end = state.src.find("\n", state.pos, state.posMax)
        maximum = state.posMax if line_end < 0 else line_end
        closing = state.src.find("]]", state.pos, maximum)
        end = maximum if closing < 0 else closing + 2
        token = state.push("trellis_citation", "", 0)
        token.content = state.src[state.pos : end]
        state.pos = end
        return True

    parser = _CitationMarkdown("commonmark")
    parser.inline.ruler.after("link", "trellis_citation", candidate)
    return parser


def answer_citations(content: str) -> tuple[CitationReference, ...]:
    if "[[source:" not in content:
        return ()
    if len(content.encode("utf-8")) > MAX_CITATION_ANSWER_BYTES:
        raise ProviderError(
            "citation_validation_limit",
            "The answer is too large to validate its source citations. Try a shorter answer.",
        )
    references: dict[CitationReference, None] = {}
    for block in _citation_parser().parse(content):
        if block.type != "inline":
            continue
        for token in block.children or ():
            if token.type != "trellis_citation":
                continue
            match = _MARKER.fullmatch(token.content)
            if match is None:
                raise ProviderError("citation_invalid", CITATION_FAILURE_MESSAGE)
            try:
                start_line = int(match[2])
                reference = CitationReference(
                    str(UUID(match[1])), start_line, int(match[3]) if match[3] else start_line
                )
            except ValueError:
                raise ProviderError("citation_invalid", CITATION_FAILURE_MESSAGE) from None
            references[reference] = None
            if len(references) > 50:
                raise ProviderError("citation_invalid", CITATION_FAILURE_MESSAGE)
    return tuple(references)
