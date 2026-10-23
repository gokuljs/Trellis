from pathlib import Path

from app.application.errors import ApplicationError
from app.application.ports import SessionRepository, WorkspaceBindingPort
from app.application.tools import ToolRegistry
from app.domain.capabilities import RuntimeCapabilities
from app.domain.models import ModelDescriptor, Session
from app.domain.recovery import (
    RECOVERY_CONTRACT_VERSION,
    RecoveryCompatibility,
    RecoveryContract,
    WorkspaceRecoveryReference,
)
from app.domain.runtime import RunSnapshot

RUNTIME_VERSION = "checkpoint-runtime-v1"
ADAPTER_VERSION = "v1"


class RecoveryContracts:
    def __init__(
        self,
        installation_id: str,
        sessions: SessionRepository,
        tools: ToolRegistry | None,
        bindings: WorkspaceBindingPort | None,
        capabilities: RuntimeCapabilities,
        adapter_kinds: frozenset[str],
    ) -> None:
        self.installation_id = installation_id
        self.sessions = sessions
        self.tools = tools
        self.bindings = bindings
        self.capabilities = capabilities
        self.adapter_versions = tuple(sorted((kind, ADAPTER_VERSION) for kind in adapter_kinds))

    async def _workspace(self, session: Session | None) -> WorkspaceRecoveryReference | None:
        if session is None or not self.capabilities.workspace_enabled or self.bindings is None:
            return None
        return await self.bindings.reference(session.id, session.workspace_path)

    async def create(
        self, session: Session, request_id: str, model: ModelDescriptor
    ) -> RecoveryContract:
        if model.adapter_kind not in dict(self.adapter_versions):
            raise ApplicationError("provider_not_available", "The selected adapter is unavailable.")
        workspace = await self._workspace(session)
        root = Path(workspace.canonical_path) if workspace is not None else None
        offered = (
            {tool.name for tool in self.tools.specs(root, capabilities=self.capabilities)}
            if model.supports_tools and self.tools is not None
            else set()
        )
        return RecoveryContract(
            RECOVERY_CONTRACT_VERSION,
            RUNTIME_VERSION,
            ((model.adapter_kind, ADAPTER_VERSION),),
            tuple(
                (name, version)
                for name, version in self.tools.recovery_versions()
                if name in offered
            )
            if self.tools is not None
            else (),
            request_id,
            self.installation_id,
            workspace,
        )

    async def compatibility(self, run: RunSnapshot) -> RecoveryCompatibility:
        session = await self.sessions.get_session(run.session_id)
        return RecoveryCompatibility(
            RUNTIME_VERSION,
            self.adapter_versions,
            self.tools.recovery_versions() if self.tools is not None else (),
            self.installation_id,
            await self._workspace(session),
        )
