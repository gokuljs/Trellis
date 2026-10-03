class ApplicationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProviderError(ApplicationError):
    """A sanitized failure reported by a provider adapter."""
