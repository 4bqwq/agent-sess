class SessError(Exception):
    """An error that should be shown directly to a sess user."""


class MetadataError(SessError):
    """The project-local sess metadata cannot be read or written."""


class ProviderError(SessError):
    """A native provider cannot complete the requested operation."""


class TargetError(SessError):
    """A session target cannot be resolved unambiguously."""
