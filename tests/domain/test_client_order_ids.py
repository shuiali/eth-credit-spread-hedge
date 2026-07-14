"""Parseable, bounded Bybit order-link ID tests."""

import re

import pytest

from eth_credit_hedge.domain.client_order_ids import (
    ClientOrderId,
    ClientOrderRole,
)


def test_every_role_round_trips_through_compact_exchange_value() -> None:
    for role in ClientOrderRole:
        client_id = ClientOrderId(
            strategy_instance="01",
            cycle=7,
            level=1,
            role=role,
            attempt=2,
            nonce="9F3C",
        )

        value = str(client_id)

        assert len(value) <= 36
        assert re.fullmatch(r"[A-Za-z0-9_-]+", value)
        assert ClientOrderId.parse(value) == client_id


def test_entry_role_matches_documented_shape() -> None:
    client_id = ClientOrderId(
        strategy_instance="01",
        cycle=7,
        level=1,
        role=ClientOrderRole.HEDGE_ENTRY,
        attempt=2,
        nonce="9F3C",
    )

    assert str(client_id) == "ECH-01-C0007-L01-ENTRY-A02-9F3C"


def test_new_ids_use_a_fresh_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    nonces = iter(("a1b2c3", "d4e5f6"))
    monkeypatch.setattr(
        "eth_credit_hedge.domain.client_order_ids.secrets.token_hex",
        lambda _: next(nonces),
    )

    first = ClientOrderId.new("01", 7, 1, ClientOrderRole.HEDGE_TP, 1)
    second = ClientOrderId.new("01", 7, 1, ClientOrderRole.HEDGE_TP, 1)

    assert first.nonce == "A1B2C3"
    assert second.nonce == "D4E5F6"
    assert first != second


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"strategy_instance": "TOOLONG"}, "strategy instance"),
        ({"cycle": 10_000}, "cycle"),
        ({"level": 100}, "level"),
        ({"attempt": 100}, "attempt"),
        ({"nonce": "not-hex"}, "nonce"),
    ],
)
def test_client_id_components_are_strictly_bounded(
    values: dict[str, object],
    message: str,
) -> None:
    fields: dict[str, object] = {
        "strategy_instance": "01",
        "cycle": 7,
        "level": 1,
        "role": ClientOrderRole.HEDGE_STOP,
        "attempt": 1,
        "nonce": "A1B2C3",
    }
    fields.update(values)

    with pytest.raises(ValueError, match=message):
        ClientOrderId(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ECH-01-C7-L01-ENTRY-A02-9F3C",
        "ECH-01-C0007-L01-UNKNOWN-A02-9F3C",
        "ECH-01-C0007-L01-ENTRY-A02-$$$$",
        "ECH-01-C0007-L01-ENTRY-A02-9F3C-extra",
    ],
)
def test_parser_rejects_malformed_ids(value: str) -> None:
    with pytest.raises(ValueError, match="invalid client order ID"):
        ClientOrderId.parse(value)
