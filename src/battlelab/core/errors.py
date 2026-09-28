"""Domain error hierarchy for the Battlelab platform."""

class BattlelabError(Exception):
    """Base exception for all Battlelab errors."""
    pass

class ConfigurationError(BattlelabError):
    """Raised when configuration is invalid, missing, or malformed."""
    pass

class ArtifactNotFoundError(BattlelabError):
    """Raised when a requested bot artifact cannot be found."""
    pass

class ArtifactBuildError(BattlelabError):
    """Raised when compiling or preparing an immutable artifact fails."""
    pass

class MatchExecutionError(BattlelabError):
    """Raised when a match cannot be executed."""
    pass

class InfrastructureError(BattlelabError):
    """Raised when a match fails due to platform or system failure, not gameplay."""
    pass

class MatchTimeoutError(InfrastructureError):
    """Raised when a match or bot execution exceeds hard time limits."""
    pass

class DeterminismError(BattlelabError):
    """Raised when identical seeds/inputs yield disparate outputs."""
    pass

class ReplayError(BattlelabError):
    """Raised when a replay file operation fails."""
    pass

class ReplayCorruptedError(ReplayError):
    """Raised when a replay file is structurally invalid or checksum mismatches."""
    pass

class CapabilityNotSupportedError(BattlelabError):
    """Raised when an operation is attempted on an adapter lacking support."""
    def __init__(self, capability: str, adapter_name: str, message: str = ""):
        self.capability = capability
        self.adapter_name = adapter_name
        full_msg = f"Adapter '{adapter_name}' does not support '{capability}'."
        if message:
            full_msg += f" Details: {message}"
        super().__init__(full_msg)

class PromotionGateError(BattlelabError):
    """Raised when a candidate fails one or more promotion gate criteria."""
    def __init__(self, message: str, violations: list[str] | None = None):
        super().__init__(message)
        self.violations = violations or []

class StorageError(BattlelabError):
    """Raised when database or filesystem storage operations fail."""
    pass
