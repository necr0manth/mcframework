"""Domain errors raised by the framework."""


class FrameworkError(Exception):
    """Base class for expected user-facing failures."""


class MetadataError(FrameworkError):
    """Metadata is malformed or violates the supported schema."""


class UnsafePathError(MetadataError):
    """A path escapes its declared root or is otherwise unsafe."""


class ResolutionError(FrameworkError):
    """A layer graph or operation cannot be resolved."""


class MergeConflict(ResolutionError):
    """Same-layer operations perform incompatible writes."""


class ArtifactError(FrameworkError):
    """An exact package artifact cannot be obtained or verified."""


class TransportError(FrameworkError):
    """An instance transport operation failed."""


class UpdateError(FrameworkError):
    """An explicit package update cannot be performed."""
