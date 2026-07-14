"""Append-only canonical shadow-intent recorder."""

from __future__ import annotations

from pathlib import Path

from eth_credit_hedge.application.shadow_mode import ShadowIntent


class JsonLinesShadowRecorder:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, intent: ShadowIntent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(intent.to_json())
            handle.write("\n")
