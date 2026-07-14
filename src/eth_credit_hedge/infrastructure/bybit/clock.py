"""Midpoint-based Bybit server clock synchronization."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal


class BybitClockError(RuntimeError):
    """Base class for clock conditions that block private requests."""


class ClockNotSynchronizedError(BybitClockError):
    """No valid server-time observation is available."""


class ClockDriftError(BybitClockError):
    """The local clock differs too far from the exchange clock."""


class ClockUncertaintyError(BybitClockError):
    """Network RTT makes the clock estimate too uncertain."""


class ClockStaleError(BybitClockError):
    """The last successful clock synchronization is too old."""


@dataclass(frozen=True, slots=True)
class ClockSyncSample:
    """One server-time observation adjusted to the local request midpoint."""

    offset_ms: Decimal
    round_trip_time_ms: int
    uncertainty_ms: Decimal
    synchronized_at_monotonic: float


def _wall_time_ms() -> int:
    return time.time_ns() // 1_000_000


class ServerClock:
    """Provide exchange-adjusted timestamps only while synchronization is safe."""

    __slots__ = (
        "_monotonic_seconds",
        "_sample",
        "_wall_time_ms",
        "max_absolute_offset_ms",
        "max_age_seconds",
        "max_uncertainty_ms",
    )

    def __init__(
        self,
        *,
        max_absolute_offset_ms: int = 500,
        max_uncertainty_ms: int = 250,
        max_age_seconds: float = 60.0,
        wall_time_ms: Callable[[], int] | None = None,
        monotonic_seconds: Callable[[], float] | None = None,
    ) -> None:
        if max_absolute_offset_ms < 0:
            raise ValueError("maximum absolute offset must not be negative")
        if max_uncertainty_ms < 0:
            raise ValueError("maximum uncertainty must not be negative")
        if max_age_seconds <= 0:
            raise ValueError("maximum clock sample age must be positive")
        self.max_absolute_offset_ms = Decimal(max_absolute_offset_ms)
        self.max_uncertainty_ms = Decimal(max_uncertainty_ms)
        self.max_age_seconds = max_age_seconds
        self._wall_time_ms = wall_time_ms or _wall_time_ms
        self._monotonic_seconds = monotonic_seconds or time.monotonic
        self._sample: ClockSyncSample | None = None

    @property
    def sample(self) -> ClockSyncSample | None:
        return self._sample

    def record_sample(
        self,
        *,
        request_sent_at_ms: int,
        response_received_at_ms: int,
        server_time_ms: int,
    ) -> ClockSyncSample:
        if response_received_at_ms < request_sent_at_ms:
            raise ValueError("response time must not precede request time")
        round_trip_time_ms = response_received_at_ms - request_sent_at_ms
        midpoint_ms = (
            Decimal(request_sent_at_ms) + Decimal(response_received_at_ms)
        ) / Decimal(2)
        sample = ClockSyncSample(
            offset_ms=Decimal(server_time_ms) - midpoint_ms,
            round_trip_time_ms=round_trip_time_ms,
            uncertainty_ms=Decimal(round_trip_time_ms) / Decimal(2),
            synchronized_at_monotonic=self._monotonic_seconds(),
        )
        self._validate_quality(sample)
        self._sample = sample
        return sample

    def timestamp_ms(self) -> int:
        sample = self._sample
        if sample is None:
            raise ClockNotSynchronizedError(
                "server clock must be synchronized before private requests"
            )
        age_seconds = self._monotonic_seconds() - sample.synchronized_at_monotonic
        if age_seconds < 0 or age_seconds > self.max_age_seconds:
            raise ClockStaleError(
                f"server clock synchronization is stale ({age_seconds:.3f}s)"
            )
        self._validate_quality(sample)
        return int(Decimal(self._wall_time_ms()) + sample.offset_ms)

    def _validate_quality(self, sample: ClockSyncSample) -> None:
        if abs(sample.offset_ms) > self.max_absolute_offset_ms:
            raise ClockDriftError(
                f"server clock offset {sample.offset_ms}ms exceeds "
                f"{self.max_absolute_offset_ms}ms"
            )
        if sample.uncertainty_ms > self.max_uncertainty_ms:
            raise ClockUncertaintyError(
                f"server clock uncertainty {sample.uncertainty_ms}ms exceeds "
                f"{self.max_uncertainty_ms}ms"
            )
