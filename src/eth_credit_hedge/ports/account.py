"""Exchange-neutral private account port."""

from __future__ import annotations

from typing import Protocol

from eth_credit_hedge.domain.execution import ExchangePosition, WalletState


class AccountPort(Protocol):
    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]: ...

    async def get_wallet_state(self) -> WalletState: ...
