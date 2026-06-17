"""Custom exceptions for Ash."""


class AshError(Exception):
    """Base exception for Ash errors."""


class AshConfigError(AshError):
    """Raised when Ash configuration cannot be loaded or validated."""
