"""Runtime configuration with no dependency on the legacy game framework."""
from dataclasses import dataclass
import os


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class DecisionSettings:
    decision_url: str = "https://api.bamboonode.dev/v1/decisions"
    decision_model: str = ""
    decision_timeout: float = 30.0
    decision_api_key_file: str = ""

    @classmethod
    def load(cls):
        return cls(
            decision_url=os.getenv("STARTLUX_DECISION_URL", cls.decision_url),
            decision_model=os.getenv("STARTLUX_DECISION_MODEL", ""),
            decision_timeout=float(os.getenv("STARTLUX_DECISION_TIMEOUT", "30")),
            decision_api_key_file=os.getenv("STARTLUX_DECISION_KEY_FILE", ""),
        )
