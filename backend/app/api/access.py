from uuid import UUID

from app.application.errors import ApplicationError
from app.core.config import Settings


def require_private_access(settings: Settings, user_id: UUID) -> None:
    if user_id in settings.private_user_ids:
        return
    if settings.environment in {"development", "test"} and not settings.private_user_ids:
        return
    raise ApplicationError("private_access_denied", "This account does not have access to Trellis.")
