"""Player state machine transitions."""

from __future__ import annotations

import pytest
from app.core.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    can_transition,
    ensure_transition,
)


def test_all_states_have_self_transition() -> None:
    for state in ALLOWED_TRANSITIONS:
        assert can_transition(state, state)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("stopped", "playing"),
        ("playing", "paused"),
        ("playing", "stopped"),
        ("paused", "playing"),
        ("paused", "stopped"),
        ("buffering", "playing"),
        ("buffering", "error"),
        ("playing", "error"),
    ],
)
def test_allowed_transitions(current: str, target: str) -> None:
    ensure_transition(current, target)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("stopped", "paused"),
        ("stopped", "error"),
        ("error", "paused"),  # error -> paused is invalid; error -> playing is allowed
    ],
)
def test_invalid_transitions_are_rejected(current: str, target: str) -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(current, target)  # type: ignore[arg-type]


def test_invalid_transition_message() -> None:
    with pytest.raises(InvalidTransition, match="stopped -> paused"):
        ensure_transition("stopped", "paused")
