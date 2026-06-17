"""Background workers — own one in-flight LLM turn per chat slot."""

from .state import WorkerState
from .manager import WorkerManager

__all__ = ["WorkerState", "WorkerManager"]
