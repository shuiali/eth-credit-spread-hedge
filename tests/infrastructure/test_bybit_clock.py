"""Bybit server-time synchronization safety tests."""

from decimal import Decimal

import pytest

from eth_credit_hedge.infrastructure.bybit.clock import (
    ClockDriftError,
    ClockNotSynchronizedError,
    ClockStaleError,
    ClockUncertaintyError,
    ServerClock,
)


class FakeTime:
    def __init__(self, *, wall_time_ms: int, monotonic_seconds: float) -> None:
        self.wall_time_ms = wall_time_ms
        self.monotonic_seconds = monotonic_seconds

    def wall(self) -> int:
        return self.wall_time_ms

    def monotonic(self) -> float:
        return self.monotonic_seconds


def test_clock_sample_uses_midpoint_offset_and_half_rtt_uncertainty() -> None:
    fake_time = FakeTime(wall_time_ms=1_001_000, monotonic_seconds=11.0)
    clock = ServerClock(
        max_absolute_offset_ms=250,
        max_uncertainty_ms=100,
        max_age_seconds=30,
        wall_time_ms=fake_time.wall,
        monotonic_seconds=fake_time.monotonic,
    )

    sample = clock.record_sample(
        request_sent_at_ms=1_000_000,
        response_received_at_ms=1_000_100,
        server_time_ms=1_000_120,
    )

    assert sample.round_trip_time_ms == 100
    assert sample.uncertainty_ms == Decimal("50")
    assert sample.offset_ms == Decimal("70")
    assert clock.timestamp_ms() == 1_001_070


def test_clock_rejects_private_timestamp_before_synchronization() -> None:
    clock = ServerClock()

    with pytest.raises(ClockNotSynchronizedError):
        clock.timestamp_ms()


def test_clock_rejects_excessive_absolute_offset() -> None:
    clock = ServerClock(max_absolute_offset_ms=100)

    with pytest.raises(ClockDriftError, match="offset"):
        clock.record_sample(
            request_sent_at_ms=1_000_000,
            response_received_at_ms=1_000_020,
            server_time_ms=1_000_500,
        )


def test_clock_rejects_excessive_network_uncertainty() -> None:
    clock = ServerClock(
        max_absolute_offset_ms=1000,
        max_uncertainty_ms=25,
    )

    with pytest.raises(ClockUncertaintyError, match="uncertainty"):
        clock.record_sample(
            request_sent_at_ms=1_000_000,
            response_received_at_ms=1_000_100,
            server_time_ms=1_000_050,
        )


def test_clock_rejects_stale_sync() -> None:
    fake_time = FakeTime(wall_time_ms=1_000_000, monotonic_seconds=10.0)
    clock = ServerClock(
        max_age_seconds=30,
        wall_time_ms=fake_time.wall,
        monotonic_seconds=fake_time.monotonic,
    )
    clock.record_sample(
        request_sent_at_ms=999_990,
        response_received_at_ms=1_000_010,
        server_time_ms=1_000_000,
    )
    fake_time.monotonic_seconds = 40.001

    with pytest.raises(ClockStaleError, match="stale"):
        clock.timestamp_ms()


def test_clock_requests_refresh_halfway_to_stale() -> None:
    fake_time = FakeTime(wall_time_ms=1_000_000, monotonic_seconds=10.0)
    clock = ServerClock(
        max_age_seconds=30,
        wall_time_ms=fake_time.wall,
        monotonic_seconds=fake_time.monotonic,
    )

    assert not clock.needs_refresh()
    clock.record_sample(
        request_sent_at_ms=999_990,
        response_received_at_ms=1_000_010,
        server_time_ms=1_000_000,
    )
    fake_time.monotonic_seconds = 24.999
    assert not clock.needs_refresh()
    fake_time.monotonic_seconds = 25.0
    assert clock.needs_refresh()


def test_clock_rejects_invalid_sample_order() -> None:
    clock = ServerClock()

    with pytest.raises(ValueError, match="response time"):
        clock.record_sample(
            request_sent_at_ms=1_000_001,
            response_received_at_ms=1_000_000,
            server_time_ms=1_000_000,
        )
