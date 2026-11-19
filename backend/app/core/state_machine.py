"""Player state machine (SPEC §7.1).

Allowed transitions are explicit so invalid transitions fail loudly instead of
silently corrupting persisted state.
"""

from __future__ import annotations

from app.core.models import PlayerStatus

ALLOWED_TRANSITIONS: dict[PlayerStatus, frozenset[PlayerStatus]] = {
    "stopped": frozenset({"stopped", "playing", "buffering"}),
    "buffering": frozenset({"buffering", "playing", "paused", "stopped", "error"}),
    "playing": frozenset({"playing", "paused", "buffering", "stopped", "error"}),
    "paused": frozenset({"paused", "playing", "buffering", "stopped", "error"}),
    "error": frozenset({"error", "stopped", "playing", "buffering"}),
}


class InvalidTransition(ValueError):
    """Raised when a caller tries a transition not in the state machine."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid player transition: {current} -> {target}")


def can_transition(current: PlayerStatus, target: PlayerStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: PlayerStatus, target: PlayerStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
