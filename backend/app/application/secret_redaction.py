"""Redact configured secrets and recognizable submitted credentials."""

import json
import re

_BEARER_TOKEN = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/-]{6,})")
_KEY_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"
    r"[A-Za-z0-9_]*[\"']?\s*[:=]\s*[\"']?)([^\s\"';,}]+)"
)
_STANDALONE_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,})\b"
)


class SecretRedactor:
    def __init__(self, *secrets: str) -> None:
        self._secrets = tuple(
            sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        )

    @property
    def streaming_secrets(self) -> tuple[str, ...]:
        return self._secrets

    def redact_text(self, content: str) -> str:
        for secret in self._secrets:
            content = content.replace(secret, "[REDACTED]")
        content = _BEARER_TOKEN.sub(lambda match: f"{match[1]}[REDACTED]", content)
        content = _KEY_ASSIGNMENT.sub(lambda match: f"{match[1]}[REDACTED]", content)
        return _STANDALONE_TOKEN.sub("[REDACTED]", content)

    def redact_source_text(self, content: str) -> str:
        """Filter configured credentials even when source wrapping divided them."""
        compact = content.replace("\n", "")
        matches: list[tuple[int, int]] = []
        for secret in self._secrets:
            needle = secret.replace("\n", "")
            if not needle:
                continue
            offset = 0
            while (start := compact.find(needle, offset)) >= 0:
                offset = start + len(needle)
                matches.append((start, offset))
        if not matches:
            return self.redact_text(content)
        positions = [index for index, character in enumerate(content) if character != "\n"]
        merged: list[tuple[int, int]] = []
        for start, end in sorted(matches):
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        pieces = []
        offset = 0
        for start, end in merged:
            pieces.extend((content[offset : positions[start]], "[REDACTED]"))
            offset = positions[end - 1] + 1
        pieces.append(content[offset:])
        return self.redact_text("".join(pieces))

    def redact_record(self, value: object) -> object:
        if isinstance(value, dict):
            redacted: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                normalized = key.casefold().replace("-", "_")
                sensitive = normalized in {
                    "token",
                    "secret",
                    "password",
                    "credential",
                    "api_key",
                } or (
                    normalized.endswith(
                        ("_token", "_secret", "_password", "_credential", "_api_key")
                    )
                )
                redacted[self.redact_text(key)] = (
                    "[REDACTED]" if sensitive else self.redact_record(item)
                )
            return redacted
        if isinstance(value, list | tuple):
            return [self.redact_record(item) for item in value]
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def redact_tool_text(self, content: str) -> str:
        """Preserve JSON escapes when untrusted tool strings contain credentials."""
        try:
            value = json.loads(content)
        except ValueError:
            return self.redact_text(content)
        if not isinstance(value, dict | list):
            return self.redact_text(content)
        return json.dumps(self.redact_record(value), ensure_ascii=False, separators=(",", ":"))


COMMON_SECRET_REDACTOR = SecretRedactor()
