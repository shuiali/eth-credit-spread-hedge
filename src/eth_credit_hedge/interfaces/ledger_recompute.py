"""Offline command for independently replaying canonical accounting JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence, cast

from eth_credit_hedge.domain.accounting.errors import AccountingContractError
from eth_credit_hedge.domain.accounting.events import (
    AccountingEvent,
    OptionQuoteRecorded,
    event_from_dict,
)
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerReconstructor


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True, help="canonical event JSONL")
    parser.add_argument("--quotes", type=Path, help="optional option-quote JSONL")
    args = parser.parse_args(argv)
    events = _read_events(args.events)
    quotes = _read_events(args.quotes) if args.quotes else ()
    if not all(isinstance(event, OptionQuoteRecorded) for event in quotes):
        raise AccountingContractError("quotes JSONL must contain only OptionQuoteRecorded events")
    option_quotes = cast(tuple[OptionQuoteRecorded, ...], quotes)
    state = CombinedLedgerReconstructor().reconstruct(events, option_quotes)
    print(json.dumps(state.to_dict(), sort_keys=True))
    return 0


def _read_events(path: Path) -> tuple[AccountingEvent, ...]:
    events: list[AccountingEvent] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line, parse_float=lambda _: _reject_float(line_number))
        except json.JSONDecodeError as error:
            raise AccountingContractError(f"invalid JSONL at {path}:{line_number}") from error
        if not isinstance(payload, dict):
            raise AccountingContractError(f"JSONL event must be an object at {path}:{line_number}")
        events.append(event_from_dict(payload))
    return tuple(events)


def _reject_float(line_number: int) -> None:
    raise AccountingContractError(
        f"binary JSON number is forbidden in accounting JSONL at line {line_number}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
