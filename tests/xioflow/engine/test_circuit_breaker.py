import pytest
import time
from xiosync.subsystems.xioflow.engine.circuit_breaker import CircuitBreaker, CircuitState

def test_circuit_breaker_initial_state():
    cb = CircuitBreaker()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True

def test_circuit_breaker_trips_to_open():
    cb = CircuitBreaker(failure_threshold=3)
    
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False

def test_circuit_breaker_recovers_to_half_open():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
    
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False
    
    time.sleep(0.15)
    
    assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN
    
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
