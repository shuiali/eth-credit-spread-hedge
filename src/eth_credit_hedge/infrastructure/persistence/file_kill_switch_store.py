"""Atomic local-file persistence for the out-of-band kill switch."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from eth_credit_hedge.domain.control import KillSwitchMode, KillSwitchState


class FileKillSwitchStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def load(self) -> KillSwitchState | None:
        return self._load()

    async def save(
        self,
        state: KillSwitchState,
        *,
        expected_version: int | None,
    ) -> None:
        current = self._load()
        current_version = None if current is None else current.version
        if current_version != expected_version:
            raise RuntimeError("kill-switch state changed concurrently")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "mode": state.mode.value,
                    "reason": state.reason,
                    "requested_by": state.requested_by,
                    "changed_at_utc": state.changed_at_utc.isoformat(),
                    "version": state.version,
                },
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _load(self) -> KillSwitchState | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("kill-switch file must contain an object")
            return KillSwitchState(
                mode=KillSwitchMode(str(raw["mode"])),
                reason=str(raw["reason"]),
                requested_by=str(raw["requested_by"]),
                changed_at_utc=datetime.fromisoformat(str(raw["changed_at_utc"])),
                version=int(raw["version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("kill-switch state is unreadable") from exc
