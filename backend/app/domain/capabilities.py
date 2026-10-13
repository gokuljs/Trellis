"""Operator-owned permissions for runtime effects."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

RuntimeCapability = Literal["local_read", "local_patch", "local_command"]


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    enabled: frozenset[RuntimeCapability] = frozenset()

    @property
    def workspace_enabled(self) -> bool:
        return bool(self.enabled)

    def allows(self, capability: RuntimeCapability) -> bool:
        return capability in self.enabled

    def allows_all(self, capabilities: Iterable[RuntimeCapability]) -> bool:
        return self.enabled.issuperset(capabilities)


DEFAULT_RUNTIME_CAPABILITIES = RuntimeCapabilities()
