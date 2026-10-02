"""Shared plugin lifecycle error types."""


class PluginLifecycleError(ValueError):
    """Raised when a local plugin lifecycle operation is unsafe or invalid."""


class PluginDependencyError(PluginLifecycleError):
    """Raised when a plugin mutation would violate the enabled dependency graph."""

    def __init__(
        self,
        message: str,
        *,
        plugins: tuple[str, ...] | frozenset[str] = (),
    ) -> None:
        super().__init__(message)
        self.plugins = frozenset(plugins)
