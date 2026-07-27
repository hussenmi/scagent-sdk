"""Project-specific errors with actionable failure modes."""


class ScagentSDKError(Exception):
    """Base class for expected scagent-sdk failures."""


class SessionNotFoundError(ScagentSDKError):
    """The requested scientific session does not exist."""


class SessionFormatError(ScagentSDKError):
    """A persisted session file is missing, incompatible, or malformed."""


class EventLogCorruptionError(SessionFormatError):
    """The append-only event log is malformed or has a sequence gap."""


class SessionIdentityError(SessionFormatError):
    """Persisted files disagree about which scientific session they belong to."""


class ModelProfileError(ScagentSDKError):
    """A model profile is missing required or internally consistent configuration."""


class ModelProfileNotFoundError(ModelProfileError):
    """The requested model profile does not exist."""


class GatewayProbeError(ScagentSDKError):
    """A configured model gateway could not satisfy a diagnostic probe."""


class RuntimeExecutionError(ScagentSDKError):
    """The model runtime failed before producing a valid terminal result."""


class ContextRolloverRequired(RuntimeExecutionError):
    """An exact runtime transcript no longer fits the model's advertised context window."""

    def __init__(
        self,
        message: str,
        *,
        total_tokens: int | None = None,
        context_window_tokens: int | None = None,
        output_reserve_tokens: int | None = None,
        safety_margin_tokens: int | None = None,
        source: str | None = None,
    ):
        super().__init__(message)
        self.total_tokens = total_tokens
        self.context_window_tokens = context_window_tokens
        self.output_reserve_tokens = output_reserve_tokens
        self.safety_margin_tokens = safety_margin_tokens
        self.source = source

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "total_tokens": self.total_tokens,
            "context_window_tokens": self.context_window_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "source": self.source,
        }


class CapabilityError(ScagentSDKError):
    """A skill capability is invalid or could not execute safely."""


class CapabilityManifestError(CapabilityError):
    """A capability manifest or its associated skill package is malformed."""


class CapabilityExecutionError(CapabilityError):
    """A deterministic capability failed or returned an invalid result."""


class CapabilityInterrupted(CapabilityExecutionError):
    """A capability worker was stopped by an explicit user interrupt, not by a defect."""


class EnvironmentProfileError(CapabilityExecutionError):
    """A logical execution environment is unavailable or invalid."""
