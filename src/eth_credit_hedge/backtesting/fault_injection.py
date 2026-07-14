"""Deterministic named fault checkpoints for restart and outage tests."""

from __future__ import annotations

from dataclasses import dataclass


class InjectedProcessCrash(BaseException):
    """A simulated process termination that application handlers cannot catch."""


@dataclass(frozen=True, slots=True)
class FaultRule:
    checkpoint: str
    occurrence: int = 1

    def __post_init__(self) -> None:
        if not self.checkpoint.strip():
            raise ValueError("fault checkpoint cannot be empty")
        if type(self.occurrence) is not int or self.occurrence <= 0:
            raise ValueError("fault occurrence must be positive")


class FaultInjector:
    """Raise once at each configured checkpoint occurrence."""

    def __init__(self, rules: tuple[FaultRule, ...]) -> None:
        identities = [(rule.checkpoint, rule.occurrence) for rule in rules]
        if len(identities) != len(set(identities)):
            raise ValueError("fault rules must be unique")
        self._rules = frozenset(identities)
        self._counts: dict[str, int] = {}
        self._fired: set[tuple[str, int]] = set()

    def checkpoint(self, name: str) -> None:
        occurrence = self._counts.get(name, 0) + 1
        self._counts[name] = occurrence
        identity = (name, occurrence)
        if identity in self._rules and identity not in self._fired:
            self._fired.add(identity)
            raise InjectedProcessCrash(
                f"injected process crash at {name} occurrence {occurrence}"
            )
