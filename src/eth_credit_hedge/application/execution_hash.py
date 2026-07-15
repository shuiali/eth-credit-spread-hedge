"""Deterministic hashes for normalized execution-history observations."""

from __future__ import annotations

import hashlib
import json

from eth_credit_hedge.domain.execution import ExecutionUpdate


def execution_payload_hash(execution: ExecutionUpdate) -> str:
    payload = {
        "executed_at": execution.executed_at.isoformat(),
        "execution_id": execution.execution_id,
        "fee": str(execution.fee),
        "is_maker": execution.is_maker,
        "order_id": execution.order_id,
        "order_link_id": execution.order_link_id,
        "price": str(execution.price),
        "quantity": str(execution.quantity),
        "side": execution.side,
        "symbol": execution.symbol,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
