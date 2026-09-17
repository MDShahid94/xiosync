from __future__ import annotations

import enum
import threading
import time

import structlog

logger = structlog.get_logger(__name__)

class CircuitState(enum.Enum):
    CLOSED = 1
    OPEN = 2
    HALF_OPEN = 3

class CircuitBreaker:
    """A 3-state circuit breaker pattern."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.last_failure_time = 0.0
        self.lock = threading.Lock()

    def allow_request(self) -> bool:
        """Check if a request is allowed."""
        with self.lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("circuit_breaker_half_open")
                    return True
                return False
            # HALF_OPEN state
            return False

    def record_success(self) -> None:
        """Record a successful request."""
        with self.lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self.failures = 0
                logger.info("circuit_breaker_closed")
            elif self.state == CircuitState.CLOSED:
                self.failures = 0

    def record_failure(self) -> None:
        """Record a failed request."""
        with self.lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.last_failure_time = time.time()
                logger.warning("circuit_breaker_open_from_half_open")
            elif self.state == CircuitState.CLOSED:
                self.failures += 1
                if self.failures >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    self.last_failure_time = time.time()
                    logger.warning("circuit_breaker_open_from_closed", failures=self.failures)
