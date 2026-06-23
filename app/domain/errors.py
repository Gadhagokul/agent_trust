from fastapi import status


class DomainError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


class AgentNotFoundError(DomainError):
    def __init__(self, message: str = "Agent not found"):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, code="agent_not_found", message=message)


class InsufficientDataError(DomainError):
    def __init__(self, message: str = "Insufficient data for scoring"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, code="insufficient_data", message=message)


class DatabaseUnavailableError(DomainError):
    """Raised when the external DB is unreachable or connection fails."""
    def __init__(self, message: str = "Database is currently unavailable"):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, code="database_unavailable", message=message)


class SchemaChangedError(DomainError):
    """Raised when the external DB schema has changed (missing table or column)."""
    def __init__(self, message: str = "External database schema has changed. Service is degraded."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, code="schema_changed", message=message)