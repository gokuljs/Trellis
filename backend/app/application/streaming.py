"""Bounded streaming checks, not whole-answer validation.

Unfinished tokens and configured-secret prefixes stay private until their boundary
is known. Only checked text reaches the publication buffer. Callers must consume
``push`` immediately, flush that buffer on its deadline, and use ``finish`` on
normal provider termination. Exceptional exits can discard unpublished data.
Terminal flushing suppresses unfinished credential candidates; it never makes
partial output complete. Already published batches remain durable.
"""

import math
import re
from collections.abc import Iterator, Sequence

from app.application.errors import ProviderError
from app.application.secret_redaction import SecretRedactor

_DELIMITERS = re.compile(r"[\s\"';,}]")
_PENDING_KEY = re.compile(
    r"(?i)\b[A-Za-z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"
    r"[A-Za-z0-9_]*[\"']?\s*(?:[:=]\s*[\"']?[^\s\"';,}]*)?$"
)
_PENDING_BEARER = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/-]*)$")
_PENDING_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]*|ghp_[A-Za-z0-9]*|github_pat_[A-Za-z0-9_]*)$")
_CREDENTIAL_SPANS = tuple(
    re.compile(pattern.pattern[:-1], pattern.flags)
    for pattern in (_PENDING_KEY, _PENDING_BEARER, _PENDING_TOKEN)
)
_REDACTED = "[REDACTED]"
_INPUT_SLICE_CHARACTERS = 128


class TextStreamBatcher:
    def __init__(
        self,
        *,
        secrets: Sequence[str] = (),
        max_batch_bytes: int = 4096,
        max_holdback_bytes: int = 8192,
        flush_interval_seconds: float = 0.1,
    ) -> None:
        if not 4 <= max_batch_bytes <= 4096:
            raise ValueError("publication batches must hold 4 to 4096 UTF-8 bytes")
        if max_holdback_bytes < 1:
            raise ValueError("stream holdback must be positive")
        if not math.isfinite(flush_interval_seconds) or flush_interval_seconds <= 0:
            raise ValueError("flush interval must be finite and positive")
        self._secrets = tuple(
            sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        )
        if any(len(secret.encode("utf-8")) > max_holdback_bytes for secret in self._secrets):
            raise ValueError("configured secret exceeds stream holdback")
        self._redactor = SecretRedactor(*self._secrets)
        self._max_batch_bytes = max_batch_bytes
        self._max_holdback_bytes = max_holdback_bytes
        self._flush_interval_seconds = flush_interval_seconds
        self._raw = ""
        self._safe = ""
        self._safe_bytes = 0
        self._flush_at: float | None = None
        self._closed = False
        self._failed = False
        self._redaction_pending = False

    @property
    def pending_bytes(self) -> int:
        return len(self._raw.encode("utf-8")) + self._safe_bytes

    @property
    def next_flush_at(self) -> float | None:
        """Deadline for publishing checked text; holdback has no publication deadline."""
        return self._flush_at

    def push(self, text: str, *, now: float) -> Iterator[str]:
        if self._closed or self._failed:
            raise ValueError("text stream is closed")
        if not math.isfinite(now):
            raise ValueError("stream clock must be finite")
        # Slicing keeps a huge provider chunk from creating a second huge buffer.
        for offset in range(0, len(text), _INPUT_SLICE_CHARACTERS):
            self._raw += text[offset : offset + _INPUT_SLICE_CHARACTERS]
            cutoff = self._safe_cutoff()
            checked = self._redactor.redact_text(self._raw[:cutoff])
            self._raw = self._raw[cutoff:]
            if len(self._raw.encode("utf-8")) > self._max_holdback_bytes:
                self._raw = ""
                self._failed = True
                self._redaction_pending = True
            yield from self._append_checked(checked, now=now)
            if self._failed:
                raise ProviderError(
                    "stream_check_limit", "The response exceeded the streaming safety limit."
                )
        if self.flush_due(now=now):
            yield from self.flush()

    def flush_due(self, *, now: float) -> bool:
        return self._flush_at is not None and now >= self._flush_at

    def flush(self) -> Iterator[str]:
        if self._safe:
            batch = self._safe
            self._safe = ""
            self._safe_bytes = 0
            self._flush_at = None
            yield batch

    def finish(self) -> Iterator[str]:
        if self._closed:
            return
        self._closed = True
        # A terminal one-letter overlap is ordinary text, not a credential.
        # Recognizable credential forms below still fail closed at any length.
        cutoff = self._known_prefix_cutoff(minimum_length=6)
        bearer = _PENDING_BEARER.search(self._raw)
        if bearer is not None and bearer.group(1):
            cutoff = min(cutoff, bearer.start(1))
        token = _PENDING_TOKEN.search(self._raw)
        if token is not None:
            cutoff = min(cutoff, token.start())
        cutoff = self._protect_literals(cutoff)
        checked = self._redactor.redact_text(self._raw[:cutoff])
        if cutoff < len(self._raw) or self._redaction_pending:
            checked += _REDACTED
        self._raw = ""
        yield from self._append_checked(checked, now=0.0)
        yield from self.flush()

    def _append_checked(self, text: str, *, now: float) -> Iterator[str]:
        for character in text:
            size = len(character.encode("utf-8"))
            if self._safe_bytes + size > self._max_batch_bytes:
                yield from self.flush()
            if not self._safe:
                self._flush_at = now + self._flush_interval_seconds
            self._safe += character
            self._safe_bytes += size
            if self._safe_bytes == self._max_batch_bytes:
                yield from self.flush()

    def _safe_cutoff(self) -> int:
        cutoff = len(self._raw)
        atom_start = 0
        for delimiter in _DELIMITERS.finditer(self._raw):
            atom_start = delimiter.end()
        if atom_start < len(self._raw):
            cutoff = atom_start
        for pattern in (_PENDING_KEY, _PENDING_BEARER):
            candidate = pattern.search(self._raw)
            if candidate is not None:
                cutoff = min(cutoff, candidate.start())
        cutoff = min(cutoff, self._known_prefix_cutoff())
        # A known literal can include delimiters. Do not cut an already complete
        # literal in two merely because a token or another literal is unfinished.
        previous = -1
        while previous != cutoff:
            previous = cutoff
            for pattern in _CREDENTIAL_SPANS:
                for span in pattern.finditer(self._raw):
                    if span.start() < cutoff < span.end():
                        cutoff = span.start()
            cutoff = self._protect_literals(cutoff)
        return cutoff

    def _protect_literals(self, cutoff: int) -> int:
        previous = -1
        while previous != cutoff:
            previous = cutoff
            for secret in self._secrets:
                start = self._raw.find(secret)
                while start >= 0:
                    if start < cutoff < start + len(secret):
                        cutoff = start
                    start = self._raw.find(secret, start + 1)
        return cutoff

    def _known_prefix_cutoff(self, minimum_length: int = 1) -> int:
        cutoff = len(self._raw)
        for secret in self._secrets:
            for length in range(min(len(self._raw), len(secret) - 1), minimum_length - 1, -1):
                if self._raw.endswith(secret[:length]):
                    cutoff = min(cutoff, len(self._raw) - length)
                    break
        return cutoff
