"""Request pacing that looks human, plus a circuit breaker.

The enrichment stage hits one page per job, which is far more requests than the
search API needs. Three things keep that from getting an IP banned:

1. A randomised gap between requests — never a fixed metronome.
2. Occasional longer pauses, the way a person stops to read something.
3. A circuit breaker: consecutive failures mean you are probably already being
   blocked, so escalate the wait and eventually stop rather than hammer on.
"""

import logging
import random
import time

LOGGER = logging.getLogger(__name__)


class HumanDelay:
    """Randomised pacing between requests."""

    def __init__(
        self,
        min_seconds: float = 4.0,
        max_seconds: float = 11.0,
        long_pause_every: int = 12,
        long_pause_seconds: tuple[float, float] = (25.0, 60.0),
        sleeper=time.sleep,
        rng: random.Random | None = None,
    ):
        if min_seconds < 0 or max_seconds < min_seconds:
            raise ValueError("need 0 <= min_seconds <= max_seconds")

        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self.long_pause_every = long_pause_every
        self.long_pause_seconds = long_pause_seconds
        self._sleep = sleeper
        self._rng = rng or random.Random()
        self._count = 0

    def wait(self):
        """Block for a human-looking interval. First call returns immediately."""
        if self._count == 0:
            self._count = 1
            return

        self._count += 1

        if self.long_pause_every and self._count % self.long_pause_every == 0:
            delay = self._rng.uniform(*self.long_pause_seconds)
            LOGGER.info("Taking a longer pause (%.0fs) after %d requests", delay, self._count)
        else:
            delay = self._rng.uniform(self.min_seconds, self.max_seconds)

        self._sleep(delay)


class CircuitBreaker:
    """Backs off on consecutive failures, then trips to stop the run.

    A run of failures almost always means blocking, captcha, or an expired
    session. Continuing makes it worse, so each failure waits longer and after
    `trip_after` in a row the breaker opens and the caller stops.
    """

    def __init__(
        self,
        trip_after: int = 5,
        base_backoff: float = 20.0,
        max_backoff: float = 300.0,
        sleeper=time.sleep,
    ):
        self.trip_after = trip_after
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._sleep = sleeper
        self.consecutive_failures = 0
        self.total_failures = 0
        self.total_successes = 0

    @property
    def is_open(self) -> bool:
        """True once too many failures happened in a row — stop calling."""
        return self.consecutive_failures >= self.trip_after

    def record_success(self):
        self.consecutive_failures = 0
        self.total_successes += 1

    def record_failure(self):
        self.consecutive_failures += 1
        self.total_failures += 1

        if self.is_open:
            LOGGER.error(
                "Circuit breaker tripped after %d consecutive failures — stopping. "
                "You are most likely blocked or your session expired.",
                self.consecutive_failures,
            )
            return

        backoff = min(
            self.base_backoff * (2 ** (self.consecutive_failures - 1)),
            self.max_backoff,
        )
        LOGGER.warning(
            "Enrichment failure %d/%d — backing off %.0fs",
            self.consecutive_failures, self.trip_after, backoff,
        )
        self._sleep(backoff)
